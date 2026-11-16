"""WFM authentication for listing sync (stretch goal).

Order of attempts: explicit JWT (--jwt / WFM_JWT) -> cached token -> v1 email/password sign-in.
Every token is checked with GET /v2/me before use. Tokens are never printed or logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wfopt.config import Config
from wfopt.market.client import WfmClient, WfmError, v2_data

SIGNIN_URL = "https://api.warframe.market/v1/auth/signin"


class AuthError(RuntimeError):
    pass


@dataclass
class Me:
    ingame_name: str
    id: str
    source: str  # where the token came from (never the token itself)


def _token_file(cfg: Config) -> Path:
    return cfg.data_dir / "wfm_token.json"


def _strip(tok: str) -> str:
    tok = tok.strip()
    for p in ("JWT ", "Bearer "):
        if tok.startswith(p):
            tok = tok[len(p):]
    return tok.strip()


def signin(client: WfmClient, email: str, password: str) -> str:
    resp = client.request(
        "POST",
        SIGNIN_URL,
        json_body={"email": email, "password": password, "auth_type": "header"},
        extra_headers={"Authorization": "JWT"},
    )
    if resp.status_code >= 400:
        # body may contain a reason (bad credentials, captcha); it never contains the password
        raise AuthError(f"v1 sign-in failed ({resp.status_code}): {resp.text[:300]}")
    tok = resp.headers.get("Authorization", "")
    if not tok or tok.strip() == "JWT":
        raise AuthError("v1 sign-in returned no token. Fall back to --jwt / WFM_JWT (the JWT cookie from your browser).")
    return _strip(tok)


def check_token(client: WfmClient, token: str) -> dict | None:
    old = client.token
    client.token = token
    try:
        resp = client.request("GET", "/v2/me", auth=True)
    finally:
        client.token = old
    if resp.status_code != 200:
        return None
    try:
        data = v2_data(resp.json())
    except (ValueError, WfmError):
        return None
    if not data or not (data.get("ingameName") or data.get("id")):
        return None
    return data


def authenticate(cfg: Config, client: WfmClient, jwt: str | None = None) -> Me:
    candidates: list[tuple[str, str]] = []
    if jwt:
        candidates.append(("--jwt", _strip(jwt)))
    if os.environ.get("WFM_JWT"):
        candidates.append(("WFM_JWT", _strip(os.environ["WFM_JWT"])))
    tf = _token_file(cfg)
    if tf.exists():
        try:
            candidates.append(("cached token", _strip(json.loads(tf.read_text(encoding="utf-8"))["token"])))
        except (OSError, ValueError, KeyError):
            pass
    for source, tok in candidates:
        me = check_token(client, tok)
        if me:
            return _use(cfg, client, tok, me, source)

    email, password = os.environ.get("WFM_EMAIL"), os.environ.get("WFM_PASSWORD")
    if not (email and password):
        raise AuthError("No valid token and no WFM_EMAIL / WFM_PASSWORD in .env. Set them, or pass --jwt.")
    tok = signin(client, email, password)
    me = check_token(client, tok)
    if not me:
        raise AuthError("Signed in, but /v2/me rejected the token. Try --jwt with the browser cookie.")
    return _use(cfg, client, tok, me, "email/password sign-in")


def _use(cfg: Config, client: WfmClient, tok: str, me: dict, source: str) -> Me:
    client.token = tok
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    _token_file(cfg).write_text(json.dumps({"token": tok}), encoding="utf-8")
    return Me(me.get("ingameName", ""), me.get("id", ""), source)
