"""Retromon: bounded system history with a native terminal dashboard."""

import argparse
from dataclasses import dataclass, field
from heapq import nlargest
import os
import shutil
import sys
import threading
import time
from typing import Dict, List, Optional

import psutil
from terminal_ui import render_dashboard

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

HISTORY_CAPACITY = 600
SAMPLE_INTERVAL = 1.0
PROCESS_LIMIT = 20


@dataclass
class SystemSnapshot:
    global_cpu: Optional[float]
    global_mem: float
    processes: List[Dict]
    timestamp: float = field(default_factory=time.time)
    monotonic: float = field(default_factory=time.monotonic)
    memory_used: int = 0
    memory_total: int = 0
    process_count: int = 0
    sequence: int = -1


class DataCollector:
    def __init__(self):
        self._known_processes = set()
        self._primed = False

    def collect(self) -> SystemSnapshot:
        # Initialize baselines in the same thread that takes later samples.
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        processes, known = [], set()
        attrs = ["pid", "create_time", "cpu_percent", "memory_percent"]
        for proc in psutil.process_iter(attrs, ad_value=None):
            try:
                info = proc.info
                identity = (info["pid"], info["create_time"])
                known.add(identity)
                # A new process has no previous CPU delta; that is not 0%.
                process_cpu = info["cpu_percent"] if info["create_time"] is not None and identity in self._known_processes else None
                processes.append((proc, {"pid": info["pid"], "created": info["create_time"],
                                         "cpu": process_cpu, "mem": info["memory_percent"]}))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        # At most 40 records, preserving memory leaders as well as CPU leaders.
        leaders = {}
        for metric in ("cpu", "mem"):
            for proc, record in nlargest(PROCESS_LIMIT, processes, key=lambda item: item[1][metric] or 0):
                leaders[(record["pid"], record["created"])] = (proc, record)
        retained = []
        for proc, record in leaders.values():
            try:
                # psutil caches Process objects. is_running also detects reused PIDs.
                if not proc.is_running():
                    continue
                try:
                    record["name"] = proc.name() or "<unavailable>"
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    record["name"] = "<unavailable>"
                retained.append(record)
            except psutil.NoSuchProcess:
                continue
        snap = SystemSnapshot(cpu if self._primed else None, memory.percent, retained,
                              memory_used=memory.total - memory.available,
                              memory_total=memory.total, process_count=len(processes))
        self._primed = True
        self._known_processes = known
        return snap


class RingBuffer:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("History capacity must be positive")
        self.capacity, self.buffer = capacity, [None] * capacity
        self.tail, self.size, self.next_sequence = 0, 0, 0
        self.lock = threading.Lock()

    def push(self, item: SystemSnapshot):
        with self.lock:
            item.sequence = self.next_sequence
            self.next_sequence += 1
            self.buffer[self.tail] = item
            self.tail = (self.tail + 1) % self.capacity
            self.size = min(self.size + 1, self.capacity)

    def get_from_newest(self, age: int):
        with self.lock:
            if age < 0 or age >= self.size:
                return None
            return self.buffer[(self.tail - 1 - age) % self.capacity]

    def view(self, sequence=None):
        """Read consistent history, reporting overwritten selections."""
        with self.lock:
            if not self.size:
                return None, [], False
            oldest, newest = self.next_sequence - self.size, self.next_sequence - 1
            expired = sequence is not None and sequence < oldest
            selected = newest if sequence is None else max(oldest, min(sequence, newest))
            snapshot = self.buffer[(self.tail - 1 - (newest - selected)) % self.capacity]
            history = [self.buffer[(self.tail - self.size + i) % self.capacity] for i in range(self.size)]
            return snapshot, history, expired


class KeyReader:
    """Decode queued bytes without blocking on incomplete escape sequences."""

    def __init__(self):
        self.pending = bytearray()
        self.escape_started = None

    def read(self):
        if IS_WINDOWS:
            while msvcrt.kbhit():
                self.pending.extend(msvcrt.getch())
        elif select.select([sys.stdin], [], [], 0)[0]:
            data = os.read(sys.stdin.fileno(), 64)
            if not data:
                return "QUIT"
            self.pending.extend(data)
        return self.decode()

    def decode(self, now=None):
        now = time.monotonic() if now is None else now
        simple = {ord("q"): "QUIT", ord(" "): "LIVE", ord("c"): "CPU", ord("m"): "MEM",
                  ord("h"): "LEFT", ord("l"): "RIGHT"}
        escapes = {b"\x1b[D": "LEFT", b"\x1b[C": "RIGHT", b"\x1b[A": "UP", b"\x1b[B": "DOWN",
                   b"\x1b[5~": "BACK10", b"\x1b[6~": "FORWARD10", b"\x1bOD": "LEFT",
                   b"\x1bOC": "RIGHT", b"\x1bOA": "UP", b"\x1bOB": "DOWN"}
        windows = {b"K": "LEFT", b"M": "RIGHT", b"H": "UP", b"P": "DOWN", b"I": "BACK10", b"Q": "FORWARD10"}
        while self.pending:
            first = self.pending[0]
            if first in (0, 224):
                if len(self.pending) >= 2:
                    key = windows.get(bytes(self.pending[1:2]))
                    del self.pending[:2]
                    self.escape_started = None
                    if key:
                        return key
                    continue
            elif first == 27:
                for sequence, key in escapes.items():
                    if self.pending.startswith(sequence):
                        del self.pending[:len(sequence)]
                        self.escape_started = None
                        return key
                if not any(sequence.startswith(self.pending) for sequence in escapes):
                    del self.pending[0]
                    self.escape_started = None
                    continue
            else:
                del self.pending[0]
                self.escape_started = None
                key = simple.get(first + 32 if 65 <= first <= 90 else first)
                if key:
                    return key
                continue
            if self.escape_started is None:
                self.escape_started = now
            if now - self.escape_started < 0.08:
                return None
            del self.pending[0]
            self.escape_started = None
        return None


