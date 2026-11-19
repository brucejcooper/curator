"""Sign-in: the reader app signs in with your OIDC provider (authorization
code + PKCE, a public client) and sends the access token as a bearer token.
Here we check it: RS256 pinned, key from the provider's JWKS, issuer,
audience and expiry. Anyone the provider lets in is a reader.

The provider being down is not the token being bad (#37): while its JWKS
can't be fetched, a token is checked against the keys last fetched, with the
same checks; with no key to check it against, sign-in is unavailable
(AuthUnavailable, a 503), never "signed out" (a 401 ends the reader's session)."""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt

from curator.config import OIDC_CLIENT_ID as AUDIENCE
from curator.config import OIDC_ISSUER as ISSUER
from curator.config import OWNER

log = logging.getLogger("curator.auth")


class AuthUnavailable(Exception):
    """The provider's keys couldn't be fetched, and none we hold fit the token:
    we can't tell whether it is valid."""


@dataclass(frozen=True)
class User:
    id: str  # authentik username: stable, readable in the database
    name: str

    @property
    def is_owner(self) -> bool:
        return self.id == OWNER


class Jwks(jwt.PyJWKClient):
    """The provider's signing keys, remembering the last set fetched for when
    the provider can't be reached. An outage is logged once, when it starts."""

    def __init__(self, uri: str):
        super().__init__(uri, cache_keys=True, lifespan=3600, timeout=10)
        self.last: list[jwt.PyJWK] = []
        self.down = False

    def fetch_data(self):
        try:
            data = super().fetch_data()
        except json.JSONDecodeError as e:  # a proxy's error page, say
            raise jwt.PyJWKClientConnectionError(f"The JWKS endpoint didn't answer JSON: {e}") from e
        if self.down:
            log.warning("sign-in keys reachable again at %s", self.uri)
            self.down = False
        return data

    def get_signing_keys(self, refresh: bool = False) -> list[jwt.PyJWK]:
        self.last = super().get_signing_keys(refresh)
        return self.last

    def get_signing_key(self, kid: str) -> jwt.PyJWK:
        try:
            return super().get_signing_key(kid)
        except (jwt.PyJWKClientConnectionError, jwt.PyJWKSetError) as e:  # unreachable, or no usable keys
            if not self.down:
                log.warning("can't fetch sign-in keys from %s, using the last ones fetched: %s", self.uri, e)
                self.down = True
            key = self.match_kid(self.last, kid)
            if key is None:
                raise AuthUnavailable(str(e)) from e
            return key


@lru_cache(maxsize=1)
def _jwks() -> Jwks:
    return Jwks(ISSUER.rstrip("/") + "/jwks/")


def user_from_token(token: str) -> User | None:
    """The signed-in user, or None if the token isn't a valid authentik token
    for this app. The token's own header is never trusted for the algorithm.
    Raises AuthUnavailable if the provider can't be asked for the key."""
    if not token:
        return None
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER, leeway=30)
    except (jwt.InvalidTokenError, jwt.PyJWKClientError):
        return None
    username = str(claims.get("preferred_username") or "")
    if not username:
        return None
    return User(id=username, name=str(claims.get("name") or username))
