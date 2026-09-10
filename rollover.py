#!/usr/bin/env python3
"""
Roll the submission threads over before GitHub closes them.

An issue accepts 2,500 comments and then silently accepts no more. Both of
this chain's threads hit that wall, which takes the whole submission channel
down without any error anyone can see.

So the active thread numbers live in chain/issues.json rather than in the
README's prose. This script checks how full each one is, opens a replacement
when it is nearly full, points the README at it, and leaves a signpost on the
old thread. Run on a schedule by .github/workflows/rollover.yml.
"""

import json
import os
import re
import subprocess
import sys

ISSUES_FILE = os.path.join("chain", "issues.json")
README = "README.md"
LIMIT = 2400  # GitHub's hard cap is 2500; leave room for the signpost

TITLES = {
    "block": "Mine a block",
    "mempool": "Mempool and identities",
}
BODIES = {
    "block": (
        "Paste the `rofl-block-v1:` line printed by `python3 miner.py` here.\n\n"
        "The node validates it, appends it to the chain if it holds up, and "
        "replies with the result. See the [README](../../#mine-a-block).\n"
    ),
    "mempool": (
        "Paste the `rofl-tx-v1:` line printed by `python3 wallet.py send` here "
        "to queue a transaction, or the `rofl-id-v1:` line printed by "
        "`python3 wallet.py identity` to put your handle beside your balance.\n\n"
        "See the [README](../../#send-coins).\n"
    ),
}


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def comment_count(number: int) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    return int(gh("api", f"repos/{repo}/issues/{number}", "--jq", ".comments"))


def main() -> int:
    with open(ISSUES_FILE, encoding="utf-8") as fh:
        issues = json.load(fh)

    readme = open(README, encoding="utf-8").read()
    rolled = []

    for kind, number in sorted(issues.items()):
        count = comment_count(number)
        print(f"{kind}: issue #{number} holds {count} comment(s)")
        if count < LIMIT:
            continue

        new = gh(
            "issue", "create",
            "--title", TITLES[kind],
            "--body", BODIES[kind],
            "--label", "rofl",
        )
        new_number = int(new.rstrip("/").rsplit("/", 1)[-1])
        print(f"{kind}: opened #{new_number}")

        readme = re.sub(rf"\.\./\.\./issues/{number}\b", f"../../issues/{new_number}", readme)
        issues[kind] = new_number
        rolled.append((kind, number, new_number))

        gh("issue", "comment", str(number), "--body",
           f"This thread is full — GitHub stops accepting comments at 2,500.\n\n"
           f"Submissions continue on **#{new_number}**.")
        gh("issue", "lock", str(number), "--reason", "resolved")

    if not rolled:
        print("nothing to roll over")
        return 0

    with open(ISSUES_FILE, "w", encoding="utf-8") as fh:
        json.dump(issues, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(README, "w", encoding="utf-8") as fh:
        fh.write(readme)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("rolled=true\n")
            fh.write("summary=" + ", ".join(f"{k} #{o}→#{n}" for k, o, n in rolled) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
