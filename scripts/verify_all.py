"""Run all integration and unit checks, including the real stdio subprocess."""

import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-v", "tests"]))
