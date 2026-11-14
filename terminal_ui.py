"""Pure terminal layout: no sampling, input, or third-party UI framework."""

from datetime import datetime
import re
import unicodedata

ANSI = re.compile(r"(\x1b\[[0-9;]*m)")
COLORS = {"title": "96", "good": "92", "warning": "93", "error": "91",
          "text": "97", "muted": "37", "border": "90"}


def clean_text(text):
    # Never allow process names or exception messages to control the terminal.
    return "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(text))


def cell_width(text):
    text = ANSI.sub("", text) if "\033" in text else text
    if text.isascii():
        return len(text)
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def fit(text, width):
    measured = cell_width(text)
    if measured <= width:
        return text + " " * (width - measured)
    output, used = [], 0
    for part in ANSI.split(text):
        if ANSI.fullmatch(part):
            output.append(part)
            continue
        for char in part:
            size = 1 if char.isascii() else 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if used + size > width:
                return "".join(output) + ("\033[0m" if "\033" in text else "") + " " * (width - used)
            output.append(char)
            used += size
    return "".join(output) + " " * max(0, width - used)


def percent(value):
    return "--" if value is None else f"{value:.1f}%"


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def memory_size(value):
    return f"{value / (1024 ** 3):.1f} GiB"


def sparkline(values, width, ascii_only=False):
    glyphs = "._-=+*#@" if ascii_only else "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    result = []
    count = min(width, len(values))
    for column in range(count):
        available = [v for v in values[column * len(values) // count:(column + 1) * len(values) // count] if v is not None]
        result.append(glyphs[min(7, int(max(available) / 100 * 7))] if available else " ")
    return "".join(result)


def render_dashboard(snapshot, history, size, *, live=True, sort_by="cpu", scroll=0,
                     error=None, notice="", stale=0, color=True, ascii_only=False):
    columns, rows = size
    width, height = min(120, max(1, columns - 1)), max(1, rows - 1)

    def paint(text, role="text", bold=False):
        return f"\033[{('1;' if bold else '')}{COLORS[role]}m{text}\033[0m" if color else text

    def safe(text):
        text = clean_text(text)
        return text.encode("ascii", errors="replace").decode("ascii") if ascii_only else text

    if width < 43 or height < 17:
        lines = [paint("RETROMON", "title", True), "Window too small: resize to at least 44 x 18.",
                 "Recording continues in the background.", "Q quit | Space live"]
        return [fit(line, width) for line in lines[:height]], 0

    inner = width - 4
    left = "|" if ascii_only else "\u2502"
    horizontal = "-" if ascii_only else "\u2500"
    corners = ("+", "+", "+", "+") if ascii_only else ("\u256d", "\u256e", "\u2570", "\u256f")

    def box(text=""):
        return paint(left, "border") + " " + fit(text, inner) + " " + paint(left, "border")

    def separated(first, second):
        return first + " " * max(1, inner - cell_width(first) - cell_width(second)) + second

    def meter(value, length):
        filled = 0 if value is None else round(min(100, max(0, value)) / 100 * length)
        active, empty = ("#", ".") if ascii_only else ("\u2588", "\u2591")
        role = "warning" if value is not None and value >= 80 else "good"
        return paint(active * filled, role) + paint(empty * (length - filled), "border")

    divider = paint(("+" if ascii_only else "\u251c") + horizontal * (width - 2)
                    + ("+" if ascii_only else "\u2524"), "border")
    frame = [paint(corners[0] + horizontal * (width - 2) + corners[1], "border")]
    if error:
        state, state_role = "ERROR", "error"
    elif stale >= 3:
        state, state_role = "STALE", "warning"
    else:
        state, state_role = ("LIVE", "good") if live else ("REWIND", "warning")
    clock = datetime.fromtimestamp(snapshot.timestamp).strftime("%H:%M:%S") if snapshot else "--:--:--"
    frame.append(box(separated(paint("RETROMON", "title", True), paint(f"{clock}  [{state}]", state_role, True))))
    if height >= 26:
        frame.append(box(paint("SYSTEM RECORDER  /  Your last ten minutes, within reach.", "muted")))
    frame.append(divider)
    cpu, mem = (snapshot.global_cpu, snapshot.global_mem) if snapshot else (None, None)
    column = (inner - 3) // 2
    frame.append(box(fit(paint(f"CPU  {percent(cpu)}", "text", True), column) + "   "
                     + paint(f"RAM  {percent(mem)}", "text", True)))
    frame.append(box(meter(cpu, column) + "   " + meter(mem, inner - column - 3)))
    if height >= 26:
        recorded = [s.global_cpu for s in history if s.sequence <= snapshot.sequence and snapshot.monotonic - s.monotonic <= 60]
        trend = sparkline(recorded, max(1, column - 4), ascii_only)
        detail = f"{memory_size(snapshot.memory_used)} / {memory_size(snapshot.memory_total)}" if snapshot else "Measuring..."
        frame.append(box(fit(paint("CPU " + trend, "title"), column) + "   " + paint(detail, "muted")))
    frame.append(divider)

    if history:
        earliest, latest = history[0], history[-1]
        span, age = latest.monotonic - earliest.monotonic, latest.monotonic - snapshot.monotonic
        label = "At newest sample" if live else f"{duration(age)} behind newest"
        frame.append(box(separated(paint("HISTORY", "title", True), paint(f"{len(history)}/600  |  {label}", "muted"))))
        track_width = max(1, inner - 15)
        position = round((snapshot.monotonic - earliest.monotonic) / max(0.001, span) * (track_width - 1))
        marker = "@" if ascii_only else "\u25cf"
        track = horizontal * position + marker + horizontal * (track_width - position - 1)
        frame.append(box(paint(track, state_role) + paint(f"  {duration(span)} kept", "muted")))
    else:
        frame.append(box(paint("HISTORY  /  Building your first snapshot...", "title")))
        frame.append(box(paint("CPU deltas arrive after the next sample.", "muted")))
    frame.append(divider)

    processes = sorted(snapshot.processes, key=lambda p: p[sort_by] if p[sort_by] is not None else -1, reverse=True) if snapshot else []
    fixed_rows = 17 if height >= 26 else 15
    visible = height - fixed_rows
    scroll = min(scroll, max(0, len(processes) - max(1, visible)))
    end = min(len(processes), scroll + visible)
    pid_width, name_width = 8, inner - 28
    header = f"{'PID':>{pid_width}}  {'PROCESS':<{name_width}}  {'CPU %':>7}  {'RAM %':>7}"
    frame.append(box(paint(header, "title", True)))
    for process in processes[scroll:end]:
        name = fit(safe(process["name"]), name_width)
        row = f"{process['pid']:>{pid_width}}  {name}  {percent(process['cpu']):>7}  {percent(process['mem']):>7}"
        frame.append(box(paint(row, "warning" if (process[sort_by] or 0) >= 80 else "text")))
    for index in range(max(0, visible - (end - scroll))):
        frame.append(box(paint("Waiting for process data...", "muted") if not snapshot and index == 0 else ""))
    feedback = safe(error) if error else safe(notice)
    if not feedback:
        feedback = f"Last sample {stale}s ago; collector delayed." if stale >= 3 else (
            f"{snapshot.process_count if snapshot else 0} scanned  |  {scroll + 1 if end else 0}-{end}/{len(processes)} saved  |  {sort_by.upper()} sort  |  -- = measuring")
    frame.append(box(paint(feedback, "error" if error else "warning" if notice or stale >= 3 else "muted")))
    frame.append(divider)
    if inner >= 68:
        frame.append(box(paint("Left/Right History   PgUp/PgDn 10 samples   Space Live" if ascii_only else "\u2190/\u2192 History   PgUp/PgDn 10 samples   Space Live", "muted")))
        frame.append(box(paint("Up/Down Scroll      C CPU / M RAM         Q Quit" if ascii_only else "\u2191/\u2193 Scroll    C CPU / M RAM         Q Quit", "muted")))
    else:
        frame.append(box(paint("H/L history  Space live  Q quit", "muted")))
        frame.append(box(paint("Up/Down scroll  C CPU / M RAM", "muted")))
    frame.append(paint(corners[2] + horizontal * (width - 2) + corners[3], "border"))
    return [fit(line, width) for line in frame[:height]], scroll
