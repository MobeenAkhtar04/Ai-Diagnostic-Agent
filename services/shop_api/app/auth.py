"""Verifies the short lived internal tokens the worker sends (HS256 JWT with iat)."""

import os

import jwt

SECRET = os.environ.get("INTERNAL_JWT_SECRET", "dev-internal-secret")
LEEWAY_S = int(os.environ.get("JWT_LEEWAY_S", "10"))


def verify_internal_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"], leeway=LEEWAY_S, options={"require": ["iat", "exp"]})
