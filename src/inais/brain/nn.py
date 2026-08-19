"""A real, trainable neural network — NumPy, actual backprop, weights versioned in Postgres.

Scope, stated honestly: this network does NOT generate language and does not replace the LLM.
It learns the user's *attention function* — a 1536-d embedding in, one probability out — from
genuine behavioural signals (what they engage with, which mail they act on). Two heads:

  interest         → how much does the user care about this topic?  (steers what the bot
                     chooses to teach itself while they are away)
  email_importance → is this message worth interrupting them for?   (sharpens triage and
                     lets confidently-unimportant mail skip the LLM call entirely)

The architecture GROWS with the data, which matters because one human generates few examples:
with 1536 input dimensions and ~50 examples, a hidden layer only memorises noise, so the
candidate set starts linear (a single sigmoid unit — still trained by gradient descent) and
adds hidden units once there is enough signal to justify them. Candidates are compared by
k-fold cross-validation (a single 20% holdout on 60 examples is too noisy to choose on), the
winner is retrained on everything, and it only goes active if it beats the incumbent's CV AUC.
"""

from __future__ import annotations

import io
import json
import logging

import numpy as np

from inais import db, llm
from inais.config import settings

log = logging.getLogger(__name__)

MODEL_NAMES = ("interest", "email_importance", "engagement", "complexity")
DEFAULT_INPUT_DIM = 1536
CONTEXT_DIMS = 5   # time-of-day + weekday features, for heads where the clock matters


def time_features(dt) -> list[float]:
    """Cyclical clock features: hour and weekday as sin/cos pairs, plus a weekend flag.

    Whether an unprompted message lands depends on WHEN as much as on what it says — 9am
    Tuesday and 1am Saturday are different users. Sin/cos keeps midnight adjacent to 23:00
    instead of maximally distant, which a raw hour number gets wrong.
    """
    import math

    hour_angle = 2 * math.pi * (dt.hour + dt.minute / 60) / 24
    day_angle = 2 * math.pi * dt.weekday() / 7
    return [math.sin(hour_angle), math.cos(hour_angle),
            math.sin(day_angle), math.cos(day_angle),
            1.0 if dt.weekday() >= 5 else 0.0]
MIN_USABLE_AUC = 0.58     # below this the model never steers anything
CV_FOLDS = 5


