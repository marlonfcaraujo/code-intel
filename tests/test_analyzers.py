from __future__ import annotations

from pathlib import Path

from code_intel.analyzers.javascript import JavaScriptAnalyzer
from code_intel.analyzers.python import PythonAnalyzer


def test_python_analyzer_extracts_symbols_and_internal_imports(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "helpers.py").write_text("def helper() -> str:\n    return 'ok'\n")
    target = package / "service.py"
    target.write_text(
        "from .helpers import helper\n\nclass Service:\n    def run(self) -> str:\n        return helper()\n"
    )

    all_paths = {
        "pkg/__init__.py",
        "pkg/helpers.py",
        "pkg/service.py",
    }
    analysis = PythonAnalyzer().analyze(target, tmp_path, all_paths)

    assert [symbol.qualified_name for symbol in analysis.symbols] == ["Service", "Service.run"]
    assert analysis.dependencies[0].target_path == "pkg/helpers.py"
    assert analysis.dependencies[0].resolved is True


def test_python_analyzer_resolves_src_layout_absolute_imports(tmp_path: Path) -> None:
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "service.py").write_text("class Service:\n    pass\n")
    target = package / "api.py"
    target.write_text("from app.service import Service\n")

    analysis = PythonAnalyzer().analyze(target, tmp_path, {"src/app/api.py", "src/app/service.py"})

    assert analysis.dependencies[0].target_path == "src/app/service.py"
    assert analysis.dependencies[0].resolved is True


def test_javascript_analyzer_resolves_relative_imports(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "client.js").write_text("export function request() { return true }\n")
    target = source_dir / "view.jsx"
    target.write_text("import { request } from './client'\nexport const View = () => request()\n")

    analysis = JavaScriptAnalyzer().analyze(target, tmp_path, {"src/client.js", "src/view.jsx"})

    assert analysis.dependencies[0].target_path == "src/client.js"
    assert analysis.dependencies[0].resolved is True
    assert {symbol.name for symbol in analysis.symbols} == {"View"}
