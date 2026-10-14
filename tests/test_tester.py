"""The tester's 501 stub and the rule that tester/ never imports app."""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as server_app

TESTER_DIR = Path(__file__).resolve().parents[1] / "tester"

client = TestClient(server_app)


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/tester/test/setup"), ("POST", "/tester/test/run"), ("GET", "/tester/test/report")],
)
def test_tester_paths_return_501(method, path):
    """Verify tester stubs return 501 with a message and the tester source."""
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["source"] == "tester"
    assert body["message"]


def _imported_modules(source: Path) -> list[str]:
    """List modules named by import and absolute from-import statements in a source file."""
    modules = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(source))):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_tester_never_imports_app():
    """Verify all tester Python files avoid importing the system's app package."""
    sources = sorted(TESTER_DIR.rglob("*.py"))
    assert sources
    for source in sources:
        for module in _imported_modules(source):
            assert module != "app" and not module.startswith("app."), f"{source} imports {module}"
