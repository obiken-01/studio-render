import pytest

from app.config import Settings


def test_short_secret_refuses_to_start():
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(jwt_secret_key="short", jwt_issuer="i", jwt_audience="a")


def test_empty_allowlist_allows_nobody(settings):
    s = settings.model_copy(update={"studio_allowed_user_ids": ""})
    assert s.allowed_user_ids == frozenset()


def test_allowlist_parses_and_trims():
    s = Settings(jwt_secret_key="x" * 32, jwt_issuer="i", jwt_audience="a",
                 studio_allowed_user_ids=" 7, 12 ,")
    assert s.allowed_user_ids == frozenset({"7", "12"})


def test_cors_origins_parse_and_trim():
    s = Settings(jwt_secret_key="x" * 32, jwt_issuer="i", jwt_audience="a",
                 cors_allowed_origins=" https://a.example , https://b.example ,")
    assert s.cors_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_issuer_refuses_to_start(value):
    with pytest.raises(ValueError, match="JWT_ISSUER"):
        Settings(jwt_secret_key="x" * 32, jwt_issuer=value, jwt_audience="a")


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_audience_refuses_to_start(value):
    with pytest.raises(ValueError, match="JWT_AUDIENCE"):
        Settings(jwt_secret_key="x" * 32, jwt_issuer="i", jwt_audience=value)
