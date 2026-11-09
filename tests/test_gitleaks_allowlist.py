from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POOL_ROTATION_FIXTURE_FINGERPRINT = (
    "3c26b48129a731973fc1c078f5447968f5c22558:"
    "tests/db/test_pool_rotation.py:generic-api-key:168"
)


def test_pool_rotation_fixture_is_allowlisted_by_exact_fingerprint() -> None:
    """Keep the full-history false-positive exception narrowly scoped."""
    entries = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".gitleaksignore").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert POOL_ROTATION_FIXTURE_FINGERPRINT in entries
    assert not any(entry.endswith(":tests/db/test_pool_rotation.py") for entry in entries)
