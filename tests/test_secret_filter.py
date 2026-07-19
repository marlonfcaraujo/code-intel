from __future__ import annotations

from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.secret_filter import REDACTED_SECRET, is_secret_path, redact_secret_line, redact_source_text


def test_secret_path_filter_matches_common_secret_files() -> None:
    assert is_secret_path(".env")
    assert is_secret_path("config/.env.production")
    assert is_secret_path("certs/private.pem")
    assert is_secret_path(".ssh/id_rsa")


def test_secret_path_filter_does_not_block_normal_source_names() -> None:
    assert not is_secret_path("src/app/secrets.py")
    assert not is_secret_path("src/config/settings.py")


def test_redact_secret_line_masks_provider_tokens_and_assignments() -> None:
    line = 'OPENAI_API_KEY = "sk-1234567890abcdefghijklmnopqrstuvwxyz"'

    redacted = redact_secret_line(line)

    assert "sk-1234567890" not in redacted
    assert redacted == f'OPENAI_API_KEY = "{REDACTED_SECRET}"'
    assert redact_secret_line("authorization = 'Bearer abcdefghijklmnopqrstuvwxyz123456'") == (
        f"authorization = 'Bearer {REDACTED_SECRET}'"
    )


def test_redact_source_text_preserves_private_key_block_line_count() -> None:
    source = "\n".join(
        [
            "PRIVATE_KEY = '''-----BEGIN PRIVATE KEY-----",
            "abc123abc123abc123abc123abc123abc123abc123",
            "-----END PRIVATE KEY-----'''",
        ]
    )

    redacted = redact_source_text(source)

    assert redacted.splitlines() == [REDACTED_SECRET, REDACTED_SECRET, REDACTED_SECRET]


def test_catalog_text_index_redacts_private_key_block_body(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    private_key_body = "abc123abc123abc123abc123abc123abc123abc123"
    (repo / "settings.py").write_text(
        "\n".join(
            [
                "PRIVATE_KEY = '''-----BEGIN PRIVATE KEY-----",
                private_key_body,
                "-----END PRIVATE KEY-----'''",
            ]
        )
    )

    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    assert store.search_text(private_key_body, context_lines=0) == []
    assert store.source_line_range("settings.py", 1, 3) == {
        1: REDACTED_SECRET,
        2: REDACTED_SECRET,
        3: REDACTED_SECRET,
    }
