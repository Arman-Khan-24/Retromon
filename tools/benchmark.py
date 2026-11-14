"""Measure the actual app in a POSIX pseudo-terminal; includes Python and psutil."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import signal
import statistics
import subprocess
import sys
import time

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=660, help="seconds; 660 fills history and measures another minute")
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parents[1] / "app.py")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("This harness needs a POSIX pseudo-terminal. Run on Linux or macOS.")
    if args.duration < 3:
        parser.error("Duration must be at least 3 seconds.")
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
    started = time.monotonic()
    source_paths = [path for path in (args.app, args.app.parent / "terminal_ui.py") if path.exists()]
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    cpu_model = platform.processor()
    if sys.platform.startswith("linux"):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    environment["TERM"] = "xterm-256color"
    child = subprocess.Popen([sys.executable, "-u", str(args.app.resolve())],
                             stdin=slave, stdout=slave, stderr=slave,
                             cwd=args.app.resolve().parent,
                             env=environment)
    process = psutil.Process(child.pid)
    samples, full_history, output_bytes, frames, saved = [], [], 0, 0, 0
    first_frame = first_snapshot = None
    next_sample = started + 1
    plain_tail = ""
    cpu_start = sum(process.cpu_times()[:2])
    cpu_end = cpu_start
    measured = 0
    uss = None
    interrupted = False

    def stop_run(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop_run)
    signal.signal(signal.SIGINT, stop_run)
    checkpoint = args.output.with_suffix(".progress.json")
    next_checkpoint = started
    try:
        while not interrupted and time.monotonic() - started < args.duration:
            if child.poll() is not None:
                raise RuntimeError(f"App exited early with code {child.returncode}: {plain_tail[-1000:]}")
            if select.select([master], [], [], 0.05)[0]:
                data = os.read(master, 65536)
                output_bytes += len(data)
                frames += data.count(b"\x1b[H")
                if first_frame is None and b"\x1b[H" in data:
                    first_frame = time.monotonic() - started
                plain_tail = (plain_tail + re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", data.decode("utf-8", errors="replace")))[-16000:]
                counts = re.findall(r"(\d+)/600", plain_tail)
                if counts:
                    saved = int(counts[-1])
                if first_snapshot is None and (saved or "Global RAM Usage:" in plain_tail):
                    first_snapshot = time.monotonic() - started
            now = time.monotonic()
            if now >= next_checkpoint:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                checkpoint.write_text(json.dumps({"elapsed_seconds": round(now-started, 1),
                    "retained_samples": saved, "last_rss_mib": round(samples[-1]/1024**2, 3) if samples else None,
                    "full_history_observations": len(full_history)}, indent=2) + "\n")
                next_checkpoint = now + 30
            if now >= next_sample:
                rss = process.memory_info().rss
                samples.append(rss)
                if saved == 600:
                    full_history.append(rss)
                measured = now - started
                cpu_end = sum(process.cpu_times()[:2])
                next_sample = now + 1
        if not samples:
            samples.append(process.memory_info().rss)
        measured = time.monotonic() - started
        cpu_end = sum(process.cpu_times()[:2])
        try:
            uss = process.memory_full_info().uss
        except (psutil.AccessDenied, AttributeError):
            pass
    finally:
        try:
            os.write(master, b"q")
            child.wait(timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        os.close(master)
        os.close(slave)

    result = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "app": str(args.app), "duration_seconds": round(measured, 3),
        "completed_requested_duration": not interrupted,
        "source_sha256": source_hashes,
        "source_changed_during_run": any(hashlib.sha256(path.read_bytes()).hexdigest() != source_hashes[path.name] for path in source_paths),
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "cpu_model": cpu_model,
                        "psutil": psutil.__version__, "logical_cpus": psutil.cpu_count(),
                        "host_ram_mib": round(psutil.virtual_memory().total / 1024 ** 2, 2),
                        "process_count_at_end": len(psutil.pids()), "terminal": "100x32 POSIX PTY",
                        "colors": "enabled; NO_COLOR removed for both comparison runs"},
        "rss_mib": {"first_sample": round(samples[0] / 1024 ** 2, 3),
                    "median": round(statistics.median(samples) / 1024 ** 2, 3),
                    "peak": round(max(samples) / 1024 ** 2, 3), "final": round(samples[-1] / 1024 ** 2, 3)},
        "uss_mib_at_end": round(uss / 1024 ** 2, 3) if uss is not None else None,
        "mean_cpu_percent_one_logical_core": round((cpu_end - cpu_start) / measured * 100, 3),
        "first_frame_ms": round(first_frame * 1000, 2) if first_frame is not None else None,
        "first_snapshot_ms": round(first_snapshot * 1000, 2) if first_snapshot is not None else None,
        "frames": frames, "frames_per_second": round(frames / measured, 3),
        "terminal_bytes_per_second": round(output_bytes / measured, 2),
        "retained_samples_observed": saved,
        "full_history_observations": len(full_history),
        "full_history_rss_mib": {"min": round(min(full_history) / 1024 ** 2, 3),
                                 "max": round(max(full_history) / 1024 ** 2, 3)} if full_history else None,
        "exit_code": child.returncode,
        "method": "One idle live app; 1 Hz sampling; RSS observed once per second; CPU is process CPU time / wall time. Includes interpreter and dependency memory. Shared host, no controlled synthetic load."
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
