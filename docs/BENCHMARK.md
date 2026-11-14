# Retromon measurements

Measured on 14 November 2026. These results describe this laptop and this workload, not a performance guarantee across machines.

## Method

The benchmark launches the actual app inside a 100 × 32 POSIX pseudo-terminal and drains its output. It observes process RSS once per second and computes mean app CPU usage from accumulated user plus system CPU time divided by elapsed wall time. Python, psutil, the background collector, and retained history are included. Observer and external terminal-emulator CPU are excluded.

Both original and updated versions run in live view at their normal one-second sampling target, without keyboard input. Colors are enabled for both; the harness removes `NO_COLOR` only from child environments. Each run lasts 660 seconds. This fills the updated 600-slot history and measures another minute after rollover.

The apps run concurrently on the same shared host. Normal host activity and some verification work continue during the measurements. This is a single observational comparison, not an isolated or statistically repeated load benchmark. RSS is sampled, so a very short allocation peak could be missed. The original UI does not expose retained sample count; its `retained_samples_observed` field is zero because that count cannot be read from its output.

Source SHA-256 hashes are recorded before and after each run. The baseline is `app.py` from Git commit `b80aea3`. The updated run measures the local implementation whose hashes appear in the result JSON.

## Reproduce

```bash
python tools/benchmark.py --duration 660 --output docs/benchmark-local.json
python tools/sampling_benchmark.py --samples 60 --output docs/sampling-local.json
```

The first command needs Linux or macOS. The sampling-only command also works without an interactive terminal. The sampling benchmark excludes its initial warmup call and measures 60 collector calls at the normal one-second interval; p95 uses the nearest-rank method.

## Interpretation

- **RSS** is resident memory mapped by the app, including shared pages. MiB means 1,048,576 bytes.
- **USS** is private memory observed at the end of the run, where supported.
- **CPU percentage** uses one logical core as 100%. Divide by the four logical cores on this host to express the share of total CPU capacity.
- **History memory is bounded**, rather than a constant byte count from startup. RSS grows as snapshots fill the buffer and may fluctuate with process churn, Python allocation behavior, and OS memory pressure.
- **Redraw reduction** compares observed full-frame writes in idle live view. It does not imply the same percentage reduction in total CPU usage.
- **Startup timing** is one observed launch, not a startup-time distribution.

## Validation

28 local regression tests pass. They cover rollover, stable rewind, expired selections, unavailable fields, reused process IDs, CPU/RAM leader retention, partial and queued keys, compact layouts, sanitized names, collector recovery, and real Linux terminal navigation, resizing, and restoration after quit or Ctrl+C.

CI is configured for Python 3.10 and 3.13 on Linux, Windows, and macOS. Those hosted jobs have not been run in this session. Native Windows/macOS console behavior is not claimed as locally verified.

## Results

Hardware: Intel Core i3-4005U, four logical threads, 3,829.99 MiB (3.74 GiB) usable RAM. Linux 6.18.60, Python 3.14.9, psutil 7.2.2. Both runs completed approximately eleven minutes without source changes.

| Metric | Original (`b80aea3`) | Updated |
|---|---:|---:|
| First observed RSS | 17.70 MiB | 21.75 MiB |
| Median observed RSS | 19.89 MiB | 25.35 MiB |
| Peak observed RSS | 21.59 MiB | 28.19 MiB |
| End-of-run private memory (USS) | 12.19 MiB | 18.12 MiB |
| Mean CPU, one logical core = 100% | 6.32% | 6.22% |
| Full-frame writes per second | 9.910 | 1.001 |
| Terminal output per second | 15338 bytes | 5800 bytes |
| First frame, one observed launch | 293 ms | 366 ms |
| First snapshot, one observed launch | 394 ms | 499 ms |

The updated app retained 600 snapshots and produced 60 RSS observations after history filled. During that final minute, RSS stayed between **28.168 and 28.188 MiB**. This supports bounded history during this run; it does not prove constant RSS across arbitrary process churn or longer workloads.

Mean app CPU corresponds to approximately **1.56% of this four-thread CPU's total capacity**. This is an average, not a peak CPU limit. Overall CPU use was close to the original; the small difference is not sufficient evidence for a general CPU-speedup claim.

The updated UI emitted **89.9% fewer full-frame writes** and approximately 62.2% fewer terminal bytes during idle live view. Extra RAM is expected: snapshots now retain both CPU and RAM leaders, up to 40 process records instead of 20, plus timestamp and identity metadata.

## Collector latency

The reusable sampling harness measured 60 warmed collector calls while scanning 205–229 processes. Median wall latency was **51.0 ms**, p95 **64.4 ms**, maximum 88.2 ms. Mean CPU time per collection was 50.3 ms.

An additional equivalent 60-sample check on the same final build observed p95 **95.2 ms** and maximum 137.6 ms while scanning 204–206 processes. The observed p95 range, 64.4–95.2 ms, illustrates scheduling and host-workload variation. Do not describe one run's p95 as a universal latency bound.

[Raw comparison results](benchmark-results.json) · [Sampling results](sampling-results.json)
