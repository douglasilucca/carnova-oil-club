import pytest

from app import normalize_database_url


@pytest.mark.parametrize(
    "input_url, expected_url",
    [
        ("postgres://user:pass@host/db", "postgresql+psycopg2://user:pass@host/db"),
        ("postgresql://user:pass@host/db", "postgresql+psycopg2://user:pass@host/db"),
        ("postgresql+psycopg2://user:pass@host/db", "postgresql+psycopg2://user:pass@host/db"),
        ("sqlite:///oilclub.db", "sqlite:///oilclub.db"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        ("postgresql+psycopg://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
        ("postgresql+asyncpg://user:pass@host/db", "postgresql+asyncpg://user:pass@host/db"),
    ],
)
def test_normalize_database_url(input_url, expected_url):
    assert normalize_database_url(input_url) == expected_url
