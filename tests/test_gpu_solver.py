#!/usr/bin/env python3
"""
Correctness tests for the CUDA subset-sum miner.

Skipped automatically when gpu/rofl_gpu.dll (or .so) has not been built.

    python3 tests/test_gpu_solver.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rofl import gpu_solver  # noqa: E402
from rofl import pow as powfn  # noqa: E402


def ok(label):
    print(f"  ok    {label}")


def main():
    print("ROFL GPU solver tests")
    print()
    if gpu_solver.find_library() is None:
        print("  skip  CUDA library not built (run gpu/build.ps1)")
        return 0

    gpu = gpu_solver.GpuSolver()
    print(f"  device {gpu.device_name}")
    print(f"  batch  {gpu.batch_size} instances   VRAM {gpu.vram_used / (1 << 20):.0f} MiB")
    print()

    core = b"1|00" + b"ab" * 32 + b"|cd" * 32 + b"|1700000000|1e010000|gpuminer|1"
    # The above is just unique bytes; instance() only needs a stable header_core.

    # ---- instance derivation matches pow.py --------------------------------
    for j, nonce in ((0, 0), (0, 1), (3, 17), (7, 255), (12, 1000), (0, 65535)):
        py_nums, py_t = powfn.instance(core, j, nonce)
        gpu_nums, gpu_t = gpu.make_instance(core, j, nonce)
        assert gpu_nums == py_nums, (j, nonce, gpu_nums[:4], py_nums[:4])
        assert gpu_t == py_t, (j, nonce, gpu_t, py_t)
    ok("instance() matches pow.py for six (j, nonce) pairs")

    # ---- MITM agrees with the reference on solvability ---------------------
    numbers, targets, cpu_masks = [], [], []
    samples = 48
    for nonce in range(samples):
        nums, tgt = powfn.instance(core, 0, nonce)
        numbers.append(nums)
        targets.append(tgt)
        cpu_masks.append(powfn.solve_instance(nums, tgt))

    t0 = time.perf_counter()
    gpu_masks = gpu.solve_instances(numbers, targets)
    gpu_ms = (time.perf_counter() - t0) * 1000

    for i in range(samples):
        cpu, got = cpu_masks[i], gpu_masks[i]
        if cpu is None:
            assert got is None, f"GPU solved nonce {i} but CPU did not: {got}"
        else:
            assert got is not None, f"GPU missed a solution CPU found at nonce {i}"
            assert powfn.check_one(core, 0, i, got), f"GPU mask {got} failed verify at nonce {i}"
            # CPU mask must also verify (sanity on the reference).
            assert powfn.check_one(core, 0, i, cpu)
    solvable = sum(1 for m in gpu_masks if m is not None)
    ok(f"MITM matches CPU solvability on {samples} instances "
       f"({solvable} solvable, GPU {gpu_ms:.1f} ms)")

    # ---- full puzzle grind -------------------------------------------------
    solutions, tried = gpu.solve_puzzles(core, 4)
    assert len(solutions) == 4
    for j, (nonce, subset) in enumerate(solutions):
        assert powfn.check_one(core, j, nonce, subset), (j, nonce, subset)
        cpu_nonce, cpu_subset = powfn.solve_puzzle(core, j)
        assert nonce == cpu_nonce, (j, nonce, cpu_nonce)
        assert powfn.check_one(core, j, cpu_nonce, cpu_subset)
    ok(f"solve_puzzles(k=4) matches first CPU nonce ({tried} instances)")

    gpu.close()
    print()
    print("all GPU tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
