import os
import time

SECRET = "test-signing-key-long-enough-for-hmac-sha256-!!"
ISSUER = "ralphy-test"
AUDIENCE = "ralphy-test"

# app.main builds a module-level app at import time, which needs valid settings.
os.environ.setdefault("JWT_SECRET_KEY", SECRET)
os.environ.setdefault("JWT_ISSUER", ISSUER)
os.environ.setdefault("JWT_AUDIENCE", AUDIENCE)

import jwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(
        jwt_secret_key=SECRET,
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        studio_allowed_user_ids="7",
        cors_allowed_origins="https://tools.ralphalcaide.com",
        studio_data_dir=tmp_path / "studio",
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture
def make_token():
    def _make(sub="7", user_type="Work", secret=None, iss=None, aud=None, exp_delta=900,
              alg="HS256"):
        payload = {
            "sub": sub,
            "user_type": user_type,
            "iss": iss or ISSUER,
            "aud": aud or AUDIENCE,
            "exp": int(time.time()) + exp_delta,
        }
        return jwt.encode(payload, (secret or SECRET).encode("utf-8"), algorithm=alg)

    return _make


@pytest.fixture
def auth(make_token):
    return {"Authorization": f"Bearer {make_token()}"}
