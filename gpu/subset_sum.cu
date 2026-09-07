/*
 * ROFL GPU subset-sum miner.
 *
 * Meet-in-the-middle on n=40 (two halves of 20): generate 2^20 left subset
 * sums, segmented radix-sort them, then probe every right-half subset in
 * parallel with a binary search. Instances are packed into a VRAM-sized
 * batch so an RTX 3070 Ti stays busy.
 *
 * Instance derivation matches rofl/pow.py exactly (double SHA-256 seed,
 * 38-bit numbers, target near S/2). Verification stays on the Python
 * reference path.
 */

#include "subset_sum.h"

#include <cuda_runtime.h>
#include <cub/cub.cuh>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr int N = 40;
constexpr int HALF_N = 20;
constexpr int HALF = 1 << HALF_N;
constexpr int B_BITS = 38;
constexpr int BLOCK = 256;
constexpr int MAX_NONCE = 1 << 16;
constexpr int K_MAX = 1024;
constexpr int SUM_BITS = 43; /* 20 * (2^38-1) < 2^43 */
constexpr int MIN_BATCH = 8;
constexpr int MAX_BATCH = 256;
constexpr int MAX_SPECULATIVE = 8; /* extra nonces per puzzle when the batch would be tiny */

static std::string g_error;

static bool ck(cudaError_t e, const char *what) {
    if (e == cudaSuccess) {
        return true;
    }
    g_error = std::string(what) + ": " + cudaGetErrorString(e);
    return false;
}

/* -------------------------------------------------------------------------- */
/* SHA-256 (host) — FIPS 180-4, matches hashlib.sha256                          */
/* -------------------------------------------------------------------------- */

static const uint32_t SHA_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static inline uint32_t rotr32(uint32_t x, int n) {
    return (x >> n) | (x << (32 - n));
}

