#!/usr/bin/env python3
"""
End-to-end consensus tests for ROFL.

Runs at the chain's easiest allowed difficulty so the whole suite takes a
few seconds. Exercises a real spend with real signatures, then tries to
break the chain eight different ways and asserts each attempt is rejected.

    python3 tests/test_chain.py
"""

import copy
import hashlib
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rofl import chain as chainmod  # noqa: E402
from rofl import consensus as k  # noqa: E402
from rofl import crypto  # noqa: E402
from rofl import pow as rp  # noqa: E402

# Shrink the puzzle so the suite runs in seconds. The live chain uses n=40;
# n=16 exercises exactly the same code paths at 2^8 instead of 2^20 work.
rp.N = 16
rp.B = rp.N - 2
rp.UNIT_WORK = 1 << (rp.N // 2)
k.POW_LIMIT_BITS = 0x1F010000
k.GENESIS_BITS = k.POW_LIMIT_BITS

PASSED = []
BASE_TIME = 1_750_000_000


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def expect_reject(label, fn):
    """Assert that `fn` raises ConsensusError, and show the message."""
    try:
        fn()
    except k.ConsensusError as exc:
        msg = str(exc)
        short = msg if len(msg) < 88 else msg[:85] + "…"
        print(f"  ok    {label}\n          rejected: {short}")
        PASSED.append(label)
        return
    raise AssertionError(f"FAILED: {label} was accepted but should have been rejected")


def new_key():
    priv_bytes = secrets.token_bytes(32)
    priv = crypto.privkey_from_bytes(priv_bytes)
    pub = crypto.ser_pubkey(crypto.pubkey(priv))
    return priv, pub.hex(), crypto.pubkey_to_address(pub)


def mine_block(blocks, miner, address, txs=None, message="", timestamp=None):
    """Build and mine a valid block extending `blocks`."""
    txs = txs or []
    height = len(blocks)
    state = chainmod.replay(blocks, strict_time=False)
    bits = chainmod.bits_for_height(height, blocks)

    fees = 0
    working = state.utxos.copy()
    for t in txs:
        fees += k.validate_tx(t, working, height)
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)

    coinbase = k.Tx(
        coinbase=message,
        cb_height=height,
        outputs=[k.TxOut(k.block_subsidy(height) + fees, address)],
    )
    all_txs = [coinbase] + txs
    ts = timestamp if timestamp is not None else BASE_TIME + height * 600

    block = k.Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=k.merkle_root([t.txid() for t in all_txs]),
        timestamp=ts,
        bits=bits,
        miner=miner,
        txs=all_txs,
    )

    core = block.header_core()
    kk = block.puzzles()
    block.solution = rp.encode_solutions(
        [rp.solve_puzzle(core, j) for j in range(kk)]
    )
    assert block.block_hash() == crypto.sha256d(block.header()).hex()
    return block


def sign_tx(tx, priv):
    digest = tx.sighash()
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig
    return tx


