from inais.orchestrator.router import Route, rule_route


def test_finance_keywords_route_to_finance():
    assert rule_route("how is my binance portfolio doing?") == Route("finance", "complex", "rule")


def test_email_keywords_route_to_email():
    assert rule_route("any new email in my inbox?") == Route("email", "complex", "rule")


def test_greeting_is_simple():
    r = rule_route("hey")
    assert r is not None
    assert r.complexity == "simple"


def test_unknown_falls_through_to_classifier():
    assert rule_route("explain the difference between TCP and UDP") is None


def test_rule_matches_are_labelled_as_rules():
    """/why reports how a turn was routed, so the provenance has to be accurate."""
    for text in ("check my binance balance", "draft a reply", "remind me at 5", "quiz me"):
        route = rule_route(text)
        assert route is not None and route.source == "rule"
