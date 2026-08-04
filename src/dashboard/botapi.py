"""The client half of the bot's internal API.

Every call carries two independent proofs:

* the **client certificate**, presented at the TLS layer and checked against
  the bot's CA (and, in production, pinned by CN). This authenticates the
  *dashboard*.
* a **scoped token**, minted here per request from the shared signing key,
  naming the acting Discord user, the target guild, and the exact operation.
  This authorises one specific thing on behalf of one specific person.

Neither is sufficient alone, and that is deliberate: a leaked certificate
cannot act as a user, and a leaked signing key cannot reach the port.

Note the token names an actor but does not *prove* they are an administrator.
The bot decides that itself, per request, from its own gateway cache. Nothing
this module sends is trusted as an authority claim.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from api_tokens import (
    OP_GUILD_CHANNELS,
    OP_GUILD_PANEL,
    OP_GUILD_ROLES,
    OP_GUILD_SETTINGS,
    OP_LIST_GUILDS,
    mint_token,
)

logger = logging.getLogger(__name__)

# The bot answers about at most this many guilds in one call.
MAX_GUILD_IDS = 200


class BotAPIError(Exception):
    """The bot API could not be reached, or refused the request."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class BotAPIClient:
    def __init__(
        self,
        base_url: str,
        *,
        client_cert: str,
        client_key: str,
        ca_bundle: str,
        signing_key: bytes,
        timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.signing_key = signing_key
        self.timeout = timeout
        self._session = requests.Session()
        # Both halves of mTLS in one place: our certificate, and the CA we
        # require the bot to have been signed by. `verify` must never become
        # False -- without it this is an encrypted channel to whoever answers.
        self._session.cert = (client_cert, client_key)
        self._session.verify = ca_bundle

    # ----- plumbing -----
    def _get(self, path: str, operation: str, actor_id: int, guild_id=None) -> dict:
        token = mint_token(
            self.signing_key,
            actor_id=int(actor_id),
            operation=operation,
            guild_id=None if guild_id is None else int(guild_id),
        )
        try:
            response = self._session.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as error:
            # Worth its own branch: this is the failure that means the trust
            # chain is wrong, not that the request was refused. Confusing the
            # two costs hours.
            raise BotAPIError(f"TLS failure talking to the bot API: {error}") from error
        except requests.RequestException as error:
            raise BotAPIError(f"could not reach the bot API: {error}") from error

        if response.status_code == 200:
            return response.json()

        reason = ""
        try:
            reason = response.json().get("error", "")
        except ValueError:
            pass
        logger.warning(
            "bot API refused %s for actor=%s guild=%s: %s %s",
            operation,
            actor_id,
            guild_id,
            response.status_code,
            reason,
        )
        raise BotAPIError(reason or "bot API refused the request", response.status_code)

    # ----- reads -----
    def healthz(self) -> dict:
        """No token: proves the certificate and tunnel work, independent of auth."""
        try:
            response = self._session.get(
                f"{self.base_url}/healthz", timeout=self.timeout
            )
        except requests.RequestException as error:
            raise BotAPIError(f"could not reach the bot API: {error}") from error
        if response.status_code != 200:
            raise BotAPIError("bot API is not healthy", response.status_code)
        return response.json()

    def admin_guild_ids(self, actor_id: int, guild_ids: list) -> set:
        """Which of these guilds the bot is in AND this user administers.

        The bot answers only about guilds the caller has standing in, so a
        guild missing from the response means either "bot not there" or "not
        yours" — indistinguishable on purpose.
        """
        wanted = [str(int(g)) for g in guild_ids][:MAX_GUILD_IDS]
        if not wanted:
            return set()
        payload = self._get(
            f"/api/v1/guilds?ids={','.join(wanted)}",
            OP_LIST_GUILDS,
            actor_id,
        )
        return {str(g) for g in payload.get("present", [])}

    def settings(self, actor_id: int, guild_id) -> dict:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/settings",
            OP_GUILD_SETTINGS,
            actor_id,
            guild_id,
        )

    def roles(self, actor_id: int, guild_id) -> list:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/roles", OP_GUILD_ROLES, actor_id, guild_id
        )

    def channels(self, actor_id: int, guild_id) -> list:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/channels",
            OP_GUILD_CHANNELS,
            actor_id,
            guild_id,
        )

    def panel(self, actor_id: int, guild_id) -> dict:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/panel", OP_GUILD_PANEL, actor_id, guild_id
        )
