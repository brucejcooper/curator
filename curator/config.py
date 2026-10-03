"""Where things live, and the settings that apply to everyone."""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CURATOR_DATA", REPO / "state"))
DB = DATA / "curator.sqlite"
FEEDS = DATA / "feeds"  # one folder per reader: feed.json, feed.xml
BACKUPS = DATA / "backups"
# The server the shared Mastodon hashtags are read from, with the owner's token.
MASTODON_SERVER = os.environ.get("MASTODON_SERVER", "mastodon.social")
OWNER = os.environ.get("CURATOR_OWNER", "admin")  # the admin; owns what predates multiple readers
PUBLIC_URL = os.environ.get("CURATOR_PUBLIC_URL", "http://localhost:8000")
# Where the code lives (feeds link to it; fetchers name it in User-Agent).
SITE = os.environ.get("CURATOR_SITE", PUBLIC_URL)
USER_AGENT = f"curator/0.1 (+{SITE})"
# Sign-in: your OIDC provider (authentik, ...) and the public client the reader app uses.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "curator")
SIGN_IN_LABEL = os.environ.get("CURATOR_SIGN_IN_LABEL", "Sign in")
# Where "importance as news" is judged from (traits are shared, so one place).
REGION = os.environ.get("CURATOR_REGION", "Australia")
# Profiles from before they moved into the database, imported once.
LEGACY_PROFILE = Path(os.environ.get("CURATOR_LEGACY_PROFILE", DATA / "repo" / "profile"))
