"""Language analyzer registry."""

from code_intel.analyzers.base import Analyzer
from code_intel.analyzers.css import CssAnalyzer
from code_intel.analyzers.javascript import JavaScriptAnalyzer
from code_intel.analyzers.python import PythonAnalyzer

ANALYZERS: tuple[Analyzer, ...] = (
    PythonAnalyzer(),
    JavaScriptAnalyzer(),
    CssAnalyzer(),
)

SUPPORTED_EXTENSIONS = frozenset(extension for analyzer in ANALYZERS for extension in analyzer.extensions)

__all__ = ["ANALYZERS", "SUPPORTED_EXTENSIONS", "Analyzer"]
