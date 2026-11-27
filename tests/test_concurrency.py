"""Cross-process lock ownership and stale-lock recovery tests."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


class TestMindLockConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "MIND").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self):
        env = os.environ.copy()
        env["MIND_ROOT"] = str(self.root)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def _start_holder(self, stale_seconds):
        ready = self.root / "holder-ready"
        script = (
            "import time\n"
            "from pathlib import Path\n"
            "import mindlib.core as core\n"
            f"core.LOCK_STALE_SECONDS = {stale_seconds!r}\n"
            "heartbeat = core.MindLock._heartbeat\n"
            "core.MindLock._heartbeat = "
            "lambda self: heartbeat(self, interval=0.03)\n"
            "lock = core.MindLock(core.find_root())\n"
            "lock.__enter__()\n"
            f"Path({str(ready)!r}).write_text(lock._owner, encoding='ascii')\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(SRC),
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.is_file():
                return proc, ready.read_text(encoding="ascii")
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                if proc.stderr:
                    proc.stderr.close()
                self.fail(f"lock holder exited early: {stderr}")
            time.sleep(0.02)
        proc.terminate()
        proc.wait(timeout=5)
        if proc.stderr:
            proc.stderr.close()
        self.fail("lock holder did not acquire the lock")

    def _stop_holder(self, proc):
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
        if proc.stderr:
            proc.stderr.close()

    def _contend(self, stale_seconds):
        script = (
            "import mindlib.core as core\n"
            f"core.LOCK_STALE_SECONDS = {stale_seconds!r}\n"
            "try:\n"
            "    with core.MindLock(core.find_root()):\n"
            "        print('ACQUIRED')\n"
            "except RuntimeError:\n"
            "    print('BUSY')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(SRC),
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_live_heartbeat_prevents_lock_steal(self):
        proc, owner = self._start_holder(0.5)
        try:
            time.sleep(0.2)
            result = self._contend(0.5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "BUSY")
            self.assertIsNone(proc.poll(), "the lock owner must remain alive")
            self.assertEqual(
                (self.root / "mind.lock").read_text(encoding="ascii"), owner)
        finally:
            self._stop_holder(proc)

    def test_killed_holder_lock_is_stolen_after_stale_threshold(self):
        proc, _ = self._start_holder(0.15)
        self._stop_holder(proc)
        time.sleep(0.2)
        result = self._contend(0.15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ACQUIRED")
        self.assertFalse((self.root / "mind.lock").exists())


if __name__ == "__main__":
    unittest.main()
