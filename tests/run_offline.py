"""Run the entire suite with network attempts blocked and audited in children."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="lofin-offline-") as td:
        marker = Path(td) / "attempts.txt"
        env = dict(os.environ)
        env["LOFIN_OFFLINE_ATTEMPTS"] = str(marker)
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(ROOT / "tests" / "offline_guard"),
                str(ROOT),
                env.get("PYTHONPATH", ""),
            ]
        )
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT,
            env=env,
        )
        if marker.exists():
            print(marker.read_text(), file=sys.stderr)
            return 1
        print("Offline audit: no network attempts.")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
