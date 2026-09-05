"""Disposable real backend for browser_workbench_smoke.cjs (minimal fake solver)."""
from __future__ import annotations
import json
import os
import shutil
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("project", "config", "records"):
        shutil.copytree(REPO / "examples" / "minimal-runtime" / name, root / name)
    os.chdir(root)
    from control_plane.runtime.composition import compose_runtime
    from control_plane.runtime.loop import RuntimeLoop
    from control_plane.web.status_server import StatusServer
    context = compose_runtime(root)
    server = StatusServer(("127.0.0.1", 0), middleware=context.middleware,
                          project_root=root, allow_writes=True)
    stop = threading.Event()
    loop = RuntimeLoop(context.dispatcher, min_interval=.1, max_interval=.5)
    def run():
        while not stop.wait(.1):
            if (root / "runtime.enabled").exists():
                loop.run(max_rounds=1)
    def watch():
        while not stop.wait(.1):
            if (root / "server.stop").exists():
                server.shutdown()
                return
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    threading.Thread(target=watch, daemon=True).start()
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "root": str(root)}), flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        stop.set()
        worker.join(5)
        server.server_close()
        context.close()
        (root / "runtime-errors.json").write_text(json.dumps([(name, str(error)) for name, error in loop.errors]))

if __name__ == "__main__":
    main()
