"""Sign-in tokens, checked offline against a local key: a bad token is a 401,
but the provider being down is not (#37): tokens are checked against the keys
last fetched, and with none to check against the API answers 503."""

import io
import json
import time
import urllib.request
from urllib.error import URLError

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from curator import auth, server

ISSUER = "https://auth.example/application/o/curator/"


def keypair(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**public, "kid": kid, "use": "sig", "alg": "RS256"}


KEY, JWK = keypair("k1")
OTHER, _ = keypair("k1")  # the same kid, but not the provider's key


class Provider:
    """The provider's JWKS endpoint, up or down."""

    def __init__(self):
        self.up = True
        self.fetches = 0

    def open(self, request, timeout=None):
        self.fetches += 1
        if not self.up:
            raise URLError("Connection refused")
        return io.BytesIO(json.dumps({"keys": [JWK]}).encode())


@pytest.fixture
def provider(monkeypatch):
    p = Provider()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: p)
    monkeypatch.setattr(auth, "ISSUER", ISSUER)
    monkeypatch.setattr(auth, "AUDIENCE", "curator")
    auth._jwks.cache_clear()
    yield p
    auth._jwks.cache_clear()


def token(key=KEY, kid="k1", **claims):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": "curator", "iat": now, "exp": now + 300, "preferred_username": "alex", "name": "Alex"}
    return jwt.encode({**body, **claims}, key, algorithm="RS256", headers={"kid": kid})


def expire_cache():
    """As an hour on: the key set is due to be fetched again."""
    client = auth._jwks()
    client.jwk_set_cache.put(None)
    client.get_signing_key.cache_clear()


def test_a_valid_token_is_a_reader_and_a_bad_one_is_not(provider):
    assert auth.user_from_token(token()) == auth.User("alex", "Alex")
    assert auth.user_from_token(token(exp=int(time.time()) - 120)) is None
    assert auth.user_from_token(token(aud="someone-else")) is None
    assert auth.user_from_token(token(iss="https://evil.example/")) is None
    assert auth.user_from_token(token(key=OTHER)) is None
    assert auth.user_from_token("not.a.token") is None
    assert auth.user_from_token(jwt.encode({"preferred_username": "alex"}, "a-shared-secret-at-least-32-bytes-long", algorithm="HS256")) is None


def test_while_the_provider_is_down_the_last_keys_still_check_tokens(provider):
    assert auth.user_from_token(token()) == auth.User("alex", "Alex")
    provider.up = False
    expire_cache()
    assert auth.user_from_token(token()) == auth.User("alex", "Alex")
    assert auth._jwks().down
    # The same checks: an expired or forged token is still refused.
    assert auth.user_from_token(token(exp=int(time.time()) - 120)) is None
    assert auth.user_from_token(token(key=OTHER)) is None
    # A key we've never seen can't be checked: unavailable, not invalid.
    with pytest.raises(auth.AuthUnavailable):
        auth.user_from_token(token(kid="k2"))
    provider.up = True
    expire_cache()
    assert auth.user_from_token(token()) == auth.User("alex", "Alex")
    assert not auth._jwks().down


def test_with_no_keys_ever_fetched_sign_in_is_unavailable(provider):
    provider.up = False
    with pytest.raises(auth.AuthUnavailable):
        auth.user_from_token(token())


def test_an_error_page_instead_of_keys_is_the_provider_down(provider, monkeypatch):
    monkeypatch.setattr(provider, "open", lambda request, timeout=None: io.BytesIO(b"<html>502 Bad Gateway</html>"))
    with pytest.raises(auth.AuthUnavailable):
        auth.user_from_token(token())


def test_the_api_answers_503_not_401_when_it_cant_check(provider, alex):
    client = TestClient(server.app)
    bearer = {"Authorization": f"Bearer {token()}"}
    provider.up = False
    r = client.get("/api/me", headers=bearer)
    assert r.status_code == 503 and r.json() == {"error": "sign-in unavailable"} and r.headers["retry-after"]
    provider.up = True
    assert client.get("/api/me", headers=bearer).json()["user"] == "alex"
    provider.up = False
    expire_cache()
    assert client.get("/api/me", headers=bearer).status_code == 200
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token(key=OTHER)}"}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token(exp=int(time.time()) - 120)}"}).status_code == 401
