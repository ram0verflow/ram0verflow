#!/usr/bin/env python3
"""
ROFL miner. Standard library only -- no pip install, no dependencies.

    python3 miner.py --miner YOUR_GITHUB_HANDLE --message "gm"

Mining happens entirely on your machine. When it finds a block it prints a
submission line; paste that as a comment on the block issue and a GitHub
Action validates it and appends it to the chain.

    python3 miner.py --help    for all options
"""

import argparse
import base64
import json
import sys
import time
import urllib.request

from rofl import chain as chainmod
from rofl import crypto
from rofl import pow as powfn
from rofl.consensus import (
    Block,
    ConsensusError,
    MAX_TXS_PER_BLOCK,
    Tx,
    TxOut,
    bits_to_target,
    block_subsidy,
    target_to_work,
    check_message,
    check_miner_name,
    difficulty,
    format_amount,
    merkle_root,
    validate_tx,
)

RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/"
PREFIX = "rofl-block-v1:"


def fetch_remote(repo: str, path: str) -> str:
    url = RAW_BASE.format(repo=repo) + path
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode()
    except Exception as exc:  # noqa: BLE001 - network errors vary widely
        raise SystemExit(f"could not fetch {url}: {exc}")


def load_chain(args):
    """Load the chain either from a local checkout or straight from GitHub."""
    if args.local:
        blocks = chainmod.load_blocks()
        mempool_raw = ""
        try:
            with open("chain/mempool.jsonl", encoding="utf-8") as fh:
                mempool_raw = fh.read()
        except FileNotFoundError:
            pass
    else:
        raw = fetch_remote(args.repo, "chain/blocks.jsonl")
        blocks = [
            chainmod.Block.from_dict(json.loads(line))
            for line in raw.splitlines()
            if line.strip()
        ]
        try:
            mempool_raw = fetch_remote(args.repo, "chain/mempool.jsonl")
        except SystemExit:
            mempool_raw = ""
    mempool = [Tx.from_dict(json.loads(l)) for l in mempool_raw.splitlines() if l.strip()]
    return blocks, mempool


def select_txs(mempool, state, height):
    """Greedily take mempool transactions that still validate, highest fee first."""
    scored = []
    working = state.utxos.copy()
    for tx in mempool:
        try:
            fee = validate_tx(tx, working, height)
            scored.append((fee, tx))
        except ConsensusError:
            continue
    scored.sort(key=lambda p: -p[0])

    chosen, fees = [], 0
    working = state.utxos.copy()
    for fee, tx in scored:
        if len(chosen) >= MAX_TXS_PER_BLOCK - 1:
            break
        try:
            actual = validate_tx(tx, working, height)
        except ConsensusError:
            continue
        for i in tx.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(tx, height)
        chosen.append(tx)
        fees += actual
    return chosen, fees


def mine(block: Block, k: int):
    """
    Solve the k puzzles this block's difficulty demands.

    Each puzzle is independent, so this is embarrassingly parallel and an
    obvious place for a faster solver to win. The reference implementation
    is deliberately plain meet-in-the-middle.
    """
    core = block.header_core()
    solutions = []
    started = time.time()
    tried = 0
    for j in range(k):
        nonce = 0
        while True:
            numbers, target = powfn.instance(core, j, nonce)
            subset = powfn.solve_instance(numbers, target)
            tried += 1
            if subset is not None:
                solutions.append((nonce, subset))
                break
            nonce += 1
        done = j + 1
        rate = (time.time() - started) / done
        sys.stderr.write(
            f"\r  puzzle {done}/{k}   {rate:.2f}s each   "
            f"{tried/done:.2f} instances per solve   "
            f"eta {(k - done) * rate:6.0f}s   "
        )
        sys.stderr.flush()
    return solutions, tried, time.time() - started


def main():
    ap = argparse.ArgumentParser(description="Mine a ROFL block.")
    ap.add_argument("--miner", required=True, help="your GitHub handle (goes in the header)")
    ap.add_argument("--message", default="", help=f"coinbase message, max 80 bytes")
    ap.add_argument("--address", default=None, help="pay the reward here (default: wallet file)")
    ap.add_argument("--wallet", default="rofl-wallet.json", help="wallet file for the address")
    ap.add_argument("--repo", default="ram0verflow/ram0verflow", help="repo to mine against")
    ap.add_argument("--local", action="store_true", help="use the local chain/ directory")
    ap.add_argument("--no-txs", action="store_true", help="mine an empty block, ignore mempool")
    args = ap.parse_args()

    check_miner_name(args.miner)
    check_message(args.message)

    address = args.address
    if not address:
        try:
            with open(args.wallet, encoding="utf-8") as fh:
                address = json.load(fh)["address"]
        except FileNotFoundError:
            raise SystemExit(
                f"no --address given and no wallet at {args.wallet}.\n"
                f"run:  python3 wallet.py new"
            )
    if not crypto.address_is_valid(address):
        raise SystemExit(f"invalid payout address: {address}")

    blocks, mempool = load_chain(args)
    state = chainmod.replay(blocks, strict_time=False)
    height = state.height + 1
    bits = state.next_bits()
    k = powfn.k_for_work(target_to_work(bits_to_target(bits)), height)

    txs, fees = ([], 0) if args.no_txs else select_txs(mempool, state, height)
    reward = block_subsidy(height) + fees

    coinbase = Tx(coinbase=args.message, cb_height=height, outputs=[TxOut(reward, address)])
    all_txs = [coinbase] + txs
    root = merkle_root([t.txid() for t in all_txs])

    timestamp = max(int(time.time()), state.median_time_past() + 1)

    block = Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=root,
        timestamp=timestamp,
        bits=bits,
        miner=args.miner,
        txs=all_txs,
    )

    print(f"ROFL miner  ·  building on {state.tip_hash[:16]}…  ·  height {height}")
    print(f"  difficulty {difficulty(bits):,.1f}   bits {bits:#010x}")
    print(f"  puzzles    {k} x subset-sum(n={powfn.N})")
    print(f"  reward     {format_amount(reward)} ROFL "
          f"(subsidy {format_amount(block_subsidy(height))} + fees {format_amount(fees)})")
    print(f"  txs        {len(txs)} from mempool")
    print()

    solutions, tried, elapsed = mine(block, k)
    sys.stderr.write("\r" + " " * 78 + "\r")
    block.solution = powfn.encode_solutions(solutions)

    payload = base64.b64encode(
        json.dumps(block.to_dict(), separators=(",", ":"), sort_keys=True).encode()
    ).decode()

    print(f"  solved {k} puzzle(s) in {elapsed:.1f}s "
          f"({tried} instances, {100*k/tried:.0f}% solvable)")
    print(f"  hash   {block.block_hash()}")
    print()
    print("Paste this as a comment on the block submission issue:")
    print()
    print(PREFIX + payload)


if __name__ == "__main__":
    main()
