from __future__ import annotations

from pathlib import Path

from code_intel.cataloger import build_catalog
from code_intel.mcp_server import (
    catalog_health_tool,
    catalog_repo_tool,
    explain_file_tool,
    find_symbols_tool,
    related_tests_tool,
    risk_report_tool,
    savings_report_tool,
)


def test_mcp_tool_helpers_return_catalog_data(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    catalog = catalog_repo_tool(str(repo))
    symbols = find_symbols_tool("Service", repo_path=str(repo))
    explanation = explain_file_tool("src/app/service.py", repo_path=str(repo))
    tests = related_tests_tool("src/app/service.py", repo_path=str(repo))
    risk = risk_report_tool(repo_path=str(repo), limit=5)
    health = catalog_health_tool(str(repo))
    savings = savings_report_tool(str(repo))

    assert catalog["file_count"] == 4
    assert symbols["symbols"][0]["path"] == "src/app/service.py"
    assert "src/app/api.py" in explanation["direct_dependents"]
    assert tests["tests"][0]["path"] == "tests/test_service.py"
    assert risk["files"]
    assert health["catalog_exists"] is True
    assert savings["events"] >= 4
    assert savings["estimated_saved_tokens"] > 0


def test_mcp_related_tests_handles_unknown_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    result = related_tests_tool("missing.py", repo_path=str(repo))

    assert result["ok"] is False
    assert result["tests"] == []


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/service.py").write_text("class Service:\n    pass\n")
    (repo / "src/app/api.py").write_text("from .service import Service\n")
    (repo / "src/app/extra.py").write_text("\n".join(f"VALUE_{index} = {index}" for index in range(60)))
    (repo / "tests/test_service.py").write_text("from src.app.service import Service\n")
    return repo
