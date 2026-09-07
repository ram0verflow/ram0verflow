# ROFL consensus specification

Version 1.0. This document defines every rule a ROFL block must satisfy.
It is normative: `rofl/consensus.py` is the reference implementation, and
where the two disagree, this document is wrong and should be fixed.

ROFL is a Bitcoin-derived chain with two unusual properties: its canonical
ledger is a file in a GitHub repository, relayed by issue comments and pull
requests; and its proof of work is subset-sum rather than hashing, so mining
rewards a better solver rather than better silicon. Consensus is enforced by a workflow that any observer can
re-run, and by `verify.py`, which trusts nothing but `chain/blocks.jsonl`.

ROFL coins have no value, no market, and no bridge to anything. The chain
exists to be read.

---

## 1. Units

| | |
|---|---|
| Coin | ROFL |
| Base unit | laff |
| 1 ROFL | 100 000 000 laffs |

All amounts in blocks are integers denominated in laffs. Fractional laffs
do not exist.

## 2. Chain parameters

| Parameter | ROFL | Bitcoin |
|---|---|---|
| Initial subsidy | 50 ROFL | 50 BTC |
| Halving interval | 210 blocks | 210 000 blocks |
| Retarget interval | 16 blocks | 2016 blocks |
| Target block spacing | 600 s | 600 s |
| Retarget clamp | ×4 / ÷4 | ×4 / ÷4 |
| Coinbase maturity | 10 blocks | 100 blocks |
| Median time span | 11 blocks | 11 blocks |
| Max future drift | 7200 s | 7200 s |
| Proof-of-work limit | `0x1e100000` (k = 1) | `0x1d00ffff` |
| Proof-of-work ceiling | `0x1d03ffff` (k = 1024) | — |
| Genesis difficulty | `0x1e010000` (k = 15) | `0x1d00ffff` |
| Work function | subset-sum, n = 40 | double SHA-256 |
| Max puzzles per block | 1024 | — |
| Max transactions per block | 16 | ~4000 |
| Coinbase message | ≤ 80 bytes (≤ 256 at genesis) | ≤ 100 bytes |
| Transfer memo | ≤ 120 bytes | — |

One puzzle takes about 1.8 seconds in the reference solver, so the floor
(k = 1) is a couple of seconds of work and genesis (k = 15) is about half a
minute. Ten-minute spacing lands near k = 340, which retargeting will find
on its own. The floor is deliberately not at zero: difficulty falls during
quiet stretches, and without a floor a chain nobody is mining would hand out
free blocks.

## 3. Hashing

`H(x)` denotes SHA-256. All chain hashes use `H(H(x))`, written `H²`, as
Bitcoin does.

## 4. Serialisation

ROFL serialises to a canonical UTF-8 byte string rather than a binary
format. Fields are joined with `|`, which is forbidden inside any
user-supplied field, so the encoding is unambiguous.

### 4.1 Transaction core

```
v{version}|cb:{cb_height}:{hex(message)}|out:{value}:{address}|…|lt{locktime}
v{version}|in:{txid}:{vout}:{pubkey}|…|memo:{hex(memo)}|out:{value}:{address}|…|lt{locktime}
```

The first form is the coinbase, the second a spend. The memo is inside the
core, so it is committed to by both the txid and every signature: a note
cannot be altered, stripped or added in transit.

`txid = H²(core)`.

Signatures are **not** part of the core. Public keys **are**. A transaction
therefore has exactly one txid no matter how it was signed, which removes
Bitcoin's pre-segwit malleability by construction rather than by patch.

### 4.2 Signature hash

```
sighash = H²(core) = txid
```

Every input signs the same digest, committing to all inputs, all outputs,
and every public key involved. This is equivalent to Bitcoin's `SIGHASH_ALL`
with no other sighash modes available.

### 4.3 Block header

The header comes in two pieces. The **core** is what the puzzles are seeded
from, and excludes the solution, since the solution is the answer to the
puzzles the core defines:

