"""The engagement head and the proactive decision contract (pure logic only)."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from inais.brain.nn import CONTEXT_DIMS, MLP, time_features
from inais.brain.signals import HARVEST_AFTER_HOURS, REPLY_WINDOW_MINUTES
from inais.jobs.proactive import parse_decision


# ---------- time features ----------

def test_time_features_have_the_declared_dimension():
    assert len(time_features(datetime(2026, 8, 19, 9, 30))) == CONTEXT_DIMS


def test_midnight_sits_next_to_late_evening_not_opposite_it():
    """The whole point of sin/cos over a raw hour number.

    Only the hour components (first two dims) — crossing midnight legitimately moves the
    weekday features, and that change is signal, not error.
    """
    before = np.array(time_features(datetime(2026, 8, 19, 23, 55))[:2])
    after = np.array(time_features(datetime(2026, 8, 20, 0, 5))[:2])
    noon = np.array(time_features(datetime(2026, 8, 19, 12, 0))[:2])
    assert np.linalg.norm(before - after) < 0.1
    assert np.linalg.norm(before - noon) > 1.5


def test_weekend_flag():
    assert time_features(datetime(2026, 8, 22, 12))[-1] == 1.0   # Saturday
    assert time_features(datetime(2026, 8, 19, 12))[-1] == 0.0   # Wednesday


def test_features_are_bounded():
    for hour in range(24):
        for value in time_features(datetime(2026, 8, 19, hour)):
            assert -1.0 <= value <= 1.0


def test_mlp_trains_on_embedding_plus_context():
    """The engagement head's real input shape: 1536 + time features."""
    rng = np.random.default_rng(0)
    dim = 64 + CONTEXT_DIMS
    x = rng.normal(size=(120, dim))
    y = (x[:, -CONTEXT_DIMS] > 0).astype(float)   # signal lives in a context column
    net = MLP(input_dim=dim, hidden_dim=0)
    net.fit(x, y, np.ones(120), epochs=120)
    from inais.brain.nn import auc

    assert auc(y, net.predict(x)) > 0.9


# ---------- proactive decision parsing ----------

def test_send_decision_with_voice():
    got = parse_decision('{"action": "send", "medium": "voice", "message": "Morning."}',
                         voice_allowed=True)
    assert got == ("voice", "Morning.")


def test_voice_downgrades_to_text_when_disabled():
    got = parse_decision('{"action": "send", "medium": "voice", "message": "Morning."}',
                         voice_allowed=False)
    assert got == ("text", "Morning.")


def test_skip_and_empty_produce_no_send():
    assert parse_decision('{"action": "skip", "medium": "text", "message": ""}', True) is None
    assert parse_decision('{"action": "send", "medium": "text", "message": ""}', True) is None
    assert parse_decision("", True) is None
    assert parse_decision("NOTHING", True) is None


def test_unknown_medium_falls_back_to_text():
    got = parse_decision('{"action": "send", "medium": "hologram", "message": "hi"}', True)
    assert got == ("text", "hi")


def test_plain_text_reply_is_tolerated_as_a_text_send():
    """The model sometimes ignores the JSON contract; a good message shouldn't be lost."""
    assert parse_decision("Heads up — the lab report is due at 5.", True) == (
        "text", "Heads up — the lab report is due at 5.")


# ---------- harvesting constants ----------

def test_harvest_waits_longer_than_the_reply_window():
    """Labelling 'no reply' before the user had the whole window would poison the data."""
    assert HARVEST_AFTER_HOURS * 60 > REPLY_WINDOW_MINUTES


# ---------- the local router ----------

def _clustered(n_classes=4, dim=64, n=300, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(scale=2.0, size=(n_classes, dim))
    y = rng.integers(0, n_classes, size=n)
    return centers[y] + rng.normal(size=(n, dim)), y.astype(float)


def test_softmax_net_learns_a_four_way_split():
    from inais.brain.nn import SoftmaxNet

    x, y = _clustered()
    net = SoftmaxNet(x.shape[1], 4, hidden_dim=0)
    net.fit(x, y, epochs=100)
    assert float(np.mean(net.predict_proba(x).argmax(1) == y.astype(int))) > 0.95


def test_softmax_probabilities_sum_to_one():
    from inais.brain.nn import SoftmaxNet

    x, _ = _clustered(n=20)
    probs = SoftmaxNet(x.shape[1], 4).predict_proba(x)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert (probs >= 0).all()


def test_softmax_weights_survive_a_round_trip():
    from inais.brain.nn import SoftmaxNet

    for hidden in (0, 8):
        x, y = _clustered(n=80)
        net = SoftmaxNet(x.shape[1], 4, hidden_dim=hidden)
        net.fit(x, y, epochs=30)
        restored = SoftmaxNet.from_bytes(net.to_bytes(), x.shape[1], 4, hidden)
        assert np.allclose(restored.predict_proba(x), net.predict_proba(x))


def test_cv_accuracy_rejects_unlearnable_labels():
    from inais.brain.nn import softmax_cv_accuracy

    rng = np.random.default_rng(3)
    x = rng.normal(size=(160, 64))
    y = rng.integers(0, 4, size=160).astype(float)
    assert softmax_cv_accuracy(x, y, 4, 0, folds=4) < 0.5


def test_router_gates_are_strict_enough():
    """A misroute sends the question to the wrong specialist; deferring costs one API call."""
    from inais.brain.nn import ROUTER_MIN_ACC_TO_USE, ROUTER_MIN_CONFIDENCE

    assert ROUTER_MIN_ACC_TO_USE >= 0.8
    assert ROUTER_MIN_CONFIDENCE >= 0.5
