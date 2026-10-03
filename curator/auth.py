"""Sign-in: the reader app signs in with your OIDC provider (authorization
code + PKCE, a public client) and sends the access token as a bearer token.
Here we check it: RS256 pinned, key from the provider's JWKS, issuer,
audience and expiry. Anyone the provider lets in is a reader."""

import os
from dataclasses import dataclass
from functools import lru_cache

import jwt

from curator.config import OIDC_CLIENT_ID as AUDIENCE
from curator.config import OIDC_ISSUER as ISSUER
from curator.config import OWNER


@dataclass(frozen=True)
class User:
    id: str  # authentik username: stable, readable in the database
    name: str

    @property
    def is_owner(self) -> bool:
        return self.id == OWNER


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(ISSUER.rstrip("/") + "/jwks/", cache_keys=True, lifespan=3600)


def user_from_token(token: str) -> User | None:
    """The signed-in user, or None if the token isn't a valid authentik token
    for this app. The token's own header is never trusted for the algorithm."""
    if not token:
        return None
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER, leeway=30)
    except Exception:
        return None
    username = str(claims.get("preferred_username") or "")
    if not username:
        return None
    return User(id=username, name=str(claims.get("name") or username))
