#ifndef ROFL_SUBSET_SUM_H
#define ROFL_SUBSET_SUM_H

#include <stdint.h>
#include <stddef.h>

#ifdef _WIN32
#define ROFL_API __declspec(dllexport)
#else
#define ROFL_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*rofl_progress_fn)(int solved, int k, int tried, void *user);

ROFL_API void *rofl_gpu_create(int device_id, int batch_size);
ROFL_API void rofl_gpu_destroy(void *ctx);
ROFL_API const char *rofl_gpu_last_error(void);

ROFL_API int rofl_gpu_device_name(void *ctx, char *buf, int len);
ROFL_API int rofl_gpu_batch_size(void *ctx);
ROFL_API unsigned long long rofl_gpu_vram_used(void *ctx);
ROFL_API unsigned long long rofl_gpu_vram_total(void *ctx);

/* Must match rofl/pow.py instance() bit-for-bit. */
ROFL_API int rofl_gpu_make_instance(
    const uint8_t *header_core,
    int header_len,
    int j,
    int nonce,
    uint64_t *numbers_out,
    uint64_t *target_out);

/* numbers: n * 40 uint64, targets: n uint64, out_masks: n uint64 (0 = unsolved). */
ROFL_API int rofl_gpu_solve_instances(
    void *ctx,
    const uint64_t *numbers,
    const uint64_t *targets,
    int n,
    uint64_t *out_masks);

/* Grind all k puzzles for one block. out_nonces/out_subsets length k. */
ROFL_API int rofl_gpu_solve_puzzles(
    void *ctx,
    const uint8_t *header_core,
    int header_len,
    int k,
    int *out_nonces,
    uint64_t *out_subsets,
    int *out_tried,
    rofl_progress_fn progress,
    void *progress_user);

#ifdef __cplusplus
}
#endif

#endif
