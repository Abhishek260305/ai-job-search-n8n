"""The container image stays in step with the code: what it installs, copies, runs and leaves out. The image itself
is built on the VM (docs/dashboard.md); these checks catch the mistakes that would only show up there."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]
DASHBOARD = API.parent
DOCKERFILE = DASHBOARD / "Dockerfile"
# import name -> the requirements.txt line that provides it (pydantic and starlette come with fastapi)
PROVIDED_BY = {"fastapi": "fastapi", "starlette": "fastapi", "pydantic": "fastapi", "psycopg": "psycopg[binary]",
               "uvicorn": "uvicorn"}

pytestmark = pytest.mark.skipif(not DOCKERFILE.is_file(), reason="not in the source tree")


def app_modules() -> list[Path]:
    return sorted(p for p in API.glob("*.py"))


def requirement_names() -> set[str]:
    lines = (API / "requirements.txt").read_text().splitlines()
    return {line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()}


def test_every_third_party_import_is_installed_in_the_image():
    local = {p.stem for p in app_modules()}
    required = requirement_names()
    for module in app_modules():
        for node in ast.walk(ast.parse(module.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top in local or top == "__future__":
                    continue
                assert top in PROVIDED_BY, f"{module.name} imports {top}, which requirements.txt does not provide"
                assert PROVIDED_BY[top] in required, f"{module.name} needs {PROVIDED_BY[top]} in requirements.txt"


def test_test_only_packages_stay_out_of_the_image():
    assert not requirement_names() & {"pytest", "httpx"}


def test_the_command_runs_the_factory_on_the_exposed_port():
    text = DOCKERFILE.read_text()
    assert 'CMD ["uvicorn", "app:build", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]' in text
    assert "EXPOSE 8080" in text
    assert "http://127.0.0.1:8080/api/health" in text, "the health check must use an allowed Host and the real port"


def test_it_runs_as_an_unprivileged_user():
    users = re.findall(r"^USER (\S+)", DOCKERFILE.read_text(), re.M)
    assert users and users[-1] not in ("root", "0")


def test_the_built_app_lands_where_the_settings_look():
    text = DOCKERFILE.read_text()
    assert "WORKDIR /app" in text and "COPY api/*.py ./" in text
    assert "COPY --from=web /web/dist ./static" in text
    settings = (API / "settings.py").read_text()
    assert 'Path(__file__).resolve().parent / "static"' in settings


def test_the_build_context_leaves_out_tests_secrets_and_local_builds():
    ignored = set((DASHBOARD / ".dockerignore").read_text().split())
    assert {"api/tests", "web/node_modules", "web/dist", "**/.env", "**/__pycache__"} <= ignored


def test_the_web_build_has_a_lockfile_for_npm_ci():
    web = DASHBOARD / "web"
    assert (web / "package.json").is_file() and (web / "package-lock.json").is_file()
    assert "COPY web/package.json web/package-lock.json ./" in DOCKERFILE.read_text()
