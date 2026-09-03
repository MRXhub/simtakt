"""Command line entry point for the runtime."""
from __future__ import annotations

import argparse
import sys

from .composition import RuntimeCompositionError, compose_runtime
from .loop import RuntimeLoop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m control_plane.runtime")
    parser.add_argument("--project-root", required=True, help="project workspace root")
    parser.add_argument("--max-rounds", type=int, default=None)
    args = parser.parse_args(argv)
    context = None
    try:
        context = compose_runtime(args.project_root)
        RuntimeLoop(context.dispatcher).run(max_rounds=args.max_rounds)
        return 0
    except (RuntimeCompositionError, ValueError, OSError) as exc:
        print(f"runtime cannot start: {exc}", file=sys.stderr)
        return 2
    finally:
        if context is not None:
            context.exit_stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
