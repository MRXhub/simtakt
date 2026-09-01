"""Tiny stand-in for a commercial simulator, deliberately process-oriented."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from pathlib import Path

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--pid-file", default="solver.pid.json")
    p.add_argument("--mode", choices=("normal", "diverge", "hang", "tree", "nonutf8"), default="normal")
    a = p.parse_args()
    root = Path(a.workspace); root.mkdir(parents=True, exist_ok=True)
    (root / a.pid_file).write_text(json.dumps({"pid": os.getpid(), "token": a.token}), encoding="utf-8")
    if a.mode == "tree":
        child = subprocess.Popen([sys.executable, __file__, "--workspace", str(root), "--token", a.token + ".child", "--pid-file", "solver.child.pid.json", "--mode", "hang"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (root / "solver.child.pid").write_text(str(child.pid), encoding="ascii")
    encoding = "cp936" if a.mode == "nonutf8" else "utf-8"
    with (root / "solver.log").open("w", encoding=encoding, errors="replace") as log:
        log.write("fake solver started\n")
        if a.mode in ("normal", "nonutf8"):
            log.write("CONVERGED: residual=0 温度\n")
            (root / "result.json").write_text(json.dumps({"value": 42, "token": a.token}), encoding="utf-8")
        elif a.mode == "diverge": log.write("diverged 收敛失败\n")
        log.flush()
        if a.mode in ("hang", "tree"):
            while True: time.sleep(1)
    return 0

if __name__ == "__main__": raise SystemExit(main())
