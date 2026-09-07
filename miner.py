#!/usr/bin/env python3
"""
ROFL miner. The CPU solver is standard library only -- no pip install.

    python3 miner.py --miner YOUR_GITHUB_HANDLE --message "gm"

An optional CUDA backend lives in gpu/. On an RTX 3070 Ti (or any NVIDIA
card with a recent toolkit) build it once:

    powershell -File gpu/build.ps1          # Windows
    bash gpu/build.sh                       # Linux

then mine with --backend gpu (the default is auto: GPU if the library is
present, otherwise the reference CPU solver). Compare them with:

    python3 miner.py --benchmark

Mining happens entirely on your machine. The miner solves every puzzle the
current tip requires, prints a submission line, then keeps going: it waits
for that height to land on the chain and mines the next block. Ctrl+C stops
it. `--once` restores the old one-block-and-exit behaviour.

Paste each `rofl-block-v1:` line as a comment on the block issue; a GitHub
Action validates it and appends it to the chain.

    python3 miner.py --help    for all options
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from rofl import chain as chainmod
from rofl import crypto
from rofl import gpu_solver
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

_GITHUB_TOKEN = None


def get_github_token() -> str:
    global _GITHUB_TOKEN
    if _GITHUB_TOKEN is not None:
        return _GITHUB_TOKEN
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        _GITHUB_TOKEN = token.strip()
        return _GITHUB_TOKEN
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            _GITHUB_TOKEN = proc.stdout.strip()
            return _GITHUB_TOKEN
    except Exception:
        pass
    _GITHUB_TOKEN = ""
    return _GITHUB_TOKEN


def fetch_remote(repo: str, path: str, bust_cache: bool = False) -> str:
    # 1. If bust_cache is requested, query GitHub API directly to bypass Fastly CDN edge cache.
    if bust_cache:
        token = get_github_token()
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "User-Agent": "ROFL-Miner",
            "Accept": "application/vnd.github.v3.raw",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise SystemExit(f"could not fetch {api_url}: 404 Not Found")
            # For 403 rate-limit or other API errors, fall through to raw URL
        except Exception:
            pass

    # 2. Fallback: raw.githubusercontent.com
    url = RAW_BASE.format(repo=repo) + path
    if bust_cache:
        url += f"?t={int(time.time() * 1000)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ROFL-Miner", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except Exception as exc:  # noqa: BLE001 - network errors vary widely
        raise SystemExit(f"could not fetch {url}: {exc}")


def submit_block(repo: str, issue: int, line: str) -> bool:
    """Submit block via gh issue comment if gh CLI is available."""
    try:
        subprocess.run(
            ["gh", "issue", "comment", str(issue), "--repo", repo, "--body", line],
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except Exception as exc:
        sys.stderr.write(f"\n  could not submit via gh: {exc}\n")
        return False


def load_chain(args, bust_cache: bool = False):
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
        raw = fetch_remote(args.repo, "chain/blocks.jsonl", bust_cache=bust_cache)
        blocks = [
            chainmod.Block.from_dict(json.loads(line))
            for line in raw.splitlines()
            if line.strip()
        ]
        try:
            mempool_raw = fetch_remote(args.repo, "chain/mempool.jsonl", bust_cache=bust_cache)
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


def _progress_line(done, k, tried, started):
    rate = (time.time() - started) / done if done else 0.0
    inst = tried / done if done else 0.0
    sys.stderr.write(
        f"\r  puzzle {done}/{k}   {rate:.2f}s each   "
        f"{inst:.2f} instances per solve   "
        f"eta {(k - done) * rate:6.0f}s   "
    )
    sys.stderr.flush()


def mine_cpu(block: Block, k: int):
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
        _progress_line(j + 1, k, tried, started)
    return solutions, tried, time.time() - started


def mine_gpu(block: Block, k: int, gpu: gpu_solver.GpuSolver):
    """Same contract as mine_cpu, using the CUDA meet-in-the-middle solver."""
    core = block.header_core()
    started = time.time()

    def on_progress(done, kk, tried):
        _progress_line(done, kk, tried, started)

    solutions, tried = gpu.solve_puzzles(core, k, progress=on_progress)
    for j, (nonce, subset) in enumerate(solutions):
        if not powfn.check_one(core, j, nonce, subset):
            sys.stderr.write(
                f"\n  GPU solution for puzzle {j} failed verify; "
                "re-solving that puzzle on CPU\n"
            )
            nonce, subset = powfn.solve_puzzle(core, j)
            solutions[j] = (nonce, subset)
    return solutions, tried, time.time() - started


def mine(block: Block, k: int, gpu=None):
    if gpu is not None:
        return mine_gpu(block, k, gpu)
    return mine_cpu(block, k)


def _fmt_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f" {n / (1 << 30):.2f} GB"
    return f" {n / (1 << 20):.0f} MiB"


def open_gpu(args):
    """Load the CUDA solver or explain why not. Returns GpuSolver | None."""
    want = args.backend
    lib = gpu_solver.find_library()
    if want == "cpu":
        print("  solver     CPU  (reference meet-in-the-middle)")
        return None
    if lib is None:
        msg = (
            "CUDA miner library not found. Build it with:\n"
            "  powershell -File gpu/build.ps1"
        )
        if want == "gpu":
            raise SystemExit(msg)
        print(f"  solver     CPU  (GPU library missing — {msg.splitlines()[1].strip()})")
        return None
    try:
        gpu = gpu_solver.GpuSolver(device=args.device, batch_size=args.batch_size)
    except (OSError, RuntimeError) as exc:
        if want == "gpu":
            raise SystemExit(f"CUDA miner failed to start: {exc}") from exc
        print(f"  solver     CPU  (GPU init failed: {exc})")
        return None
    print(
        f"  solver     CUDA  {gpu.device_name}  batch={gpu.batch_size}  "
        f"VRAM {_fmt_bytes(gpu.vram_used).strip()}/{_fmt_bytes(gpu.vram_total).strip()}"
    )
    return gpu


def run_benchmark(args):
    """Time the reference CPU solver against the CUDA miner on identical instances."""
    gpu = open_gpu(args)
    if gpu is None:
        raise SystemExit("benchmark needs the CUDA library (powershell -File gpu/build.ps1)")

    samples = max(8, args.samples)
    core = b"bench|deadbeef" * 8
    print()
    print(f"ROFL GPU benchmark  ·  {samples} instances  ·  n={powfn.N}")
    print(f"  device     {gpu.device_name}")
    print(f"  batch      {gpu.batch_size} instances in flight")
    print(f"  VRAM       {_fmt_bytes(gpu.vram_used).strip()} used / "
          f"{_fmt_bytes(gpu.vram_total).strip()} total")
    print()

    numbers, targets = [], []
    for nonce in range(samples):
        nums, tgt = powfn.instance(core, 0, nonce)
        numbers.append(nums)
        targets.append(tgt)

    t0 = time.perf_counter()
    cpu_masks = [powfn.solve_instance(nums, tgt) for nums, tgt in zip(numbers, targets)]
    cpu_s = time.perf_counter() - t0

    # Warm the GPU (context, kernels, CUB) so the timed run is steady-state.
    gpu.solve_instances(numbers[: min(8, samples)], targets[: min(8, samples)])

    t0 = time.perf_counter()
    gpu_masks = gpu.solve_instances(numbers, targets)
    gpu_s = time.perf_counter() - t0

    mismatches = 0
    for i, (cpu, got) in enumerate(zip(cpu_masks, gpu_masks)):
        cpu_ok = cpu is not None
        gpu_ok = got is not None
        if cpu_ok != gpu_ok:
            mismatches += 1
            continue
        if got is not None and not powfn.check_one(core, 0, i, got):
            mismatches += 1
    if mismatches:
        raise SystemExit(f"GPU/CPU disagree on {mismatches}/{samples} instances")

    solvable = sum(1 for m in gpu_masks if m is not None)
    speedup = cpu_s / gpu_s if gpu_s > 0 else float("inf")
    print(f"  CPU        {cpu_s:.3f}s   {samples / cpu_s:.2f} inst/s   "
          f"{cpu_s / samples * 1000:.1f} ms/inst")
    print(f"  GPU        {gpu_s:.3f}s   {samples / gpu_s:.2f} inst/s   "
          f"{gpu_s / samples * 1000:.2f} ms/inst")
    print(f"  speedup    {speedup:,.1f}x")
    print(f"  solvable   {solvable}/{samples}  ({100 * solvable / samples:.0f}%)")
    print()

    k = 8
    t0 = time.perf_counter()
    solutions, tried = gpu.solve_puzzles(core, k)
    grind_s = time.perf_counter() - t0
    for j, (nonce, subset) in enumerate(solutions):
        if not powfn.check_one(core, j, nonce, subset):
            raise SystemExit(f"GPU puzzle {j} failed verify")
    print(f"  grind      {k} puzzles in {grind_s:.3f}s  "
          f"({tried} instances, {tried / k:.2f} per puzzle)")
    # ~55% of random instances are solvable, so a puzzle costs ~1.8 instances.
    inst_s = samples / gpu_s if gpu_s else 0.0
    expected = 1.8
    print(f"  estimate   k=15 block  ~{15 * expected / inst_s:.2f}s    "
          f"k=340 block  ~{340 * expected / inst_s:.2f}s   "
          f"(from {inst_s:.0f} inst/s × {expected} inst/puzzle)")
    gpu.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Mine a ROFL block.")
    ap.add_argument("--miner", default=None, help="your GitHub handle (goes in the header)")
    ap.add_argument("--message", default="", help=f"coinbase message, max 80 bytes")
    ap.add_argument("--address", default=None, help="pay the reward here (default: wallet file)")
    ap.add_argument("--wallet", default="rofl-wallet.json", help="wallet file for the address")
    ap.add_argument("--repo", default="ram0verflow/ram0verflow", help="repo to mine against")
    ap.add_argument("--local", action="store_true", help="use the local chain/ directory")
    ap.add_argument("--no-txs", action="store_true", help="mine an empty block, ignore mempool")
    ap.add_argument(
        "--backend",
        choices=("auto", "gpu", "cpu"),
        default="auto",
        help="solver: CUDA if available (auto), force GPU, or the reference CPU solver",
    )
    ap.add_argument("--device", type=int, default=0, help="CUDA device index")
    ap.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="GPU instances in flight (0 = auto from free VRAM)",
    )
    ap.add_argument(
        "--benchmark",
        action="store_true",
        help="time GPU vs the CPU reference solver and exit",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=32,
        help="instances to time in --benchmark (default 32)",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="mine a single block and exit (default: keep mining)",
    )
    ap.add_argument(
        "--poll",
        type=float,
        default=8.0,
        help="seconds between chain checks while waiting for a block to land",
    )
    ap.add_argument(
        "--submit",
        action="store_true",
        help="automatically post mined block to GitHub issue using gh CLI",
    )
    ap.add_argument(
        "--issue",
        type=int,
        default=1,
        help="GitHub issue number to submit blocks to (default 1)",
    )
    args = ap.parse_args()

    if args.benchmark:
        return run_benchmark(args)

    if not args.miner:
        ap.error("--miner is required unless --benchmark")

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

    gpu = open_gpu(args)
    try:
        while True:
            blocks, mempool = load_chain(args, bust_cache=True)
            state = chainmod.replay(blocks, strict_time=False)
            height = state.height + 1
            bits = state.next_bits()
            k = powfn.k_for_work(target_to_work(bits_to_target(bits)))

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

            print()
            print(f"ROFL miner  ·  building on {state.tip_hash[:16]}…  ·  height {height}")
            print(f"  difficulty {difficulty(bits):,.1f}   bits {bits:#010x}")
            print(f"  puzzles    {k} x subset-sum(n={powfn.N})")
            print(f"  reward     {format_amount(reward)} ROFL "
                  f"(subsidy {format_amount(block_subsidy(height))} + fees {format_amount(fees)})")
            print(f"  txs        {len(txs)} from mempool")
            print()
            sys.stdout.flush()

            solutions, tried, elapsed = mine(block, k, gpu=gpu)
            sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.flush()
            block.solution = powfn.encode_solutions(solutions)

            payload = base64.b64encode(
                json.dumps(block.to_dict(), separators=(",", ":"), sort_keys=True).encode()
            ).decode()
            line = PREFIX + payload

            print(f"  solved {k} puzzle(s) in {elapsed:.1f}s "
                  f"({tried} instances, {100*k/tried:.0f}% solvable)")
            print(f"  hash   {block.block_hash()}")
            print()

            if args.submit:
                print(f"Submitting block {height} to {args.repo} issue #{args.issue} via gh...")
                if submit_block(args.repo, args.issue, line):
                    print(f"  ✓ submitted block {height} successfully!")
                else:
                    print("  ! gh submission failed. Please paste the line below manually:")
                    print()
                    print(line)
            else:
                print("Paste this as a comment on the block submission issue:")
                print()
                print(line)

            if args.once:
                break

            print()
            print(f"Waiting for height {height} to land on chain (poll interval {args.poll:.1f}s)...")
            poll_started = time.time()
            while True:
                time.sleep(args.poll)
                try:
                    new_blocks, new_mempool = load_chain(args, bust_cache=True)
                except (SystemExit, Exception):
                    continue

                new_state = chainmod.replay(new_blocks, strict_time=False)
                if new_state.height >= height:
                    sys.stderr.write("\r" + " " * 78 + "\r")
                    sys.stderr.flush()
                    if new_state.tip_hash == block.block_hash():
                        print(f"✓ Block {height} landed on chain! (tip {new_state.tip_hash[:16]}…)")
                    else:
                        print(f"✓ Chain advanced to height {new_state.height} (tip {new_state.tip_hash[:16]}…)")
                    break
                else:
                    waited = int(time.time() - poll_started)
                    sys.stderr.write(
                        f"\r  waiting for height {height} to land (waited {waited}s, chain tip {new_state.height})... "
                    )
                    sys.stderr.flush()
    except KeyboardInterrupt:
        print("\n\nMining stopped.")
        return 0
    finally:
        if gpu is not None:
            gpu.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
