from __future__ import annotations

from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.change_report import compute_change_report
from code_intel.cli import main
from code_intel.risk import top_risk_files
from code_intel.tests_map import find_related_tests


def test_catalog_find_explain_and_related_tests(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    result = build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    assert result.file_count == 4
    assert result.symbol_count >= 4
    assert result.dependency_count >= 2

    symbols = store.search_symbols("Service")
    assert symbols[0]["path"] == "src/app/service.py"

    report = compute_change_report(repo, store, "src/app/service.py")
    assert report.path == "src/app/service.py"
    assert "src/app/api.py" in report.direct_dependents
    assert "tests/test_service.py" in {match.path for match in report.related_tests}
    assert report.blast_score == 4.5
    assert report.risk == "low"

    related_tests = find_related_tests(repo, store, "src/app/service.py")
    assert related_tests[0].path == "tests/test_service.py"


def test_risk_report_orders_highest_scores_first(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    rows = top_risk_files(repo, store, limit=3)

    assert rows
    assert rows[0].score >= rows[-1].score
    assert any(row.path == "src/app/service.py" for row in rows)


def test_cli_smoke_scan_and_find(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)

    assert main(["scan", str(repo)]) == 0
    assert main(["find", "--repo", str(repo), "Service"]) == 0

    captured = capsys.readouterr()
    assert "Cataloged 4 files" in captured.out
    assert "src/app/service.py" in captured.out


def _make_python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/__init__.py").write_text("")
    (repo / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'ok'\n")
    (repo / "src/app/api.py").write_text(
        "from .service import Service\n\ndef handler() -> str:\n    return Service().run()\n"
    )
    (repo / "tests/test_service.py").write_text(
        "from src.app.service import Service\n\ndef test_service() -> None:\n    assert Service().run() == 'ok'\n"
    )
    return repo
