from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from control_plane.evaluation.automation_policy import (
    DEFAULT_AUTOMATION_POLICY,
    AutomationPolicyError,
    resolve_automation_policy,
)


class AutomationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "project").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        (self.root / "project" / "AUTOMATION_POLICY.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_missing_file_returns_complete_builtin_default(self):
        self.assertEqual(resolve_automation_policy(self.root), DEFAULT_AUTOMATION_POLICY)

    def test_invalid_json_fails_closed(self):
        (self.root / "project" / "AUTOMATION_POLICY.json").write_text("{", encoding="utf-8")
        with self.assertRaises(AutomationPolicyError):
            resolve_automation_policy(self.root)

    def test_unknown_top_level_and_profile_fields_fail_closed(self):
        for value in (
            {**DEFAULT_AUTOMATION_POLICY, "extra": True},
            {**DEFAULT_AUTOMATION_POLICY, "profiles": {**DEFAULT_AUTOMATION_POLICY["profiles"], "assisted": {"timeout_transient_rerun": True, "pathological_point": "report-and-wait", "extra": 1}}},
        ):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(AutomationPolicyError):
                    resolve_automation_policy(self.root)

    def test_bad_profile_reference_and_types_fail_closed(self):
        cases = [
            {**DEFAULT_AUTOMATION_POLICY, "default_profile": "missing"},
            {**DEFAULT_AUTOMATION_POLICY, "platform": {**DEFAULT_AUTOMATION_POLICY["platform"], "requeue_limit": "2"}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(AutomationPolicyError):
                    resolve_automation_policy(self.root)

    def test_valid_file_overrides_default(self):
        value = json.loads(json.dumps(DEFAULT_AUTOMATION_POLICY))
        value["platform"]["requeue_limit"] = 4
        value["default_profile"] = "manual"
        self.write(value)
        loaded = resolve_automation_policy(self.root)
        self.assertEqual(loaded["platform"]["requeue_limit"], 4)
        self.assertEqual(loaded["default_profile"], "manual")
        self.assertEqual(set(loaded), {"platform", "profiles", "default_profile"})


if __name__ == "__main__":
    unittest.main()
