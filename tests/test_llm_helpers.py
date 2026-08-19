from inais.llm import estimate_cost, parse_json_block, vec_literal


def test_vec_literal_roundtrippable():
    lit = vec_literal([0.1, -0.25, 1.0])
    assert lit.startswith("[") and lit.endswith("]")
    values = [float(x) for x in lit[1:-1].split(",")]
    assert values == [0.1, -0.25, 1.0]


def test_estimate_cost_prefix_match():
    # dated model ids resolve through their prefix
    cost = estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0)
    assert cost == 1.0


def test_estimate_cost_unknown_model_is_zero():
    assert estimate_cost("some-future-model", 1000, 1000) == 0.0


def test_parse_json_block_plain():
    assert parse_json_block('{"a": 1}') == {"a": 1}


def test_parse_json_block_fenced():
    text = 'Here you go:\n```json\n{"new_facts": []}\n```\nDone.'
    assert parse_json_block(text) == {"new_facts": []}


def test_parse_json_block_embedded_in_prose():
    text = 'Sure! {"x": [1, 2]} — that is all.'
    assert parse_json_block(text) == {"x": [1, 2]}


def test_parse_json_block_garbage():
    assert parse_json_block("no json here") == {}


# ---------- reasoning-model token budgets ----------

def test_reasoning_models_get_a_floor():
    """GPT-5 spends hidden reasoning tokens from the same budget — 60 tokens 400s outright."""
    from inais.llm import REASONING_MIN_TOKENS, completion_budget

    assert completion_budget("gpt-5-mini", 60) == REASONING_MIN_TOKENS
    assert completion_budget("gpt-5", 120) == REASONING_MIN_TOKENS
    assert completion_budget("o3-mini", 50) == REASONING_MIN_TOKENS


def test_a_generous_request_is_left_alone():
    from inais.llm import completion_budget

    assert completion_budget("gpt-5", 8000) == 8000


def test_non_reasoning_models_keep_small_budgets():
    """Flooring everything would waste money on models that don't need it."""
    from inais.llm import completion_budget

    assert completion_budget("gpt-4.1-mini", 60) == 60
    assert completion_budget("gpt-4o-mini", 120) == 120


def test_token_limit_errors_are_recognised():
    from inais.llm import _is_token_limit_error

    real = ("Could not finish the message because max_tokens or model output limit was "
            "reached. Please try again with higher max_tokens.")
    assert _is_token_limit_error(Exception(real))
    assert not _is_token_limit_error(Exception("invalid api key"))


# ---------- pricing ----------

def test_every_configured_model_family_has_a_price():
    from inais.llm import estimate_cost

    for model in ("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4o-mini",
                  "claude-sonnet-5", "claude-haiku-4-5", "text-embedding-3-small"):
        assert estimate_cost(model, 1_000_000, 1_000_000) > 0, f"{model} priced at zero"


def test_specific_prefixes_win_over_the_generic_one():
    """gpt-5-mini must not be billed at gpt-5 rates."""
    from inais.llm import estimate_cost

    assert estimate_cost("gpt-5-mini", 1_000_000, 0) < estimate_cost("gpt-5", 1_000_000, 0)
    assert estimate_cost("gpt-4o-mini", 1_000_000, 0) < estimate_cost("gpt-4o", 1_000_000, 0)


def test_dated_model_ids_still_resolve():
    from inais.llm import estimate_cost

    assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) > 0
