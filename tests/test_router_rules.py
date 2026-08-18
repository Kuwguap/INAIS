from inais.orchestrator.router import Route, rule_route


def test_finance_keywords_route_to_finance():
    r = rule_route("how is my binance portfolio doing?")
    assert r == Route("finance", "complex")


def test_email_keywords_route_to_email():
    r = rule_route("any new email in my inbox?")
    assert r == Route("email", "complex")


def test_greeting_is_simple():
    r = rule_route("hey")
    assert r is not None
    assert r.complexity == "simple"


def test_unknown_falls_through_to_classifier():
    assert rule_route("explain the difference between TCP and UDP") is None
