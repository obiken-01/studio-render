import jwt
from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.errors import ApiError


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "unauthorized", "Please sign in again.")
    try:
        claims = jwt.decode(
            token.strip(),
            settings.jwt_secret_key.encode("utf-8"),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=0,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        raise ApiError(401, "unauthorized", "Please sign in again.") from None
    if claims.get("user_type") != "Work":
        raise ApiError(403, "forbidden", "Studio is for Work accounts only.")
    sub = str(claims["sub"])
    if sub not in settings.allowed_user_ids:
        raise ApiError(403, "forbidden", "Studio is private.")
    return sub
