import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psutil
import app
from terminal_ui import ANSI, cell_width, render_dashboard, sparkline


def snapshot(cpu=25.0, processes=None, clock=10.0):
    return app.SystemSnapshot(cpu, 40.0, processes or [], monotonic=clock,
                              memory_used=2 * 1024 ** 3, memory_total=8 * 1024 ** 3)


def process(pid, cpu=10.0, mem=1.0, name="worker", created=1.0, running=True):
    return SimpleNamespace(info={"pid": pid, "cpu_percent": cpu, "memory_percent": mem,
                                 "create_time": created}, name=lambda: name, is_running=lambda: running)


class HistoryTests(unittest.TestCase):
    def test_empty_and_invalid_reads(self):
        buffer = app.RingBuffer(3)
        self.assertIsNone(buffer.get_from_newest(0))
        buffer.push(snapshot())
        self.assertIsNone(buffer.get_from_newest(-1))
        self.assertIsNone(buffer.get_from_newest(1))
        self.assertEqual(app.RingBuffer(3).view(), (None, [], False))
        with self.assertRaises(ValueError):
            app.RingBuffer(0)

    def test_wraparound_retains_only_newest_samples(self):
        buffer = app.RingBuffer(3)
        for value in range(8):
            buffer.push(snapshot(cpu=value))
        newest, history, _ = buffer.view()
        self.assertEqual([s.global_cpu for s in history], [5, 6, 7])
        self.assertEqual(newest.sequence, 7)
        self.assertEqual(buffer.size, 3)

    def test_rewind_selection_does_not_drift(self):
        buffer = app.RingBuffer(5)
        for value in range(3):
            buffer.push(snapshot(clock=value * 2.5))
        selected, _, _ = buffer.view(1)
        buffer.push(snapshot(clock=12))
        again, _, expired = buffer.view(selected.sequence)
        self.assertIs(again, selected)
        self.assertFalse(expired)

    def test_expired_selection_is_explicit(self):
        buffer = app.RingBuffer(2)
        for value in range(4):
            buffer.push(snapshot(cpu=value))
        selected, _, expired = buffer.view(0)
        self.assertTrue(expired)
        self.assertEqual(selected.sequence, 2)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.cpu = patch.object(app.psutil, "cpu_percent", return_value=30).start()
        patch.object(app.psutil, "virtual_memory", return_value=SimpleNamespace(
            percent=40, total=8000, available=4800)).start()
        self.rows = patch.object(app.psutil, "process_iter", return_value=[process(1)]).start()
        self.addCleanup(patch.stopall)

    def test_first_cpu_read_is_unavailable_then_becomes_measured(self):
        collector = app.DataCollector()
        first = collector.collect()
        self.assertIsNone(first.global_cpu)
        self.assertIsNone(first.processes[0]["cpu"])
        second = collector.collect()
        self.assertEqual(second.global_cpu, 30)
        self.assertEqual(second.processes[0]["cpu"], 10)

    def test_missing_fields_do_not_crash(self):
        self.rows.return_value = [process(1, cpu=None, mem=None, name=None, created=None)]
        collector = app.DataCollector()
        collector.collect()
        row = collector.collect().processes[0]
        self.assertEqual(row["name"], "<unavailable>")
        self.assertIsNone(row["mem"])
        self.assertIsNone(row["cpu"])

    def test_pid_reuse_does_not_inherit_cpu_baseline(self):
        collector = app.DataCollector()
        collector.collect()
        self.rows.return_value = [process(1, created=2.0)]
        self.assertIsNone(collector.collect().processes[0]["cpu"])

    def test_cached_reused_process_is_not_retained(self):
        self.rows.return_value = [process(1, running=False)]
        self.assertEqual(app.DataCollector().collect().processes, [])

    def test_name_lookup_permission_and_exit_races_are_safe(self):
        for exception in (psutil.AccessDenied(1), psutil.NoSuchProcess(1)):
            with self.subTest(exception=type(exception).__name__):
                row = process(1)
                def unavailable():
                    raise exception
                row.name = unavailable
                self.rows.return_value = [row]
                retained = app.DataCollector().collect().processes
                if isinstance(exception, psutil.AccessDenied):
                    self.assertEqual(retained[0]["name"], "<unavailable>")
                else:
                    self.assertEqual(retained, [])

    def test_memory_hog_is_retained_behind_cpu_leaders(self):
        self.rows.return_value = [process(i, cpu=i + 10) for i in range(25)] + [process(99, cpu=0, mem=70)]
        collector = app.DataCollector()
        collector.collect()
        result = collector.collect()
        self.assertIn(99, [p["pid"] for p in result.processes])
        self.assertEqual(result.process_count, 26)

    def test_leader_union_is_bounded(self):
        self.rows.return_value = [process(i, cpu=100-i, mem=i) for i in range(100)]
        collector = app.DataCollector()
        collector.collect()
        self.assertEqual(len(collector.collect().processes), 40)

    def test_disappearing_process_is_skipped(self):
        class Vanished:
            @property
            def info(self):
                raise psutil.NoSuchProcess(123)
        self.rows.return_value = [Vanished(), process(1)]
        self.assertEqual(app.DataCollector().collect().process_count, 1)


