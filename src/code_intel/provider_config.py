"""Config helpers for symbol provider selection.

These defaults are intentionally environment-driven so users can switch primary
symbol providers and benchmark provider sets without changing code or repo
configuration.
"""

from __future__ import annotations

import os
from typing import Literal, cast

ProviderName = Literal["catalog", "jcodemunch"]

DEFAULT_SYMBOL_PROVIDER: ProviderName = "catalog"
DEFAULT_BENCHMARK_PROVIDERS: tuple[ProviderName, ...] = ("catalog", "jcodemunch")

_DEFAULT_PROVIDER_ENV = "CODE_INTEL_DEFAULT_SYMBOL_PROVIDER"
_DEFAULT_BENCHMARK_PROVIDERS_ENV = "CODE_INTEL_BENCHMARK_PROVIDERS"


def resolve_default_symbol_provider() -> ProviderName:
    """Return configured default symbol provider.

    Returns ``catalog`` when no env var is set or when the configured value is not
    supported.
    """
    return _parse_single_provider(os.getenv(_DEFAULT_PROVIDER_ENV), fallback=DEFAULT_SYMBOL_PROVIDER)


def resolve_default_benchmark_providers() -> tuple[ProviderName, ...]:
    """Return configured benchmark provider ordering.

    Uses comma-separated values in ``CODE_INTEL_BENCHMARK_PROVIDERS`` and filters
    unsupported values. Falls back to ``(catalog, jcodemunch)`` when nothing
    remains after filtering.
    """
    return _parse_provider_list(os.getenv(_DEFAULT_BENCHMARK_PROVIDERS_ENV), fallback=DEFAULT_BENCHMARK_PROVIDERS)


def _parse_single_provider(value: str | None, *, fallback: ProviderName) -> ProviderName:
    """Parse a provider string into a supported ``ProviderName``."""
    if value is None:
        return fallback

    normalized = value.strip().lower()
    if normalized in _supported_providers():
        return cast(ProviderName, normalized)
    return fallback


def _parse_provider_list(value: str | None, *, fallback: tuple[ProviderName, ...]) -> tuple[ProviderName, ...]:
    """Parse a comma-separated provider list with deduplication."""
    if not value:
        return fallback

    provider_candidates = [item.strip().lower() for item in value.split(",") if item.strip()]
    parsed: list[ProviderName] = []
    for provider in provider_candidates:
        if provider in _supported_providers() and provider not in parsed:
            parsed.append(cast(ProviderName, provider))
    return tuple(parsed) if parsed else fallback


def _supported_providers() -> set[str]:
    return {"catalog", "jcodemunch"}
