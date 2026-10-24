"""Import explicitly approved GitHub Issues into the local Jarvis board.

No issue body is executed. No network access except GitHub API; no AI calls.
Default mode is read-only preview. Apply requires --apply and an approved label.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.board.service import BoardError, BoardStore, MAX_TASKS, _text

REPO = "coddddin/jarvis-mcp"
OWNER = "coddddin"
LABEL = "jarvis-approved"


class IssueRejected(BoardError):
    """One Issue's own data cannot be imported; the rest of the batch continues.

    code is a fixed identifier (never the title or another source text)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BoardFull(BoardError):
    """The board reached MAX_TASKS: every later Issue would fail too, so the batch stops."""


def _source_title(value):
    """The validated source title, or IssueRejected with 'invalid_title' (type/length) or
    'title_redacted' (credentials or control characters), before any write."""
    try:
        return _text(value, "title", 256)
    except BoardError:
        plain = isinstance(value, str) and value.strip() and len(value) <= 256
        raise IssueRejected("title_redacted" if plain else "invalid_title") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authorization header to a redirect destination.
        raise ValueError("GitHub redirects are not accepted")


def urlopen(request, *, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def fetch_issues(repo, token, *, max_pages=3):
    """Only open Issues (exclude PRs), bounded pagination and response size."""
    if repo != REPO or type(max_pages) is not int or not 1 <= max_pages <= 3:
        raise ValueError("Invalid inbox source or page limit")
    result = []
    for page in range(1, max_pages + 1):
        url = (f"https://api.github.com/repos/{repo}/issues?"
               f"state=open&labels={LABEL}&per_page=100&page={page}")
        req = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jarvis-inbox-mvp",
        })
        with urlopen(req, timeout=15) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("GitHub response too large")
        page_items = json.loads(raw)
        if not isinstance(page_items, list):
            raise ValueError("Unexpected GitHub response")
        result.extend(x for x in page_items if isinstance(x, dict) and "pull_request" not in x)
        if len(page_items) < 100:
            break
    else:
        raise ValueError("Inbox page limit reached; no changes applied")
    return result


def eligible(issue, *, owner, label):
    """An Issue is data, never authorization by itself."""
    if not isinstance(issue, dict) or "pull_request" in issue:
        return False
    author = issue.get("user") or {}
    labels = issue.get("labels") or []
    if not isinstance(author, dict) or not isinstance(labels, list):
        return False
    login = author.get("login")
    return (issue.get("state") == "open"
            and isinstance(login, str) and login.casefold() == owner.casefold()
            and any(isinstance(x, dict) and x.get("name") == label for x in labels)
            and type(issue.get("number")) is int
            and 0 < issue["number"] < 2**31)


def import_issue(board, issue, *, repo):
    """Atomically create the board task and dedup mapping in one SQLite transaction."""
    if repo != REPO or not eligible(issue, owner=OWNER, label=LABEL):
        raise IssueRejected("not_approved")
    number = issue["number"]
    key = f"{repo}#{number}"
    # Validate the entire source before truncating, including secrets beyond the prefix.
    source_title = _source_title(issue.get("title"))
    prefix = f"[GitHub #{number}] "
    title = _text(prefix + source_title[:200 - len(prefix)], "title", 200)
    creator = board._agent("user:github-inbox")
    # Deliberately omit untrusted body; humans can read the canonical Issue.
    detail = _text(f"Imported approved GitHub Issue: https://github.com/{repo}/issues/{number}",
                   "detail", 2000)
    now = board.clock()
    with board.connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS github_issue_imports ("
                   "issue_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, imported_at REAL NOT NULL)")
        previous = db.execute("SELECT task_id FROM github_issue_imports WHERE issue_key=?",
                              (key,)).fetchone()
        if previous:
            return {"issue": key, "task_id": previous[0], "created": False}
        if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= MAX_TASKS:
            raise BoardFull("Board task limit reached")
        task_id = uuid4().hex
        db.execute("INSERT INTO tasks(id,title,detail,status,creator,owner,lease_until,paths,"
                   "depends_on,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (task_id, title, detail, "open", creator, None, None, "[]", "[]", now, now))
        board._event(db, task_id, None, "open", creator, now)
        db.execute("INSERT INTO github_issue_imports VALUES (?,?,?)", (key, task_id, now))
        return {"issue": key, "task_id": task_id, "created": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="coddddin/jarvis-mcp")
    parser.add_argument("--owner", default="coddddin",
                        help="Only Issues authored by this login are accepted")
    parser.add_argument("--label", default="jarvis-approved")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Create open board tasks; never execute them")
    args = parser.parse_args(argv)
    if args.repo != "coddddin/jarvis-mcp":
        parser.error("Repository is fixed for this MVP")
    if args.owner != "coddddin" or args.label != "jarvis-approved":
        parser.error("Owner and approval label are fixed for this MVP")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("Set GITHUB_TOKEN with read-only Issues permission")
    try:
        guard = Guardrails(args.workspace.resolve())
        guard.check_running()
        issues = fetch_issues(args.repo, token)
        approved = [x for x in issues if eligible(x, owner=args.owner, label=args.label)]
        if not args.apply:
            print(json.dumps({"mode": "preview", "approved_issue_numbers":
                              [x["number"] for x in approved]}, ensure_ascii=False))
            return 0
        board = BoardStore(guard)
        results, rejected, halted = [], [], None
        for item in approved:
            # Only an Issue's own bad data is skipped. A stop marker or a storage failure stops the
            # batch safely; Issues already applied keep their receipts and a rerun is idempotent.
            try:
                guard.check_running()
                results.append(import_issue(board, item, repo=args.repo))
            except IssueRejected as exc:
                rejected.append({"issue": f"{args.repo}#{item['number']}", "error": exc.code})
            except GuardrailError:
                halted = "stopped"
                break
            except BoardFull:
                halted = "board_full"
                break
            except (BoardError, sqlite3.Error, OSError):
                halted = "storage_unavailable"
                break
        # Receipt is not assignment or execution authority. Report the current handoff gap
        # without sending employee messages or claiming tasks on the user's behalf.
        # An open task can already have an assignment message awaiting claim.
        # Ownership alone cannot prove that another assignment is needed.
        unclaimed = []
        if halted is None:
            for result in results:
                task = board.get(result['task_id'])
                if task['status'] == 'open' and task['owner'] is None:
                    unclaimed.append(task['id'])
            next_action = "check_existing_assignment" if unclaimed else "inspect_existing_tasks"
        else:
            next_action = "resolve_failure_then_rerun"  # the board may be unreadable; do not guess
        print(json.dumps({"mode": "apply", "results": results, "rejected": rejected, "halted": halted,
                          "dispatch": {"automatic_execution": False, "unclaimed_task_ids": unclaimed,
                                       "next_action": next_action}}, ensure_ascii=False))
        if halted is not None:
            print(f"GitHub inbox stopped: {halted}", file=sys.stderr)
            return 1
        return 2 if rejected else 0
    except (HTTPError, URLError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"GitHub inbox failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

