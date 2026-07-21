from __future__ import annotations

import pytest

from code_intel.provider_config import (
    DEFAULT_BENCHMARK_PROVIDERS,
    resolve_default_benchmark_providers,
    resolve_default_symbol_provider,
)


def test_resolve_default_symbol_provider_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_INTEL_DEFAULT_SYMBOL_PROVIDER", raising=False)
    assert resolve_default_symbol_provider() == "catalog"

    monkeypatch.setenv("CODE_INTEL_DEFAULT_SYMBOL_PROVIDER", "jcodemunch")
    assert resolve_default_symbol_provider() == "jcodemunch"

    monkeypatch.setenv("CODE_INTEL_DEFAULT_SYMBOL_PROVIDER", "unknown")
    assert resolve_default_symbol_provider() == "catalog"


def test_resolve_default_benchmark_providers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_INTEL_BENCHMARK_PROVIDERS", raising=False)
    assert resolve_default_benchmark_providers() == DEFAULT_BENCHMARK_PROVIDERS

    monkeypatch.setenv("CODE_INTEL_BENCHMARK_PROVIDERS", "jcodemunch,catalog")
    assert resolve_default_benchmark_providers() == ("jcodemunch", "catalog")

    monkeypatch.setenv("CODE_INTEL_BENCHMARK_PROVIDERS", "unknown")
    assert resolve_default_benchmark_providers() == DEFAULT_BENCHMARK_PROVIDERS
