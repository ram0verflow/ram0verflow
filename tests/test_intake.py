#!/usr/bin/env python3
"""Tests for submission-issue rollover selection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ensure_submission_issue import ACTIVE_LABEL, choose_submission_issue


def issue(number, comments, *, active=False, state="open", pull_request=False):
    result = {
        "number": number,
        "comments": comments,
        "state": state,
        "labels": ([{"name": ACTIVE_LABEL}] if active else []),
    }
    if pull_request:
        result["pull_request"] = {"url": "https://example.invalid/pr"}
    return result


def main():
    assert choose_submission_issue([issue(1, 10, active=True)], 2200)["number"] == 1

    # Rotate away from an active issue before GitHub's 2500-comment hard limit.
    issues = [issue(1, 2200, active=True), issue(2, 7)]
    assert choose_submission_issue(issues, 2200)["number"] == 2

    # Prefer the newest standby when recovering a repository with no active label.
    issues = [issue(1, 2500), issue(2, 100), issue(4, 100)]
    assert choose_submission_issue(issues, 2200)["number"] == 4

    # Closed issues and pull requests are never submission targets.
    issues = [issue(3, 0, state="closed"), issue(4, 0, pull_request=True)]
    assert choose_submission_issue(issues, 2200) is None

    # Once every issue is near capacity the caller must create a fresh one.
    assert choose_submission_issue([issue(1, 2200), issue(2, 2500)], 2200) is None

    print("5 intake rollover checks passed")


if __name__ == "__main__":
    main()