class MLP:
    """Sigmoid output over an optional tanh hidden layer, trained with Adam on BCE loss.

    hidden_dim == 0 is a genuine (single-layer) network: linear projection + sigmoid, which
    is the right capacity when examples are scarce.
    """

    def __init__(self, input_dim: int = DEFAULT_INPUT_DIM, hidden_dim: int = 0,
                 seed: int = 17) -> None:
        rng = np.random.default_rng(seed)
        self.input_dim = input_dim
        self.hidden_dim = max(0, hidden_dim)
        if self.hidden_dim:
            self.w1 = rng.normal(0.0, np.sqrt(2.0 / input_dim), (input_dim, self.hidden_dim))
            self.b1 = np.zeros(self.hidden_dim)
            self.w2 = rng.normal(0.0, np.sqrt(1.0 / self.hidden_dim), (self.hidden_dim, 1))
        else:
            self.w1 = None
            self.b1 = None
            self.w2 = np.zeros((input_dim, 1))
        self.b2 = np.zeros(1)

    # ---- inference ----

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = np.tanh(x @ self.w1 + self.b1) if self.hidden_dim else x
        z = h @ self.w2 + self.b2
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        return p.ravel(), h

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.forward(np.atleast_2d(x))[0]

    # ---- training ----

    def _params(self) -> dict[str, np.ndarray]:
        params = {"w2": self.w2, "b2": self.b2}
        if self.hidden_dim:
            params["w1"] = self.w1
            params["b1"] = self.b1
        return params

    def fit(self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None,
            epochs: int = 200, lr: float = 5e-3, l2: float = 1e-2, batch_size: int = 32,
            seed: int = 17) -> float:
        """Mini-batch Adam. Returns the final mean training loss."""
        n = x.shape[0]
        w = np.ones(n) if sample_weight is None else sample_weight.astype(float)
        rng = np.random.default_rng(seed)
        m = {k: np.zeros_like(v) for k, v in self._params().items()}
        v = {k: np.zeros_like(val) for k, val in self._params().items()}
        beta1, beta2, eps, step = 0.9, 0.999, 1e-8, 0
        loss = 0.0

        for _ in range(epochs):
            order = rng.permutation(n)
            epoch_loss = 0.0
            for start in range(0, n, batch_size):
                idx = order[start:start + batch_size]
                xb, yb, wb = x[idx], y[idx], w[idx]
                p, h = self.forward(xb)
                p_safe = np.clip(p, 1e-7, 1 - 1e-7)
                epoch_loss += float(np.sum(
                    -wb * (yb * np.log(p_safe) + (1 - yb) * np.log(1 - p_safe))))

                # backprop
                d_z = ((p - yb) * wb / max(1, len(idx)))[:, None]
                grads = {"w2": h.T @ d_z + l2 * self.w2, "b2": d_z.sum(axis=0)}
                if self.hidden_dim:
                    d_h = (d_z @ self.w2.T) * (1.0 - h ** 2)
                    grads["w1"] = xb.T @ d_h + l2 * self.w1
                    grads["b1"] = d_h.sum(axis=0)

                step += 1
                for key, grad in grads.items():
                    m[key] = beta1 * m[key] + (1 - beta1) * grad
                    v[key] = beta2 * v[key] + (1 - beta2) * (grad ** 2)
                    m_hat = m[key] / (1 - beta1 ** step)
                    v_hat = v[key] / (1 - beta2 ** step)
                    setattr(self, key, getattr(self, key) - lr * m_hat / (np.sqrt(v_hat) + eps))
            loss = epoch_loss / max(1, n)
        return loss

    # ---- serialization ----

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez_compressed(buf, **self._params())
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, blob: bytes, input_dim: int, hidden_dim: int) -> MLP:
        net = cls(input_dim, hidden_dim)
        with np.load(io.BytesIO(blob)) as data:
            net.w2, net.b2 = data["w2"], data["b2"]
            if hidden_dim:
                net.w1, net.b1 = data["w1"], data["b1"]
        return net


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks with ties averaged — required for a correct AUC on tied scores."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=float)
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or sorted_values[i] != sorted_values[start]:
            ranks[order[start:i]] = (start + i + 1) / 2.0
            start = i
    return ranks


def auc(y: np.ndarray, scores: np.ndarray) -> float:
    """Rank-based ROC AUC (0.5 = coin flip). Returns 0.5 when only one class is present.

    Ties are averaged, so an untrained net that outputs one constant scores exactly 0.5
    instead of an arbitrary value that could sneak past the promotion gate.
    """
    pos, neg = scores[y > 0.5], scores[y <= 0.5]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    ranks = _average_ranks(np.concatenate([pos, neg]))
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def candidate_architectures(n: int, max_hidden: int) -> list[int]:
    """Hidden-layer sizes worth trying at this sample size (0 = linear)."""
    candidates = [0]
    if n >= 150:
        candidates.append(min(8, max_hidden))
    if n >= 400:
        candidates.append(min(16, max_hidden))
    if n >= 1000:
        candidates.append(max_hidden)
    return sorted({c for c in candidates if c >= 0})


