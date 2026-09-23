"""Run with python3 tests/check_grader.py after make. No student solution needed."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
handout = root / 'sfslab'
with tempfile.TemporaryDirectory(prefix='sfs-check-') as tmp:
    tmp = Path(tmp)
    env = dict(os.environ, SFS_DISK_DIR=str(tmp), SFS_KEEP_FAILED_DISKS='0')
    names = ['test.img', 'test_perf.img', 'test_conc_C00.img', 'fail_A00_test.img']
    for name in names:
        (tmp / name).write_bytes(b'user image: preserve me')
    for args, expected in [(['test-sfs', '-q'], 1),
                           (['test-sfs-baseline', '--tsan-only'], 0)]:
        result = subprocess.run([str(handout / args[0]), *args[1:]],
                                cwd=handout, env=env, capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == expected, result.stdout + result.stderr
        if args[0] == 'test-sfs':
            assert 'Lab correctness:' in result.stdout
            assert 'Local benchmark total:' not in result.stdout
        for name in names:
            assert (tmp / name).read_bytes() == b'user image: preserve me'
    assert not list(tmp.glob('sfs-test.*')), 'successful/quiet runs leaked images'

    # A failed consistency check must leave all five concurrent images available.
    checker_dir = tmp / 'rejecting-checker'
    checker_dir.mkdir()
    checker = checker_dir / 'sfs-fsck'
    checker.write_text('#!/bin/sh\nexit 1\n')
    checker.chmod(0o755)
    result = subprocess.run([str(handout / 'test-sfs-baseline'), '--tsan-only'],
                            cwd=checker_dir,
                            env=dict(env, SFS_KEEP_FAILED_DISKS='1'),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 66, result.stdout + result.stderr
    kept = list(tmp.glob('sfs-test.*/fail_C*.img'))
    assert len(kept) == 5, f'concurrent failure images lost: {kept}'

    # Preserve valid block chains but give two live files the same name.
    valid = next(tmp.glob('sfs-test.*/fail_C00_*.img'))
    subprocess.run([str(handout / 'sfs-fsck'), str(valid)], check=True, timeout=10)
    image = bytearray(valid.read_bytes())
    image[72:96] = image[40:64]  # names in the first two 32-byte entries
    duplicate = tmp / 'duplicate.img'
    duplicate.write_bytes(image)
    result = subprocess.run([str(handout / 'sfs-fsck'), str(duplicate)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'duplicate file name' in result.stderr

    source = tmp / 'check.c'
    source.write_text(r'''
#define main driver_main
#include "test-sfs.c"
#undef main
#include <assert.h>
static int fail_reads, corrupt_reads;
ssize_t __real_sfs_read(int fd, char *buf, size_t len);
ssize_t __wrap_sfs_read(int fd, char *buf, size_t len)
{
    if (fail_reads) return -EIO;
    ssize_t result = __real_sfs_read(fd, buf, len);
    if (corrupt_reads && result > 0) buf[0] ^= 1;
    return result;
}
static int noisy_trace(void)
{
    char noise[4096] = {0};
    for (;;)
        (void)write(STDERR_FILENO, noise, sizeof noise);
    return 1;
}
static pthread_barrier_t gate;
static void *worker(void *arg)
{
    c_api_enter();
    pthread_barrier_wait(&gate); /* A driver lock would deadlock here. */
    CHECK(0, "intentional concurrent failure");
    c_api_leave();
    return NULL;
}
int main(void)
{
    _Static_assert(_Generic(&trace_ok, _Atomic int *: 1, default: 0),
                   "failure flag must be atomic");
    quiet_mode = 1;
    trace_ok = 1;
    pthread_t a, b;
    assert(pthread_barrier_init(&gate, NULL, 2) == 0);
    assert(pthread_create(&a, NULL, worker, NULL) == 0);
    assert(pthread_create(&b, NULL, worker, NULL) == 0);
    pthread_join(a, NULL);
    pthread_join(b, NULL);
    assert(!trace_ok && trace_fail_count == 2);
    char *output;
    size_t length;
    assert(run_trace_with_timeout("noise", noisy_trace, &output, &length) == 0);
    free(output);

    init_disk_paths();
    double ops = run_perf_benchmark_raw();
    assert(isfinite(ops) && ops > 0 && !perf_worker_failed);
    fail_reads = 1;
    assert(run_perf_benchmark() == 0 && perf_worker_failed);
    fail_reads = 0;
    corrupt_reads = 1;
    perf_worker_failed = 0;
    assert(run_perf_benchmark() == 0 && perf_worker_failed);

    /* Keep the existing score thresholds and the stale-workload fallback. */
    assert(score_perf_absolute(49999) == 0);
    assert(score_perf_absolute(50000) == 3);
    assert(score_perf_absolute(999999) == 3);
    assert(score_perf_absolute(1000000) == 5);
    FILE *baseline;
    for (int version = 3; version <= 4; version++)
    {
        baseline = fopen(".perf_baseline", "w");
        assert(baseline);
        fprintf(baseline, "1000000\nWORKLOAD=v%d\nSFS_DISK_DIR=%s\n",
                version, getenv("SFS_DISK_DIR"));
        assert(fclose(baseline) == 0);
        assert(score_perf_against_baseline(2500000) == 5);
    }
    baseline = fopen(".perf_baseline", "w");
    assert(baseline);
    fprintf(baseline, "1000000\nWORKLOAD=%s\nSFS_DISK_DIR=%s\n",
            PERF_WORKLOAD_VERSION, getenv("SFS_DISK_DIR"));
    assert(fclose(baseline) == 0);
    const struct { double ops; int score; } thresholds[] = {
        {849999, 0}, {850000, 3}, {1199999, 3}, {1200000, 5},
        {1399999, 5}, {1400000, 7}, {1799999, 7}, {1800000, 9},
        {2499999, 9}, {2500000, 10}
    };
    for (size_t i = 0; i < sizeof thresholds / sizeof thresholds[0]; i++)
        assert(score_perf_against_baseline(thresholds[i].ops) == thresholds[i].score);
    return 0;
}
'''.replace('#include "test-sfs.c"',
               (handout / 'test-sfs.c').read_text().replace(
                   '#define TRACE_TIMEOUT_SEC 30', '#define TRACE_TIMEOUT_SEC 1')
               .replace('#define PERF_OUTER_ITERS 16000',
                        '#define PERF_OUTER_ITERS 3')))
    binary = tmp / 'check'
    subprocess.run(['gcc', '-std=c11', '-D_GNU_SOURCE=1', '-pthread',
                    '-I', str(handout), str(source),
                    str(handout / 'sfs-baseline-ref.c'),
                    str(handout / 'sfs-support.c'), '-Wl,--wrap=sfs_read',
                    '-o', str(binary)], check=True)
    result = subprocess.run([str(binary)], cwd=tmp, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

    # Exercise the real parent/child score transport without a student solution.
    # Stub only correctness/TSan and the measured result, after their definitions.
    source.write_text((handout / 'test-sfs.c').read_text().replace(
        'int main(int argc, char *argv[])', r'''
static int stub_perf_result(void)
{
    const char *exit_code = getenv("SFS_TEST_PERF_EXIT");
    if (exit_code) _exit(atoi(exit_code));
    return atoi(getenv("SFS_TEST_PERF_SCORE"));
}
#define run_category(label, traces, count) (count)
#define run_tsan_check() TSAN_CLEAN
#define run_perf_benchmark() stub_perf_result()
int main(int argc, char *argv[])
'''))
    subprocess.run(['gcc', '-std=c11', '-D_GNU_SOURCE=1', '-pthread',
                    '-I', str(handout), str(source),
                    str(handout / 'sfs-baseline-ref.c'),
                    str(handout / 'sfs-support.c'), '-o', str(binary)], check=True)
    cases = [(None, score, score) for score in (0, 3, 5, 7, 9, 10)]
    cases += [(code, 10, 0) for code in (0, 1, 3, 10, 42, 255)]
    cases += [(None, score, 0) for score in (-1, 42)]
    for exit_code, score, expected in cases:
        case_env = dict(env, SFS_TEST_PERF_SCORE=str(score))
        case_env.pop('SFS_TEST_PERF_EXIT', None)
        if exit_code is not None:
            case_env['SFS_TEST_PERF_EXIT'] = str(exit_code)
        result = subprocess.run([str(binary), '--benchmark'], cwd=tmp,
                                env=case_env, capture_output=True,
                                text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f'  Score: {expected}/10\n' in result.stdout, result.stdout
        assert (f'Local benchmark total: {12 + expected}/22  (+ up to 4 style pts)'
                in result.stdout), result.stdout
print('Grader regression checks passed.')
