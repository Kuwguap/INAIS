from inais.agents.email_agent import is_binance_security, prefilter


def _meta(labels, from_="a@b.com", subject="s", snippet=""):
    return {"labels": labels, "from": from_, "subject": subject, "snippet": snippet,
            "id": "x", "thread_id": "t"}


def test_prefilter_drops_promotions():
    assert prefilter(_meta(["INBOX", "CATEGORY_PROMOTIONS"])) is False


def test_prefilter_drops_social_and_spam():
    assert prefilter(_meta(["INBOX", "CATEGORY_SOCIAL"])) is False
    assert prefilter(_meta(["INBOX", "SPAM"])) is False


def test_prefilter_drops_non_inbox():
    assert prefilter(_meta(["CATEGORY_PERSONAL"])) is False


def test_prefilter_keeps_inbox_mail():
    assert prefilter(_meta(["INBOX", "IMPORTANT"])) is True


def test_binance_security_alert_detected():
    meta = _meta(["INBOX"], from_="Binance <no-reply@ses.binance.com>",
                 subject="New device login detected")
    assert is_binance_security(meta) is True


def test_binance_marketing_not_security():
    meta = _meta(["INBOX"], from_="Binance <no-reply@ses.binance.com>",
                 subject="Earn rewards with our new token launchpool!")
    assert is_binance_security(meta) is False


def test_non_binance_security_words_not_flagged():
    meta = _meta(["INBOX"], from_="it@university.edu", subject="password reset")
    assert is_binance_security(meta) is False
