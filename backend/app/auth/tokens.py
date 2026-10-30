from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import HTTPException, status

from app.config import get_settings

ALGORITHM = "HS256"


def create_access_token(*, user_id: UUID, tenant_id: UUID, permission_version: int, token_type: str = "user") -> str:
    """签发短期会话令牌；令牌声明用于识别用户，不直接授予访问权限。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "pv": permission_version,
        "typ": token_type,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, *, expected_type: str = "user") -> tuple[UUID, UUID]:
    """验证令牌签名、签发方和受众，仅返回用户与租户标识。"""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "tid", "pv", "exp", "iat", "iss", "aud"]},
        )
        if claims.get("typ", "user") != expected_type:
            raise ValueError("Unexpected token type")
        return UUID(claims["sub"]), UUID(claims["tid"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session") from exc

