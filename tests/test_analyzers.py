from __future__ import annotations

from pathlib import Path

from code_intel.analyzers.css import CssAnalyzer
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
    assert analysis.dependencies[0].category == "code"


def test_python_analyzer_extracts_top_level_public_constants(tmp_path: Path) -> None:
    target = tmp_path / "constants.py"
    target.write_text(
        "DEFAULT_TIMEOUT = 30\n"
        "VALUE_CONFIGS: dict[str, str] = {'alpha': 'beta'}\n"
        "URL_A, URL_B = ('a', 'b')\n"
        "_PRIVATE_SETTING = 'hidden'\n"
        "logger = object()\n"
    )

    analysis = PythonAnalyzer().analyze(target, tmp_path, {"constants.py"})
    symbols = {symbol.name: symbol for symbol in analysis.symbols}

    assert symbols["DEFAULT_TIMEOUT"].kind == "constant"
    assert symbols["VALUE_CONFIGS"].signature.startswith("VALUE_CONFIGS:")
    assert symbols["URL_A"].line == 3
    assert symbols["URL_B"].line == 3
    assert "_PRIVATE_SETTING" not in symbols
    assert "logger" not in symbols


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
    assert analysis.dependencies[0].category == "code"
    assert {symbol.name for symbol in analysis.symbols} == {"View"}


def test_javascript_analyzer_classifies_components_hooks_and_constants(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    target = source_dir / "view.jsx"
    target.write_text(
        "const MAX_ITEMS = 10;\n"
        "function LocalPanel() { return <section /> }\n"
        "const MiniRackColumn = ({ rack }) => <div>{rack}</div>;\n"
        "export const useRackCapacity = () => ({ available: true });\n"
        "export const SurfacePanel = ({ children }) => <section>{children}</section>;\n"
        "export default memo(function EmptyState() { return <p /> });\n"
    )

    analysis = JavaScriptAnalyzer().analyze(target, tmp_path, {"src/view.jsx"})
    symbols = {symbol.name: symbol for symbol in analysis.symbols}

    assert symbols["MAX_ITEMS"].kind == "constant"
    assert symbols["LocalPanel"].kind == "component"
    assert symbols["MiniRackColumn"].kind == "component"
    assert symbols["useRackCapacity"].kind == "hook"
    assert symbols["SurfacePanel"].kind == "component"
    assert symbols["EmptyState"].kind == "component"


def test_javascript_analyzer_resolves_css_imports_when_css_is_cataloged(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "styles.css").write_text(".panel { color: red; }\n")
    target = source_dir / "view.jsx"
    target.write_text("import './styles.css'\nexport const View = () => null\n")

    analysis = JavaScriptAnalyzer().analyze(target, tmp_path, {"src/styles.css", "src/view.jsx"})

    assert analysis.dependencies[0].target_path == "src/styles.css"
    assert analysis.dependencies[0].resolved is True
    assert analysis.dependencies[0].category == "code"


def test_javascript_analyzer_classifies_external_asset_and_unresolved_imports(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    asset_dir = tmp_path / "assets"
    source_dir.mkdir()
    asset_dir.mkdir()
    (asset_dir / "logo.png").write_bytes(b"png")
    target = source_dir / "view.jsx"
    target.write_text(
        "import React from 'react'\n"
        "import logo from '../assets/logo.png?url'\n"
        "import missing from './missing'\n"
        "export const View = () => <img src={logo} alt={missing} />\n"
    )

    analysis = JavaScriptAnalyzer().analyze(target, tmp_path, {"src/view.jsx"})
    dependencies = {dependency.import_name: dependency for dependency in analysis.dependencies}

    assert dependencies["react"].target_path == "react"
    assert dependencies["react"].category == "external"
    assert dependencies["react"].resolved is False
    assert dependencies["../assets/logo.png?url"].target_path == "assets/logo.png"
    assert dependencies["../assets/logo.png?url"].category == "asset"
    assert dependencies["../assets/logo.png?url"].resolved is False
    assert dependencies["./missing"].target_path == "./missing"
    assert dependencies["./missing"].category == "unresolved"
    assert dependencies["./missing"].resolved is False


def test_python_analyzer_classifies_unresolved_absolute_import_as_external(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    target = package / "service.py"
    target.write_text("import httpx\nimport json\nfrom .missing import helper\n")

    analysis = PythonAnalyzer().analyze(target, tmp_path, {"pkg/service.py"})
    dependencies = {dependency.import_name: dependency for dependency in analysis.dependencies}

    assert dependencies["httpx"].target_path == "httpx"
    assert dependencies["httpx"].category == "external"
    assert dependencies["httpx"].resolved is False
    assert dependencies["json"].target_path == "json"
    assert dependencies["json"].category == "stdlib"
    assert dependencies["json"].resolved is False
    assert dependencies[".missing"].target_path == "missing"
    assert dependencies[".missing"].category == "unresolved"
    assert dependencies[".missing"].resolved is False


def test_css_analyzer_extracts_selectors_and_keyframes(tmp_path: Path) -> None:
    target = tmp_path / "styles.css"
    target.write_text(".panel { color: red; }\n@keyframes fade-in { from { opacity: 0; } }\n")

    analysis = CssAnalyzer().analyze(target, tmp_path, {"styles.css"})

    assert analysis.source_file.language == "css"
    assert [symbol.name for symbol in analysis.symbols] == [".panel", "@keyframes fade-in"]
