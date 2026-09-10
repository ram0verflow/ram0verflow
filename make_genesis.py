#!/usr/bin/env python3
"""
Mine the ROFL genesis block. Run once, before anything is pushed.

    python3 make_genesis.py --miner ram0verflow --message "..." --address rofl1...

The genesis block is immutable: its hash is the root every later block
chains back to. Change the message or the payout address after the chain
has started and every block after it becomes invalid.
"""

import argparse
import os
import sys
import time

from rofl import chain as chainmod
from rofl import crypto, render
from rofl import pow as powfn
from rofl.consensus import (
    GENESIS_BITS,
    Block,
    Tx,
    TxOut,
    bits_to_target,
    block_subsidy,
    check_message,
    check_miner_name,
    difficulty,
    format_amount,
    merkle_root,
    target_to_work,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--miner", required=True)
    ap.add_argument("--message", required=True, help="goes in the coinbase, max 256 bytes at genesis")
    ap.add_argument("--address", required=True, help="receives the genesis reward")
    ap.add_argument("--timestamp", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    check_miner_name(args.miner)
    check_message(args.message, is_genesis=True)
    if not crypto.address_is_valid(args.address):
        raise SystemExit(f"invalid address: {args.address}")

    if os.path.exists(chainmod.BLOCKS_FILE) and not args.force:
        raise SystemExit(
            f"{chainmod.BLOCKS_FILE} already exists. Refusing to re-mine genesis.\n"
            f"Use --force only if the chain has never been published."
        )

    coinbase = Tx(coinbase=args.message, cb_height=0, outputs=[TxOut(block_subsidy(0), args.address)])
    root = merkle_root([coinbase.txid()])
    ts = args.timestamp or int(time.time())

    block = Block(
        height=0,
        prev_hash="00" * 32,
        merkle_root=root,
        timestamp=ts,
        bits=GENESIS_BITS,
        miner=args.miner,
        txs=[coinbase],
    )

    target = bits_to_target(GENESIS_BITS)
    k = powfn.k_for_work(target_to_work(target), 0)
    print(f"mining ROFL genesis at difficulty {difficulty(GENESIS_BITS):,.1f}")
    print(f"  puzzles  {k} x subset-sum(n={powfn.N})")
    print(f'  message  "{args.message}"')
    print(f"  reward   {format_amount(block_subsidy(0))} ROFL -> {args.address}")
    core = block.header_core()
    solutions = []
    started = time.time()
    for j in range(k):
        nonce = 0
        while True:
            numbers, tgt = powfn.instance(core, j, nonce)
            subset = powfn.solve_instance(numbers, tgt)
            if subset is not None:
                solutions.append((nonce, subset))
                break
            nonce += 1
        sys.stderr.write(f"\r  puzzle {j+1}/{k}, {time.time()-started:.0f}s…   ")
        sys.stderr.flush()
    sys.stderr.write("\r" + " " * 60 + "\r")

    block.solution = powfn.encode_solutions(solutions)
    elapsed = time.time() - started

    if os.path.exists(chainmod.BLOCKS_FILE):
        os.remove(chainmod.BLOCKS_FILE)
    chainmod.append_block(block)

    state = chainmod.load_state()
    if os.path.exists("README.md"):
        render.update_readme(state)
    render.render_svg(state)

    print(f"  found in {elapsed:.1f}s at {nonce/elapsed/1000:,.0f} kH/s")
    print()
    print(f"  genesis hash  {block.block_hash()}")
    print(f"  nonce         {nonce:,}")
    print(f"  timestamp     {ts}")
    print()
    print("written to chain/blocks.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