```
core   = {height}|{prev_hash}|{merkle_root}|{timestamp}|{bits:08x}|{miner}|{algo}
header = core|{solution}
```

`block_hash = H²(header)`, so block identity commits to the answer as well
as the question.

The miner's GitHub handle is **inside the core**. Two consequences, and both
matter: every miner is working on a different set of puzzles, so a solution
posted publicly is useless to anyone else; and a solved block cannot be
re-submitted under a different handle without redoing all of the work. This
is what makes an untrusted relay channel safe.

`algo` selects the work function. Only `1` (subset-sum) is defined. The
field exists so a second work function can be added without a new header
format.

## 5. Proof of work: subset-sum

Bitcoin asks for a nonce whose header hashes below a target. ROFL asks for
subsets of numbers that sum exactly to targets.

### 5.1 One puzzle

Puzzle `j` of a block, at per-puzzle nonce `v`:

```
seed = H²(core ‖ "|" ‖ j ‖ "|" ‖ v)
a_i  = H(seed ‖ i) mod 2^b            for i = 0 … n−1
S    = Σ a_i
T    = S/2 + (H(seed ‖ "target") mod 2·⌊S/16⌋) − ⌊S/16⌋
```

with `n = 40` and `b = n − 2 = 38`. Any `a_i` that comes out zero is set to 1.

A solution is a non-empty subset of `{0 … n−1}` whose elements sum to exactly
`T`, submitted as an `n`-bit mask.

Two constants there are load-bearing.

**Why `b = n − 2`.** Density — how wide the numbers are relative to how many
there are — decides whether subset-sum is hard. Below roughly 0.94, lattice
reduction solves instances outright. Here density is `n/b ≈ 1.05`, just
inside the hard regime.

**Why `T` sits near `S/2`.** Subset sums are not spread evenly; they pile up
around half the total the way sums of coin flips pile up around the middle.
A target drawn uniformly across the range lands in the tail where almost no
subsets reach. The first implementation of this specification did exactly
that, and zero of twelve instances had any solution. Placing `T` near the
mean puts it where the subsets are: about 55% of instances are then solvable,
measured over 24 puzzles.

### 5.2 The grind

An instance is random, so it may have no solution at all. A miner who finds
none increments `v` and gets a fresh instance. That grind is what makes
mining a lottery rather than a footrace, exactly as nonce grinding is in
Bitcoin.

### 5.3 Difficulty

The best known attack is meet-in-the-middle: enumerate every subset sum of
each half and look for a pair adding to `T`. That costs about `2^(n/2)` time
**and memory**, and memory binds first — in Python, `n = 44` already wants
280 MB to buy 2.4 seconds. Difficulty therefore cannot come from growing `n`.

It comes from repetition instead, as it does in Bitcoin. A block carries `k`
solved puzzles:

```
work = 2²⁵⁶ ÷ (target + 1)          Bitcoin's chainwork formula, unchanged
k    = clamp(work ÷ 2²⁰, 1, 1024)
```

`bits` uses Bitcoin's compact nBits encoding: an exponent byte followed by a
three-byte mantissa, `target = mantissa · 256^(exponent−3)`, sign bit clear.
Because `k` is derived from the same `work` that chainwork measures,
retargeting (§6) needs no special case.

Verification is `k` puzzle derivations and `k · n` additions: 0.06 s at the
maximum difficulty.

## 6. Difficulty adjustment

At every height that is a multiple of 16 and greater than zero:

```
actual   = timestamp[h−1] − timestamp[h−17]
actual   = clamp(actual, TIMESPAN/4, TIMESPAN·4)
target'  = target · actual / TIMESPAN
target'  = clamp(target', POW_CEILING, POW_LIMIT)
```

where `TIMESPAN = 16 · 600` seconds (2 h 40 m). At all other heights, `bits` must
equal the previous block's `bits`.

