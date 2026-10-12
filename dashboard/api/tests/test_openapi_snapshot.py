"""The frontend's types are generated from dashboard/web/src/api/openapi.json; this fails when the API changed and
the file was not regenerated (the fix is printed in the message)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2]
SNAPSHOT = DASHBOARD / "web" / "src" / "api" / "openapi.json"
SCRIPT = DASHBOARD / "scripts" / "dump-openapi.py"
REGENERATE = ("regenerate it: uv run --no-project --python 3.12 --with-requirements dashboard/api/requirements.txt "
              "python dashboard/scripts/dump-openapi.py && (cd dashboard/web && npm run gen:api)")

pytestmark = pytest.mark.skipif(not SNAPSHOT.is_file(), reason="not in the source tree")


def load_script():
    spec = importlib.util.spec_from_file_location("dump_openapi", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_schema_matches_the_code():
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == load_script().schema(), \
        f"{SNAPSHOT.relative_to(DASHBOARD.parent)} is out of date; {REGENERATE}"


def test_the_generated_types_are_newer_than_the_schema_file():
    types = SNAPSHOT.with_name("schema.d.ts")
    assert types.is_file(), f"schema.d.ts is missing; {REGENERATE}"
    for name in json.loads(SNAPSHOT.read_text(encoding="utf-8"))["components"]["schemas"]:
        assert f"{name}: {{" in types.read_text(encoding="utf-8"), f"schema.d.ts lacks {name}; {REGENERATE}"
