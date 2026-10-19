"""Run all release/privacy gates before pushing this repository's HEAD to origin/main."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
for args in ([sys.executable, '-B', 'tools/check_release.py', '--history', '--self-test'],
             [sys.executable, '-B', 'tools/run_secret_scan.py'],
             [sys.executable, '-B', 'build_release.py']):
    subprocess.run(args, cwd=root, check=True)
subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=root, check=True)
