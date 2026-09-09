#!/usr/bin/env python3
"""Keep a writable, discoverable ROFL submission issue available."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request


COMMENT_LIMIT = 2500
ROLLOVER_AT = 2200
SUBMISSION_LABEL = "rofl"
ACTIVE_LABEL = "rofl-active"


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"GitHub API {status}: {detail}")


def api_request(token: str, repo: str, path: str, *, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "rofl-submission-intake",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        try:
            detail = json.loads(payload).get("message", payload)
        except json.JSONDecodeError:
            detail = payload
        raise GitHubAPIError(exc.code, detail) from exc


def label_names(issue: dict) -> set[str]:
    return {
        label["name"] if isinstance(label, dict) else str(label)
        for label in issue.get("labels", [])
    }


def choose_submission_issue(
    issues: list[dict], rollover_at: int
) -> Optional[dict]:
    """Prefer a healthy active issue, then the newest healthy standby."""
    issues = [
        issue
        for issue in issues
        if "pull_request" not in issue
        and issue.get("state") == "open"
        and issue.get("comments", 0) < rollover_at
    ]
    active = [issue for issue in issues if ACTIVE_LABEL in label_names(issue)]
    pool = active or issues
    return max(pool, key=lambda issue: issue["number"], default=None)


def ensure_label(token: str, repo: str, name: str, color: str, description: str):
    encoded = urllib.parse.quote(name, safe="")
    try:
        api_request(token, repo, f"/labels/{encoded}")
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise
        api_request(
            token,
            repo,
            "/labels",
            method="POST",
            body={"name": name, "color": color, "description": description},
        )


def create_submission_issue(token: str, repo: str) -> dict:
    return api_request(
        token,
        repo,
        "/issues",
        method="POST",
        body={
            "title": "ROFL submissions",
            "body": (
                "Post `rofl-block-v1:` block candidates or `rofl-tx-v1:` "
                "transactions here. This issue was opened automatically before "
                "the previous submission issue reached GitHub's comment limit."
            ),
            "labels": [SUBMISSION_LABEL, ACTIVE_LABEL],
        },
    )


def set_active_issue(token: str, repo: str, issues: list[dict], target: dict):
    target_number = target["number"]
    if ACTIVE_LABEL not in label_names(target):
        api_request(
            token,
            repo,
            f"/issues/{target_number}/labels",
            method="POST",
            body={"labels": [ACTIVE_LABEL]},
        )

    encoded = urllib.parse.quote(ACTIVE_LABEL, safe="")
    for issue in issues:
        if issue["number"] == target_number or ACTIVE_LABEL not in label_names(issue):
            continue
        try:
            api_request(
                token,
                repo,
                f"/issues/{issue['number']}/labels/{encoded}",
                method="DELETE",
            )
        except GitHubAPIError as exc:
            # A concurrent run may already have removed it.
            if exc.status != 404:
                raise


def write_outputs(issue: dict, created: bool):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"issue_number={issue['number']}\n")
        output.write(f"issue_url={issue['html_url']}\n")
        output.write(f"created={str(created).lower()}\n")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2

    rollover_at = int(os.environ.get("ROFL_ISSUE_ROLLOVER_AT", ROLLOVER_AT))
    if not 1 <= rollover_at < COMMENT_LIMIT:
        raise ValueError(
            f"ROFL_ISSUE_ROLLOVER_AT must be between 1 and {COMMENT_LIMIT - 1}"
        )

    ensure_label(token, repo, SUBMISSION_LABEL, "8A5F10", "ROFL submissions")
    ensure_label(
        token,
        repo,
        ACTIVE_LABEL,
        "2DA44E",
        "Current writable ROFL submission issue",
    )
    issues = api_request(
        token,
        repo,
        f"/issues?state=open&labels={SUBMISSION_LABEL}&per_page=100",
    )
    target = choose_submission_issue(issues, rollover_at)
    created = target is None
    if created:
        target = create_submission_issue(token, repo)
        issues.append(target)
    set_active_issue(token, repo, issues, target)
    write_outputs(target, created)
    action = "Created" if created else "Using"
    print(
        f"{action} submission issue #{target['number']} "
        f"({target.get('comments', 0)}/{COMMENT_LIMIT} comments): "
        f"{target['html_url']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
