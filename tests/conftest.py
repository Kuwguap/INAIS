import os

# Minimal env so settings() resolves in unit tests — no network, no DB.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("OWNER_TELEGRAM_ID", "42")
