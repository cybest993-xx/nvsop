from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_openapi_compatibility import (
    compatibility_errors,
    load_declarations,
    main,
    undeclared_errors,
)


def contract() -> dict[str, Any]:
    return {
        "paths": {
            "/api/v1/auth/session": {
                "get": {},
                "post": {},
            }
        },
        "components": {
            "schemas": {
                "SessionView": {
                    "type": "object",
                    "required": ["user_id"],
                    "properties": {
                        "user_id": {"type": "string"},
                        "display_name": {"type": ["string", "null"]},
                        "status": {"type": "string", "enum": ["active", "deactivated"]},
                    },
                }
            }
        },
    }


def operation_contract() -> dict[str, Any]:
    return {
        "paths": {
            "/widgets/{widget_id}": {
                "post": {
                    "operationId": "updateWidget",
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"label": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Updated",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "string"},
                                            "label": {"type": ["string", "null"]},
                                        },
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": "Invalid",
                            "content": {"application/problem+json": {"schema": {"type": "object"}}},
                        },
                    },
                }
            }
        },
        "components": {"schemas": {}},
    }


class OpenApiCompatibilityTest(unittest.TestCase):
    def test_accepts_a_new_endpoint_and_optional_field(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        current["paths"]["/api/v1/liveness"] = {"get": {}}
        current["components"]["schemas"]["SessionView"]["properties"]["expires_at"] = {
            "type": "string"
        }

        self.assertEqual([], compatibility_errors(previous, current))

    def test_rejects_a_removed_path(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        del current["paths"]["/api/v1/auth/session"]

        self.assertIn(
            "path removed: /api/v1/auth/session",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_operation(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        del current["paths"]["/api/v1/auth/session"]["get"]

        self.assertIn(
            "operation removed: GET /api/v1/auth/session",
            compatibility_errors(previous, current),
        )

    def test_rejects_an_operation_id_change(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["operationId"] = "replaceWidget"

        self.assertIn(
            "operationId changed: POST /widgets/{widget_id}: updateWidget -> replaceWidget",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_request_body(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        del current["paths"]["/widgets/{widget_id}"]["post"]["requestBody"]

        self.assertIn(
            "request body removed: POST /widgets/{widget_id}",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_request_body_that_became_required(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["requestBody"]["required"] = True

        self.assertIn(
            "request body became required: POST /widgets/{widget_id}",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_new_required_field_in_an_inline_request_schema(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        body = current["paths"]["/widgets/{widget_id}"]["post"]["requestBody"]
        body["content"]["application/json"]["schema"]["properties"]["owner_id"] = {"type": "string"}
        body["content"]["application/json"]["schema"]["required"] = ["owner_id"]

        self.assertIn(
            "required field added: request POST /widgets/{widget_id} application/json.owner_id",
            compatibility_errors(previous, current),
        )

    def test_rejects_adding_a_constraint_to_an_unconstrained_inline_request_schema(self) -> None:
        previous = operation_contract()
        del previous["paths"]["/widgets/{widget_id}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] = {"type": "object"}

        self.assertIn(
            "type narrowed at request POST /widgets/{widget_id} application/json: "
            "unconstrained -> ['object']",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_request_parameter(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["parameters"].pop()

        self.assertIn(
            "parameter removed: POST /widgets/{widget_id} query.verbose",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_new_required_request_parameter(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["parameters"].append(
            {
                "name": "tenant",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            }
        )

        self.assertIn(
            "required parameter added: POST /widgets/{widget_id} header.tenant",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_request_parameter_that_became_required(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["parameters"][1]["required"] = True

        self.assertIn(
            "parameter became required: POST /widgets/{widget_id} query.verbose",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_narrowed_parameter_schema(self) -> None:
        previous = operation_contract()
        previous["paths"]["/widgets/{widget_id}"]["post"]["parameters"][1]["schema"] = {
            "type": ["boolean", "null"]
        }
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["parameters"][1]["schema"] = {
            "type": "boolean"
        }

        self.assertIn(
            "type narrowed at parameter POST /widgets/{widget_id} query.verbose: "
            "['boolean', 'null'] -> ['boolean']",
            compatibility_errors(previous, current),
        )

    def test_accepts_optional_request_and_response_additions(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        operation = current["paths"]["/widgets/{widget_id}"]["post"]
        operation["parameters"].append(
            {
                "name": "trace",
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
            }
        )
        operation["requestBody"]["content"]["application/json"]["schema"]["properties"][
            "description"
        ] = {"type": "string"}
        operation["responses"]["201"]["content"]["application/json"]["schema"]["properties"][
            "updated_at"
        ] = {"type": "string"}
        operation["responses"]["202"] = {"description": "Accepted"}

        self.assertEqual([], compatibility_errors(previous, current))

    def test_rejects_a_removed_response(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        del current["paths"]["/widgets/{widget_id}"]["post"]["responses"]["400"]

        self.assertIn(
            "response removed: POST /widgets/{widget_id} 400",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_response_header(self) -> None:
        previous = operation_contract()
        previous["paths"]["/widgets/{widget_id}"]["post"]["responses"]["201"]["headers"] = {
            "x-request-id": {"schema": {"type": "string"}}
        }
        current = deepcopy(previous)
        del current["paths"]["/widgets/{widget_id}"]["post"]["responses"]["201"]["headers"]

        self.assertIn(
            "response header removed: POST /widgets/{widget_id} 201 header.x-request-id",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_response_media_type(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        del current["paths"]["/widgets/{widget_id}"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]

        self.assertIn(
            "response media type removed: POST /widgets/{widget_id} 201 application/json",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_field_in_an_inline_response_schema(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        del current["paths"]["/widgets/{widget_id}"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]["properties"]["label"]

        self.assertIn(
            "field removed: response POST /widgets/{widget_id} 201 application/json.label",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_narrowed_inline_response_schema(self) -> None:
        previous = operation_contract()
        current = deepcopy(previous)
        current["paths"]["/widgets/{widget_id}"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]["properties"]["label"] = {"type": "string"}

        self.assertIn(
            "type narrowed at response POST /widgets/{widget_id} 201 application/json.label: "
            "['null', 'string'] -> ['string']",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_field(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        del current["components"]["schemas"]["SessionView"]["properties"]["display_name"]

        self.assertIn(
            "field removed: schema SessionView.display_name",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_new_required_field(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        current["components"]["schemas"]["SessionView"]["properties"]["expires_at"] = {
            "type": "string"
        }
        current["components"]["schemas"]["SessionView"]["required"].append("expires_at")

        self.assertIn(
            "required field added: schema SessionView.expires_at",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_narrowed_type(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        current["components"]["schemas"]["SessionView"]["properties"]["display_name"] = {
            "type": "string"
        }

        self.assertIn(
            "type narrowed at schema SessionView.display_name: ['null', 'string'] -> ['string']",
            compatibility_errors(previous, current),
        )

    def test_rejects_adding_a_type_constraint_to_an_unconstrained_field(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        previous["components"]["schemas"]["SessionView"]["properties"]["display_name"] = {}
        current["components"]["schemas"]["SessionView"]["properties"]["display_name"] = {
            "type": "string"
        }

        self.assertIn(
            "type narrowed at schema SessionView.display_name: unconstrained -> ['string']",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_removed_enum_value(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        current["components"]["schemas"]["SessionView"]["properties"]["status"]["enum"] = ["active"]

        self.assertIn(
            "enum values removed at schema SessionView.status: ['deactivated']",
            compatibility_errors(previous, current),
        )

    def test_rejects_a_new_enum_constraint_on_an_unconstrained_field(self) -> None:
        previous = contract()
        current = deepcopy(previous)
        current["components"]["schemas"]["SessionView"]["properties"]["display_name"]["enum"] = [
            "王丽"
        ]

        self.assertIn(
            "enum narrowed at schema SessionView.display_name: unconstrained -> ['王丽']",
            compatibility_errors(previous, current),
        )


class DeclaredBreakingChangesTest(unittest.TestCase):
    """ADR-0003 允许协同发布的破坏性变更，但必须逐条显式登记。"""

    def test_declared_breaks_are_acknowledged_and_undeclared_ones_still_fail(self) -> None:
        declared = {"field removed: schema SessionView.user_id": {"change": "x"}}

        self.assertEqual(
            undeclared_errors(
                [
                    "field removed: schema SessionView.user_id",
                    "field removed: schema SessionView.display_name",
                ],
                declared,
            ),
            ["field removed: schema SessionView.display_name"],
        )

    def test_a_missing_declaration_file_acknowledges_nothing(self) -> None:
        self.assertEqual(load_declarations(None), {})

    def test_each_declaration_needs_a_reason_and_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "breaking-changes.json"
            path.write_text(
                json.dumps({"changes": [{"change": "field removed: schema X"}]}), encoding="utf-8"
            )

            with self.assertRaises(SystemExit):
                load_declarations(path)

    def test_declarations_are_indexed_by_the_exact_reported_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "breaking-changes.json"
            path.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "change": "field removed: schema X.y",
                                "reason": "改为文件身份字段",
                                "issue": "#350",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                list(load_declarations(path)),
                ["field removed: schema X.y"],
            )


class MainEntryTest(unittest.TestCase):
    """门禁入口本身必须按声明放过或拦截，而不只是辅助函数。"""

    def _baseline(self) -> dict[str, Any]:
        return {
            "paths": {},
            "components": {
                "schemas": {
                    "UploadAttemptView": {
                        "type": "object",
                        "required": ["object_version_id"],
                        "properties": {"object_version_id": {"type": ["string", "null"]}},
                    }
                }
            },
        }

    def _renamed(self) -> dict[str, Any]:
        current = self._baseline()
        schema = current["components"]["schemas"]["UploadAttemptView"]
        schema["properties"]["final_object_key"] = schema["properties"].pop("object_version_id")
        schema["required"] = ["final_object_key"]
        return current

    def _run(self, contract: dict[str, Any], declarations: dict[str, Any]) -> int:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "openapi.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            declarations_path = Path(directory) / "breaking-changes.json"
            declarations_path.write_text(json.dumps(declarations), encoding="utf-8")
            baseline = self._baseline()
            return main(
                ["check", "origin/main", str(contract_path), str(declarations_path)],
                previous_loader=lambda reference, path: baseline,
            )

    def test_main_rejects_an_undeclared_break_and_accepts_the_declared_one(self) -> None:
        self.assertEqual(self._run(self._renamed(), {"changes": []}), 1)

        declared = {
            "changes": [
                {
                    "change": "field removed: schema UploadAttemptView.object_version_id",
                    "reason": "改为定稿文件身份",
                    "issue": "#350",
                },
                {
                    "change": "required field added: schema UploadAttemptView.final_object_key",
                    "reason": "改为定稿文件身份",
                    "issue": "#350",
                },
            ]
        }

        self.assertEqual(self._run(self._renamed(), declared), 0)

    def test_main_rejects_a_declaration_whose_metadata_is_not_a_string(self) -> None:
        declared = {
            "changes": [
                {
                    "change": "field removed: schema UploadAttemptView.object_version_id",
                    "reason": True,
                    "issue": ["#350"],
                }
            ]
        }

        with self.assertRaises(SystemExit):
            self._run(self._renamed(), declared)


if __name__ == "__main__":
    unittest.main()
