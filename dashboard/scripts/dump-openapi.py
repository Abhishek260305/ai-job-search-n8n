"""Write the dashboard API's OpenAPI schema to dashboard/web/src/api/openapi.json, straight from the code (no server,
no database, no tunnel). The frontend's TypeScript types are generated from that file:

    uv run --no-project --python 3.12 --with-requirements dashboard/api/requirements.txt python dashboard/scripts/dump-openapi.py
    (cd dashboard/web && npm run gen:api)

dashboard/api/tests/test_openapi_snapshot.py fails when the file no longer matches the code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
TARGET = DASHBOARD / "web" / "src" / "api" / "openapi.json"
sys.path.insert(0, str(DASHBOARD / "api"))

from app import create_app  # noqa: E402
from settings import load_settings  # noqa: E402

# Placeholder settings: the schema does not depend on them, and nothing connects anywhere.
SETTINGS = load_settings({"DASHBOARD_PASSWORD": "schema-dump-only", "DASHBOARD_SECRET": "x" * 32,
                          "DB_PASSWORD": "unused", "STATIC_DIR": str(DASHBOARD / "web" / "dist")})


def schema() -> dict:
    return create_app(SETTINGS, store=None).openapi()


def render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render(schema()), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(DASHBOARD.parent)}")
