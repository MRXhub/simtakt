import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "minimal-runtime"

# Loading minimal_components.py executes ``from control_plane... import ...``,
# so the repository root must be importable even when this module is run
# directly (python tests/test_minimal_runtime_example.py) rather than under a
# test runner that already places it on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_spec = importlib.util.spec_from_file_location(
    "_minimal_components", EXAMPLE / "minimal_components.py"
)
_minimal_components = importlib.util.module_from_spec(_spec)
sys.modules["_minimal_components"] = _minimal_components
assert _spec.loader is not None
_spec.loader.exec_module(_minimal_components)
FixedQuotaResourceMonitor = _minimal_components.FixedQuotaResourceMonitor
sys.modules.pop("_minimal_components", None)

class MinimalRuntimeExampleTests(unittest.TestCase):
    def test_end_to_end_demo(self):
        result = subprocess.run(
            [sys.executable, str(EXAMPLE / "run_demo.py")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("evaluation terminal status: qualified", result.stdout)
        self.assertFalse((EXAMPLE / ".runtime").exists())

    def test_receipt_is_readable(self):
        with tempfile.TemporaryDirectory() as td:
            monitor = FixedQuotaResourceMonitor({"config": {"runtime_dir": td}})
            artifact_id, path = monitor.record_decision({"action": "wait"}, [], [], {"processors": 4})
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())["artifact_id"], artifact_id)

    def test_atomic_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as td:
            code = ("import sys,time; sys.path.insert(0, sys.argv[1]); "
                    "from minimal_components import FixedQuotaResourceMonitor as M; "
                    "m=M({'config':{'runtime_dir':sys.argv[2]}}); "
                    "exec('with m.locked_snapshot(\\'x\\'):\\n time.sleep(.15)')")
            args = [sys.executable, "-c", code, str(EXAMPLE), td]
            first = subprocess.Popen(args)
            second = subprocess.Popen(args)
            self.assertEqual(first.wait(timeout=5), 0)
            self.assertEqual(second.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
