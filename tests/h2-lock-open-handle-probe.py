"""Probe Windows rename behavior while another process has the lock file open."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if os.name != "nt":
        print("probe requires Windows")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "mind.lock"
        stale = Path(tmp) / "mind.lock.stale"
        lock.write_text("owner", encoding="ascii")
        fd = os.open(str(lock), os.O_RDONLY)
        code = """import os,sys
try:
    os.replace(sys.argv[1], sys.argv[2])
    print('renamed')
except OSError as error:
    print(type(error).__name__ + ': ' + str(error))
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, str(lock), str(stale)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        finally:
            os.close(fd)
        if result.returncode:
            print(result.stderr.strip())
            return result.returncode
        print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
