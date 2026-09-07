"""
Optional CUDA backend for the ROFL subset-sum miner.

The reference solver in pow.py stays the source of truth for verification.
This module loads gpu/rofl_gpu.dll (Windows) or gpu/librofl_gpu.so (Linux)
if it has been built, and exposes the same (nonce, subset) answers.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Optional


PROGRESS_FN = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def library_candidates():
    env = os.environ.get("ROFL_GPU_LIB")
    if env:
        yield Path(env)
    root = _project_root()
    names = (
        ("win32", "rofl_gpu.dll"),
        ("cygwin", "rofl_gpu.dll"),
        ("darwin", "librofl_gpu.dylib"),
    )
    plat = sys.platform
    if plat in ("win32", "cygwin"):
        yield root / "gpu" / "rofl_gpu.dll"
        yield root / "rofl_gpu.dll"
    elif plat == "darwin":
        yield root / "gpu" / "librofl_gpu.dylib"
        yield root / "librofl_gpu.dylib"
    else:
        yield root / "gpu" / "librofl_gpu.so"
        yield root / "librofl_gpu.so"
    for _, name in names:
        yield root / "gpu" / name


def find_library() -> Optional[Path]:
    seen = set()
    for path in library_candidates():
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    return None


def load_library(path: Optional[Path] = None):
    path = path or find_library()
    if path is None:
        raise FileNotFoundError(
            "CUDA miner library not found. Build it with:  powershell -File gpu/build.ps1"
        )
    lib = ctypes.CDLL(str(path))
    lib.rofl_gpu_last_error.restype = ctypes.c_char_p
    lib.rofl_gpu_create.restype = ctypes.c_void_p
    lib.rofl_gpu_create.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.rofl_gpu_destroy.argtypes = [ctypes.c_void_p]
    lib.rofl_gpu_device_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.rofl_gpu_batch_size.restype = ctypes.c_int
    lib.rofl_gpu_batch_size.argtypes = [ctypes.c_void_p]
    lib.rofl_gpu_vram_used.restype = ctypes.c_ulonglong
    lib.rofl_gpu_vram_used.argtypes = [ctypes.c_void_p]
    lib.rofl_gpu_vram_total.restype = ctypes.c_ulonglong
    lib.rofl_gpu_vram_total.argtypes = [ctypes.c_void_p]
    lib.rofl_gpu_make_instance.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    lib.rofl_gpu_solve_instances.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    lib.rofl_gpu_solve_puzzles.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_int),
        PROGRESS_FN,
        ctypes.c_void_p,
    ]
    return lib, path


def _err(lib) -> str:
    msg = lib.rofl_gpu_last_error()
    return msg.decode() if msg else "unknown GPU error"


class GpuSolver:
    def __init__(self, device: int = 0, batch_size: int = 0, lib_path: Optional[Path] = None):
        self._lib, self.lib_path = load_library(lib_path)
        self._ctx = self._lib.rofl_gpu_create(int(device), int(batch_size))
        if not self._ctx:
            raise RuntimeError(_err(self._lib))
        self._progress_cb = None

        name_buf = ctypes.create_string_buffer(96)
        self._lib.rofl_gpu_device_name(self._ctx, name_buf, 96)
        self.device_name = name_buf.value.decode(errors="replace") or f"CUDA device {device}"
        self.batch_size = int(self._lib.rofl_gpu_batch_size(self._ctx))
        self.vram_used = int(self._lib.rofl_gpu_vram_used(self._ctx))
        self.vram_total = int(self._lib.rofl_gpu_vram_total(self._ctx))
        self.device = device

    def close(self):
        if getattr(self, "_ctx", None):
            self._lib.rofl_gpu_destroy(self._ctx)
            self._ctx = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - destructor must not raise
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def make_instance(self, header_core: bytes, j: int, nonce: int):
        nums = (ctypes.c_uint64 * 40)()
        target = ctypes.c_uint64()
        core = ctypes.create_string_buffer(header_core, len(header_core))
        rc = self._lib.rofl_gpu_make_instance(
            core, len(header_core), int(j), int(nonce), nums, ctypes.byref(target)
        )
        if rc != 0:
            raise RuntimeError(_err(self._lib))
        return [int(nums[i]) for i in range(40)], int(target.value)

    def solve_instances(self, numbers, targets):
        n = len(numbers)
        if n == 0:
            return []
        if n != len(targets):
            raise ValueError("numbers and targets length mismatch")
        flat = (ctypes.c_uint64 * (n * 40))()
        tarr = (ctypes.c_uint64 * n)()
        for i, row in enumerate(numbers):
            if len(row) != 40:
                raise ValueError("each instance must have 40 numbers")
            for t, v in enumerate(row):
                flat[i * 40 + t] = int(v)
            tarr[i] = int(targets[i])
        out = (ctypes.c_uint64 * n)()
        rc = self._lib.rofl_gpu_solve_instances(self._ctx, flat, tarr, n, out)
        if rc < 0:
            raise RuntimeError(_err(self._lib))
        return [int(out[i]) or None for i in range(n)]

    def solve_puzzles(self, header_core: bytes, k: int, progress=None):
        if k <= 0:
            return [], 0
        nonces = (ctypes.c_int * k)()
        subsets = (ctypes.c_uint64 * k)()
        tried = ctypes.c_int(0)

        if progress is not None:

            def _wrap(solved, kk, ntried, _user):
                progress(int(solved), int(kk), int(ntried))

            cb = PROGRESS_FN(_wrap)
            self._progress_cb = cb  # keep alive across the C call
        else:
            self._progress_cb = None
            cb = ctypes.cast(0, PROGRESS_FN)

        core_buf = ctypes.create_string_buffer(header_core, len(header_core))
        rc = self._lib.rofl_gpu_solve_puzzles(
            self._ctx,
            ctypes.cast(core_buf, ctypes.c_void_p),
            len(header_core),
            int(k),
            nonces,
            subsets,
            ctypes.byref(tried),
            cb,
            None,
        )
        if rc != 0:
            raise RuntimeError(_err(self._lib))
        solutions = [(int(nonces[j]), int(subsets[j])) for j in range(k)]
        return solutions, int(tried.value)


def try_create(device: int = 0, batch_size: int = 0) -> Optional[GpuSolver]:
    if find_library() is None:
        return None
    try:
        return GpuSolver(device=device, batch_size=batch_size)
    except (OSError, RuntimeError, FileNotFoundError):
        return None
