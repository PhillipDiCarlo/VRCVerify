"""Discord OAuth — used to establish *who* someone is, and nothing else.

The single most important thing in this module is what it throws away.

`login()` exchanges the code, reads the user's id and guild list, and then
discards the access token and refresh token without storing them anywhere. The
public host is assumed to be compromised eventually; a stored Discord token
would let whoever compromised it act as every user who ever logged in, against
Discord, indefinitely. An id and a stale guild list are worth far less.

What the guild list is *for* also matters. It renders the picker — which server
tiles to show, and which to grey out. It is **not** authority. The `permissions`
field Discord returns here describes the user at the moment they authorised,
and is used only as a display hint; every real decision is re-asked of the bot,
which reads its own gateway cache. See `bot_api.dashboard_is_admin`.
"""

from __future__ import annotations

import secrets
from typing import Optional
from urllib.parse import urlencode

import requests

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"

# `identify` gives us the user id. `guilds` gives us the list to render.
# Nothing else is requested, because nothing else is needed, and every extra
# scope is something a compromised host could have used.
SCOPES = "identify guilds"

# Discord's ADMINISTRATOR permission bit.
ADMINISTRATOR = 0x8


class OAuthError(Exception):
    """The authorisation could not be completed."""


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send the browser to start a login."""
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            # Always re-prompt. Without this Discord silently reuses an
            # existing authorisation, which makes "log in as someone else" on a
            # shared machine quietly impossible.
            "prompt": "consent",
        }
    )


def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> str:
    """Swap the authorisation code for an access token. Returns the token."""
    http = session or requests
    response = http.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    if response.status_code != 200:
        # Deliberately not echoing the body: it can contain the code we sent.
        raise OAuthError(f"token exchange failed ({response.status_code})")
    token = response.json().get("access_token")
    if not token:
        raise OAuthError("token exchange returned no access_token")
    return token


def fetch_identity(
    access_token: str, *, timeout: int = 10, session: Optional[requests.Session] = None
) -> tuple[str, list]:
    """Read the user's id and guild list. Returns (discord_id, guilds)."""
    http = session or requests
    headers = {"Authorization": f"Bearer {access_token}"}

    me = http.get(f"{DISCORD_API}/users/@me", headers=headers, timeout=timeout)
    if me.status_code != 200:
        raise OAuthError(f"could not read the user ({me.status_code})")
    discord_id = str(me.json()["id"])

    guilds = http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers, timeout=timeout)
    if guilds.status_code != 200:
        raise OAuthError(f"could not read the guild list ({guilds.status_code})")

    return discord_id, [_shape_guild(g) for g in guilds.json()]


def _shape_guild(raw: dict) -> dict:
    """Keep only what the picker draws, and label the hint as a hint.

    `admin_hint` is exactly that. It comes from the permissions Discord handed
    us at authorisation time, so it is already stale by the time it is
    rendered, and a user demoted since then would still see the tile. That is
    acceptable *because opening the server asks the bot*, which answers from
    its own gateway cache. Never let this field gate anything.
    """
    try:
        permissions = int(raw.get("permissions", 0))
    except (TypeError, ValueError):
        permissions = 0
    return {
        "id": str(raw.get("id", "")),
        "name": raw.get("name") or "(unnamed server)",
        "icon": raw.get("icon"),
        "admin_hint": bool(raw.get("owner")) or bool(permissions & ADMINISTRATOR),
    }


def login(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> tuple[str, list]:
    """The whole flow, ending with the token out of scope and unreferenced.

    Returning only (discord_id, guilds) is the point: there is no path by which
    a caller could persist the token, because it never leaves this function.
    `tests/test_dashboard.py` asserts that no stored session ever contains it.
    """
    access_token = exchange_code(
        code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        timeout=timeout,
        session=session,
    )
    return fetch_identity(access_token, timeout=timeout, session=session)


def icon_url(guild: dict, size: int = 64) -> Optional[str]:
    """Discord's CDN URL for a guild icon, at an explicit small size.

    Explicit size because the default is large, and the picker may draw dozens
    of these; the width is the whole cost of the page.
    """
    if not guild.get("icon") or not guild.get("id"):
        return None
    return (
        f"https://cdn.discordapp.com/icons/{guild['id']}/{guild['icon']}.png"
        f"?size={size}"
    )
