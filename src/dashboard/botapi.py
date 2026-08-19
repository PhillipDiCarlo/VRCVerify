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
    OP_GUILD_OVERVIEW,
    OP_GUILD_PANEL,
    OP_VERIFY_GROUP,
    OP_GUILD_ROLES,
    OP_GUILD_SETTINGS,
    OP_GUILD_AUDIT,
    OP_POST_PANEL,
    OP_LIST_GUILDS,
    OP_PUT_STRIPE_SUBSCRIPTION,
    OP_UPDATE_SETTINGS,
    SYSTEM_ACTOR_ID,
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

    # ----- the one write -----
    def update_settings(self, actor_id: int, guild_id, changes: dict) -> dict:
        """Ask the bot to store these settings. It decides whether to.

        Nothing here validates the values. The bot owns the allowlist, the
        types and the plan gate, and duplicating any of that on this side would
        create a second opinion that can drift from the enforcing one -- while
        adding nothing, because this process is the one an attacker would be
        standing in.
        """
        token = mint_token(
            self.signing_key,
            actor_id=int(actor_id),
            operation=OP_UPDATE_SETTINGS,
            guild_id=int(guild_id),
        )
        try:
            response = self._session.patch(
                f"{self.base_url}/api/v1/guilds/{int(guild_id)}/settings",
                headers={"Authorization": f"Bearer {token}"},
                json={"fields": changes},
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as error:
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
            "bot API refused a save for actor=%s guild=%s: %s %s",
            actor_id,
            guild_id,
            response.status_code,
            reason,
        )
        raise BotAPIError(reason or "bot API refused the save", response.status_code)

    def post_panel(self, actor_id: int, guild_id, channel_id) -> dict:
        """Ask the bot to put the instructions panel in this channel.

        Whether that means posting a new one or refreshing the existing one is
        the bot's call, not ours -- it is the only side that can see where the
        panel actually is.
        """
        token = mint_token(
            self.signing_key,
            actor_id=int(actor_id),
            operation=OP_POST_PANEL,
            guild_id=int(guild_id),
        )
        try:
            response = self._session.post(
                f"{self.base_url}/api/v1/guilds/{int(guild_id)}/panel",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel_id": str(channel_id)},
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as error:
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
            "bot API refused a panel post for actor=%s guild=%s: %s %s",
            actor_id, guild_id, response.status_code, reason,
        )
        raise BotAPIError(reason or "bot API refused the panel post", response.status_code)

    def verify_group(self, actor_id: int, guild_id) -> dict:
        """Ask the bot to check this guild's VRChat group setup.

        Sends no body, and there is nothing it could usefully send: the group
        being checked comes from the guild's stored settings on the bot's side.
        This process never handles a group id on the way in, so it cannot be
        talked into naming a different one -- which is the whole point, since
        the answer to this call makes a VRChat account join a group.
        """
        token = mint_token(
            self.signing_key,
            actor_id=int(actor_id),
            operation=OP_VERIFY_GROUP,
            guild_id=int(guild_id),
        )
        try:
            response = self._session.post(
                f"{self.base_url}/api/v1/guilds/{int(guild_id)}/verify-group",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as error:
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
            "bot API refused a group verification for actor=%s guild=%s: %s %s",
            actor_id, guild_id, response.status_code, reason,
        )
        raise BotAPIError(
            reason or "bot API refused the group check", response.status_code
        )

    def put_stripe_subscription(self, guild_id, subscription: dict) -> dict:
        """Forward one verified Stripe event to the bot, as the system actor.

        The only call in this client with no signed-in user behind it. A
        renewal a year from now is asked for by Stripe, not by a person, and
        the admin who originally checked out may have left the server — so it
        names SYSTEM_ACTOR_ID rather than borrowing somebody's identity, and
        the bot records the change against a fixed non-human actor.

        The authority here is Stripe's signature, checked before this is
        reached. Everything this call adds is the same as every other: mTLS,
        a token bound to this method, this path and this guild, single use.

        A refusal is not swallowed. It becomes a non-2xx to Stripe, which
        retries for up to three days — long enough to cover a bot restart, a
        Tailscale blip or a homelab power cut. Answering 200 on a failed
        forward is the one outcome that loses a subscription permanently.
        """
        token = mint_token(
            self.signing_key,
            actor_id=SYSTEM_ACTOR_ID,
            operation=OP_PUT_STRIPE_SUBSCRIPTION,
            guild_id=int(guild_id),
        )
        try:
            response = self._session.put(
                f"{self.base_url}/api/v1/guilds/{int(guild_id)}/stripe-subscription",
                headers={"Authorization": f"Bearer {token}"},
                json={"subscription": subscription},
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as error:
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
            "bot API refused a Stripe subscription write for guild %s: %s %s",
            guild_id,
            response.status_code,
            reason,
        )
        raise BotAPIError(
            reason or "bot API refused the subscription write", response.status_code
        )

    def audit(self, actor_id: int, guild_id) -> list:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/audit", OP_GUILD_AUDIT, actor_id, guild_id
        )

    def panel(self, actor_id: int, guild_id) -> dict:
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/panel", OP_GUILD_PANEL, actor_id, guild_id
        )

    def overview(self, actor_id: int, guild_id) -> dict:
        """The Overview page's counts. One call, so one Administrator check."""
        return self._get(
            f"/api/v1/guilds/{int(guild_id)}/overview",
            OP_GUILD_OVERVIEW,
            actor_id,
            guild_id,
        )
