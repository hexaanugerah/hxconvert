"""Configuration parsing for cloud deployment (Vercel/Railway/TiDB)."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite://",
        "storage_dir": Path("storage"),
        "environment": "development",
        "cookie_samesite": "lax",
        "cookie_secure": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mysql://user:pw@host:4000/db", "mysql+pymysql://user:pw@host:4000/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:////tmp/app.db", "sqlite:////tmp/app.db"),
        ("  mysql://u:p@h/db  ", "mysql+pymysql://u:p@h/db"),
    ],
)
def test_database_url_normalization(raw, expected):
    assert make_settings(database_url=raw).database_url == expected


def test_explicit_database_driver_is_preserved():
    url = "mysql+pymysql://u:p@h/db"
    assert make_settings(database_url=url).database_url == url


def test_cookies_are_secure_by_default_in_production():
    assert make_settings(environment="production").cookie_is_secure is True


def test_cookies_are_plain_http_in_development():
    assert make_settings(environment="development").cookie_is_secure is False


def test_samesite_none_forces_secure_cookies():
    # Browsers reject SameSite=None without Secure, even on localhost.
    settings = make_settings(environment="development", cookie_samesite="none", cookie_secure=False)
    assert settings.cookie_is_secure is True


def test_explicit_cookie_secure_wins():
    assert make_settings(environment="production", cookie_secure=False).cookie_is_secure is False
    assert make_settings(environment="development", cookie_secure=True).cookie_is_secure is True


def test_cookie_samesite_is_normalized():
    assert make_settings(cookie_samesite="None").cookie_samesite == "none"
    assert make_settings(cookie_samesite=" LAX ").cookie_samesite == "lax"


def test_invalid_cookie_samesite_is_rejected():
    with pytest.raises(ValidationError):
        make_settings(cookie_samesite="invalid")


def test_absolute_storage_dir_is_kept(tmp_path):
    settings = make_settings(storage_dir=tmp_path)
    assert settings.storage_dir == tmp_path
