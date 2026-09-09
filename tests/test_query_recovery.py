"""Public synthetic retrieval cases independent of the original agent pilot."""

from pathlib import Path

import pytest

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.lookup import lookup, lookup_to_dict
from code_intel.query_recovery import query_terms

CASES = [
    (
        "cache",
        "invalidate_cache",
        "Remove cached entries for a namespace.",
        "where are cached entries invalidated for a namespace",
    ),
    (
        "retry",
        "retry_request",
        "Retry failed requests with exponential backoff.",
        "how do failed requests use exponential backoff",
    ),
    (
        "limits",
        "enforce_upload_limit",
        "Reject uploads larger than maximum bytes.",
        "reject oversized uploads above maximum bytes",
    ),
    (
        "identity",
        "normalize_identifier",
        "Normalize identifiers by stripping whitespace.",
        "strip whitespace from identifiers",
    ),
    ("storage", "load_snapshot", "Read a JSON snapshot from disk.", "load saved JSON snapshot from disk"),
    ("auth", "redact_credentials", "Hide credentials before writing audit logs.", "hide credentials in audit logs"),
]


@pytest.fixture
def repository(tmp_path: Path):
    (tmp_path / "src").mkdir()
    for module, name, doc, _query in CASES:
        (tmp_path / "src" / f"{module}.py").write_text(f'def {name}(value):\n    """{doc}"""\n    return value\n')
    (tmp_path / "src" / "policy.py").write_text(
        'def resolve_policy(values):\n    """Apply policy."""\n'
        '    values.setdefault("enabled", True)\n    values.setdefault("timeout", 60)\n    return values\n'
    )
    (tmp_path / "src" / "noise.py").write_text(
        "\n".join(
            f'def unrelated_{index}(value):\n    """Process input state."""\n    return value\n' for index in range(50)
        )
    )
    build_catalog(tmp_path)
    return tmp_path, CatalogStore.for_repo(tmp_path)


@pytest.mark.parametrize("module,name,doc,question", CASES)
def test_distinct_questions_retrieve_correct_symbol_in_top_three(repository, module, name, doc, question):
    root, store = repository
    result = lookup(root, store, question, limit=3, include_tests=False)
    assert any(hit.path == f"src/{module}.py" and hit.label == f"function {name}" for hit in result.hits)
    assert result.recovery_terms
    assert len(result.hits) <= 3
    payload = lookup_to_dict(result, store=store)
    target = next(hit for hit in payload["hits"] if hit["path"] == f"src/{module}.py")
    assert target["summary"] == doc
    assert f"def {name}" in target["context"]["content"]


def test_source_body_evidence_recovers_without_descriptive_docstring(repository):
    root, store = repository
    result = lookup(root, store, "missing enabled timeout defaults", limit=3, include_tests=False)
    assert result.hits[0].label == "function resolve_policy"


def test_exact_identifier_never_enters_recovery(repository, monkeypatch):
    root, store = repository

    def unexpected(*args, **kwargs):
        raise AssertionError("Exact lookup must not enter recovery")

    monkeypatch.setattr(store, "keyword_symbol_candidates", unexpected)
    result = lookup(root, store, "invalidate_cache")
    assert result.hits[0].label == "function invalidate_cache"
    assert not result.recovery_terms


def test_no_match_guidance_and_noise_rejection(repository):
    root, store = repository
    for query in ("please tell me where the function is", "galactic banana telemetry", "cache interstellar banana"):
        payload = lookup_to_dict(lookup(root, store, query))
        assert not payload["hits"]
        assert "identifier" in payload["guidance"]


def test_terms_are_bounded_and_identifiers_preserved():
    assert query_terms("some_exact_identifier") == []
    assert query_terms("x" * 513) == []
    assert len(query_terms("one two three four five six seven eight nine ten")) <= 8
    assert query_terms("atomically publishes snapshots") == ["atomic", "publish", "snapshot"]


def test_recovery_respects_disabled_symbol_and_text_facets(repository, monkeypatch):
    root, store = repository
    seen = []

    def collect(terms, *, include_body):
        seen.append(include_body)
        return []

    monkeypatch.setattr(store, "keyword_symbol_candidates", collect)
    lookup(root, store, "missing enabled timeout defaults", text_limit=0)
    assert seen == [False]
    lookup(root, store, "missing enabled timeout defaults", symbol_limit=0)
    assert seen == [False]
    lookup(root, store, "missing enabled timeout defaults", include_fuzzy_symbols=False)
    assert seen == [False]


def test_context_uses_redacted_indexed_source(repository):
    root, _store = repository
    (root / "src" / "credentials.py").write_text(
        'def credential_policy():\n    """Return credentials policy."""\n'
        '    password = "synthetic-password-value"\n    return bool(password)\n'
    )
    build_catalog(root)
    store = CatalogStore.for_repo(root)
    payload = lookup_to_dict(lookup(root, store, "credential_policy"), store=store)
    context = payload["hits"][0]["context"]["content"]
    assert "synthetic-password-value" not in context
    assert "REDACTED_SECRET" in context


def test_invalid_sql_terms_and_result_context_bounds(repository):
    root, store = repository
    assert store.keyword_symbol_candidates(["foo%bar", "' OR 1=1"]) == []
    payload = lookup_to_dict(lookup(root, store, "load saved JSON snapshot from disk"), store=store)
    for hit in payload["hits"]:
        if "context" in hit:
            assert hit["context"]["end_line"] - hit["context"]["start_line"] < 40
            assert len(hit["context"]["content"]) <= 6000
