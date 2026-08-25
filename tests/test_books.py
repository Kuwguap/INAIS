"""Guap Books integration — pure render/format logic and the delivery invariant.
No network, no real DB: the poll test fakes the pool, storage and bot.
"""

from __future__ import annotations

import asyncio

from inais.integrations import guapbooks
from inais.jobs import books_watch


# ---------- render helpers ----------

def test_render_overview_shows_counts():
    text = guapbooks.render_overview(
        {"queued": 2, "running": 1, "failed": 0, "ideas_open": 4, "books_ready": 3, "books_listed": 1})
    assert "queued: 2" in text and "running: 1" in text
    assert "books ready: 3" in text and "Skillshare: 1" in text


def test_render_jobs_empty_and_full():
    assert "/ebook" in guapbooks.render_jobs([])
    text = guapbooks.render_jobs([
        {"kind": "full", "status": "running", "progress": "writing chapter 3/5",
         "book_title": "The Focus Reset"},
        {"kind": "ideas", "status": "failed", "error": "boom"},
    ])
    assert "The Focus Reset" in text and "writing chapter 3/5" in text
    assert "boom" in text


def test_render_ideas_lists_titles_with_start_hint():
    text = guapbooks.render_ideas({"ideas": [
        {"title": "Side Hustle Stack", "angle": "student-friendly", "rationale": "demand up"}]})
    assert "1. Side Hustle Stack" in text
    assert "student-friendly" in text
    assert "/ebook" in text
    assert "empty" in guapbooks.render_ideas({"ideas": []})


def test_render_books_marks_listed():
    text = guapbooks.render_books([
        {"title": "A", "status": "ready", "pages": 40, "skillshare_url": "https://s"},
        {"title": "B", "status": "writing", "pages": None, "skillshare_url": None},
    ])
    assert "listed ✓" in text and "A" in text and "B" in text


def test_jsonb_accepts_str_dict_and_garbage():
    assert guapbooks._jsonb({"a": 1}) == {"a": 1}
    assert guapbooks._jsonb('{"a": 1}') == {"a": 1}
    assert guapbooks._jsonb("not json") == {}
    assert guapbooks._jsonb(None) == {}


def test_filename_sanitised():
    assert books_watch._filename("The Focus Reset!") == "The_Focus_Reset.pdf"
    assert books_watch._filename("") == "book.pdf"


# ---------- delivery invariant: mark delivered ONLY after every send succeeded ----------

class _FakeBot:
    def __init__(self, fail_document: bool = False):
        self.calls: list[str] = []
        self.fail_document = fail_document

    async def send_document(self, chat_id, doc, caption=None):
        if self.fail_document:
            raise RuntimeError("telegram down")
        self.calls.append("document")

    async def send_photo(self, chat_id, photo, caption=None):
        self.calls.append("photo")

    async def send_message(self, chat_id, text):
        self.calls.append("message")


def _wire_factory(monkeypatch, job: dict, bot: _FakeBot) -> list[str]:
    """Point books_watch at a fake pool/storage/queue; returns the delivered-ids list."""
    delivered: list[str] = []

    class _Cfg:
        guapbooks_enabled = True

    monkeypatch.setattr(books_watch, "settings", lambda: _Cfg())
    monkeypatch.setattr(books_watch.db, "pool", lambda: object())
    monkeypatch.setattr(books_watch.controls, "is_paused", lambda: False)

    async def fake_ready(limit: int = 5):
        return [job]

    async def fake_fetch(path: str):
        return b"%PDF-fake"

    async def fake_mark(job_id: str):
        delivered.append(job_id)

    monkeypatch.setattr(guapbooks, "ready_undelivered", fake_ready)
    monkeypatch.setattr(guapbooks, "fetch_asset", fake_fetch)
    monkeypatch.setattr(guapbooks, "mark_delivered", fake_mark)
    return delivered


_BOOK_JOB = {"id": "j1", "kind": "full", "result": {}, "requester_chat_id": 1,
             "book_id": "b1", "title": "T", "pdf_path": "books/b1/book.pdf",
             "flyer_path": "books/b1/flyer.png", "pages": 30, "pdf_bytes": 999}


def test_poll_sends_then_marks(monkeypatch):
    bot = _FakeBot()
    delivered = _wire_factory(monkeypatch, dict(_BOOK_JOB), bot)
    n = asyncio.run(books_watch.poll(bot))
    assert n == 1
    assert bot.calls == ["document", "photo"]
    assert delivered == ["j1"]


def test_poll_failed_send_leaves_job_undelivered(monkeypatch):
    bot = _FakeBot(fail_document=True)
    delivered = _wire_factory(monkeypatch, dict(_BOOK_JOB), bot)
    n = asyncio.run(books_watch.poll(bot))
    assert n == 0
    assert delivered == []  # next tick retries — a duplicate beats a dropped book


def test_poll_ideas_job_sends_text(monkeypatch):
    bot = _FakeBot()
    job = {"id": "j2", "kind": "ideas", "requester_chat_id": 1, "title": None,
           "pdf_path": None, "flyer_path": None,
           "result": {"ideas": [{"title": "X"}]}}
    delivered = _wire_factory(monkeypatch, job, bot)
    n = asyncio.run(books_watch.poll(bot))
    assert n == 1 and bot.calls == ["message"] and delivered == ["j2"]


# ---------- library UI + downloads (added with the inline-button/download feature) ----------

def test_render_library_empty_and_populated():
    assert "empty" in guapbooks.render_library([])
    assert "1 finished" in guapbooks.render_library([{"id": "x", "title": "T", "pages": 10}])


def test_render_book_detail_shows_meta_and_gates_unready():
    ready = guapbooks.render_book_detail(
        {"title": "Money", "status": "ready", "pages": 20, "word_count": 5000, "pdf_path": "p"})
    assert "Money" in ready and "20 pages" in ready and "5,000 words" in ready
    unready = guapbooks.render_book_detail({"title": "WIP", "status": "designing"})
    assert "production" in unready  # download gated until ready


def test_library_keyboard_within_64_bytes():
    from inais.bot import keyboards

    UUID = "123e4567-e89b-12d3-a456-426614174000"
    lib = keyboards.books_library_kb([{"id": UUID, "title": "Book", "pages": 12}])
    act = keyboards.book_actions_kb(
        {"id": UUID, "pdf_path": "p", "cover_path": "c", "flyer_path": "f"})
    cds = [b.callback_data for kb in (lib, act) for row in kb.inline_keyboard for b in row]
    assert "gblib" in cds
    assert f"gbdl:pdf:{UUID}" in cds
    assert all(len(c.encode()) <= 64 for c in cds)


def test_book_actions_only_offers_files_that_exist():
    from inais.bot import keyboards

    kb = keyboards.book_actions_kb({"id": "x", "pdf_path": "p.pdf"})  # no cover/flyer
    cds = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "gbdl:pdf:x" in cds
    assert not any(c.startswith("gbdl:cover") or c.startswith("gbdl:flyer") for c in cds)


def test_download_kind_maps_to_a_storage_column():
    from inais.bot.routers.books import _ASSET_COLUMN

    assert _ASSET_COLUMN == {"pdf": "pdf_path", "cover": "cover_path", "flyer": "flyer_path"}