def run():
    now = BASE_TIME + 200 * 600
    print("ROFL consensus tests")
    print()

    alice_priv, alice_pub, alice_addr = new_key()
    bob_priv, bob_pub, bob_addr = new_key()

    # ---- build a chain -------------------------------------------------
    blocks = [mine_block([], "ram0verflow", alice_addr, message="ROFL genesis")]
    ok("genesis mined and self-consistent")

    for _ in range(1, 12):
        blocks.append(mine_block(blocks, "ram0verflow", alice_addr))
    state = chainmod.replay(blocks, now=now)
    assert state.height == 11
    ok(f"12 blocks replay clean (height {state.height})")

    # ---- a real spend --------------------------------------------------
    gen_coinbase_txid = blocks[0].txs[0].txid()
    entry = state.utxos.get(gen_coinbase_txid, 0)
    assert entry and entry["address"] == alice_addr

    amount = 10 * k.COIN
    fee = 5000
    change = entry["value"] - amount - fee
    spend = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(amount, bob_addr), k.TxOut(change, alice_addr)],
    )
    sign_tx(spend, alice_priv)

    blocks.append(mine_block(blocks, "octocat", bob_addr, txs=[spend], message="first spend"))
    state = chainmod.replay(blocks, now=now)
    ok("block containing a signed transaction accepted")

    balances = state.utxos.balances()
    assert balances[bob_addr] == amount + k.block_subsidy(12) + fee, balances[bob_addr]
    ok(f"bob holds {k.format_amount(balances[bob_addr])} ROFL (10 received + reward + fee)")

    emitted = chainmod.emitted_supply(state.height)
    assert emitted == state.circulating(), (emitted, state.circulating())
    ok(f"supply conserved: {k.format_amount(emitted)} ROFL emitted == unspent")

    # ---- coinbase maturity ---------------------------------------------
    young_txid = blocks[12].txs[0].txid()
    bad_spend = k.Tx(
        inputs=[k.TxIn(young_txid, 0, bob_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(bad_spend, bob_priv)
    expect_reject(
        "immature coinbase cannot be spent",
        lambda: mine_block(blocks, "octocat", bob_addr, txs=[bad_spend]),
    )

    # ---- retarget ------------------------------------------------------
    while len(blocks) < 17:
        blocks.append(mine_block(blocks, "ram0verflow", alice_addr))
    state = chainmod.replay(blocks, now=now)
    b16 = blocks[16]
    assert b16.height == 16
    assert b16.bits == chainmod.bits_for_height(16, blocks[:16])
    ok(f"difficulty retargeted at height 16 (bits {b16.bits:#010x})")

    good = list(blocks)

    # ---- tampering -----------------------------------------------------
    t = copy.deepcopy(good)
    sols = rp.decode_solutions(t[5].solution)
    sols[0] = (sols[0][0], sols[0][1] ^ 1)
    t[5].solution = rp.encode_solutions(sols)
    expect_reject("altered subset breaks proof of work", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].outputs[0].value += 1
    expect_reject("altered output breaks the merkle root", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[3].txs[0].outputs[0].value += 1
    t[3].merkle_root = k.merkle_root([x.txid() for x in t[3].txs])
    expect_reject("inflated coinbase rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].inputs[0].sig = "00" * 64
    t[12].merkle_root = k.merkle_root([x.txid() for x in t[12].txs])
    expect_reject("forged signature rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].miner = "someone-else"
    expect_reject("stealing a block by renaming the miner invalidates the puzzles",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[7].solution = t[7].solution + "0" * (2 * rp.SOLUTION_BYTES)
    expect_reject("padding the solution with an extra puzzle rejected",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    del t[8]
    for i, b in enumerate(t):
        b.height = i
    expect_reject("removing a block breaks the hash chain", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[9].bits = k.POW_LIMIT_BITS + 0x00010000
    expect_reject("mining at the wrong difficulty rejected", lambda: chainmod.replay(t, now=now))

    dbl = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(dbl, alice_priv)
    expect_reject(
        "double spend of an already-spent output rejected",
        lambda: mine_block(good, "ram0verflow", alice_addr, txs=[dbl]),
    )

    stale = mine_block(good[:13], "ram0verflow", alice_addr)
    expect_reject(
        "block built on a stale tip rejected",
        lambda: chainmod.replay(good + [stale], now=now),
    )

    # ---- v2 difficulty adjustment --------------------------------------
    # 1. Test true 16-block window calculation without off-by-one
    v2_blocks = list(good)
    while len(v2_blocks) < 33:
        v2_blocks.append(mine_block(v2_blocks, "ram0verflow", alice_addr))
    expected_v2_bits = k.next_bits(
        32,
        v2_blocks[31].bits,
        v2_blocks[15].timestamp,
        v2_blocks[31].timestamp,
        v2_height=32,
    )
    assert chainmod.bits_for_height(32, v2_blocks[:32], v2_height=32) == expected_v2_bits
    ok("v2 difficulty retarget uses true 16-block window (anchor height 15)")

    # 2. Test difficulty ceiling clamping to POW_CEILING_BITS
    fast_bits = k.next_bits(
        528,
        0x17012b23,
        1000,
        1016,
        v2_height=528,
    )
    assert fast_bits == k.POW_CEILING_BITS
    ok(f"v2 difficulty retarget enforces ceiling POW_CEILING_BITS ({fast_bits:#010x})")

    # ---- final state ---------------------------------------------------
    final = chainmod.replay(good, now=now)
    print()
    print(f"  {len(PASSED)} checks passed")
    print(f"  height {final.height}  tip {final.tip_hash[:24]}…")
    print(f"  chainwork {final.chainwork:,} expected hashes")
    print(f"  supply {k.format_amount(final.circulating())} ROFL")
    return 0


if __name__ == "__main__":
    sys.exit(run())
