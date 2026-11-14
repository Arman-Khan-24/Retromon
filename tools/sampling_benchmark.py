"""Measure steady-state collector latency at the normal sampling interval."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DataCollector, SAMPLE_INTERVAL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("Use at least two samples.")
    collector = DataCollector()
    collector.collect()
    wall, cpu, counts = [], [], []
    for _ in range(args.samples):
        tick, start_cpu = time.monotonic(), time.process_time()
        snapshot = collector.collect()
        wall.append((time.monotonic() - tick) * 1000)
        cpu.append((time.process_time() - start_cpu) * 1000)
        counts.append(snapshot.process_count)
        time.sleep(max(0, SAMPLE_INTERVAL - (time.monotonic() - tick)))
    result = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "samples": args.samples, "interval_seconds": SAMPLE_INTERVAL,
        "wall_ms": {"median": round(statistics.median(wall), 3),
                    "p95": round(sorted(wall)[math.ceil(len(wall) * .95) - 1], 3),
                    "max": round(max(wall), 3)},
        "mean_cpu_time_ms": round(statistics.mean(cpu), 3),
        "process_count_range": [min(counts), max(counts)],
        "python": platform.python_version(), "psutil": psutil.__version__,
        "method": "First call excluded; collector runs once per second. Wall latency includes OS scheduling; p95 uses nearest rank."
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
