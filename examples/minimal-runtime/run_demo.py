"""Run the minimal runtime for a few bounded rounds."""
from __future__ import annotations
import shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"

def main() -> int:
    shutil.rmtree(RUNTIME, ignore_errors=True)
    command = [sys.executable, "-m", "control_plane.runtime", "--project-root", str(ROOT), "--max-rounds", "3"]
    try:
        result = subprocess.run(command, cwd=ROOT.parent.parent, capture_output=True, text=True, timeout=15)
        print("runtime rounds: 3")
        if result.stdout: print(result.stdout, end="")
        if result.stderr: print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    finally:
        shutil.rmtree(RUNTIME, ignore_errors=True)

if __name__ == "__main__": raise SystemExit(main())
