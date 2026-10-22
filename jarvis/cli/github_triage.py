"""Pure, rule-based GitHub Issue triage. Never execute Issue content."""
import argparse
import json
from pathlib import Path
import re

CATEGORIES = {
    "bug": ("bug", "error", "오류", "버그", "실패"),
    "documentation": ("docs", "document", "문서", "가이드", "readme"),
    "feature": ("feature", "기능", "추가", "개선", "구현"),
}
MAX_EVENT_BYTES = 256_000


def classify(event):
    """Produce an advisory report; no labels, comments, or commands are applied."""
    issue = event.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue:
        return {"accepted": False, "reason": "not_an_issue"}
    author = issue.get("user")
    labels = issue.get("labels")
    if (not isinstance(author, dict) or author.get("login", "").casefold() != "coddddin"
            or not isinstance(labels, list)
            or not any(isinstance(x, dict) and x.get("name") == "jarvis-approved" for x in labels)):
        return {"accepted": False, "reason": "not_approved"}
    if issue.get("state") != "open" or type(issue.get("number")) is not int:
        return {"accepted": False, "reason": "not_open"}
    title = issue.get("title")
    if not isinstance(title, str) or len(title) > 200:
        return {"accepted": False, "reason": "invalid_title"}
    words = set(re.findall(r"[a-z]+|[가-힣]+", title.lower()))
    category = next((kind for kind, keywords in CATEGORIES.items()
                     if words.intersection(keywords)), "review")
    return {"accepted": True, "issue_number": issue["number"],
            "category": category, "requires_human_review": True,
            "executed": False}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("event", type=Path, help="GitHub event JSON")
    args = parser.parse_args(argv)
    if args.event.stat().st_size > MAX_EVENT_BYTES:
        parser.error("Event too large")
    data = json.loads(args.event.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        parser.error("Invalid event")
    print(json.dumps(classify(data), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
