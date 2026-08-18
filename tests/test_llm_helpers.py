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
