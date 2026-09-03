import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import control_plane.runtime.composition as comp
from control_plane.runtime.composition import RuntimeCompositionError, compose_runtime


class CompositionTests(unittest.TestCase):
    def root(self, document=None, topology=None):
        td = tempfile.TemporaryDirectory(); root = Path(td.name)
        (root / "project").mkdir()
        if document is not None:
            (root / "project" / "RUNTIME_COMPONENTS.json").write_text(json.dumps(document), encoding="utf8")
        (root / "project" / "EXECUTION_TOPOLOGY.json").write_text(json.dumps(topology or {"formal_target_ids": ["one"]}), encoding="utf8")
        self.addCleanup(td.cleanup)
        return root

    def doc(self, worker="worker_factory", monitor="monitor_factory"):
        return {"schema_version": 1, "components": [
            {"name": "worker", "module": "runtime_test_components", "factory": worker, "interface_version": 1},
            {"name": "resource_monitor", "module": "runtime_test_components", "factory": monitor, "interface_version": 1},
        ]}

    def install(self, worker=None, monitor=None):
        mod = types.ModuleType("runtime_test_components")
        def wf(entry): return worker if worker is not None else self.good_worker()
        def mf(entry): return monitor if monitor is not None else self.good_monitor()
        mod.worker_factory = wf; mod.monitor_factory = mf
        sys.modules[mod.__name__] = mod
        self.addCleanup(lambda: sys.modules.pop(mod.__name__, None))

    @staticmethod
    def good_worker():
        return types.SimpleNamespace(start_session=lambda: None, resume_session=lambda: None,
            observe_session=lambda: None, collect_session=lambda: None, terminate_session=lambda: None)

    @staticmethod
    def good_monitor():
        return types.SimpleNamespace(locked_snapshot=lambda: None, record_decision=lambda: None,
            locked_dispatch=lambda: None)

    def compose(self, root, formal_target_ids=None):
        patches = [patch.object(
            comp, "parse_execution_topology",
            return_value={"formal_target_ids": formal_target_ids or ["one"]}),
            patch.object(comp, "resolve_control_plane_database", return_value=root / "x.db"),
            patch.object(comp, "resolve_governed_scheduling_policy", return_value=object()),
            patch.object(
                comp, "SQLiteEvaluationRepository",
                return_value=types.SimpleNamespace(),
            ),
            patch.object(comp, "EvaluationMiddleware", return_value=object()),
            patch.object(
                comp, "PreparedExecutionDispatcher",
                return_value=types.SimpleNamespace(),
            )]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return compose_runtime(root)

    def test_missing_bad_json_and_missing_field(self):
        with self.assertRaisesRegex(RuntimeCompositionError, "missing project/RUNTIME_COMPONENTS.json"):
            compose_runtime(self.root())
        r = self.root(); (r / "project/RUNTIME_COMPONENTS.json").write_text("{bad", encoding="utf8")
        with self.assertRaisesRegex(RuntimeCompositionError, "not valid JSON"): compose_runtime(r)
        with self.assertRaisesRegex(RuntimeCompositionError, "schema_version"):
            compose_runtime(self.root({"components": []}))
        missing_field = {"schema_version": 1, "components": [
            {"name": "worker", "module": "x", "factory": "f"},
            {"name": "resource_monitor", "module": "x", "factory": "f", "interface_version": 1}]}
        with self.assertRaisesRegex(RuntimeCompositionError, "worker missing field interface_version"):
            compose_runtime(self.root(missing_field))

    def test_load_errors_name_component(self):
        bad = self.doc(); bad["components"][0]["module"] = "does_not_exist"
        with self.assertRaisesRegex(RuntimeCompositionError, "worker.*module import failed"): compose_runtime(self.root(bad))
        self.install(); bad = self.doc(worker="not_callable"); sys.modules["runtime_test_components"].not_callable = 1
        with self.assertRaisesRegex(RuntimeCompositionError, "worker.*not callable"): compose_runtime(self.root(bad))
        self.install(worker=RuntimeError("unused"))
        sys.modules["runtime_test_components"].worker_factory = lambda e: (_ for _ in ()).throw(ValueError("x"))
        with self.assertRaisesRegex(RuntimeCompositionError, "worker.*construction failed"): compose_runtime(self.root(self.doc()))

    def test_required_optional_methods_and_summary(self):
        missing = types.SimpleNamespace(start_session=lambda: None, resume_session=lambda: None, observe_session=lambda: None)
        self.install(worker=missing, monitor=self.good_monitor())
        with self.assertRaisesRegex(RuntimeCompositionError, "worker.*collect_session.*runtime_test_components:worker_factory"):
            self.compose(self.root(self.doc()))
        optional = types.SimpleNamespace(start_session=lambda: None, resume_session=lambda: None,
            observe_session=lambda: None, collect_session=lambda: None)
        self.install(worker=optional, monitor=self.good_monitor())
        ctx = self.compose(self.root(self.doc()))
        self.assertEqual(ctx.assembly_summary["worker"]["termination"], "无法确认终止")
        ctx.close()

    def test_locked_dispatch_required_only_for_multiple_targets(self):
        monitor = types.SimpleNamespace(locked_snapshot=lambda: None, record_decision=lambda: None)
        self.install(worker=self.good_worker(), monitor=monitor)
        with self.assertRaisesRegex(RuntimeCompositionError, "resource_monitor.*locked_dispatch"):
            self.compose(self.root(self.doc()), formal_target_ids=["a", "b"])
        ctx = self.compose(self.root(self.doc()), formal_target_ids=["a"])
        ctx.close()

    def test_lifecycle_open_rollback_and_close_errors(self):
        events = []
        def obj(name, fail_open=False, fail_close=False):
            def open_():
                events.append("open:" + name)
                if fail_open:
                    raise ValueError("open")
            def close_():
                events.append("close:" + name)
                if fail_close:
                    raise ValueError("close")
            return types.SimpleNamespace(
                start_session=lambda: None, resume_session=lambda: None,
                observe_session=lambda: None, collect_session=lambda: None,
                locked_snapshot=lambda: None, record_decision=lambda: None,
                open=open_, close=close_)
        w = obj("w"); m = obj("m")
        self.install(worker=w, monitor=m)
        sys.modules["runtime_test_components"].extra_factory = lambda entry: obj("x", fail_open=True)
        document = self.doc()
        document["components"].append({"name": "extra", "module": "runtime_test_components",
            "factory": "extra_factory", "interface_version": 1})
        with self.assertRaises(RuntimeCompositionError):
            self.compose(self.root(document))
        self.assertEqual(events, ["open:w", "open:m", "open:x", "close:x", "close:m", "close:w"])
        events.clear(); w = obj("w", fail_close=True); m = obj("m")
        self.install(worker=w, monitor=m)
        ctx = self.compose(self.root(self.doc()))
        with self.assertRaises(ValueError):
            ctx.close()
        self.assertEqual(events.count("close:w"), 1)
        self.assertEqual(events.count("close:m"), 1)

    def test_module_entrypoint_reports_human_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-m", "control_plane.runtime", "--project-root", td], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(p.returncode, 0); self.assertIn("runtime cannot start", p.stderr); self.assertNotIn("Traceback", p.stderr)

    def _composed_dispatcher_ids(self, root):
        """Compose the runtime, capturing every dispatcher_id handed to the dispatcher."""
        captured = []
        def fake_dispatcher(*args, **kwargs):
            captured.append(kwargs.get("dispatcher_id"))
            return types.SimpleNamespace()
        patches = [
            patch.object(comp, "parse_execution_topology", return_value={"formal_target_ids": ["one"]}),
            patch.object(comp, "resolve_control_plane_database", return_value=root / "x.db"),
            patch.object(comp, "resolve_governed_scheduling_policy", return_value=object()),
            patch.object(comp, "SQLiteEvaluationRepository", return_value=types.SimpleNamespace()),
            patch.object(comp, "EvaluationMiddleware", return_value=object()),
            patch.object(comp, "PreparedExecutionDispatcher", fake_dispatcher),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ctx = compose_runtime(root)
        ctx.close()
        return captured

    def test_dispatcher_id_default_is_process_scoped_and_differs_across_assemblies(self):
        # With no explicit override the dispatcher gets an id of the form
        # ``runtime:<host>:<pid>:<8hex>`` and each assembly draws a fresh random
        # suffix, so two constructions never share an id.
        self.install()
        root = self.root(self.doc())
        ids = self._composed_dispatcher_ids(root)
        ids += self._composed_dispatcher_ids(root)
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        pattern = re.compile(
            r"^runtime:" + re.escape(socket.gethostname()) + r":\d+:[0-9a-f]{8}$"
        )
        self.assertTrue(all(pattern.match(item) for item in ids))
        self.assertTrue(all(item.startswith(f"runtime:{socket.gethostname()}:{os.getpid()}:") for item in ids))

    def test_dispatcher_id_explicit_override_wins(self):
        # A top-level dispatcher_id in RUNTIME_COMPONENTS.json must replace the
        # generated process-unique id.
        self.install()
        document = self.doc()
        document["dispatcher_id"] = "explicit-dispatcher-deployment-42"
        root = self.root(document)
        ids = self._composed_dispatcher_ids(root)
        self.assertEqual(ids, ["explicit-dispatcher-deployment-42"])

    def test_dispatcher_id_empty_override_is_rejected(self):
        # An override that strips to empty must fail closed rather than silently
        # falling back to a generated id.
        document = self.doc()
        document["dispatcher_id"] = "   "
        with self.assertRaisesRegex(RuntimeCompositionError, "dispatcher_id override must be non-empty"):
            comp._dispatcher_id(self.root(document), [])


if __name__ == "__main__": unittest.main()
