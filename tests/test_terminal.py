import os
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LaunchTests(unittest.TestCase):
    def test_noninteractive_launch_has_actionable_error(self):
        result = subprocess.run([sys.executable, str(ROOT / "app.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("interactive terminal", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


@unittest.skipIf(os.name == "nt", "POSIX PTY test; native Windows console requires manual verification")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        self.fcntl, self.select, self.struct, self.termios, self.time = fcntl, select, struct, termios, time
        self.master, self.slave = pty.openpty()
        self.resize(80, 24)
        self.settings = termios.tcgetattr(self.slave)
        self.child = subprocess.Popen([sys.executable, "-u", str(ROOT / "app.py")],
                                      stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                      cwd=ROOT, env={**os.environ, "TERM": "xterm-256color"})

    def tearDown(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()
        os.close(self.master)
        os.close(self.slave)

    def resize(self, columns, rows):
        self.fcntl.ioctl(self.slave, self.termios.TIOCSWINSZ, self.struct.pack("HHHH", rows, columns, 0, 0))

    def read_for(self, seconds):
        data = bytearray()
        deadline = self.time.monotonic() + seconds
        while self.time.monotonic() < deadline:
            timeout = max(0, deadline - self.time.monotonic())
            if self.select.select([self.master], [], [], timeout)[0]:
                data.extend(os.read(self.master, 65536))
        return data.decode("utf-8", errors="replace")

    def plain(self, text):
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)

    def wait_for_exit(self):
        # TCSADRAIN can wait for the PTY reader during terminal restoration.
        output = []
        deadline = self.time.monotonic() + 3
        while self.child.poll() is None:
            if self.time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.child.args, 3)
            output.append(self.read_for(0.05))
        output.append(self.read_for(0.05))
        return "".join(output)

    def assert_terminal_restored(self):
        actual = self.termios.tcgetattr(self.slave)
        expected = self.settings.copy()
        if sys.platform == "darwin":
            # Darwin sets PENDIN when ICANON is restored; it is input queue state.
            # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/tty.c
            actual[3] &= ~self.termios.PENDIN
            expected[3] &= ~self.termios.PENDIN
        self.assertEqual(actual, expected)

    def test_navigation_resize_escape_and_terminal_restoration(self):
        first = self.read_for(2.2)
        self.assertIn("[LIVE]", self.plain(first))
        os.write(self.master, b"\x1b[D")
        rewind = self.plain(self.read_for(0.2))
        clocks = re.findall(r"(\d\d:\d\d:\d\d)\s+\[REWIND\]", rewind)
        self.assertTrue(clocks)
        selected_clock = clocks[-1]
        later = self.plain(self.read_for(1.3))
        self.assertIn(selected_clock + "  [REWIND]", later)
        os.write(self.master, b"\x1bm ")
        resumed = self.plain(self.read_for(0.25))
        self.assertIn("[LIVE]", resumed)
        self.assertIn("MEM sort", resumed)
        self.resize(30, 10)
        self.assertIn("Window too small", self.plain(self.read_for(0.2)))
        self.resize(100, 32)
        restored = self.read_for(0.2)
        self.assertIn("SYSTEM RECORDER", restored)
        for frame in restored.split("\x1b[H")[1:]:
            self.assertLessEqual(frame.count("\n"), 30)
        os.write(self.master, b"q")
        output = self.wait_for_exit()
        self.assertEqual(self.child.returncode, 0)
        self.assertIn("\x1b[?25h", output)
        self.assertIn("\x1b[?1049l", output)
        self.assert_terminal_restored()

    def test_interrupt_restores_terminal(self):
        import signal
        self.read_for(0.4)
        self.child.send_signal(signal.SIGINT)
        output = self.wait_for_exit()
        self.assertEqual(self.child.returncode, 0)
        self.assertIn("\x1b[?25h", output)
        self.assert_terminal_restored()


if __name__ == "__main__":
    unittest.main()
