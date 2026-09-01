"""Comprehensive focused tests for deck parameter parsing/rewriting and ParameterSchema contracts."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from control_plane.core.evaluation_contracts import ContractError
from control_plane.data.sqlite_evaluation_repository import (
    RepositoryError,
    SQLiteEvaluationRepository,
)
from control_plane.evaluation.mutation_views import (
    parse_deck,
    register_schema,
    validate_candidate_parameters as view_validate_candidate_parameters,
)
from control_plane.evaluation.parameter_schema import (
    compute_schema_revision,
    make_parameter_schema,
    validate_parameter_schema,
    validate_parameters,
)
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.simulation.deck_parameters import (
    parse_deck_parameters,
    rewrite_deck_parameters,
)
from control_plane.web.status_server import StatusRequestHandler, StatusServer


class DeckParametersParserTests(unittest.TestCase):
    """Test TCAD deck parameter parsing, warnings, and error handling."""

    def test_parse_valid_deck_numeric_types(self) -> None:
        deck = """
        # Header comment
        go atlas
        set t_total1 = 0.551
        set mesh_bias = 1
        set scientific_val = 1.25e-4
        set positive_val = +3.14
        set negative_val = -0.05
        $ Another comment
        mesh auto
        """
        result = parse_deck_parameters(deck)
        params = {p["name"]: p for p in result["parameters"]}
        self.assertEqual(len(params), 5)
        self.assertEqual(params["t_total1"]["value"], 0.551)
        self.assertEqual(params["t_total1"]["type"], "numeric")
        self.assertEqual(params["t_total1"]["value_raw"], "0.551")
        self.assertEqual(params["t_total1"]["line"], 4)
        self.assertTrue(params["t_total1"]["unique"])

        self.assertEqual(params["mesh_bias"]["value"], 1)
        self.assertEqual(params["mesh_bias"]["type"], "numeric")
        self.assertEqual(params["mesh_bias"]["line"], 5)
        self.assertTrue(params["mesh_bias"]["unique"])

        self.assertEqual(params["scientific_val"]["value"], 1.25e-4)
        self.assertEqual(params["positive_val"]["value"], 3.14)
        self.assertEqual(params["negative_val"]["value"], -0.05)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["extracts"], [])

    def test_parse_inline_comments_warning(self) -> None:
        deck = """
        set t_total1 = 0.551 # thickness in um
        set t_total2 = 0.650 $ dollar comment
        """
        result = parse_deck_parameters(deck)
        self.assertEqual(len(result["parameters"]), 2)
        self.assertEqual(result["parameters"][0]["value"], 0.551)
        self.assertEqual(result["parameters"][1]["value"], 0.650)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("inline comment", result["warnings"][0])
        self.assertIn("inline comment", result["warnings"][1])

    def test_parse_duplicate_set_lines_warning_and_non_unique(self) -> None:
        deck = """
        set t_total1 = 0.551
        set other_param = 1.0
        set t_total1 = 0.770
        """
        result = parse_deck_parameters(deck)
        self.assertEqual(len(result["parameters"]), 3)
        t_totals = [p for p in result["parameters"] if p["name"] == "t_total1"]
        self.assertEqual(len(t_totals), 2)
        self.assertFalse(t_totals[0]["unique"])
        self.assertFalse(t_totals[1]["unique"])
        self.assertTrue(result["parameters"][1]["unique"])
        self.assertTrue(any("duplicate" in w.lower() for w in result["warnings"]))

    def test_parse_malformed_numeric_values_warning(self) -> None:
        deck = """
        set valid_param = 1.0
        set bad_param1 = abc
        set bad_param2 = 1.2.3.4
        set bad_param3 = 
        """
        result = parse_deck_parameters(deck)
        self.assertEqual(len(result["parameters"]), 1)
        self.assertEqual(result["parameters"][0]["name"], "valid_param")
        self.assertEqual(len(result["warnings"]), 3)
        self.assertTrue(all("malformed" in w.lower() for w in result["warnings"]))

    def test_parse_expression_parameters(self) -> None:
        deck = """
        set Proportion = 2/3
        set t_total1 = 0.551
        set t_emit1 = $Proportion*$t_total1
        set t_base1 = $t_total1 - $t_emit1
        """
        result = parse_deck_parameters(deck)
        self.assertEqual(len(result["parameters"]), 4)
        params = {p["name"]: p for p in result["parameters"]}
        self.assertEqual(params["Proportion"]["type"], "expression")
        self.assertEqual(params["Proportion"]["kind"], "expression")
        self.assertEqual(params["Proportion"]["value_raw"], "2/3")
        self.assertIsNone(params["Proportion"]["value"])
        self.assertEqual(params["t_total1"]["type"], "numeric")
        self.assertEqual(params["t_total1"]["value"], 0.551)
        self.assertEqual(params["t_emit1"]["type"], "expression")
        self.assertEqual(params["t_emit1"]["value_raw"], "$Proportion*$t_total1")
        self.assertEqual(params["t_base1"]["type"], "expression")
        self.assertEqual(params["t_base1"]["value_raw"], "$t_total1 - $t_emit1")
        self.assertEqual(result["warnings"], [])

    def test_parse_extract_declarations(self) -> None:
        deck = """
        extract init infile="test.log"
        extract name="1Jsc" max(curve(v."anode", i."cathode"))
        extract name="1Voc" x.val from curve(v."anode", i."cathode") where y.val=0.0
        extract name="1FF" ($Pm/($1Jsc * $1Voc))*100
        # comment with extract
        extract other="ignored"
        """
        result = parse_deck_parameters(deck)
        self.assertEqual(len(result["extracts"]), 3)
        exts = {e["name"]: e for e in result["extracts"]}
        self.assertEqual(exts["1Jsc"]["expression"], 'max(curve(v."anode", i."cathode"))')
        self.assertEqual(exts["1Jsc"]["line"], 3)
        self.assertEqual(exts["1Voc"]["expression"], 'x.val from curve(v."anode", i."cathode") where y.val=0.0')
        self.assertEqual(exts["1FF"]["expression"], '($Pm/($1Jsc * $1Voc))*100')
        self.assertEqual(result["warnings"], [])

    def test_parse_real_template_smoke(self) -> None:
        template_path = Path("templates/10layer-new-f-thickness-percent-reflector-base.in")
        if not template_path.exists():
            self.skipTest("Template file not present in public distribution")
        deck = template_path.read_text(encoding="utf-8")
        result = parse_deck_parameters(deck)
        params = {p["name"]: p for p in result["parameters"]}

        numeric_count = sum(1 for p in result["parameters"] if p["type"] == "numeric")
        expression_count = sum(1 for p in result["parameters"] if p["type"] == "expression")
        self.assertGreaterEqual(numeric_count + expression_count, 40)

        for w in result["warnings"]:
            self.assertNotIn("t_emit", w)
            self.assertNotIn("t_base", w)
            self.assertNotIn("Proportion", w)

        self.assertIn("t_emit1", params)
        self.assertEqual(params["t_emit1"]["type"], "expression")
        self.assertIn("$Proportion", params["t_emit1"]["value_raw"])

        self.assertEqual(len(result["extracts"]), 7)
        extract_names = {e["name"] for e in result["extracts"]}
        self.assertIn("1Jsc", extract_names)
        self.assertIn("1Eff", extract_names)
        ext_1ff = next(e for e in result["extracts"] if e["name"] == "1FF")
        self.assertIn("$Pm", ext_1ff["expression"])

    def test_parse_non_string_deck_raises_contract_error(self) -> None:
        with self.assertRaises(ContractError):
            parse_deck_parameters(None)  # type: ignore
        with self.assertRaises(ContractError):
            parse_deck_parameters(123)  # type: ignore


class DeckParametersRewriteTests(unittest.TestCase):
    """Test deck parameter rewriting precision, whitespace preservation, and validation."""

    def test_rewrite_preserves_whitespace_and_formatting(self) -> None:
        deck = (
            "go atlas\n"
            "   set   t_total1   =   0.551   # thickness\n"
            "\tset t_total2=0.60\n"
            "set mesh_bias = 1.0\n"
        )
        rewritten = rewrite_deck_parameters(
            deck, {"t_total1": 0.75, "t_total2": 0.88}
        )
        expected = (
            "go atlas\n"
            "   set   t_total1   =   0.75   # thickness\n"
            "\tset t_total2=0.88\n"
            "set mesh_bias = 1.0\n"
        )
        self.assertEqual(rewritten, expected)

    def test_rewrite_full_repr_precision_no_truncation(self) -> None:
        deck = "set high_prec = 0.0\n"
        val = 0.12345678901234567
        rewritten = rewrite_deck_parameters(deck, {"high_prec": val})
        self.assertIn(repr(val), rewritten)
        self.assertEqual(rewritten, f"set high_prec = {repr(val)}\n")

    def test_rewrite_preserves_crlf_newlines(self) -> None:
        deck = "set a = 1.0\r\nset b = 2.0\r\n"
        rewritten = rewrite_deck_parameters(deck, {"a": 10.5})
        self.assertEqual(rewritten, "set a = 10.5\r\nset b = 2.0\r\n")

    def test_rewrite_rejects_unknown_parameter(self) -> None:
        deck = "set known = 1.0\n"
        with self.assertRaises(ContractError) as ctx:
            rewrite_deck_parameters(deck, {"unknown_param": 2.0})
        self.assertIn("unknown parameter", str(ctx.exception).lower())

    def test_rewrite_rejects_duplicate_ambiguous_parameter(self) -> None:
        deck = "set dup = 1.0\nset dup = 2.0\n"
        with self.assertRaises(ContractError) as ctx:
            rewrite_deck_parameters(deck, {"dup": 3.0})
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_rewrite_rejects_non_numeric_and_bool_values(self) -> None:
        deck = "set a = 1.0\n"
        with self.assertRaises(ContractError):
            rewrite_deck_parameters(deck, {"a": "string"})
        with self.assertRaises(ContractError):
            rewrite_deck_parameters(deck, {"a": True})
        with self.assertRaises(ContractError):
            rewrite_deck_parameters(deck, {"a": float("nan")})

    def test_rewrite_rejects_expression_parameter(self) -> None:
        deck = (
            "set Proportion = 2/3\n"
            "set t_total1 = 0.551\n"
            "set t_emit1 = $Proportion*$t_total1\n"
        )
        with self.assertRaises(ContractError) as ctx:
            rewrite_deck_parameters(deck, {"t_emit1": 0.3})
        self.assertIn("expression", str(ctx.exception).lower())

    def test_rewrite_preserves_expression_parameters_when_rewriting_numeric(self) -> None:
        deck = (
            "set Proportion = 2/3\n"
            "set t_total1 = 0.551\n"
            "set t_emit1 = $Proportion*$t_total1\n"
        )
        rewritten = rewrite_deck_parameters(deck, {"t_total1": 0.75})
        expected = (
            "set Proportion = 2/3\n"
            "set t_total1 = 0.75\n"
            "set t_emit1 = $Proportion*$t_total1\n"
        )
        self.assertEqual(rewritten, expected)


class ParameterSchemaContractTests(unittest.TestCase):
    """Test ParameterSchema document validation and revision stability."""

    def test_valid_schema_canonicalization_and_revision_stability(self) -> None:
        schema_dict_1 = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "source_package": {
                "artifact_id": "package.thickness-vector.v1",
                "revision": "sha256:" + "a" * 64,
            },
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                    "default": 0.551,
                    "unit": "um",
                    "deck_line": 10,
                },
                {
                    "name": "mesh_bias",
                    "type": "float",
                    "role": "fixed",
                    "value": 1.0,
                    "deck_line": 20,
                },
            ],
        }

        # Same parameters in reverse order
        schema_dict_2 = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "source_package": {
                "artifact_id": "package.thickness-vector.v1",
                "revision": "sha256:" + "a" * 64,
            },
            "parameters": [
                schema_dict_1["parameters"][1],
                schema_dict_1["parameters"][0],
            ],
        }

        rev1 = compute_schema_revision(schema_dict_1)
        rev2 = compute_schema_revision(schema_dict_2)
        self.assertTrue(rev1.startswith("sha256:"))
        self.assertEqual(len(rev1), 71)
        self.assertEqual(rev1, rev2)

    def test_make_parameter_schema_helper(self) -> None:
        doc = make_parameter_schema(
            parameters=[
                {
                    "name": "x",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.0, "max": 1.0},
                }
            ],
            problem_hint="test",
        )
        self.assertEqual(doc["kind"], "parameter-schema")
        self.assertEqual(doc["problem_hint"], "test")
        self.assertEqual(len(doc["parameters"]), 1)

    def test_schema_rejects_invalid_kind_and_empty_parameters(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "wrong-kind", "parameters": []})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": []})

    def test_schema_rejects_duplicate_parameter_names(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [
                        {"name": "x", "role": "variable", "bounds": {"min": 0, "max": 1}},
                        {"name": "x", "role": "fixed", "value": 2},
                    ],
                }
            )

    def test_schema_rejects_inverted_bounds_and_bad_default(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [
                        {"name": "x", "role": "variable", "bounds": {"min": 5.0, "max": 1.0}}
                    ],
                }
            )
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [
                        {
                            "name": "x",
                            "role": "variable",
                            "bounds": {"min": 1.0, "max": 5.0},
                            "default": 10.0,
                        }
                    ],
                }
            )

    def test_schema_fixed_requires_value_and_variable_requires_bounds(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "role": "fixed"}],
                }
            )
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "role": "variable"}],
                }
            )

    def test_schema_rejects_invalid_type_and_unknown_fields(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "type": "string", "role": "fixed", "value": 1}],
                }
            )
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "type": "float", "role": "fixed", "value": 1, "extra": True}],
                }
            )

    def test_schema_rejects_non_finite_and_bool_numbers(self) -> None:
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "role": "fixed", "value": True}],
                }
            )
        with self.assertRaises(ContractError):
            validate_parameter_schema(
                {
                    "kind": "parameter-schema",
                    "parameters": [{"name": "x", "role": "fixed", "value": float("inf")}],
                }
            )
    def test_schema_with_valid_extracts_canonicalization_and_revision_stability(self) -> None:
        schema_dict_1 = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                },
            ],
            "extracts": [
                {
                    "name": "1Voc",
                    "expression": 'x.val from curve(v."anode", i."cathode") where y.val=0.0',
                    "line": 15,
                },
                {
                    "name": "1Jsc",
                    "expression": 'max(curve(v."anode", i."cathode"))',
                    "line": 10,
                },
                {
                    "name": "1FF",
                    "expression": "($Pm/($1Jsc * $1Voc))*100",
                },
            ],
        }

        # Same extracts in different order
        schema_dict_2 = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                },
            ],
            "extracts": [
                schema_dict_1["extracts"][1],
                schema_dict_1["extracts"][2],
                schema_dict_1["extracts"][0],
            ],
        }

        canonical_1 = validate_parameter_schema(schema_dict_1)
        canonical_2 = validate_parameter_schema(schema_dict_2)
        self.assertIn("extracts", canonical_1)
        self.assertEqual(len(canonical_1["extracts"]), 3)
        # Verify sorted by name
        self.assertEqual(
            [e["name"] for e in canonical_1["extracts"]],
            ["1FF", "1Jsc", "1Voc"],
        )
        self.assertEqual(canonical_1["extracts"][0], {"name": "1FF", "expression": "($Pm/($1Jsc * $1Voc))*100"})
        self.assertEqual(canonical_1["extracts"][1], {"name": "1Jsc", "expression": 'max(curve(v."anode", i."cathode"))', "line": 10})
        self.assertEqual(canonical_1["extracts"][2], {"name": "1Voc", "expression": 'x.val from curve(v."anode", i."cathode") where y.val=0.0', "line": 15})

        rev1 = compute_schema_revision(schema_dict_1)
        rev2 = compute_schema_revision(schema_dict_2)
        self.assertTrue(rev1.startswith("sha256:"))
        self.assertEqual(len(rev1), 71)
        self.assertEqual(rev1, rev2)

    def test_make_parameter_schema_with_extracts(self) -> None:
        doc = make_parameter_schema(
            parameters=[
                {
                    "name": "x",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.0, "max": 1.0},
                }
            ],
            extracts=[
                {"name": "1Jsc", "expression": "max(curve())", "line": 5}
            ],
            problem_hint="test-extracts",
        )
        self.assertEqual(doc["kind"], "parameter-schema")
        self.assertEqual(doc["problem_hint"], "test-extracts")
        self.assertIn("extracts", doc)
        self.assertEqual(len(doc["extracts"]), 1)
        self.assertEqual(doc["extracts"][0]["name"], "1Jsc")
        self.assertEqual(doc["extracts"][0]["expression"], "max(curve())")
        self.assertEqual(doc["extracts"][0]["line"], 5)

    def test_schema_rejects_malformed_extracts(self) -> None:
        base_params = [{"name": "x", "role": "variable", "bounds": {"min": 0, "max": 1}}]

        # extracts not a list / sequence
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": "not-a-list"})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": 123})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": {"name": "1Jsc"}})

        # extract item not an object
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": ["not-an-object"]})

        # missing name
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"expression": "max()"}]})
        # empty name
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "", "expression": "max()"}]})
        # invalid token name
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "bad name with space", "expression": "max()"}]})
        # duplicate extract name
        with self.assertRaises(ContractError):
            validate_parameter_schema({
                "kind": "parameter-schema",
                "parameters": base_params,
                "extracts": [
                    {"name": "1Jsc", "expression": "e1"},
                    {"name": "1Jsc", "expression": "e2"},
                ],
            })

        # missing expression
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc"}]})
        # empty/whitespace expression
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "   "}]})
        # non-string expression
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": 123}]})

        # line <= 0
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "line": 0}]})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "line": -1}]})
        # line non-int / bool
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "line": True}]})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "line": "5"}]})
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "line": 1.5}]})

        # unknown keys in extract item
        with self.assertRaises(ContractError):
            validate_parameter_schema({"kind": "parameter-schema", "parameters": base_params, "extracts": [{"name": "1Jsc", "expression": "max()", "extra_field": "val"}]})

    def test_schema_without_extracts_regression_pin(self) -> None:
        schema_dict = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "source_package": {
                "artifact_id": "package.thickness-vector.v1",
                "revision": "sha256:" + "a" * 64,
            },
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                    "default": 0.551,
                    "unit": "um",
                    "deck_line": 10,
                },
                {
                    "name": "mesh_bias",
                    "type": "float",
                    "role": "fixed",
                    "value": 1.0,
                    "deck_line": 20,
                },
            ],
        }
        canonical = validate_parameter_schema(schema_dict)
        self.assertNotIn("extracts", canonical)
        rev = compute_schema_revision(schema_dict)
        self.assertEqual(
            rev,
            "sha256:7b8f1712f15f411b5ab8743d7f1b335e25dae5cfd2a5d5cd0d35094aa1b2771f",
        )

class ValidateCandidateParametersTests(unittest.TestCase):
    """Test candidate parameter validation against ParameterSchema and issue codes."""

    def setUp(self) -> None:
        self.schema = {
            "kind": "parameter-schema",
            "problem_hint": "solar-cell",
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                    "default": 0.551,
                },
                {
                    "name": "layers",
                    "type": "int",
                    "role": "variable",
                    "bounds": {"min": 1, "max": 10},
                    "default": 10,
                },
                {
                    "name": "mesh_bias",
                    "type": "float",
                    "role": "fixed",
                    "value": 1.0,
                },
            ],
        }

    def test_valid_candidate_parameters_passes(self) -> None:
        res = validate_parameters(
            self.schema,
            {"t_total1": 0.551, "layers": 10, "mesh_bias": 1.0},
        )
        self.assertTrue(res["valid"])
        self.assertEqual(res["issues"], [])

    def test_valid_candidate_omitting_fixed_parameter_passes(self) -> None:
        res = validate_parameters(
            self.schema,
            {"t_total1": 0.551, "layers": 10},
        )
        self.assertTrue(res["valid"])
        self.assertEqual(res["issues"], [])

    def test_missing_required_variable_parameter(self) -> None:
        res = validate_parameters(
            self.schema,
            {"layers": 10},
        )
        self.assertFalse(res["valid"])
        self.assertEqual(len(res["issues"]), 1)
        self.assertEqual(res["issues"][0]["code"], "missing-parameter")
        self.assertEqual(res["issues"][0]["name"], "t_total1")

    def test_unknown_parameter(self) -> None:
        res = validate_parameters(
            self.schema,
            {"t_total1": 0.551, "layers": 10, "unrecognized_param": 42.0},
        )
        self.assertFalse(res["valid"])
        self.assertEqual(len(res["issues"]), 1)
        self.assertEqual(res["issues"][0]["code"], "unknown-parameter")
        self.assertEqual(res["issues"][0]["name"], "unrecognized_param")

    def test_fixed_parameter_override_disallowed(self) -> None:
        res = validate_parameters(
            self.schema,
            {"t_total1": 0.551, "layers": 10, "mesh_bias": 2.0},
        )
        self.assertFalse(res["valid"])
        self.assertEqual(len(res["issues"]), 1)
        self.assertEqual(res["issues"][0]["code"], "fixed-parameter-override")
        self.assertEqual(res["issues"][0]["name"], "mesh_bias")

    def test_out_of_bounds_below_min_and_above_max(self) -> None:
        res_low = validate_parameters(
            self.schema,
            {"t_total1": 0.05, "layers": 10},
        )
        self.assertFalse(res_low["valid"])
        self.assertEqual(res_low["issues"][0]["code"], "out-of-bounds")
        self.assertIn("below minimum", res_low["issues"][0]["message"])

        res_high = validate_parameters(
            self.schema,
            {"t_total1": 10.0, "layers": 10},
        )
        self.assertFalse(res_high["valid"])
        self.assertEqual(res_high["issues"][0]["code"], "out-of-bounds")
        self.assertIn("exceeds maximum", res_high["issues"][0]["message"])

    def test_invalid_type_non_numeric_nan_and_non_integer(self) -> None:
        res_str = validate_parameters(
            self.schema,
            {"t_total1": "not-a-number", "layers": 10},
        )
        self.assertFalse(res_str["valid"])
        self.assertEqual(res_str["issues"][0]["code"], "invalid-type")

        res_bool = validate_parameters(
            self.schema,
            {"t_total1": True, "layers": 10},
        )
        self.assertFalse(res_bool["valid"])
        self.assertEqual(res_bool["issues"][0]["code"], "invalid-type")

        res_nan = validate_parameters(
            self.schema,
            {"t_total1": float("nan"), "layers": 10},
        )
        self.assertFalse(res_nan["valid"])
        self.assertEqual(res_nan["issues"][0]["code"], "invalid-type")

        res_float_for_int = validate_parameters(
            self.schema,
            {"t_total1": 0.551, "layers": 5.5},
        )
        self.assertFalse(res_float_for_int["valid"])
        self.assertEqual(res_float_for_int["issues"][0]["code"], "invalid-type")
        self.assertIn("integer", res_float_for_int["issues"][0]["message"])


class RepositoryAndMiddlewareTests(unittest.TestCase):
    """Test schema_documents table persistence and EvaluationMiddleware methods."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "evaluations.db"
        self.repo = SQLiteEvaluationRepository(self.db_path)
        self.middleware = EvaluationMiddleware(self.repo)
        self.sample_schema = {
            "kind": "parameter-schema",
            "problem_hint": "unit-test-problem",
            "parameters": [
                {
                    "name": "param_a",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.0, "max": 10.0},
                    "default": 1.0,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_register_and_get_schema_idempotency(self) -> None:
        # First registration
        rec1 = self.middleware.register_schema(self.sample_schema)
        self.assertTrue(rec1["revision"].startswith("sha256:"))

        # Idempotent repeated registration
        rec2 = self.middleware.register_schema(self.sample_schema)
        self.assertEqual(rec1["revision"], rec2["revision"])

        # Fetch dereferenced document
        fetched = self.middleware.get_schema(rec1["revision"])
        self.assertEqual(fetched["revision"], rec1["revision"])
        self.assertEqual(fetched["schema"]["kind"], "parameter-schema")
        self.assertEqual(len(fetched["schema"]["parameters"]), 1)

    def test_get_unknown_schema_raises_repository_error(self) -> None:
        fake_rev = "sha256:" + "0" * 64
        with self.assertRaises(RepositoryError) as ctx:
            self.middleware.get_schema(fake_rev)
        self.assertIn(f"unknown Schema: {fake_rev}", str(ctx.exception))


class WebStatusServerEndpointsTests(unittest.TestCase):
    """Test web status server endpoints for /api/packages/parse, /api/schemas, /api/candidates/validate."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "evaluations.db"
        self.repo = SQLiteEvaluationRepository(self.db_path)
        self.middleware = EvaluationMiddleware(self.repo)

        self._quiet_logs = patch.object(
            StatusRequestHandler, "log_message", lambda *args, **kwargs: None
        )
        self._quiet_logs.start()

        # Write-enabled server
        self.server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=Path(self.temp_dir.name),
            allow_writes=True,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # Read-only server (writes disabled)
        self.ro_server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=Path(self.temp_dir.name),
            allow_writes=False,
        )
        self.ro_port = self.ro_server.server_address[1]
        self.ro_thread = threading.Thread(target=self.ro_server.serve_forever, daemon=True)
        self.ro_thread.start()

        self.sample_schema = {
            "kind": "parameter-schema",
            "problem_hint": "http-test-schema",
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 5.0},
                    "default": 0.551,
                },
                {
                    "name": "mesh_bias",
                    "type": "float",
                    "role": "fixed",
                    "value": 1.0,
                },
            ],
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

        self.ro_server.shutdown()
        self.ro_server.server_close()
        self.ro_thread.join(timeout=5)

        self._quiet_logs.stop()
        self.temp_dir.cleanup()

    def _post(self, port: int, path: str, payload: object) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def _get(self, port: int, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def test_post_packages_parse_success_and_error(self) -> None:
        deck = "set t_total1 = 0.551\nset mesh_bias = 1.0\n"
        status, payload = self._post(self.port, "/api/packages/parse", {"deck_text": deck})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["parameters"]), 2)
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["extracts"], [])
        self.assertNotIn("Traceback", json.dumps(payload))

        # Bad payload (non-string deck_text)
        status, payload = self._post(self.port, "/api/packages/parse", {"deck_text": 123})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_post_schemas_registration_and_get_dereference(self) -> None:
        # Register schema
        status, reg_payload = self._post(self.port, "/api/schemas", self.sample_schema)
        self.assertEqual(status, 200)
        revision = reg_payload["revision"]
        self.assertTrue(revision.startswith("sha256:"))

        # Idempotent re-registration
        status, reg_payload_2 = self._post(self.port, "/api/schemas", self.sample_schema)
        self.assertEqual(status, 200)
        self.assertEqual(reg_payload_2["revision"], revision)

        # GET schema dereference
        status, get_payload = self._get(self.port, f"/api/schemas/{revision}")
        self.assertEqual(status, 200)
        self.assertEqual(get_payload["kind"], "parameter-schema")
        self.assertEqual(len(get_payload["parameters"]), 2)
        self.assertNotIn("Traceback", json.dumps(get_payload))

        # GET unknown schema -> 404
        fake_rev = "sha256:" + "f" * 64
        status, err_payload = self._get(self.port, f"/api/schemas/{fake_rev}")
        self.assertEqual(status, 404)
        self.assertEqual(err_payload, {"error": f"unknown Schema: {fake_rev}"})
        self.assertNotIn("Traceback", json.dumps(err_payload))

    def test_post_candidates_validate_endpoint(self) -> None:
        # Register schema first
        _, reg_payload = self._post(self.port, "/api/schemas", self.sample_schema)
        rev = reg_payload["revision"]

        # Valid candidate parameters
        status, payload = self._post(
            self.port,
            "/api/candidates/validate",
            {"schema_revision": rev, "parameters": {"t_total1": 0.551}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["issues"], [])

        # Candidate with out-of-bounds parameter
        status, payload = self._post(
            self.port,
            "/api/candidates/validate",
            {"schema_revision": rev, "parameters": {"t_total1": 99.0}},
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["issues"][0]["code"], "out-of-bounds")

        # Unknown schema revision -> 404
        fake_rev = "sha256:" + "e" * 64
        status, payload = self._post(
            self.port,
            "/api/candidates/validate",
            {"schema_revision": fake_rev, "parameters": {"t_total1": 0.551}},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": f"unknown Schema: {fake_rev}"})

        # Missing required field -> 400
        status, payload = self._post(
            self.port,
            "/api/candidates/validate",
            {"schema_revision": rev},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_write_gated_post_rejected_when_writes_disabled(self) -> None:
        # Exempted endpoints return 200 even when allow_writes=False
        status, payload = self._post(
            self.ro_port,
            "/api/packages/parse",
            {"deck_text": "set a = 1\n"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["parameters"]), 1)

        # Candidates validate is also exempted when allow_writes=False
        rec = self.middleware.register_schema(self.sample_schema)
        rev = rec["revision"]
        status, payload = self._post(
            self.ro_port,
            "/api/candidates/validate",
            {
                "schema_revision": rev,
                "parameters": {"t_total1": 0.551},
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        for path, body in (
            ("/api/schemas", self.sample_schema),
            ("/api/packages", {"manifest": {}}),
            ("/api/problems", {"problem_id": "problem:p"}),
            ("/api/studies", {"study_id": "study:s", "problem_id": "problem:p"}),
            ("/api/evaluations", {"evaluation_request": {}}),
            ("/api/contracts/build", {"kind": "problem", "spec": {}}),
        ):
            status, payload = self._post(self.ro_port, path, body)
            self.assertEqual(status, 405, path)
            self.assertIn("error", payload)
            self.assertNotIn("Traceback", json.dumps(payload))
