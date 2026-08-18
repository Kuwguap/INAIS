"""GitHub watcher — strictly read-only.

Three things worth interrupting a developer for: a PR waiting on their review, a mention on
an issue, and CI going red on their own repo. Everything here is a GET; no endpoint in this
module can comment, merge, close or push. The token should be a fine-grained PAT with read
access only — see the README.

API responses are DATA: titles and bodies come from other people and are only ever rendered
as text, never followed as instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from inais.config import settings

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_TITLE = 120


class GitHubAuthError(Exception):
    """Token missing, expired, or lacking read access."""


@dataclass
class GitHubItem:
    kind: str          # review_request | mention | ci_failure
    key: str           # stable identity for dedupe
    title: str
    url: str
    repo: str = ""

    def render(self) -> str:
        icons = {"review_request": "👀", "mention": "💬", "ci_failure": "🔴"}
        repo = f"[{self.repo}] " if self.repo else ""
        return f"{icons.get(self.kind, '•')} {repo}{self.title[:MAX_TITLE]}\n{self.url}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings().github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "INAIS-personal-assistant",
    }


def repo_of(item: dict) -> str:
    """Search results carry the repo only inside repository_url."""
    url = item.get("repository_url", "")
    return "/".join(url.rsplit("/", 2)[-2:]) if url else ""


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict:
    async with session.get(f"{API}{path}", params=params, headers=_headers()) as resp:
        if resp.status in (401, 403):
            body = await resp.text()
            if "rate limit" in body.lower():
                log.warning("github rate limited")
                return {}
            raise GitHubAuthError(f"GitHub returned {resp.status} — check GITHUB_TOKEN scopes")
        if resp.status == 404:
            return {}
        resp.raise_for_status()
        return await resp.json()


def parse_search(items: list[dict], kind: str) -> list[GitHubItem]:
    """Search API rows → notifiable items. Keyed by url so each PR/issue notifies once."""
    out = []
    for it in items:
        url = it.get("html_url", "")
        if not url:
            continue
        number = it.get("number", "")
        out.append(GitHubItem(
            kind=kind,
            key=f"{kind}:{url}",
            title=f"#{number} {it.get('title', '(no title)')}",
            url=url,
            repo=repo_of(it),
        ))
    return out


def parse_runs(runs: list[dict], repo: str) -> list[GitHubItem]:
    """Failed Actions runs. Keyed by run id, so each new failure notifies exactly once."""
    out = []
    for run in runs:
        run_id = run.get("id")
        if run_id is None:
            continue
        branch = run.get("head_branch") or "?"
        out.append(GitHubItem(
            kind="ci_failure",
            key=f"ci_failure:{repo}:{run_id}",
            title=f"{run.get('name', 'workflow')} failed on {branch}",
            url=run.get("html_url", ""),
            repo=repo,
        ))
    return out


async def fetch_all() -> list[GitHubItem]:
    """Everything currently waiting on the user. Read-only; safe to call from /github too."""
    cfg = settings()
    if not cfg.github_enabled:
        return []
    items: list[GitHubItem] = []
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        reviews = await _get(session, "/search/issues", {
            "q": "is:open is:pr review-requested:@me", "sort": "updated", "per_page": "10"})
        items += parse_search(reviews.get("items", []), "review_request")

        mentions = await _get(session, "/search/issues", {
            "q": "is:open mentions:@me", "sort": "updated", "per_page": "10"})
        items += parse_search(mentions.get("items", []), "mention")

        for repo in cfg.github_repo_list:
            runs = await _get(session, f"/repos/{repo}/actions/runs", {
                "status": "failure", "per_page": "5"})
            items += parse_runs(runs.get("workflow_runs", []), repo)
    return items


def render_digest(items: list[GitHubItem]) -> str:
    if not items:
        return "✅ GitHub is clear — no reviews, mentions or red builds waiting."
    groups = {
        "review_request": "👀 Waiting on your review",
        "mention": "💬 You were mentioned",
        "ci_failure": "🔴 CI failing",
    }
    parts = []
    for kind, heading in groups.items():
        chunk = [i for i in items if i.kind == kind]
        if chunk:
            parts.append(heading + "\n" + "\n\n".join(i.render() for i in chunk))
    return "\n\n".join(parts)
