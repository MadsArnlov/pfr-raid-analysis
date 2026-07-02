#!/usr/bin/env python3
"""
One-command refresh: fetch the latest FFLogs data, then rebuild both the
full-history dashboard and the latest-night report.

    uv run src/refresh.py                 # fetch + build both
    uv run src/refresh.py --skip-fetch    # rebuild both from the existing pulls.csv

Any other arguments are passed through to the two build scripts (e.g.
--utc-offset 1 in winter). Each underlying script remains runnable on its
own — this just chains them.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def run(script: str, *extra_args: str) -> None:
    cmd = [sys.executable, str(SCRIPT_DIR / script), *extra_args]
    print(f"\n=== {' '.join(cmd[1:])} ===")
    subprocess.run(cmd, check=True)


def main():
    args = sys.argv[1:]
    skip_fetch = "--skip-fetch" in args
    build_args = [a for a in args if a != "--skip-fetch"]

    if not skip_fetch:
        run("main.py")
    run("build_dashboard.py", *build_args)
    run("build_night_report.py", *build_args)


if __name__ == "__main__":
    main()