def cross_val_auc(x: np.ndarray, y: np.ndarray, w: np.ndarray, hidden_dim: int,
                  folds: int = CV_FOLDS) -> float:
    """Mean out-of-fold AUC. Far more stable than one small holdout at this data scale."""
    n = x.shape[0]
    folds = max(2, min(folds, n // 10 or 2))
    idx = np.arange(n)
    bounds = np.array_split(idx, folds)
    scores: list[float] = []
    for fold in bounds:
        mask = np.ones(n, dtype=bool)
        mask[fold] = False
        if len(np.unique(y[mask] > 0.5)) < 2 or len(np.unique(y[~mask] > 0.5)) < 2:
            continue  # a fold with one class tells us nothing
        net = MLP(input_dim=x.shape[1], hidden_dim=hidden_dim)
        net.fit(x[mask], y[mask], w[mask])
        scores.append(auc(y[~mask], net.predict(x[~mask])))
    return float(np.mean(scores)) if scores else 0.5


# ---------- training data + persistence ----------

async def add_example(model_name: str, text: str, label: float, note: str = "",
                      weight: float = 1.0, embedding: list[float] | None = None,
                      context: list[float] | None = None) -> bool:
    """Record one supervised example. Pass `embedding` to reuse one already computed.

    `context` carries non-text features (see time_features). Per model name the context
    length must be constant — dimensions are decided by construction, not by migration.
    """
    p = db.pool()
    if p is None or not text.strip() or model_name not in MODEL_NAMES:
        return False
    try:
        vec = llm.vec_literal(embedding if embedding is not None else await llm.embed(text))
    except Exception:
        log.exception("could not embed nn example")
        return False
    await p.execute(
        "insert into nn_examples (model_name, embedding, label, weight, note, context_features)"
        " values ($1, $2::vector, $3, $4, $5, $6)",
        model_name, vec, float(label), float(weight), note[:200] or None,
        [float(x) for x in (context or [])],
    )
    return True


def _parse_vector(raw) -> list[float]:
    """asyncpg returns pgvector columns as their text representation."""
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


async def _load_examples(
    model_name: str, limit: int = 4000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    p = db.pool()
    if p is None:
        return np.empty((0, 0)), np.empty(0), np.empty(0)
    rows = await p.fetch(
        "select embedding::text as embedding, label, weight, context_features from nn_examples"
        " where model_name = $1 order by id desc limit $2",
        model_name, limit,
    )
    if not rows:
        return np.empty((0, 0)), np.empty(0), np.empty(0)
    x = np.array([
        _parse_vector(r["embedding"]) + [float(v) for v in (r["context_features"] or [])]
        for r in rows
    ], dtype=float)
    y = np.array([float(r["label"]) for r in rows], dtype=float)
    w = np.array([float(r["weight"]) for r in rows], dtype=float)
    return x, y, w


async def load_active(model_name: str) -> tuple[MLP, dict] | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select weights, input_dim, hidden_dim, metrics, version from nn_models"
        " where name = $1 and active order by version desc limit 1",
        model_name,
    )
    if row is None:
        return None
    try:
        net = MLP.from_bytes(bytes(row["weights"]), row["input_dim"], row["hidden_dim"])
    except Exception:
        log.exception("could not deserialize %s weights", model_name)
        return None
    raw = row["metrics"]
    metrics = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    metrics["version"] = row["version"]
    return net, metrics


_cache: dict[str, tuple[MLP, dict] | None] = {}


def invalidate_cache(model_name: str | None = None) -> None:
    global _router_cache
    if model_name is None:
        _cache.clear()
        _router_cache = False
    else:
        _cache.pop(model_name, None)
        if model_name == "router":
            _router_cache = False


async def score(model_name: str, text: str,
                context: list[float] | None = None) -> float | None:
    """Predicted probability for one text (+ context), or None if no model is trustworthy."""
    cfg = settings()
    if not cfg.nn_enabled or db.pool() is None:
        return None
    if model_name not in _cache:
        _cache[model_name] = await load_active(model_name)
    loaded = _cache[model_name]
    if loaded is None:
        return None
    net, metrics = loaded
    if float(metrics.get("cv_auc", 0.0)) < MIN_USABLE_AUC:
        return None  # not yet better than guessing — don't let it steer anything
    try:
        vec = np.array(list(await llm.embed(text)) + [float(v) for v in (context or [])],
                       dtype=float)
    except Exception:
        log.exception("scoring embed failed")
        return None
    if vec.shape[0] != net.input_dim:
        return None
    return float(net.predict(vec)[0])


async def train(model_name: str) -> str:
    """Select an architecture by cross-validation, retrain on all data, promote if better."""
    cfg = settings()
    p = db.pool()
    if p is None:
        return f"{model_name}: no database."
    if model_name not in MODEL_NAMES:
        return f"{model_name}: unknown model."

    x, y, w = await _load_examples(model_name)
    n = x.shape[0]
    if n < cfg.nn_min_examples:
        return f"{model_name}: {n}/{cfg.nn_min_examples} examples — need more signal first."
    if len(np.unique(y > 0.5)) < 2:
        return f"{model_name}: {n} examples but only one class — need positives and negatives."

    rng = np.random.default_rng(7)
    perm = rng.permutation(n)
    x, y, w = x[perm], y[perm], w[perm]

    results: list[tuple[float, int]] = []
    for hidden in candidate_architectures(n, cfg.nn_hidden_dim):
        cv = cross_val_auc(x, y, w, hidden)
        results.append((cv, hidden))
        log.info("%s: hidden=%s cv_auc=%.3f", model_name, hidden, cv)
    best_auc, best_hidden = max(results)

    net = MLP(input_dim=x.shape[1], hidden_dim=best_hidden)
    loss = net.fit(x, y, w)
    in_sample = auc(y, net.predict(x))
    metrics = {
        "cv_auc": round(best_auc, 4),
        "cv_folds": min(CV_FOLDS, max(2, n // 10 or 2)),
        "in_sample_auc": round(in_sample, 4),
        "train_loss": round(float(loss), 4),
        "hidden_dim": best_hidden,
        "n": int(n),
        "candidates": {str(h): round(a, 4) for a, h in results},
    }

    previous = await load_active(model_name)
    prev_auc = float(previous[1].get("cv_auc", 0.0)) if previous else 0.0
    promote = best_auc >= max(prev_auc, MIN_USABLE_AUC)

    row = await p.fetchrow(
        "select coalesce(max(version), 0) + 1 as v from nn_models where name = $1", model_name)
    version = row["v"]
    await p.execute(
        "insert into nn_models (name, version, input_dim, hidden_dim, weights, examples,"
        " metrics, active) values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
        model_name, version, net.input_dim, best_hidden, net.to_bytes(), n,
        json.dumps(metrics), promote,
    )
    if promote:
        await p.execute(
            "update nn_models set active = false where name = $1 and version <> $2",
            model_name, version,
        )
        invalidate_cache(model_name)

    shape = "linear" if best_hidden == 0 else f"hidden={best_hidden}"
    if promote:
        verdict = "promoted — now steering decisions"
    elif previous:
        verdict = f"not promoted (active stays v{previous[1]['version']}, CV {prev_auc:.3f})"
    else:
        verdict = f"not promoted (needs CV AUC >= {MIN_USABLE_AUC})"
    return f"{model_name} v{version}: {shape}, CV AUC {best_auc:.3f} on {n} examples — {verdict}"


async def train_all() -> str:
    return "\n".join([await train(name) for name in MODEL_NAMES])


async def status() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    cfg = settings()
    lines = []
    for name in MODEL_NAMES:
        counts = await p.fetchrow(
            "select count(*) as n, count(*) filter (where label > 0.5) as pos"
            " from nn_examples where model_name = $1", name)
        active = await p.fetchrow(
            "select version, hidden_dim, metrics from nn_models"
            " where name = $1 and active order by version desc limit 1", name)
        if active is None:
            need = max(0, cfg.nn_min_examples - int(counts["n"]))
            lines.append(
                f"• {name}: untrained — {counts['n']} examples ({counts['pos']} positive)"
                + (f", {need} more to start" if need else ", ready to train (/train)"))
            continue
        raw = active["metrics"]
        m = raw if isinstance(raw, dict) else json.loads(raw)
        shape = "linear" if not active["hidden_dim"] else f"hidden={active['hidden_dim']}"
        steering = "steering" if float(m.get("cv_auc", 0)) >= MIN_USABLE_AUC else "observing only"
        lines.append(
            f"• {name}: v{active['version']} ({shape}) — CV AUC {m.get('cv_auc')} on "
            f"{m.get('n')} examples, {steering}. Now {counts['n']} examples "
            f"({counts['pos']} positive).")
    return ("🧠 Neural network status\n" + "\n".join(lines)
            + "\n\nIt learns what you pay attention to, not language. /train to retrain.")


# ---------------------------------------------------------------------------
# The local router: a softmax network distilled from the cloud classifier.
#
# Unlike the binary heads (trained on the user's own taps), this one is deliberate
# DISTILLATION: the label is what the LLM router decided for the same message. The goal is
# not judgement but independence — once the local model reproduces those decisions reliably,
# most messages never need a cloud call to be understood. The gate is strict (cv accuracy
# and per-prediction confidence) because a misroute sends a question to the wrong specialist.
# ---------------------------------------------------------------------------

ROUTER_MODEL_NAME = "router"
ROUTER_MIN_EXAMPLES = 80
ROUTER_MIN_ACC_TO_USE = 0.85       # below this, keep paying for the LLM classifier
ROUTER_MIN_CONFIDENCE = 0.60       # per-message: an unsure local router defers


class SoftmaxNet:
    """Multi-class sibling of MLP: optional tanh hidden layer into softmax, Adam on CE."""

    def __init__(self, input_dim: int, n_classes: int, hidden_dim: int = 0,
                 seed: int = 17) -> None:
        rng = np.random.default_rng(seed)
        self.input_dim = input_dim
        self.n_classes = n_classes
        self.hidden_dim = max(0, hidden_dim)
        if self.hidden_dim:
            self.w1 = rng.normal(0.0, np.sqrt(2.0 / input_dim), (input_dim, self.hidden_dim))
            self.b1 = np.zeros(self.hidden_dim)
            self.w2 = rng.normal(0.0, np.sqrt(1.0 / self.hidden_dim),
                                 (self.hidden_dim, n_classes))
        else:
            self.w1 = None
            self.b1 = None
            self.w2 = np.zeros((input_dim, n_classes))
        self.b2 = np.zeros(n_classes)

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = np.tanh(x @ self.w1 + self.b1) if self.hidden_dim else x
        z = h @ self.w2 + self.b2
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True), h

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return self.forward(np.atleast_2d(x))[0]

    def _params(self) -> dict[str, np.ndarray]:
        params = {"w2": self.w2, "b2": self.b2}
        if self.hidden_dim:
            params["w1"] = self.w1
            params["b1"] = self.b1
        return params

    def fit(self, x: np.ndarray, y: np.ndarray, epochs: int = 200, lr: float = 5e-3,
            l2: float = 1e-2, batch_size: int = 32, seed: int = 17) -> float:
        """y holds integer class indices. Returns final mean cross-entropy."""
        n = x.shape[0]
        y = y.astype(int)
        rng = np.random.default_rng(seed)
        m = {k: np.zeros_like(v) for k, v in self._params().items()}
        v = {k: np.zeros_like(val) for k, val in self._params().items()}
        beta1, beta2, eps, step = 0.9, 0.999, 1e-8, 0
        loss = 0.0
        for _ in range(epochs):
            order = rng.permutation(n)
            epoch_loss = 0.0
            for start in range(0, n, batch_size):
                idx = order[start:start + batch_size]
                xb, yb = x[idx], y[idx]
                probs, h = self.forward(xb)
                p_true = np.clip(probs[np.arange(len(idx)), yb], 1e-9, 1.0)
                epoch_loss += float(-np.log(p_true).sum())

                d_z = probs.copy()
                d_z[np.arange(len(idx)), yb] -= 1.0
                d_z /= max(1, len(idx))
                grads = {"w2": h.T @ d_z + l2 * self.w2, "b2": d_z.sum(axis=0)}
                if self.hidden_dim:
                    d_h = (d_z @ self.w2.T) * (1.0 - h ** 2)
                    grads["w1"] = xb.T @ d_h + l2 * self.w1
                    grads["b1"] = d_h.sum(axis=0)
                step += 1
                for key, grad in grads.items():
                    m[key] = beta1 * m[key] + (1 - beta1) * grad
                    v[key] = beta2 * v[key] + (1 - beta2) * (grad ** 2)
                    m_hat = m[key] / (1 - beta1 ** step)
                    v_hat = v[key] / (1 - beta2 ** step)
                    setattr(self, key,
                            getattr(self, key) - lr * m_hat / (np.sqrt(v_hat) + eps))
            loss = epoch_loss / max(1, n)
        return loss

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez_compressed(buf, **self._params())
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, blob: bytes, input_dim: int, n_classes: int,
                   hidden_dim: int) -> SoftmaxNet:
        net = cls(input_dim, n_classes, hidden_dim)
        with np.load(io.BytesIO(blob)) as data:
            net.w2, net.b2 = data["w2"], data["b2"]
            if hidden_dim:
                net.w1, net.b1 = data["w1"], data["b1"]
        return net


def softmax_cv_accuracy(x: np.ndarray, y: np.ndarray, n_classes: int, hidden_dim: int,
                        folds: int = CV_FOLDS) -> float:
    """Out-of-fold accuracy for the router candidates."""
    n = x.shape[0]
    folds = max(2, min(folds, n // 10 or 2))
    bounds = np.array_split(np.arange(n), folds)
    scores: list[float] = []
    for fold in bounds:
        mask = np.ones(n, dtype=bool)
        mask[fold] = False
        if len(np.unique(y[mask])) < 2:
            continue
        net = SoftmaxNet(x.shape[1], n_classes, hidden_dim)
        net.fit(x[mask], y[mask])
        pred = net.predict_proba(x[~mask]).argmax(axis=1)
        scores.append(float(np.mean(pred == y[~mask].astype(int))))
    return float(np.mean(scores)) if scores else 0.0


async def add_router_example(text: str, agent: str, classes: tuple[str, ...],
                             embedding: list[float] | None = None) -> bool:
    """One distillation example: what the LLM router decided for this message."""
    if agent not in classes:
        return False
    p = db.pool()
    if p is None or not text.strip():
        return False
    try:
        vec = llm.vec_literal(embedding if embedding is not None else await llm.embed(text))
    except Exception:
        log.exception("could not embed router example")
        return False
    await p.execute(
        "insert into nn_examples (model_name, embedding, label, weight, note)"
        " values ($1, $2::vector, $3, 1.0, $4)",
        ROUTER_MODEL_NAME, vec, float(classes.index(agent)), agent)
    return True


async def train_router(classes: tuple[str, ...]) -> str:
    cfg = settings()
    p = db.pool()
    if p is None:
        return "router: no database."
    x, y, _ = await _load_examples(ROUTER_MODEL_NAME)
    n = x.shape[0]
    if n < ROUTER_MIN_EXAMPLES:
        return f"router: {n}/{ROUTER_MIN_EXAMPLES} examples — still learning from the LLM."
    if len(np.unique(y.astype(int))) < 2:
        return f"router: {n} examples but only one agent seen."

    rng = np.random.default_rng(7)
    perm = rng.permutation(n)
    x, y = x[perm], y[perm]
    results = [(softmax_cv_accuracy(x, y, len(classes), h), h)
               for h in candidate_architectures(n, cfg.nn_hidden_dim)]
    best_acc, best_hidden = max(results)

    net = SoftmaxNet(x.shape[1], len(classes), best_hidden)
    loss = net.fit(x, y)
    metrics = {"cv_acc": round(best_acc, 4), "n": int(n), "hidden_dim": best_hidden,
               "classes": list(classes), "train_loss": round(float(loss), 4)}

    previous = await load_active(ROUTER_MODEL_NAME)
    prev_acc = float(previous[1].get("cv_acc", 0.0)) if previous else 0.0
    promote = best_acc >= max(prev_acc, 0.70)

    row = await p.fetchrow(
        "select coalesce(max(version), 0) + 1 as v from nn_models where name = $1",
        ROUTER_MODEL_NAME)
    await p.execute(
        "insert into nn_models (name, version, input_dim, hidden_dim, weights, examples,"
        " metrics, active) values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
        ROUTER_MODEL_NAME, row["v"], net.input_dim, best_hidden, net.to_bytes(), n,
        json.dumps(metrics), promote)
    if promote:
        await p.execute("update nn_models set active = false where name = $1 and version <> $2",
                        ROUTER_MODEL_NAME, row["v"])
        invalidate_cache(ROUTER_MODEL_NAME)
    live = "routing locally" if best_acc >= ROUTER_MIN_ACC_TO_USE else "shadowing the LLM"
    return f"router v{row['v']}: acc {best_acc:.3f} on {n} examples — {live}"


_router_cache: tuple[SoftmaxNet, dict] | None | bool = False   # False = not loaded yet


async def _load_router() -> tuple[SoftmaxNet, dict] | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select weights, input_dim, hidden_dim, metrics from nn_models"
        " where name = $1 and active order by version desc limit 1", ROUTER_MODEL_NAME)
    if row is None:
        return None
    raw = row["metrics"]
    metrics = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    classes = metrics.get("classes") or []
    if not classes:
        return None
    try:
        net = SoftmaxNet.from_bytes(bytes(row["weights"]), row["input_dim"],
                                    len(classes), row["hidden_dim"])
    except Exception:
        log.exception("could not deserialize the router")
        return None
    return net, metrics


async def classify_route(embedding: list[float]) -> tuple[str, float] | None:
    """(agent, confidence) from the local router — or None when it must defer to the LLM."""
    global _router_cache
    if not settings().nn_enabled:
        return None
    if _router_cache is False:
        _router_cache = await _load_router()
    if _router_cache is None:
        return None
    net, metrics = _router_cache
    if float(metrics.get("cv_acc", 0.0)) < ROUTER_MIN_ACC_TO_USE:
        return None
    vec = np.array(embedding, dtype=float)
    if vec.shape[0] != net.input_dim:
        return None
    probs = net.predict_proba(vec)[0]
    idx = int(probs.argmax())
    confidence = float(probs[idx])
    if confidence < ROUTER_MIN_CONFIDENCE:
        return None
    return metrics["classes"][idx], confidence
