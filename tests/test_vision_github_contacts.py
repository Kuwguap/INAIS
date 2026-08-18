"""Pure logic for vision, the GitHub watcher, and contact memory (no network, no database)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

import pytest

from inais.agents.contacts import MAX_NOTES_CHARS, append_note
from inais.bot.routers.vision import (
    EXTENSION_TYPES,
    SUPPORTED,
    image_block,
    media_type_for,
    pick_photo_size,
)
from inais.integrations import github


# ---------- vision ----------

@dataclass
class FakeSize:
    width: int
    height: int
    file_id: str = "x"


def test_pick_photo_size_takes_the_largest_within_budget():
    sizes = [FakeSize(90, 60), FakeSize(320, 213), FakeSize(1280, 853)]
    assert pick_photo_size(sizes).width == 1280


def test_pick_photo_size_skips_oversized_renditions():
    """Beyond the budget you pay more tokens for no accuracy, and may exceed the limit."""
    sizes = [FakeSize(320, 213), FakeSize(1280, 853), FakeSize(4000, 3000)]
    assert pick_photo_size(sizes, max_dimension=1600).width == 1280


def test_pick_photo_size_falls_back_when_everything_is_huge():
    sizes = [FakeSize(4000, 3000), FakeSize(6000, 4000)]
    assert pick_photo_size(sizes, max_dimension=1600).width == 4000  # smallest available


def test_pick_photo_size_handles_no_photo():
    assert pick_photo_size([]) is None


def test_media_type_from_mime_then_extension():
    assert media_type_for("image/png", None) == "image/png"
    assert media_type_for(None, "whiteboard.JPG") == "image/jpeg"
    assert media_type_for("application/octet-stream", "diagram.webp") == "image/webp"


def test_media_type_rejects_unsupported_formats():
    assert media_type_for("image/heic", "photo.heic") is None
    assert media_type_for("image/tiff", "scan.tiff") is None
    assert media_type_for(None, None) is None


def test_every_known_extension_maps_to_a_supported_media_type():
    """Filename fallback must never produce a media type the API will reject."""
    for ext, media in EXTENSION_TYPES.items():
        assert media in SUPPORTED
        assert media_type_for(None, f"file{ext}") == media


def test_image_block_is_a_valid_anthropic_block():
    block = image_block(b"\x89PNG fake bytes", "image/png")
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == b"\x89PNG fake bytes"


# ---------- github ----------

def test_parse_search_builds_stable_keys_and_repo():
    items = github.parse_search([{
        "html_url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "title": "Add retry logic",
        "repository_url": "https://api.github.com/repos/acme/api",
    }], "review_request")
    assert len(items) == 1
    item = items[0]
    assert item.repo == "acme/api"
    assert item.key == "review_request:https://github.com/acme/api/pull/42"
    assert "#42" in item.title


def test_parse_search_keys_are_unique_per_kind():
    """The same issue can both mention you and await your review — notify for each."""
    row = [{"html_url": "https://github.com/a/b/issues/1", "number": 1, "title": "t",
            "repository_url": "https://api.github.com/repos/a/b"}]
    assert github.parse_search(row, "mention")[0].key != github.parse_search(
        row, "review_request")[0].key


def test_parse_search_skips_rows_without_a_url():
    assert github.parse_search([{"number": 1, "title": "no url"}], "mention") == []


def test_parse_runs_keys_by_run_id_so_each_failure_notifies_once():
    runs = [{"id": 991, "name": "tests", "head_branch": "main",
             "html_url": "https://github.com/a/b/actions/runs/991"}]
    item = github.parse_runs(runs, "a/b")[0]
    assert item.key == "ci_failure:a/b:991"
    assert item.kind == "ci_failure"
    assert "main" in item.title


def test_parse_runs_skips_rows_without_an_id():
    assert github.parse_runs([{"name": "tests"}], "a/b") == []


def test_repo_of_handles_a_missing_repository_url():
    assert github.repo_of({}) == ""


def test_render_digest_groups_by_kind():
    items = [
        github.GitHubItem("review_request", "k1", "#1 fix", "https://x/1", "a/b"),
        github.GitHubItem("ci_failure", "k2", "tests failed", "https://x/2", "a/b"),
    ]
    out = github.render_digest(items)
    assert "Waiting on your review" in out and "CI failing" in out
    assert out.index("Waiting on your review") < out.index("CI failing")


def test_render_digest_is_positive_when_clear():
    assert "clear" in github.render_digest([]).lower()


def test_github_item_render_includes_repo_and_link():
    item = github.GitHubItem("mention", "k", "#7 question", "https://github.com/a/b/issues/7", "a/b")
    rendered = item.render()
    assert "[a/b]" in rendered and "https://github.com/a/b/issues/7" in rendered


# ---------- contacts ----------

def test_append_note_dates_and_orders_newest_last():
    first = append_note(None, "met at the robotics fair", datetime(2026, 8, 1))
    both = append_note(first, "called about the internship", datetime(2026, 8, 15))
    assert both.startswith("[2026-08-01]")
    assert both.strip().endswith("called about the internship")
    assert both.count("\n") == 1


def test_append_note_handles_blank_existing_notes():
    assert append_note("   ", "first contact", datetime(2026, 8, 1)) == "[2026-08-01] first contact"


def test_append_note_trims_old_history_without_leaving_a_partial_line():
    long_history = "\n".join(f"[2026-01-01] note number {i} padded out" for i in range(400))
    result = append_note(long_history, "newest", datetime(2026, 8, 18))
    assert len(result) <= MAX_NOTES_CHARS
    assert result.endswith("[2026-08-18] newest")
    assert result.startswith("[")          # never a fragment of a truncated line


@pytest.mark.parametrize("note", ["  spaced  ", "tabbed\t"])
def test_append_note_strips_whitespace(note):
    assert append_note(None, note, datetime(2026, 8, 1)) == f"[2026-08-01] {note.strip()}"