static void sha256_transform(uint32_t s[8], const uint8_t block[64]) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)block[i * 4] << 24) | ((uint32_t)block[i * 4 + 1] << 16) |
               ((uint32_t)block[i * 4 + 2] << 8) | (uint32_t)block[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = s[0], b = s[1], c = s[2], d = s[3];
    uint32_t e = s[4], f = s[5], g = s[6], h = s[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = h + S1 + ch + SHA_K[i] + w[i];
        uint32_t S0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    s[0] += a;
    s[1] += b;
    s[2] += c;
    s[3] += d;
    s[4] += e;
    s[5] += f;
    s[6] += g;
    s[7] += h;
}

static void sha256(const uint8_t *data, size_t len, uint8_t out[32]) {
    uint32_t s[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                     0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    size_t i = 0;
    for (; i + 64 <= len; i += 64) {
        sha256_transform(s, data + i);
    }
    uint8_t last[128];
    memset(last, 0, sizeof last);
    size_t rem = len - i;
    memcpy(last, data + i, rem);
    last[rem] = 0x80;
    size_t bits = len * 8;
    int padlen = (rem + 1 + 8 <= 64) ? 64 : 128;
    for (int b = 0; b < 8; b++) {
        last[padlen - 1 - b] = (uint8_t)(bits >> (8 * b));
    }
    sha256_transform(s, last);
    if (padlen == 128) {
        sha256_transform(s, last + 64);
    }
    for (int j = 0; j < 8; j++) {
        out[j * 4] = (uint8_t)(s[j] >> 24);
        out[j * 4 + 1] = (uint8_t)(s[j] >> 16);
        out[j * 4 + 2] = (uint8_t)(s[j] >> 8);
        out[j * 4 + 3] = (uint8_t)s[j];
    }
}

static int write_dec(uint8_t *p, int x) {
    if (x == 0) {
        p[0] = '0';
        return 1;
    }
    uint8_t tmp[16];
    int n = 0;
    unsigned v = (unsigned)x;
    while (v) {
        tmp[n++] = (uint8_t)('0' + (v % 10));
        v /= 10;
    }
    for (int i = 0; i < n; i++) {
        p[i] = tmp[n - 1 - i];
    }
    return n;
}

/* digest is a 256-bit big-endian integer. m fits in ~41 bits, so r stays in uint64. */
static uint64_t be256_mod_u64(const uint8_t d[32], uint64_t m) {
    uint64_t r = 0;
    for (int i = 0; i < 32; i++) {
        r = ((r << 8) + d[i]) % m;
    }
    return r;
}

static void make_instance(const uint8_t *core, int core_len, int j, int nonce,
                          uint64_t numbers[N], uint64_t *target) {
    uint8_t buf[512];
    if (core_len < 0 || core_len > 400) {
        memset(numbers, 0, N * sizeof(uint64_t));
        *target = 0;
        return;
    }
    memcpy(buf, core, (size_t)core_len);
    int n = core_len;
    buf[n++] = '|';
    n += write_dec(buf + n, j);
    buf[n++] = '|';
    n += write_dec(buf + n, nonce);

    uint8_t inner[32], seed[32];
    sha256(buf, (size_t)n, inner);
    sha256(inner, 32, seed);

    const uint64_t mask = (1ULL << B_BITS) - 1;
    uint64_t total = 0;
    for (int i = 0; i < N; i++) {
        uint8_t msg[36];
        memcpy(msg, seed, 32);
        msg[32] = (uint8_t)((unsigned)i >> 24);
        msg[33] = (uint8_t)((unsigned)i >> 16);
        msg[34] = (uint8_t)((unsigned)i >> 8);
        msg[35] = (uint8_t)i;
        uint8_t dig[32];
        sha256(msg, 36, dig);
        uint64_t v = 0;
        for (int b = 24; b < 32; b++) {
            v = (v << 8) | dig[b];
        }
        v &= mask;
        if (v == 0) {
            v = 1;
        }
        numbers[i] = v;
        total += v;
    }

    uint8_t tmsg[38];
    memcpy(tmsg, seed, 32);
    memcpy(tmsg + 32, "target", 6);
    uint8_t tdig[32];
    sha256(tmsg, 38, tdig);

    uint64_t spread = total / 16;
    if (spread == 0) {
        spread = 1;
    }
    uint64_t offset = be256_mod_u64(tdig, 2 * spread);
    *target = total / 2 + offset - spread;
}

/* -------------------------------------------------------------------------- */
/* Device kernels                                                               */
/* -------------------------------------------------------------------------- */

__device__ __forceinline__ uint64_t subset_sum20(const uint64_t *nums, uint32_t mask) {
    uint64_t s = 0;
#pragma unroll
    for (int i = 0; i < HALF_N; i++) {
        if (mask & (1u << i)) {
            s += nums[i];
        }
    }
    return s;
}

__global__ void gen_left_kernel(const uint64_t *numbers, uint64_t *sums, uint32_t *masks) {
    const int inst = (int)blockIdx.y;
    const uint32_t mask = blockIdx.x * blockDim.x + threadIdx.x;
    if (mask >= (uint32_t)HALF) {
        return;
    }

    __shared__ uint64_t sh[HALF_N];
    if (threadIdx.x < HALF_N) {
        sh[threadIdx.x] = numbers[(size_t)inst * N + threadIdx.x];
    }
    __syncthreads();

    const size_t idx = (size_t)inst * HALF + mask;
    sums[idx] = subset_sum20(sh, mask);
    masks[idx] = mask;
}

__global__ void probe_right_kernel(const uint64_t *numbers, const uint64_t *targets,
                                   const uint64_t *sorted_sums, const uint32_t *sorted_masks,
                                   uint64_t *best) {
    const int inst = (int)blockIdx.y;
    const uint32_t rmask = blockIdx.x * blockDim.x + threadIdx.x;
    if (rmask >= (uint32_t)HALF) {
        return;
    }

    __shared__ uint64_t sh[HALF_N];
    __shared__ uint64_t target_s;
    if (threadIdx.x < HALF_N) {
        sh[threadIdx.x] = numbers[(size_t)inst * N + HALF_N + threadIdx.x];
    }
    if (threadIdx.x == 0) {
        target_s = targets[inst];
    }
    __syncthreads();

    const uint64_t rsum = subset_sum20(sh, rmask);
    const uint64_t target = target_s;
    if (rsum > target) {
        return;
    }
    const uint64_t need = target - rsum;

    const uint64_t *arr = sorted_sums + (size_t)inst * HALF;
    int lo = 0;
    int len = HALF;
    while (len > 0) {
        int half = len >> 1;
        int mid = lo + half;
        if (arr[mid] < need) {
            lo = mid + 1;
            len = len - half - 1;
        } else {
            len = half;
        }
    }
    if (lo >= HALF || arr[lo] != need) {
        return;
    }

    const uint32_t lmask = sorted_masks[(size_t)inst * HALF + lo];
    const uint64_t full = (uint64_t)lmask | ((uint64_t)rmask << HALF_N);
    if (full == 0) {
        return;
    }
    const uint64_t packed = ((uint64_t)rmask << 32) | (uint64_t)lmask;
    atomicMin(reinterpret_cast<unsigned long long *>(best + inst),
              (unsigned long long)packed);
}

/* -------------------------------------------------------------------------- */
/* Context                                                                      */
/* -------------------------------------------------------------------------- */

struct GpuContext {
    int device = 0;
    int batch_size = 0;
    size_t vram_used = 0;
    size_t vram_total = 0;
    char name[96]{};

    uint64_t *d_numbers = nullptr;
    uint64_t *d_targets = nullptr;
    uint64_t *d_sums_in = nullptr;
    uint64_t *d_sums_out = nullptr;
    uint32_t *d_masks_in = nullptr;
    uint32_t *d_masks_out = nullptr;
    int *d_offsets = nullptr;
    uint64_t *d_best = nullptr;
    void *d_cub_temp = nullptr;
    size_t cub_temp_bytes = 0;

    std::vector<uint64_t> h_numbers;
    std::vector<uint64_t> h_targets;
    std::vector<uint64_t> h_best;

    cudaStream_t stream = nullptr;
};

static void free_device(GpuContext *c) {
    cudaFree(c->d_numbers);
    cudaFree(c->d_targets);
    cudaFree(c->d_sums_in);
    cudaFree(c->d_sums_out);
    cudaFree(c->d_masks_in);
    cudaFree(c->d_masks_out);
    cudaFree(c->d_offsets);
    cudaFree(c->d_best);
    cudaFree(c->d_cub_temp);
    c->d_numbers = c->d_targets = c->d_sums_in = c->d_sums_out = nullptr;
    c->d_masks_in = c->d_masks_out = nullptr;
    c->d_offsets = nullptr;
    c->d_best = nullptr;
    c->d_cub_temp = nullptr;
    c->cub_temp_bytes = 0;
    c->vram_used = 0;
}

static bool alloc_batch(GpuContext *c, int B) {
    free_device(c);
    auto fail = [&]() {
        free_device(c);
        return false;
    };

    if (!ck(cudaMalloc(&c->d_numbers, (size_t)B * N * sizeof(uint64_t)), "malloc numbers")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_targets, (size_t)B * sizeof(uint64_t)), "malloc targets")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_sums_in, (size_t)B * HALF * sizeof(uint64_t)), "malloc sums_in")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_sums_out, (size_t)B * HALF * sizeof(uint64_t)), "malloc sums_out")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_masks_in, (size_t)B * HALF * sizeof(uint32_t)), "malloc masks_in")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_masks_out, (size_t)B * HALF * sizeof(uint32_t)), "malloc masks_out")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_offsets, (size_t)(B + 1) * sizeof(int)), "malloc offsets")) {
        return fail();
    }
    if (!ck(cudaMalloc(&c->d_best, (size_t)B * sizeof(uint64_t)), "malloc best")) {
        return fail();
    }

    std::vector<int> offsets(B + 1);
    for (int i = 0; i <= B; i++) {
        offsets[i] = i * HALF;
    }
    if (!ck(cudaMemcpy(c->d_offsets, offsets.data(), (size_t)(B + 1) * sizeof(int),
                       cudaMemcpyHostToDevice),
            "copy offsets")) {
        return fail();
    }

    size_t temp_bytes = 0;
    cub::DeviceSegmentedRadixSort::SortPairs(
        nullptr, temp_bytes, c->d_sums_in, c->d_sums_out, c->d_masks_in, c->d_masks_out,
        B * HALF, B, c->d_offsets, c->d_offsets + 1, 0, SUM_BITS, c->stream);
    if (!ck(cudaMalloc(&c->d_cub_temp, temp_bytes), "malloc cub temp")) {
        return fail();
    }
    c->cub_temp_bytes = temp_bytes;

    c->h_numbers.assign((size_t)B * N, 0);
    c->h_targets.assign(B, 0);
    c->h_best.assign(B, 0);

    size_t used = (size_t)B * N * sizeof(uint64_t) + (size_t)B * sizeof(uint64_t) +
                  (size_t)B * HALF * sizeof(uint64_t) * 2 +
                  (size_t)B * HALF * sizeof(uint32_t) * 2 +
                  (size_t)(B + 1) * sizeof(int) + (size_t)B * sizeof(uint64_t) + temp_bytes;
    c->vram_used = used;
    c->batch_size = B;
    return true;
}