**Deviation.** Bitcoin reads the first block of the *previous* window rather
than the block immediately preceding the window being closed — an off-by-one present
since 2009 that makes each retarget cover 2015 intervals instead of 2016. ROFL uses
the full 16-interval window (`timestamp[h−1] − timestamp[h−17]`). Additionally, ROFL
enforces a maximum difficulty ceiling `POW_CEILING = 0x1d03ffff` matching `k = 1024`,
preventing difficulty from running away into unbounded territory when blocks are mined
faster than the puzzle ceiling.

## 7. Subsidy

```
subsidy(height) = 50 ROFL >> (height // 210)
```

Zero after 64 halvings. The coinbase output total must equal
`subsidy(height) + sum(fees)` **exactly**.

**Deviation.** Bitcoin permits a miner to claim *less* than the full reward,
which has permanently destroyed a small amount of BTC. ROFL requires the
exact amount, so total supply is a pure function of height and `verify.py`
can assert that emitted supply equals unspent supply.

## 8. Transaction validity

A non-coinbase transaction is valid if and only if:

1. `version` is 1.
2. It has 1–8 outputs and at most 8 inputs.
3. Every output value is a positive integer within range.
4. Every output address passes bech32 validation with HRP `rofl`.
5. No outpoint appears twice among its inputs.
6. Every input references an outpoint that is currently unspent.
7. Any coinbase output it spends is at least 10 blocks old.
8. For each input, `address(pubkey) == address` of the output being spent.
9. For each input, the signature verifies against that public key over the
   sighash, with `s ≤ n/2` (low-s; high-s signatures are rejected outright).
10. The sum of outputs does not exceed the sum of inputs.
11. The memo is ≤ 120 bytes and contains no control characters and no `|`.

The difference between inputs and outputs is the fee, claimable by the miner.

## 9. Block validity

1. Miner handle is 1–39 characters of `[A-Za-z0-9-]`.
2. Height is exactly one greater than the tip; `prev_hash` equals the tip's
   hash. Genesis is height 0 with a null `prev_hash`.
3. `bits` equals the value required by §6.
4. Timestamp is at most 7200 s in the future, and strictly greater than the
   median of the previous 11 block timestamps.
5. The block has 1–16 transactions; the first is the coinbase and no other
   transaction is.
6. No two transactions share a txid.
7. The merkle root commits to the transaction list (§10).
8. `algo` is 1, the solution decodes to exactly `k` entries for the block's
   difficulty, and every one of them solves its puzzle (§5).
9. The coinbase message contains no control characters and no `|`, and is
   ≤ 80 bytes — except at height 0, where the cap is 256 bytes. Bitcoin
   special-cases its genesis block too: it is hardcoded rather than validated,
   and its coinbase output is unspendable.
10. **BIP 34.** The coinbase commits to its own block height in `cb_height`,
    which must equal the block height. Without this, two blocks with the same
    miner, reward and message would produce the same coinbase txid.
11. **BIP 30.** No transaction in the block may share a txid with an existing
    unspent transaction.
12. Every non-coinbase transaction satisfies §8, evaluated in order against a
    UTXO set that already reflects earlier transactions in the same block.
13. The coinbase pays exactly `subsidy + fees`.

Rules 10 and 11 exist for the same reason they exist in Bitcoin: duplicate
coinbase txids silently overwrite earlier unspent outputs and destroy coins.
This was found by the ROFL test suite before launch, in exactly the form
Bitcoin hit it in 2012 (CVE-2012-1909).

## 10. Merkle root

Bitcoin's construction, including the rule that an odd node at any level is
duplicated to pair with itself. An empty transaction list is not permitted.

The duplication rule alone would allow two different transaction lists to
produce the same root (CVE-2012-2459). Rule 9.6 — no duplicate txids in a
block — closes this.

## 11. Addresses

```
address = bech32(hrp="rofl", version=0, payload=H(pubkey)[:20])
```

Public keys are 33-byte SEC1 compressed secp256k1 points — the same curve
and encoding Bitcoin uses.

