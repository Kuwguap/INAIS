"""The neural network: does it actually learn, and is the promotion gate honest?"""

from __future__ import annotations

import numpy as np

from inais.brain.nn import MLP, auc, candidate_architectures, cross_val_auc


def _task(n: int, dim: int = 64, noise: float = 0.05, seed: int = 0):
    rng = np.random.default_rng(seed)
    direction = rng.normal(size=dim)
    direction /= np.linalg.norm(direction)
    x = rng.normal(size=(n, dim))
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    y = (x @ direction + rng.normal(scale=noise, size=n) > 0).astype(float)
    return x, y, np.ones(n)


def test_auc_is_half_for_constant_scores():
    """An untrained net outputs one value everywhere — that must score 0.5, not luck."""
    y = np.array([1.0, 0.0, 1.0, 0.0])
    assert auc(y, np.full(4, 0.5)) == 0.5


def test_auc_perfect_and_inverted():
    y = np.array([0.0, 0.0, 1.0, 1.0])
    assert auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_auc_single_class_is_half():
    assert auc(np.ones(3), np.array([0.1, 0.5, 0.9])) == 0.5


def test_linear_net_learns_a_separable_task():
    x, y, w = _task(200)
    net = MLP(input_dim=x.shape[1], hidden_dim=0)
    untrained = auc(y, net.predict(x))
    net.fit(x, y, w, epochs=150)
    trained = auc(y, net.predict(x))
    assert untrained == 0.5           # zero-initialised → all ties
    assert trained > 0.9              # genuinely fitted, via real gradient descent


def test_hidden_net_learns_too():
    x, y, w = _task(200)
    net = MLP(input_dim=x.shape[1], hidden_dim=8)
    net.fit(x, y, w, epochs=150)
    assert auc(y, net.predict(x)) > 0.85


def test_predictions_are_probabilities():
    x, y, w = _task(80)
    net = MLP(input_dim=x.shape[1], hidden_dim=8)
    net.fit(x, y, w, epochs=40)
    p = net.predict(x)
    assert p.shape == (80,)
    assert np.all((p >= 0.0) & (p <= 1.0))


def test_weights_survive_a_round_trip():
    for hidden in (0, 8):
        x, y, w = _task(60)
        net = MLP(input_dim=x.shape[1], hidden_dim=hidden)
        net.fit(x, y, w, epochs=30)
        restored = MLP.from_bytes(net.to_bytes(), x.shape[1], hidden)
        assert np.allclose(restored.predict(x), net.predict(x))


def test_architecture_grows_with_data():
    """Scarce data must stay linear; capacity is only offered once examples justify it."""
    assert candidate_architectures(50, 32) == [0]
    assert candidate_architectures(150, 32) == [0, 8]
    assert 16 in candidate_architectures(400, 32)
    assert candidate_architectures(1200, 32) == [0, 8, 16, 32]


def test_architecture_respects_the_configured_cap():
    assert max(candidate_architectures(5000, 8)) == 8


def test_cross_validation_detects_pure_noise():
    """Labels independent of features must not look learnable — this gates promotion."""
    rng = np.random.default_rng(3)
    x = rng.normal(size=(120, 64))
    y = rng.integers(0, 2, size=120).astype(float)
    assert cross_val_auc(x, y, np.ones(120), hidden_dim=0, folds=4) < 0.75


def test_cross_validation_finds_real_signal():
    x, y, w = _task(150, noise=0.01)
    assert cross_val_auc(x, y, w, hidden_dim=0, folds=5) > 0.7
