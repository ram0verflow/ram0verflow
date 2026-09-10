#!/usr/bin/env python3
"""
Work through the open pull requests in one pass.

Six hundred submissions piled up while the pull request route was broken.
Handling them the ordinary way means one full chain replay per submission,
which at this height is a minute each and days in total.

So this replays once, holds the state in memory, and walks the queue against
it. A block that does not build on the current tip is stale by inspection --
wrong height or wrong prev_hash -- and needs no validation to reject. A block
that does build on the tip gets the full check and, if it passes, extends the
in-memory state so the next one can build on it in turn.

    GH_TOKEN=... GITHUB_REPOSITORY=owner/repo python3 drain.py [--limit N] [--dry-run]

Nothing from a fork is ever executed: submissions are read through the API as
inert text, exactly as the pull request workflow reads them.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time

from rofl import chain as chainmod
from rofl import crypto
from rofl import render
from rofl.consensus import (
    Block,
    ConsensusError,
    Tx,
    UTXOSet,
    format_amount,
    median_time_past,
    validate_block,
    validate_tx,
)
from submit import (
    BLOCK_PREFIX,
    ID_PREFIX,
    MAX_MEMPOOL,
    TX_PREFIX,
    find_payload,
    handle_identity,
    load_mempool,
    save_mempool,
)

REPO = os.environ.get("GITHUB_REPOSITORY", "")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def gh_write(*args: str) -> bool:
    """
    A mutating gh call, with backoff.

    GitHub throttles content creation at roughly 80 requests a minute and 500
    an hour, and answers a breach with 403 rather than 429. Closing a queue
    this size means thousands of writes, so every one is paced and a refusal
    waits rather than aborting the run.
    """
    for attempt in range(5):
        try:
            gh(*args)
            return True
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip().splitlines()[-1:] or [""]
            wait = 30 * (attempt + 1)
            print(f"      gh {args[0]} {args[1]} failed ({err[0][:90]}); "
                  f"waiting {wait}s")
            time.sleep(wait)
    return False


def api(path: str, **kw):
    return json.loads(gh("api", path, **kw))


def open_prs(limit: int):
    """Oldest first, so a chain of blocks from one miner lands in order."""
    out = []
    page = 1
    while len(out) < limit:
        batch = api(f"repos/{REPO}/pulls?state=open&per_page=100&page={page}"
                    "&sort=created&direction=asc")
        if not batch:
            break
        out += batch
        page += 1
    return out[:limit]


def submitted_file(pr) -> str | None:
    """The single added file under chain/pending/, read as inert text."""
    files = api(f"repos/{REPO}/pulls/{pr['number']}/files?per_page=10")
    if len(files) != 1:
        return None
    f = files[0]
    if f["status"] != "added" or not f["filename"].startswith("chain/pending/"):
        return None
    blob = api(f"repos/{REPO}/contents/{f['filename']}?ref={pr['head']['sha']}")
    if blob.get("encoding") != "base64" or blob.get("size", 0) > 200_000:
        return None
    return base64.b64decode(blob["content"]).decode("utf-8", "replace")


def close(number: int, body: str, dry: bool, pace: float = 0.0) -> None:
    print(f"    #{number}: {body.splitlines()[0]}")
    if dry:
        return
    gh_write("pr", "comment", str(number), "--body", body)
    gh_write("pr", "close", str(number))
    if pace:
        time.sleep(pace)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pace", type=float, default=9.0,
                    help="seconds between pull requests; keeps writes under "
                         "GitHub's hourly content-creation limit")
    args = ap.parse_args()
    pace = 0.0 if args.dry_run else args.pace

    if not REPO:
        print("GITHUB_REPOSITORY is not set")
        return 1

    print("replaying the chain once…")
    blocks = chainmod.load_blocks()
    state = chainmod.replay(blocks)
    mempool = load_mempool()
    print(f"  height {state.height}, tip {state.tip_hash[:16]}…, "
          f"{len(mempool)} in the mempool")

    prs = open_prs(args.limit)
    print(f"{len(prs)} open pull request(s)")

    accepted = stale = rejected = queued = ignored = 0

    for pr in prs:
        n, author = pr["number"], pr["user"]["login"]
        try:
            body = submitted_file(pr)
        except subprocess.CalledProcessError as exc:
            print(f"  #{n}: could not read ({exc}); leaving open")
            continue
        if body is None:
            ignored += 1          # ordinary code pull request: leave it alone
            continue

        kind, payload = find_payload(body)
        if not kind:
            close(n, "**Rejected.** No `rofl-block-v1:`, `rofl-tx-v1:` or "
                     "`rofl-id-v1:` line in that file.", args.dry_run, pace)
            rejected += 1
            continue

        if kind == "id":
            reply, changed = handle_identity(payload, author)
            close(n, reply, args.dry_run, pace)
            queued += changed
            rejected += not changed
            continue

        try:
            data = json.loads(base64.b64decode(payload, validate=True))
        except Exception as exc:  # noqa: BLE001
            close(n, f"**Rejected.** Could not decode that {kind}: `{exc}`", args.dry_run, pace)
            rejected += 1
            continue

        if kind == "block":
            try:
                block = Block.from_dict(data)
            except Exception as exc:  # noqa: BLE001
                close(n, f"**Rejected.** Malformed block: `{exc}`", args.dry_run, pace)
                rejected += 1
                continue

            # Cheap check first: does it even build on the tip?
            if block.height != state.height + 1 or block.prev_hash != state.tip_hash:
                close(n,
                      f"**Stale.** This block builds on height `{block.height - 1}`, "
                      f"but the chain is at `{state.height}`. Someone else got there "
                      f"first — that is what mining is. Re-run `miner.py` against the "
                      f"current tip and it will pick up the new work.",
                      args.dry_run)
                stale += 1
                continue

            if block.miner.lower() != author.lower():
                close(n, f"**Rejected.** This block names `{block.miner}` as the miner "
                         f"but was submitted by @{author}.", args.dry_run, pace)
                rejected += 1
                continue

            try:
                state.utxos = validate_block(
                    block, state.tip, state.utxos,
                    state.next_bits(), state.median_time_past(), int(time.time()),
                )
            except ConsensusError as exc:
                close(n, f"**Rejected.** {exc}", args.dry_run, pace)
                rejected += 1
                continue

            chainmod.append_block(block)
            state.blocks.append(block)
            state.miners[block.miner] = state.miners.get(block.miner, 0) + 1
            state.tx_count += len(block.txs)
            included = {t.txid() for t in block.txs[1:]}
            if included:
                mempool = [t for t in mempool if t.txid() not in included]
                save_mempool(mempool)
            reward = format_amount(sum(o.value for o in block.txs[0].outputs))
            close(n, f"### Block `{block.height}` accepted\n\n"
                     f"| | |\n|---|---|\n"
                     f"| hash | `{block.block_hash()}` |\n"
                     f"| miner | @{block.miner} |\n"
                     f"| reward | `{reward} ROFL` |\n", args.dry_run, pace)
            accepted += 1
            continue

        # transaction
        try:
            tx = Tx.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            close(n, f"**Rejected.** Malformed transaction: `{exc}`", args.dry_run, pace)
            rejected += 1
            continue
        if len(mempool) >= MAX_MEMPOOL:
            print(f"  #{n}: mempool full, leaving open")
            continue
        if any(t.txid() == tx.txid() for t in mempool):
            close(n, f"Already in the mempool as `{tx.txid()[:20]}…`.", args.dry_run, pace)
            rejected += 1
            continue
        working: UTXOSet = state.utxos.copy()
        height = state.height + 1
        for t in mempool:
            try:
                validate_tx(t, working, height)
            except ConsensusError:
                continue
            for i in t.inputs:
                working.spend(i.txid, i.vout)
            working.add_tx(t, height)
        try:
            fee = validate_tx(tx, working, height)
        except ConsensusError as exc:
            close(n, f"**Rejected.** {exc}", args.dry_run, pace)
            rejected += 1
            continue
        mempool.append(tx)
        save_mempool(mempool)
        close(n, f"### Transaction queued\n\n`{tx.txid()}`\n\n"
                 f"Fee `{format_amount(fee)} ROFL`. It waits for the next block.",
              args.dry_run)
        queued += 1

    if not args.dry_run and (accepted or queued):
        fresh = chainmod.load_state()
        render.update_readme(fresh)
        render.render_svg(fresh)

    print()
    print(f"  accepted  {accepted}")
    print(f"  queued    {queued}")
    print(f"  stale     {stale}")
    print(f"  rejected  {rejected}")
    print(f"  untouched {ignored}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out and (accepted or queued):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("changed=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