**Deviation.** Bitcoin's payload is `RIPEMD160(SHA256(pubkey))`. RIPEMD160
is absent from many modern OpenSSL builds, and ROFL commits to depending on
nothing outside the Python standard library, so the payload is the first 20
bytes of a single SHA-256 instead. The encoding and checksum are BIP 173
unchanged.

## 12. Signatures

ECDSA over secp256k1 with RFC 6979 deterministic nonces. Signatures are
64-byte compact `r ‖ s`, both big-endian, with `s` normalised to the lower
half of the curve order per BIP 62. High-s signatures are invalid, not
merely non-standard.

Deterministic nonces mean signing the same transaction twice produces
identical bytes, so a transaction has one canonical encoding end to end.

## 13. Chain selection

The chain is the sequence of blocks in `chain/blocks.jsonl`. A submitted
block must extend the current tip; blocks building on any earlier block are
rejected as stale. Cumulative chainwork is tracked and reported but is not
used to reorganise.

**Deviation.** Bitcoin follows the most-work chain and reorganises when a
heavier one appears. ROFL has a single serialised writer — one workflow, one
concurrency group — so competing chains cannot form. Two miners who solve
the same height race on submission time, and the loser is told the new tip
and can mine again. This is a real limitation and is the honest cost of
using a git repository as the network.

## 14. Relay

Blocks and transactions are relayed as base64 payloads prefixed
`rofl-block-v1:` and `rofl-tx-v1:`. Both are accepted as issue comments;
transactions are additionally accepted as pull requests that add a single
file under `chain/pending/`, so contributors get the pull request on their
profile.

The submitting account's login is taken from the event payload, never from
the submission itself.

A pull request is **never merged**. The node reads the submitted file through
the API as inert text, validates it, and applies the transaction to `main`
itself. This has two consequences: concurrent submissions cannot conflict,
and no code from a fork is ever executed by a workflow holding a write token
— the standard `pull_request_target` failure mode.

A block is mined by whoever submits it, and the miner's handle is fixed inside
the header (§4.3), so the relay channel never has to be trusted.

## 15. Identity (not consensus)

A GitHub handle may be bound to an address by posting, from that account:

```
rofl-id-v1:{handle}:{pubkey}:{sig}
```

where `sig` is over `H²("rofl-identity-v1|" ‖ handle)`. The signature proves
control of the key; posting from the account proves control of the handle.
Neither alone is sufficient.

Bindings live in `chain/registry.json` and decide only whose name appears
beside a balance in the rendered ledger. They are **not** part of consensus,
carry no authority over funds, and `verify.py` ignores the file entirely.
Transactions are authorised by signatures and nothing else.

## 16. Threat notes

- **Impersonation** is prevented by the miner handle living inside the header
  core (§4.3), which every puzzle is seeded from.
- **Solution theft** — copying a solved block out of a public comment thread —
  is prevented the same way: a different handle means different puzzles.
- **Theft** is prevented by §8.8–8.9; there is no scripting language and no
  path to spending an output without its private key.
- **Inflation** is prevented by §9.13 and checked globally by `verify.py`,
  which asserts emitted supply equals unspent supply on every run.
- **Spam** is rate-limited by proof of work. Invalid submissions are rejected
  in milliseconds and cost the chain nothing.
- **Untrusted fork code** is never executed: pull requests are read through
  the API and applied by the base repository, never checked out (§14).
- **A malicious repository owner** can rewrite `chain/blocks.jsonl` at will.
  They cannot forge proof of work or signatures, so any rewrite is detectable
  by anyone holding an earlier copy — but it is not preventable. A chain
  whose ledger is one person's repository is trusting that person's restraint,
  and pretending otherwise would be dishonest.

## 17. Genesis

Genesis is height 0, `prev_hash` all zeroes, mined at `0x1e010000`, with a
coinbase message of up to 256 bytes. The message is fixed at creation and is
part of the chain's identity; changing it invalidates every block after it.

ROFL's genesis message is:

> in bitcoin, we have discovered not just sound money, but the technological
> foundation of human liberty. a tool that makes freedom not just possible but
> practical, not just desirable but inevitable.
