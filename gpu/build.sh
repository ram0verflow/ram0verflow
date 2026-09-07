#!/usr/bin/env bash
# Build the ROFL CUDA subset-sum miner (Linux).
#   bash gpu/build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/librofl_gpu.so"
if ! command -v nvcc >/dev/null 2>&1; then
  echo "nvcc not found. Install the CUDA Toolkit." >&2
  exit 1
fi
nvcc -O3 -std=c++17 --shared -Xcompiler -fPIC -cudart static \
  -gencode=arch=compute_75,code=sm_75 \
  -gencode=arch=compute_80,code=sm_80 \
  -gencode=arch=compute_86,code=sm_86 \
  -gencode=arch=compute_86,code=compute_86 \
  -gencode=arch=compute_89,code=sm_89 \
  -o "$OUT" "$HERE/subset_sum.cu"
echo "built $OUT"