class InputTests(unittest.TestCase):
    def test_queued_keys_are_not_stranded(self):
        reader = app.KeyReader()
        reader.pending.extend(b" mq")
        self.assertEqual([reader.decode() for _ in range(4)], ["LIVE", "MEM", "QUIT", None])

    def test_lone_escape_times_out_without_blocking(self):
        reader = app.KeyReader()
        reader.pending.extend(b"\x1b")
        self.assertIsNone(reader.decode(now=0))
        self.assertIsNone(reader.decode(now=0.1))
        reader.pending.extend(b"q")
        self.assertEqual(reader.decode(now=0.2), "QUIT")

    def test_fragmented_arrow_sequence(self):
        reader = app.KeyReader()
        reader.pending.extend(b"\x1b[")
        self.assertIsNone(reader.decode(now=0))
        reader.pending.extend(b"D")
        self.assertEqual(reader.decode(now=0.02), "LEFT")

    def test_arrow_page_and_windows_key_sequences(self):
        reader = app.KeyReader()
        reader.pending.extend(b"\x1b[C\x1b[5~\x1b[6~\xe0K\xe0Mq")
        self.assertEqual([reader.decode() for _ in range(6)],
                         ["RIGHT", "BACK10", "FORWARD10", "LEFT", "RIGHT", "QUIT"])

    def test_unknown_escape_does_not_swallow_quit(self):
        reader = app.KeyReader()
        reader.pending.extend(b"\x1bq")
        self.assertEqual(reader.decode(), "QUIT")


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.buffer = app.RingBuffer(600)
        rows = [{"pid": i, "name": f"worker-{i}", "cpu": float(i), "mem": float(40-i)} for i in range(40)]
        for i, clock in enumerate([0, 2, 9]):
            self.buffer.push(snapshot(cpu=10 * i, processes=rows, clock=clock))
        self.snap, self.history, _ = self.buffer.view()

    def draw(self, size=(80, 24), **kwargs):
        return render_dashboard(self.snap, self.history, size, **kwargs)

    def test_layout_fits_supported_and_tiny_windows(self):
        for columns, rows in [(140, 40), (100, 32), (80, 24), (44, 18), (30, 10), (2, 2)]:
            with self.subTest(size=(columns, rows)):
                lines, _ = self.draw((columns, rows))
                self.assertLessEqual(len(lines), rows - 1)
                self.assertTrue(all(cell_width(line) <= columns - 1 for line in lines))
                if columns >= 44 and rows >= 18:
                    self.assertIn("Quit" if columns >= 73 else "quit", "\n".join(lines))
                    self.assertTrue(ANSI.sub("", lines[-1]).rstrip().endswith("\u256f"))

    def test_rewind_uses_elapsed_time_not_sample_count(self):
        lines, _ = render_dashboard(self.history[0], self.history, (80,24), live=False, color=False)
        self.assertIn("9s behind newest", "\n".join(lines))

    def test_memory_sort_and_scroll_reach_all_saved_rows(self):
        lines, _ = self.draw(sort_by="mem", color=False)
        self.assertIn("worker-0 ", "\n".join(lines))
        lines, scroll = self.draw(scroll=999, color=False)
        self.assertGreater(scroll, 0)
        self.assertIn("worker-0 ", "\n".join(lines))

    def test_plain_ascii_mode_and_terminal_injection(self):
        self.snap.processes[0]["name"] = "bad\x1b[2J\n\u4e2d\u6587"
        lines, _ = self.draw(sort_by="mem", color=False, ascii_only=True)
        text = "\n".join(lines)
        self.assertNotIn("\x1b", text)
        self.assertTrue(text.isascii())

    def test_wide_process_names_keep_table_aligned(self):
        self.snap.processes[0]["name"] = "\u4e2d\u6587" * 30
        lines, _ = self.draw(sort_by="mem")
        self.assertTrue(all(cell_width(line) == 79 for line in lines))

    def test_startup_error_and_stale_states(self):
        for kwargs, expected in [({"error": "OSError: unavailable"}, "ERROR"), ({"stale": 4}, "STALE")]:
            lines, _ = self.draw(**kwargs)
            self.assertIn(expected, "\n".join(lines))
        lines, _ = render_dashboard(None, [], (100,32), error="OSError: unavailable")
        self.assertIn("ERROR", "\n".join(lines))
        self.assertIn("CPU  --", "\n".join(lines))

    def test_sparkline_preserves_short_peaks(self):
        self.assertEqual(sparkline([0, 100, 0, 0], 2, ascii_only=True), "@.")


class WorkerTests(unittest.TestCase):
    def test_collector_error_is_reported_then_recovers(self):
        buffer, stop, status = app.RingBuffer(3), threading.Event(), {"error": None}
        failed, allow_retry = threading.Event(), threading.Event()
        class Collector:
            calls = 0
            def collect(self):
                self.calls += 1
                if self.calls == 1:
                    failed.set()
                    raise OSError("temporary failure")
                allow_retry.wait(1)
                stop.set()
                return snapshot()
        worker = threading.Thread(target=app.collect_history, args=(buffer, Collector(), stop, status))
        with patch.object(app, "SAMPLE_INTERVAL", 0.01):
            worker.start()
            try:
                self.assertTrue(failed.wait(1))
                deadline = time.monotonic() + 1
                while status["error"] is None and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertIn("temporary failure", status["error"])
                allow_retry.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(status["error"])
                self.assertEqual(buffer.size, 1)
            finally:
                stop.set()
                allow_retry.set()
                worker.join(1)


if __name__ == "__main__":
    unittest.main()
