"""Secrets come from the environment (e.g. a Docker .env) or, failing that, the
login keychain on the Mac: service "curator", account = the variable name."""

import os


def get_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    import keyring

    value = keyring.get_password("curator", name)
    if not value:
        raise RuntimeError(f"{name} is not set in the environment or the keychain (service 'curator')")
    return value