def collect_history(buffer, collector, stop, status):
    while not stop.is_set():
        started = time.monotonic()
        try:
            buffer.push(collector.collect())
            with buffer.lock:
                status["error"] = None
        except Exception as exc:
            # Failed ticks must not silently freeze the UI or discard history.
            with buffer.lock:
                status["error"] = f"{type(exc).__name__}: {exc}"
        stop.wait(max(0, SAMPLE_INTERVAL - (time.monotonic() - started)))


def run_app(color=True, ascii_only=False):
    buffer, stop, status = RingBuffer(HISTORY_CAPACITY), threading.Event(), {"error": None}
    engine = threading.Thread(target=collect_history, args=(buffer, DataCollector(), stop, status), daemon=True)
    keys = KeyReader()
    sequence, sort_by, scroll, notice = None, "cpu", 0, ""
    previous_state = previous_frame = None
    sys.stdout.write("\033[?1049h\033[?25l\033[2J")
    sys.stdout.flush()
    try:
        engine.start()
        while not stop.is_set():
            latest, key = buffer.get_from_newest(0), keys.read()
            if key == "QUIT":
                break
            if key == "LIVE":
                sequence, notice, scroll = None, "", 0
            elif key in ("LEFT", "RIGHT", "BACK10", "FORWARD10") and latest:
                step = {"LEFT": -1, "RIGHT": 1, "BACK10": -10, "FORWARD10": 10}[key]
                sequence = max(0, min(latest.sequence, (latest.sequence if sequence is None else sequence) + step))
                if step > 0 and sequence == latest.sequence:
                    sequence = None
                notice, scroll = "", 0
            elif key in ("CPU", "MEM"):
                sort_by, scroll = ("cpu" if key == "CPU" else "mem"), 0
            elif key in ("UP", "DOWN"):
                scroll = max(0, scroll + (1 if key == "DOWN" else -1))
            dimensions = shutil.get_terminal_size((80, 24))
            with buffer.lock:
                error = status["error"]
            stale = int(time.monotonic() - latest.monotonic) if latest else 0
            state = (latest.sequence if latest else -1, sequence, sort_by, scroll,
                     dimensions, error, stale if stale >= 3 else 0, notice)
            if state != previous_state:
                snapshot, history, expired = buffer.view(sequence)
                if expired:
                    sequence = snapshot.sequence
                    notice = "Selected sample expired; showing oldest retained sample."
                frame, scroll = render_dashboard(snapshot, history, dimensions, live=sequence is None,
                                                sort_by=sort_by, scroll=scroll, error=error, notice=notice,
                                                stale=stale, color=color, ascii_only=ascii_only)
                if frame != previous_frame:
                    sys.stdout.write("\033[H" + "\r\n".join(line + "\033[K" for line in frame) + "\033[J")
                    sys.stdout.flush()
                    previous_frame = frame
                previous_state = state
            stop.wait(0.03)
    finally:
        stop.set()
        if engine.ident is not None:
            engine.join(timeout=1.5)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors (also respects NO_COLOR)")
    parser.add_argument("--ascii", action="store_true", help="use ASCII borders and charts")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(2, "Retromon needs an interactive terminal. Run 'python app.py' in a terminal window.\n")
    if not IS_WINDOWS and os.environ.get("TERM") == "dumb":
        parser.exit(2, "Retromon needs a terminal with ANSI cursor support.\n")
    color = not args.no_color and "NO_COLOR" not in os.environ
    encoding = (sys.stdout.encoding or "ascii").lower().replace("-", "")
    ascii_only = args.ascii or encoding not in ("utf8", "utf_8")
    if IS_WINDOWS:
        os.system("color")
        run_app(color, ascii_only)
    else:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            run_app(color, ascii_only)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def cli():
    try:
        main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