static int pick_batch_size(int requested) {
    size_t free_b = 0, total_b = 0;
    if (cudaMemGetInfo(&free_b, &total_b) != cudaSuccess) {
        return requested > 0 ? requested : 32;
    }
    /* Leave headroom for the Windows desktop on a gaming card. An 8 GB 3070 Ti
     * otherwise happily allocates 5+ GB and the display driver starts paging. */
    size_t reserve = (size_t)1536 << 20;
    int cap = MAX_BATCH;
    if (total_b <= ((size_t)10 << 30)) {
        reserve = (size_t)2560 << 20;
        cap = 64;
    } else if (total_b <= ((size_t)14 << 30)) {
        reserve = (size_t)2048 << 20;
        cap = 96;
    }
    size_t budget = free_b > reserve ? free_b - reserve : free_b / 2;

    /* Per instance: two 8-byte sum buffers, two 4-byte mask buffers, plus CUB. */
    const size_t per = (size_t)HALF * (8 + 8 + 4 + 4) + (size_t)HALF * 8;
    int auto_b = (int)(budget / per);
    auto_b = std::max(MIN_BATCH, std::min(cap, auto_b));
    /* Prefer multiples of 16 for cleaner grids. */
    auto_b = (auto_b / 16) * 16;
    if (auto_b < MIN_BATCH) {
        auto_b = MIN_BATCH;
    }
    if (requested > 0) {
        return std::max(1, std::min(MAX_BATCH, requested));
    }
    return auto_b;
}

