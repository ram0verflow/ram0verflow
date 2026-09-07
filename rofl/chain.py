"""
Chain state: loading, replaying and extending the ROFL block chain.

The canonical chain lives in chain/blocks.jsonl, one JSON block per line,
in height order. Everything else -- balances, the UTXO set, difficulty --
is derived by replaying that file from genesis, so there is no state to
corrupt and nothing to trust.
"""

import json
import os
import time

from .consensus import (
    Block,
    ConsensusError,
    RETARGET_INTERVAL,
    RETARGET_V2_HEIGHT,
    UTXOSet,
    bits_to_target,
    block_subsidy,
    median_time_past,
    next_bits,
    target_to_work,
    validate_block,
)

BLOCKS_FILE = os.path.join("chain", "blocks.jsonl")


def load_blocks(path: str = BLOCKS_FILE):
    if not os.path.exists(path):
        return []
    blocks = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                blocks.append(Block.from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                raise ConsensusError(f"{path}:{lineno}: malformed block ({exc})")
    return blocks


def append_block(block: Block, path: str = BLOCKS_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(block.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")


def bits_for_height(height: int, blocks, v2_height: int = RETARGET_V2_HEIGHT) -> int:
    """
    The difficulty a block at `height` must use.

    Blocks 0 and 1 use the genesis difficulty. On a retarget boundary the
    window is the previous RETARGET_INTERVAL blocks.
    """
    from .consensus import GENESIS_BITS

    if height == 0:
        return GENESIS_BITS
    prev = blocks[height - 1]
    if height % RETARGET_INTERVAL != 0:
        return prev.bits

    if height >= v2_height:
        # V2: True 16-block window from the block prior to the window (height - 17) to (height - 1).
        first_index = max(0, height - RETARGET_INTERVAL - 1)
    else:
        # Legacy: Reads height - 16 (15 intervals).
        first_index = height - RETARGET_INTERVAL
        if first_index < 0:
            return prev.bits

    return next_bits(
        height,
        prev.bits,
        blocks[first_index].timestamp,
        prev.timestamp,
        v2_height=v2_height,
    )


class ChainState:
    """The result of a full replay: UTXO set, chainwork, per-miner tallies."""

    def __init__(self):
        self.blocks = []
        self.utxos = UTXOSet()
        self.chainwork = 0
        self.miners = {}  # handle -> blocks mined
        self.tx_count = 0

    @property
    def height(self) -> int:
        return len(self.blocks) - 1

    @property
    def tip(self):
        return self.blocks[-1] if self.blocks else None

    @property
    def tip_hash(self) -> str:
        return self.tip.block_hash() if self.tip else "00" * 32

    def next_bits(self) -> int:
        return bits_for_height(len(self.blocks), self.blocks)

    def median_time_past(self) -> int:
        return median_time_past(self.blocks)

    def circulating(self) -> int:
        return self.utxos.total()


def replay(blocks, now: int | None = None, strict_time: bool = True) -> ChainState:
    """
    Validate every block from genesis and return the resulting state.

    Raises ConsensusError naming the first block that breaks a rule.
    """
    if now is None:
        now = int(time.time())
    state = ChainState()

    for i, block in enumerate(blocks):
        prev = state.blocks[-1] if state.blocks else None
        expected = bits_for_height(i, state.blocks)
        mtp = median_time_past(state.blocks)
        check_now = now if strict_time else block.timestamp
        try:
            state.utxos = validate_block(block, prev, state.utxos, expected, mtp, check_now)
        except ConsensusError as exc:
            raise ConsensusError(f"block {i} ({block.block_hash()[:16]}…): {exc}") from None
        state.blocks.append(block)
        state.chainwork += target_to_work(bits_to_target(block.bits))
        state.miners[block.miner] = state.miners.get(block.miner, 0) + 1
        state.tx_count += len(block.txs)

    return state


def load_state(path: str = BLOCKS_FILE, now: int | None = None) -> ChainState:
    return replay(load_blocks(path), now=now)


def emitted_supply(height: int) -> int:
    return sum(block_subsidy(h) for h in range(height + 1))
