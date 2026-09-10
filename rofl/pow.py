"""
ROFL proof of work: subset-sum.

Bitcoin's work function is "find a nonce whose header hashes below a target".
ROFL's is "find subsets of these numbers that sum exactly to these targets".

Both are cheap to verify and expensive to find. The difference is that
nobody has built an ASIC for subset-sum, so mining ROFL means writing a
better solver rather than buying better silicon.

## One puzzle

Every puzzle is derived from the block's header and its own index:

    seed  = H²(header_core ‖ j ‖ nonce)     # header_core includes the miner
    a_i   = H(seed ‖ i) mod 2^b             # n numbers, i = 0 … n−1
    S     = Σ a_i
    T     = S/2 ± (H(seed ‖ "T") mod S/16)  # the target

with `n` = 40 and `b` = n − 2.

Two details there are load-bearing, and both were found by measurement.

*Why b = n − 2.* Density -- how wide the numbers are relative to how many
there are -- decides whether subset-sum is hard. Below about 0.94, lattice
reduction solves instances outright. Here the density is n/b ≈ 1.05, just
inside the hard regime.

*Why T sits near S/2.* Subset sums are not spread evenly. They pile up
around half the total, the way the sum of many coin flips piles up around
the middle. A target drawn uniformly from the whole range lands in the tail
where almost no subsets reach, and the first version of this file did
exactly that: zero of twelve instances had any solution at all. Placing T
near the mean puts it where the subsets actually are, and about 55% of
instances then have at least one.

A miner does not choose the puzzle. The miner's own handle is inside
`header_core`, so everyone works on different puzzles and a solution posted
publicly is worth nothing to anyone else.

## Solvability and the grind

An instance is random, so it may have no solution. Roughly 55% do. A miner
who finds none increments that puzzle's nonce and gets a fresh one. That
grind is what makes mining a lottery rather than a footrace, exactly as
nonce grinding is in Bitcoin.

## Difficulty

The best known attack is meet-in-the-middle: enumerate every subset sum of
each half and look for a pair adding to T. That is about 2^(n/2) time *and
memory*, and memory is the binding constraint -- in Python, n = 44 already
wants 280 MB to buy 2.4 seconds. Difficulty cannot come from growing n.

So it comes from repetition, as it does in Bitcoin: a block must carry `k`
solved puzzles, and

    work = k · 2^(n/2)      and therefore      k = work / 2^20

`work` is the same quantity chainwork measures, read from the same compact
`bits` field by the same formula, so difficulty retargeting needs no special
case. One puzzle takes about half a second, which puts k = 1024 at roughly
ten minutes -- the chain's target spacing.
"""

import hashlib

N = 40  # numbers per puzzle; fixed, because memory not time is the wall
B = N - 2  # bit width of each number, giving density n/b ≈ 1.05
UNIT_WORK = 1 << (N // 2)  # 2^20, the cost of one puzzle
K_MAX = 1024  # puzzles per block before RETARGET_V2_HEIGHT
K_MAX_V2 = 3000  # after it; ~the most that still fits a GitHub comment
MAX_NONCE = 1 << 16  # per-puzzle nonce is 2 bytes on the wire
SOLUTION_BYTES = 7  # 2-byte nonce + 5-byte subset mask


def k_max_for(height: int) -> int:
    """
    The puzzle cap in force at `height`.

    The original 1024 was reached at block 128, after which every retarget
    raised difficulty and bought no extra work -- so block spacing stopped
    responding to difficulty at all. V2 raises it enough for the retarget to
    steer again, and stops just short of the point where a block no longer
    fits in a GitHub comment.
    """
    from .consensus import RETARGET_V2_HEIGHT

    return K_MAX_V2 if height >= RETARGET_V2_HEIGHT else K_MAX


def k_for_work(work: int, height: int = 0) -> int:
    """Puzzles required for a given amount of work. Integer arithmetic only."""
    return max(1, min(k_max_for(height), work // UNIT_WORK))


def instance(header_core: bytes, j: int, nonce: int):
    """Derive puzzle `j` at `nonce`. Deterministic and cheap."""
    seed = hashlib.sha256(
        hashlib.sha256(
            header_core + b"|" + str(j).encode() + b"|" + str(nonce).encode()
        ).digest()
    ).digest()

    mask = (1 << B) - 1
    numbers = [
        (int.from_bytes(hashlib.sha256(seed + i.to_bytes(4, "big")).digest(), "big") & mask) or 1
        for i in range(N)
    ]
    total = sum(numbers)
    spread = max(1, total // 16)
    offset = int.from_bytes(hashlib.sha256(seed + b"target").digest(), "big") % (2 * spread)
    return numbers, total // 2 + offset - spread


def check_one(header_core: bytes, j: int, nonce: int, subset: int) -> bool:
    """Verify one solved puzzle. O(n) additions."""
    if subset <= 0 or subset >= (1 << N) or not 0 <= nonce < MAX_NONCE:
        return False
    numbers, target = instance(header_core, j, nonce)
    total = 0
    for i in range(N):
        if subset >> i & 1:
            total += numbers[i]
    return total == target


def encode_solutions(solutions) -> str:
    """Pack (nonce, subset) pairs into hex. 7 bytes each."""
    out = bytearray()
    for nonce, subset in solutions:
        out += nonce.to_bytes(2, "big") + subset.to_bytes(5, "big")
    return out.hex()


def decode_solutions(blob: str):
    raw = bytes.fromhex(blob)
    if len(raw) % SOLUTION_BYTES:
        raise ValueError("solution blob is not a whole number of entries")
    return [
        (
            int.from_bytes(raw[o:o + 2], "big"),
            int.from_bytes(raw[o + 2:o + SOLUTION_BYTES], "big"),
        )
        for o in range(0, len(raw), SOLUTION_BYTES)
    ]


def verify(header_core: bytes, blob: str, k: int) -> bool:
    """Verify a whole block's proof of work: k puzzles, all solved."""
    try:
        solutions = decode_solutions(blob)
    except ValueError:
        return False
    if len(solutions) != k:
        return False
    return all(check_one(header_core, j, n, s) for j, (n, s) in enumerate(solutions))


# --------------------------------------------------------------------------
# solving
# --------------------------------------------------------------------------


def _half_sums(numbers):
    """
    Every subset sum of `numbers`, indexed so sums[m] is the sum of the
    elements whose bit is set in m. Built by doubling, which is both the
    fastest way to do this in Python and the reason the index is the mask.
    """
    sums = [0]
    for a in numbers:
        sums += [s + a for s in sums]
    return sums


def solve_instance(numbers, target: int):
    """Meet in the middle. Returns a subset mask, or None if there is none."""
    half = N // 2
    table = {}
    for mask, s in enumerate(_half_sums(numbers[:half])):
        if s <= target and s not in table:
            table[s] = mask
    for rmask, s in enumerate(_half_sums(numbers[half:])):
        if s > target:
            continue
        lmask = table.get(target - s)
        if lmask is None:
            continue
        full = lmask | (rmask << half)
        if full:
            return full
    return None


def solve_puzzle(header_core: bytes, j: int, start_nonce: int = 0):
    """
    Grind nonces for puzzle `j` until an instance has a solution.
    Returns (nonce, subset).
    """
    for nonce in range(start_nonce, MAX_NONCE):
        numbers, target = instance(header_core, j, nonce)
        subset = solve_instance(numbers, target)
        if subset is not None:
            return nonce, subset
    raise RuntimeError(f"no solvable instance for puzzle {j}; this should not happen")