static bool run_mitm(GpuContext *c, int n) {
    if (n <= 0) {
        return true;
    }
    if (n > c->batch_size) {
        g_error = "batch larger than allocated context";
        return false;
    }

    dim3 block(BLOCK);
    dim3 grid(HALF / BLOCK, n);

    gen_left_kernel<<<grid, block, 0, c->stream>>>(c->d_numbers, c->d_sums_in, c->d_masks_in);
    if (!ck(cudaGetLastError(), "gen_left")) {
        return false;
    }

    size_t temp_bytes = c->cub_temp_bytes;
    cub::DeviceSegmentedRadixSort::SortPairs(
        c->d_cub_temp, temp_bytes, c->d_sums_in, c->d_sums_out, c->d_masks_in, c->d_masks_out,
        n * HALF, n, c->d_offsets, c->d_offsets + 1, 0, SUM_BITS, c->stream);
    if (!ck(cudaGetLastError(), "segmented sort")) {
        return false;
    }

    if (!ck(cudaMemsetAsync(c->d_best, 0xFF, (size_t)n * sizeof(uint64_t), c->stream),
            "memset best")) {
        return false;
    }

    probe_right_kernel<<<grid, block, 0, c->stream>>>(c->d_numbers, c->d_targets, c->d_sums_out,
                                                      c->d_masks_out, c->d_best);
    if (!ck(cudaGetLastError(), "probe_right")) {
        return false;
    }
    if (!ck(cudaMemcpyAsync(c->h_best.data(), c->d_best, (size_t)n * sizeof(uint64_t),
                            cudaMemcpyDeviceToHost, c->stream),
            "copy best")) {
        return false;
    }
    if (!ck(cudaStreamSynchronize(c->stream), "sync mitm")) {
        return false;
    }
    return true;
}

static uint64_t unpack_mask(uint64_t packed) {
    if (packed == ~0ULL) {
        return 0;
    }
    uint32_t lmask = (uint32_t)packed;
    uint32_t rmask = (uint32_t)(packed >> 32);
    return (uint64_t)lmask | ((uint64_t)rmask << HALF_N);
}

}  // namespace

/* -------------------------------------------------------------------------- */
/* C API                                                                        */
/* -------------------------------------------------------------------------- */

extern "C" {

const char *rofl_gpu_last_error(void) { return g_error.c_str(); }

void *rofl_gpu_create(int device_id, int batch_size) {
    g_error.clear();
    GpuContext *c = new GpuContext();
    c->device = device_id < 0 ? 0 : device_id;

    int count = 0;
    if (!ck(cudaGetDeviceCount(&count), "cudaGetDeviceCount") || count <= 0) {
        g_error = "no CUDA device";
        delete c;
        return nullptr;
    }
    if (c->device >= count) {
        g_error = "CUDA device index out of range";
        delete c;
        return nullptr;
    }
    if (!ck(cudaSetDevice(c->device), "cudaSetDevice")) {
        delete c;
        return nullptr;
    }

    cudaDeviceProp prop{};
    if (ck(cudaGetDeviceProperties(&prop, c->device), "cudaGetDeviceProperties")) {
        snprintf(c->name, sizeof c->name, "%s", prop.name);
        c->vram_total = prop.totalGlobalMem;
    } else {
        snprintf(c->name, sizeof c->name, "CUDA device %d", c->device);
    }

    if (!ck(cudaStreamCreate(&c->stream), "cudaStreamCreate")) {
        delete c;
        return nullptr;
    }

    int want = pick_batch_size(batch_size);
    while (want >= MIN_BATCH) {
        if (alloc_batch(c, want)) {
            break;
        }
        want = (want / 2 / 16) * 16;
        if (want < MIN_BATCH && batch_size <= 0) {
            want = MIN_BATCH;
            if (alloc_batch(c, want)) {
                break;
            }
            want = 0;
            break;
        }
        if (batch_size > 0) {
            /* User asked for an exact size and it did not fit. */
            break;
        }
    }
    if (c->batch_size <= 0) {
        if (g_error.empty()) {
            g_error = "failed to allocate GPU batch buffers";
        }
        if (c->stream) {
            cudaStreamDestroy(c->stream);
        }
        delete c;
        return nullptr;
    }

    cudaFuncSetCacheConfig(probe_right_kernel, cudaFuncCachePreferL1);
    return c;
}

void rofl_gpu_destroy(void *ctx) {
    if (!ctx) {
        return;
    }
    GpuContext *c = static_cast<GpuContext *>(ctx);
    cudaSetDevice(c->device);
    free_device(c);
    if (c->stream) {
        cudaStreamDestroy(c->stream);
    }
    delete c;
}

int rofl_gpu_device_name(void *ctx, char *buf, int len) {
    if (!ctx || !buf || len <= 0) {
        return -1;
    }
    GpuContext *c = static_cast<GpuContext *>(ctx);
    snprintf(buf, (size_t)len, "%s", c->name);
    return 0;
}

int rofl_gpu_batch_size(void *ctx) {
    if (!ctx) {
        return 0;
    }
    return static_cast<GpuContext *>(ctx)->batch_size;
}

unsigned long long rofl_gpu_vram_used(void *ctx) {
    if (!ctx) {
        return 0;
    }
    return (unsigned long long)static_cast<GpuContext *>(ctx)->vram_used;
}

unsigned long long rofl_gpu_vram_total(void *ctx) {
    if (!ctx) {
        return 0;
    }
    return (unsigned long long)static_cast<GpuContext *>(ctx)->vram_total;
}

int rofl_gpu_make_instance(const uint8_t *header_core, int header_len, int j, int nonce,
                           uint64_t *numbers_out, uint64_t *target_out) {
    if (!header_core || header_len <= 0 || !numbers_out || !target_out) {
        g_error = "make_instance: bad arguments";
        return -1;
    }
    if (j < 0 || nonce < 0 || nonce >= MAX_NONCE) {
        g_error = "make_instance: j/nonce out of range";
        return -1;
    }
    make_instance(header_core, header_len, j, nonce, numbers_out, target_out);
    return 0;
}

int rofl_gpu_solve_instances(void *ctx, const uint64_t *numbers, const uint64_t *targets, int n,
                             uint64_t *out_masks) {
    g_error.clear();
    if (!ctx || !numbers || !targets || !out_masks || n <= 0) {
        g_error = "solve_instances: bad arguments";
        return -1;
    }
    GpuContext *c = static_cast<GpuContext *>(ctx);
    if (!ck(cudaSetDevice(c->device), "cudaSetDevice")) {
        return -1;
    }

    int solved = 0;
    int off = 0;
    while (off < n) {
        int chunk = std::min(c->batch_size, n - off);
        if (!ck(cudaMemcpyAsync(c->d_numbers, numbers + (size_t)off * N,
                                (size_t)chunk * N * sizeof(uint64_t), cudaMemcpyHostToDevice,
                                c->stream),
                "upload numbers")) {
            return -1;
        }
        if (!ck(cudaMemcpyAsync(c->d_targets, targets + off, (size_t)chunk * sizeof(uint64_t),
                                cudaMemcpyHostToDevice, c->stream),
                "upload targets")) {
            return -1;
        }
        if (!run_mitm(c, chunk)) {
            return -1;
        }
        for (int i = 0; i < chunk; i++) {
            uint64_t full = unpack_mask(c->h_best[i]);
            out_masks[off + i] = full;
            if (full) {
                solved++;
            }
        }
        off += chunk;
    }
    return solved;
}

int rofl_gpu_solve_puzzles(void *ctx, const uint8_t *header_core, int header_len, int k,
                           int *out_nonces, uint64_t *out_subsets, int *out_tried,
                           rofl_progress_fn progress, void *progress_user) {
    g_error.clear();
    if (!ctx || !header_core || header_len <= 0 || k <= 0 || k > K_MAX || !out_nonces ||
        !out_subsets) {
        g_error = "solve_puzzles: bad arguments";
        return -1;
    }
    GpuContext *c = static_cast<GpuContext *>(ctx);
    if (!ck(cudaSetDevice(c->device), "cudaSetDevice")) {
        return -1;
    }

    std::vector<uint8_t> done(k, 0);
    std::vector<int> next_nonce(k, 0);
    std::vector<int> max_tried(k, -1);
    int solved = 0;

    for (int j = 0; j < k; j++) {
        out_nonces[j] = -1;
        out_subsets[j] = 0;
    }

    while (solved < k) {
        std::vector<int> live;
        live.reserve(k);
        for (int j = 0; j < k; j++) {
            if (!done[j] && next_nonce[j] < MAX_NONCE) {
                live.push_back(j);
            }
        }
        if (live.empty()) {
            g_error = "no solvable instance in nonce range; this should not happen";
            return -1;
        }
        std::stable_sort(live.begin(), live.end(),
                         [&](int a, int b) { return next_nonce[a] < next_nonce[b]; });

        struct Job {
            int j;
            int nonce;
        };
        std::vector<Job> jobs;
        jobs.reserve(c->batch_size);
        for (int j : live) {
            if ((int)jobs.size() >= c->batch_size) {
                break;
            }
            jobs.push_back({j, next_nonce[j]});
        }

        /* Only speculate extra nonces when the wave would leave the GPU idle.
         * A full batch of distinct puzzles is already 2^20 threads per instance. */
        if ((int)jobs.size() < c->batch_size && (int)jobs.size() < 32) {
            const int base_count = (int)jobs.size();
            int extra = 1;
            while ((int)jobs.size() < c->batch_size && extra < MAX_SPECULATIVE) {
                int added = 0;
                for (int i = 0; i < base_count && (int)jobs.size() < c->batch_size; i++) {
                    int n2 = jobs[i].nonce + extra;
                    if (n2 < MAX_NONCE) {
                        jobs.push_back({jobs[i].j, n2});
                        added++;
                    }
                }
                if (!added) {
                    break;
                }
                extra++;
            }
        }

        const int n = (int)jobs.size();
        for (int i = 0; i < n; i++) {
            make_instance(header_core, header_len, jobs[i].j, jobs[i].nonce,
                          c->h_numbers.data() + (size_t)i * N, &c->h_targets[i]);
        }
        if (!ck(cudaMemcpyAsync(c->d_numbers, c->h_numbers.data(),
                                (size_t)n * N * sizeof(uint64_t), cudaMemcpyHostToDevice,
                                c->stream),
                "upload numbers")) {
            return -1;
        }
        if (!ck(cudaMemcpyAsync(c->d_targets, c->h_targets.data(), (size_t)n * sizeof(uint64_t),
                                cudaMemcpyHostToDevice, c->stream),
                "upload targets")) {
            return -1;
        }
        if (!run_mitm(c, n)) {
            return -1;
        }

        std::fill(max_tried.begin(), max_tried.end(), -1);
        for (int i = 0; i < n; i++) {
            const int j = jobs[i].j;
            const int nonce = jobs[i].nonce;
            if (nonce > max_tried[j]) {
                max_tried[j] = nonce;
            }
            uint64_t full = unpack_mask(c->h_best[i]);
            if (!full) {
                continue;
            }
            if (!done[j] || nonce < out_nonces[j]) {
                if (!done[j]) {
                    solved++;
                }
                done[j] = 1;
                out_nonces[j] = nonce;
                out_subsets[j] = full;
            }
        }
        for (int j = 0; j < k; j++) {
            if (!done[j] && max_tried[j] >= 0) {
                next_nonce[j] = max_tried[j] + 1;
            }
        }
        if (progress) {
            int reported = 0;
            for (int j = 0; j < k; j++) {
                reported += done[j] ? out_nonces[j] + 1 : next_nonce[j];
            }
            progress(solved, k, reported, progress_user);
        }
    }

    int necessary = 0;
    for (int j = 0; j < k; j++) {
        if (!done[j] || out_nonces[j] < 0) {
            g_error = "unsolved puzzle after grind";
            return -1;
        }
        necessary += out_nonces[j] + 1;
    }
    if (out_tried) {
        *out_tried = necessary;
    }
    return 0;
}

}  // extern "C"
