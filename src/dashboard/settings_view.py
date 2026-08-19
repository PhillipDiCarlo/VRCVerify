"""Turns the bot API's settings payload into something a template can render.

Pure: no Flask, no network, no clock. Everything it needs arrives as arguments,
which is what lets the awkward cases -- a role that was deleted, a log channel
the bot can no longer post in, a plan that lapsed -- be tested directly instead
of through a request.

TWO KINDS OF "not on your plan", AND THE DIFFERENCE MATTERS
-----------------------------------------------------------
The bot gates in two different places, and `SettingsField` in bot.py records
which is which. This module must render both honestly:

* `locked` -- the bot refuses the *save*. `write_dashboard_settings` rejects it
  and leaves the stored value alone, so a later subscriber gets their original
  choice back. Shown as "Premium only".
* `active: false` but not locked -- the value saves fine for anyone, and the
  bot simply doesn't act on it. /vrcverify_setup stores an unverified role for
  a free server quite happily. Shown as "saved, not applied".

Rendering the second as though it were the first would make the website
stricter than the slash commands, and an admin would find the site refusing to
show them something they can plainly set in Discord. That is the specific
failure this module exists to avoid.
"""

from __future__ import annotations

from typing import Optional

# Display names only. The authoritative list is locales.LANGUAGE_CODES in the
# bot, which this image deliberately does not carry -- it ships api_tokens.py
# and this package, nothing else. An unrecognised code renders as itself, so
# the two drifting apart degrades to showing "pt-BR" instead of "Portuguese".
#
# Step 5 must NOT build its locale <select> from this dict. The choices have to
# come from the bot, or the dashboard could offer a language the bot cannot
# render.
# Shown in the colour picker when a server has chosen nothing. Cosmetic only:
# what "no colour" means is the bot's business, and it stores NULL either way.
DEFAULT_PANEL_SWATCH = "#5865f2"

# The bot's own cap, mirrored so the textarea stops at the same place the save
# does. The browser attribute is a courtesy; the bot enforces it.
CUSTOM_MESSAGE_MAX_LEN = 1000

# Field kinds rendered as a <select>, which is only usable with options.
CHOICE_KINDS = frozenset({"role", "role_optional", "locale", "channel"})

# A VRChat group id is 40 characters. The input allows a bit more so pasting
# the full vrchat.com URL -- which the bot accepts and reduces to the id --
# is not silently truncated at the keyboard.
GROUP_INPUT_MAXLEN = 120

LOCALE_NAMES = {
    "en-US": "English",
    "es-ES": "Spanish",
    "zh-CN": "Chinese (Simplified)",
    "ja": "Japanese",
    "de": "German",
    "nl": "Dutch",
    "hi-IN": "Hindi",
    "ar": "Arabic",
    "bn": "Bengali",
    "pt-BR": "Portuguese (Brazil)",
    "ru": "Russian",
    "pa-IN": "Punjabi",
}


class Field:
    """One rendered setting. Attributes, because Jinja reads them cleanly."""

    def __init__(
        self,
        name: str,
        label: str,
        description: str,
        kind: str,
        display: str,
        *,
        empty: bool = False,
        feature: Optional[str] = None,
        active: bool = True,
        locked: bool = False,
        swatch: Optional[str] = None,
        warnings: Optional[list] = None,
        writable: bool = False,
        choices: Optional[list] = None,
        value=None,
        maxlength: Optional[int] = None,
    ):
        self.name = name
        self.label = label
        self.description = description
        self.kind = kind
        self.display = display
        self.empty = empty
        self.feature = feature
        self.active = active
        self.locked = locked
        self.swatch = swatch
        self.warnings = warnings or []
        self.writable = writable
        self.choices = choices or []
        # The raw value a form control needs, as distinct from `display`, which
        # is prose for a human.
        self.value = value
        self.maxlength = maxlength

    @property
    def editable(self) -> bool:
        """Render a control, rather than just the value.

        The first two halves come from the bot: `writable` is which fields the
        save path accepts in this phase, `locked` is whether the plan allows
        it. The website never decides either, and it renders a control only
        when the bot would actually accept the value it produces.

        The third is local, and it matters: a picker needs something to pick
        from. When the roles or channels read failed we have no options, and an
        empty <select> is worse than the read-only view -- it invites a save
        that would clear the verified role and stop verification for everyone.
        """
        if not self.writable or self.locked:
            return False
        if self.kind in CHOICE_KINDS and not self.choices:
            return False
        return True

    @property
    def badge(self) -> Optional[str]:
        """Which of the two plan states to show, if either."""
        if self.locked:
            return "premium"
        if not self.active:
            return "inactive"
        return None


def _state(settings: dict, name: str) -> dict:
    return (settings.get("fields") or {}).get(name) or {}


def _value(settings: dict, name: str):
    return _state(settings, name).get("value")


def _plan(state: dict) -> dict:
    return {
        "feature": state.get("feature"),
        "active": bool(state.get("active", True)),
        "locked": bool(state.get("locked", False)),
        # Absent means no: a bot that has not said a field is writable has not
        # said it, and defaulting the other way would render controls that the
        # save path then refuses.
        "writable": bool(state.get("writable", False)),
    }


def _role_field(
    settings: dict,
    roles: Optional[list],
    name: str,
    label: str,
    description: str,
    *,
    unassignable_hint: str,
    empty_warning: Optional[str] = None,
    required: bool = False,
) -> Field:
    state = _state(settings, name)
    raw = state.get("value")
    warnings = []
    swatch = None
    # Options for the picker: the role's name, and nothing else. A role the bot
    # cannot manage is still offered, because /vrcverify_setup offers it too --
    # the warning below the field is the honest treatment, not an annotation
    # inside the list or removal from it.
    choices = [
        (str(role.get("id")), role.get("name") or f"Role {role.get('id')}")
        for role in (roles or [])
    ]

    if raw is None or str(raw) == "":
        display, empty = "Not set", True
        if empty_warning:
            warnings.append(empty_warning)
    else:
        empty = False
        role = _lookup(roles, raw)
        if role is None:
            # roles is None when the read failed; that is "we could not check",
            # which is a different claim from "this role is gone".
            display = f"Unknown role ({raw})"
            if roles is not None:
                warnings.append(
                    "This role no longer exists in the server. VRCVerify will "
                    "not be able to use it."
                )
        else:
            display = role.get("name") or f"Role {raw}"
            # Colour 0 is Discord's "no colour", which renders as default grey.
            if role.get("color"):
                swatch = _hex(role["color"])
            assignable = role.get("assignable")
            if assignable is False:
                warnings.append(unassignable_hint)
            elif assignable is None and roles is not None:
                warnings.append(
                    "Couldn't check whether VRCVerify can manage this role."
                )

    return Field(
        name,
        label,
        description,
        "role" if required else "role_optional",
        display,
        empty=empty,
        swatch=swatch,
        warnings=warnings,
        choices=choices,
        value="" if raw is None else str(raw),
        **_plan(state),
    )


def _lookup(entries: Optional[list], wanted) -> Optional[dict]:
    if not entries or wanted is None:
        return None
    target = str(wanted)
    for entry in entries:
        if str(entry.get("id")) == target:
            return entry
    return None


def _hex(color) -> Optional[str]:
    """Always '#' plus exactly six hex digits, or None.

    The result lands in an SVG fill attribute, so the shape of it is the
    guarantee that matters: masking to 24 bits means no value can widen it into
    anything but a colour, whatever arrives.
    """
    try:
        return "#{:06x}".format(int(color) & 0xFFFFFF)
    except (TypeError, ValueError):
        return None


def _bool_field(
    settings: dict, name: str, label: str, description: str, *, on: str, off: str
) -> Field:
    state = _state(settings, name)
    value = bool(state.get("value"))
    return Field(
        name, label, description, "bool", on if value else off, value=value, **_plan(state)
    )


def build_groups(
    settings: dict,
    roles: Optional[list],
    channels: Optional[list],
    panel: Optional[dict] = None,
) -> list:
    """The settings page, grouped the way an admin thinks about them.

    The panel's whereabouts rides along with the panel's appearance, because
    "what does it look like" and "where is it, and can the bot still reach it"
    are one question to the person reading. It stays a separate key rather than
    a tenth field: it is a status, not a setting, and nothing will ever save it.
    """
    verified = _role_field(
        settings,
        roles,
        "role_id",
        "Verified role",
        "Granted once a member's VRChat account is confirmed as 18+.",
        unassignable_hint=(
            "VRCVerify cannot grant this role. Move the VRCVerify role above it "
            "in Server Settings -> Roles, or verification will fail for every "
            "member."
        ),
        empty_warning=(
            "No verified role is set, so verification cannot complete. Members "
            "are told to contact an admin."
        ),
        required=True,
    )

    unverified = _role_field(
        settings,
        roles,
        "unverified_role_id",
        "Unverified role",
        "Removed automatically once a member verifies.",
        unassignable_hint=(
            "VRCVerify cannot remove this role. Move the VRCVerify role above "
            "it in Server Settings -> Roles."
        ),
    )

    auto_verify = _bool_field(
        settings,
        "auto_verify_new_members",
        "Auto-verify on join",
        "Members already verified with VRCVerify elsewhere get the role as soon "
        "as they join. Free for every server, always.",
        on="On",
        off="Off",
    )

    nickname = _bool_field(
        settings,
        "auto_nickname_change",
        "Nickname sync",
        "Sets a member's server nickname to their VRChat display name.",
        on="On",
        off="Off",
    )

    custom_dm = _custom_dm_field(settings)
    locale = _locale_field(settings)
    log_channel = _log_channel_field(settings, channels)
    color, icon = _panel_fields(settings)
    vrchat_group, group_enabled = _group_invite_fields(settings)

    return [
        {
            "title": "Verification",
            "blurb": "The core of the bot. These are free for every server.",
            "fields": [verified, unverified, auto_verify],
            "save_endpoint": "save_verification_settings",
        },
        {
            "title": "After verifying",
            "blurb": "What happens once a member is confirmed.",
            "fields": [nickname, custom_dm],
            "save_endpoint": "save_member_settings",
        },
        {
            "title": "Instructions panel",
            "blurb": "The message members use to start verification.",
            "fields": [locale, color, icon],
            "panel": panel_summary(panel),
            # Where the panel may be posted. Announcement channels are NOT
            # filtered out, unlike the log channel's picker: the panel is public
            # instructions, and /vrcverify_instructions can be run in one, so
            # excluding them would be stricter than the bot. Channels the bot
            # cannot post in are excluded, because that is not a choice --
            # except the one the panel is already in, where the button refreshes
            # rather than posts and so needs no Send Messages at all.
            "panel_channels": _panel_channels(channels, panel),
            "panel_channel_id": (panel or {}).get("channel_id") or "",
            # The template renders a form when a group names an endpoint AND the
            # bot said at least one of its fields is writable, so what a group
            # can save is decided here and in DASHBOARD_WRITABLE_FIELDS, never
            # in the template. A bot that has not opened a field yet renders it
            # read-only without this side needing to know why.
            "save_endpoint": "save_panel_settings",
        },
        {
            "title": "VRChat group",
            "blurb": "Invite members to your group once they're verified.",
            "fields": [vrchat_group, group_enabled],
            "group_setup": group_setup_summary(settings),
            "save_endpoint": "save_group_settings",
        },
        {
            "title": "Logging",
            "blurb": "A record of verification activity for your moderators.",
            "fields": [log_channel],
            "save_endpoint": "save_logging_settings",
        },
    ]


# What each setup state means, as a headline and the next thing to do. Keyed
# on the bot's own state codes; anything unrecognised falls through to a
# generic line rather than reaching the page as a raw identifier.
#
# Every one of these names something the admin can act on, which is the whole
# reason the worker reports nine states instead of "setup failed". An admin
# told only that it failed will open a support ticket; an admin told the bot is
# in the group but lacks `group-invites-manage` will go and tick the box.
GROUP_SETUP_COPY = {
    "unverified": (
        "pending",
        "Not checked yet",
        "Put the setup code in your group's description, invite the bot to the "
        "group, then run the check.",
    ),
    "checking": (
        "pending",
        "Checking\u2026",
        "The bot is talking to VRChat. Reload this page in a moment.",
    ),
    "timed_out": (
        "warn",
        "No answer from the checker",
        "The check was sent but nothing came back. Try again shortly.",
    ),
    "worker_unreachable": (
        "warn",
        "Couldn't start the check",
        "The bot couldn't reach the part of itself that talks to VRChat. Try "
        "again shortly.",
    ),
    "ready": (
        "ok",
        "Ready",
        "The bot is in your group and can send invites.",
    ),
    "join_requested": (
        "pending",
        "Waiting for a moderator",
        "The bot has asked to join and a group moderator needs to approve it. "
        "Run the check again once they have.",
    ),
    "not_invited": (
        "warn",
        "The bot hasn't been invited yet",
        "Invite the account below to your group from VRChat, then run the "
        "check again.",
    ),
    "no_invite_permission": (
        "warn",
        "In the group, but it can't invite anyone",
        "Give the bot's role the \u201cManage Group Invites\u201d permission in "
        "VRChat. Being an admin is not enough \u2014 it is its own tick box.",
    ),
    "code_missing": (
        "warn",
        "The setup code isn't in the group description",
        "Paste the code below anywhere in your VRChat group's description, "
        "then run the check again. You can remove it once the check passes.",
    ),
    "group_not_found": (
        "warn",
        "No VRChat group with that ID",
        "Check the ID, or paste the group's vrchat.com link instead.",
    ),
    "banned": (
        "warn",
        "The bot is banned or blocked from that group",
        "A group moderator has to lift that before setup can continue.",
    ),
    "bad_job": (
        "warn",
        "That group ID wasn't usable",
        "Check the ID, or paste the group's vrchat.com link instead.",
    ),
    "vrchat_unavailable": (
        "warn",
        "VRChat didn't answer",
        "Nothing is wrong with your setup. Try the check again shortly.",
    ),
}

# The bot's error sentence is the only free text on this page that the bot
# authored, and most of it is our own wording -- but classify_api_error can
# fold in whatever VRChat replied, which is a third party's string. Jinja
# escapes it, so this is not about injection; it is about a page that stays
# readable when an upstream error turns out to be a wall of JSON.
GROUP_ERROR_MAX_LEN = 200

GROUP_SETUP_FALLBACK = (
    "warn",
    "Setup couldn't be confirmed",
    "Run the check again. If it keeps happening, contact support.",
)


def group_setup_summary(settings: dict) -> dict:
    """How far this guild's VRChat group setup has got, ready to render.

    A status rather than a setting: nothing here is ever saved, and the bot is
    the only thing that writes any of it. It sits beside the two fields for the
    same reason the panel's whereabouts sits beside the panel's appearance --
    "what did I set" and "did it work" are one question to the person reading.
    """
    block = settings.get("group_invite") or {}
    state = str(block.get("state") or "unverified")
    tone, headline, detail = GROUP_SETUP_COPY.get(state, GROUP_SETUP_FALLBACK)

    group_id = _value(settings, "vrchat_group_id")
    account = block.get("account_to_invite")

    # A server whose subscription lapsed keeps its stored group -- the field is
    # write_locked, so the bot refuses the save rather than clearing it -- and
    # therefore keeps this status too. What it must NOT keep is the list of
    # things to go and do: there is no check button on a locked section, so
    # "paste this code in your group description and invite this account"
    # would be instructions for a task the page gives them no way to finish.
    #
    # The headline stays, because where they got to is true and worth seeing.
    # Only the next step changes, to the same promise the locked fields make.
    locked = bool(_state(settings, "vrchat_group_id").get("locked"))
    if locked:
        detail = (
            "Group invites are part of VRCVerify Premium. Your group is kept "
            "exactly as it is, and this picks up where it left off if the "
            "subscription is renewed."
        )

    warnings = []
    if locked:
        # Both of the warnings below ask for an action, and there is nothing
        # here to act with.
        pass
    elif state == "ready" and not block.get("can_see_members"):
        # Not a failure: invites work without it. It only decides whether a
        # member who is already in the group can be told so instead of being
        # offered a button that would fail.
        warnings.append(
            "Optional: add \u201cView All Members\u201d to the bot's role and "
            "VRCVerify can tell members who are already in the group, rather "
            "than sending them an invite that bounces."
        )
    if group_id and not account and not locked:
        warnings.append(
            "This VRCVerify installation has no invite account configured, so "
            "the check cannot run. Contact the bot operator."
        )

    return {
        "state": state,
        "tone": tone,
        "headline": headline,
        "detail": detail,
        # The bot's own sentence about what went wrong, when it has one. Shown
        # as well as the copy above, not instead of it: the copy says what to
        # do, and this says what VRChat actually said.
        "error": None if locked else _clip(block.get("error"), GROUP_ERROR_MAX_LEN),
        "locked": locked,
        "group_name": block.get("group_name"),
        "configured": bool(group_id),
        "group_url": f"https://vrchat.com/home/group/{group_id}" if group_id else None,
        # The usr_ id matters as much as the name. Display names are not
        # unique, and an admin who invites a lookalike account gets "the bot
        # hasn't been invited" with nothing explaining why.
        # Suppressed while locked: inviting the account is a step towards a
        # check this section cannot run.
        "account_id": None if locked else account,
        "account_url": (
            None if locked or not account
            else f"https://vrchat.com/home/user/{account}"
        ),
        "claim_code": block.get("claim_code"),
        # Once it is working the code has done its job, and leaving it on
        # screen invites someone to leave it in their group description for
        # ever. The bot stops demanding it after a successful check.
        "show_claim_code": (
            bool(block.get("claim_code")) and state != "ready" and not locked
        ),
        "warnings": warnings,
    }


def _clip(text, limit: int):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _group_invite_fields(settings: dict):
    group_state = _state(settings, "vrchat_group_id")
    raw = group_state.get("value")
    group = Field(
        "vrchat_group_id",
        "VRChat group",
        "The group verified members can be invited to. Paste the group's ID "
        "(it starts with grp_) or its vrchat.com link. Leave it empty to "
        "disconnect the group.",
        "line",
        str(raw) if raw else "No group connected",
        empty=not raw,
        value="" if raw is None else str(raw),
        maxlength=GROUP_INPUT_MAXLEN,
        **_plan(group_state),
    )

    enabled = _bool_field(
        settings,
        "vrchat_group_invite_enabled",
        "Offer the group to verified members",
        "Adds a button to the message a member gets after verifying, which "
        "invites them to your group. Nothing is sent unless they press it.",
        on="On",
        off="Off",
    )
    return group, enabled


def _custom_dm_field(settings: dict) -> Field:
    state = _state(settings, "custom_verification_requested_message")
    raw = state.get("value")
    if raw:
        display, empty = str(raw), False
    else:
        display, empty = "Using the default message", True
    return Field(
        "custom_verification_requested_message",
        "Custom verification message",
        "Replaces the default DM a member gets when they start verifying. "
        "Links may only point to discord.com or vrchat.com. Leave it empty to "
        "go back to the default.",
        "text",
        display,
        empty=empty,
        value="" if raw is None else str(raw),
        maxlength=CUSTOM_MESSAGE_MAX_LEN,
        **_plan(state),
    )


def locale_label(code: str) -> str:
    name = LOCALE_NAMES.get(code)
    return f"{name} ({code})" if name else str(code)


def _locale_field(settings: dict) -> Field:
    state = _state(settings, "instructions_locale")
    code = state.get("value") or "en-US"
    # The options come from the bot, never from LOCALE_NAMES above -- that dict
    # is for display, and a select built from it could offer a language the bot
    # cannot render.
    codes = (settings.get("choices") or {}).get("instructions_locale") or []
    return Field(
        "instructions_locale",
        "Language",
        "The language of the instructions panel and the bot's replies to members.",
        "locale",
        locale_label(code),
        choices=[(one, locale_label(one)) for one in codes],
        value=code,
        **_plan(state),
    )


def _panel_fields(settings: dict):
    color_state = _state(settings, "panel_embed_color")
    swatch = _hex(color_state.get("value"))
    color = Field(
        "panel_embed_color",
        "Panel colour",
        "The colour bar down the side of the instructions panel.",
        "color",
        swatch or "Default blue",
        empty=swatch is None,
        swatch=swatch,
        # A colour input cannot be empty, so it needs something to show while
        # the "use the default" box is ticked.
        value=swatch or DEFAULT_PANEL_SWATCH,
        **_plan(color_state),
    )

    icon = _bool_field(
        settings,
        "panel_show_icon",
        "Server icon on the panel",
        "Shows your server's icon as the panel's thumbnail.",
        on="Shown",
        off="Hidden",
    )
    return color, icon


def _log_channel_field(settings: dict, channels: Optional[list]) -> Field:
    state = _state(settings, "verification_log_channel_id")
    raw = state.get("value")
    warnings = []

    if raw is None or str(raw) == "":
        display, empty = "Not set", True
    else:
        empty = False
        channel = _lookup(channels, raw)
        if channel is None:
            display = f"Unknown channel ({raw})"
            if channels is not None:
                warnings.append("This channel no longer exists in the server.")
        else:
            display = f"#{channel.get('name') or raw}"
            if channel.get("is_news"):
                # The bot refuses these outright. Other servers can follow an
                # announcement channel, which would republish an age disclosure
                # about a named member into servers they have no relationship
                # with.
                warnings.append(
                    "This is an announcement channel. Other servers can follow "
                    "it, which would republish age disclosures about your "
                    "members. VRCVerify will not log here."
                )
            if channel.get("can_send") is False:
                warnings.append("VRCVerify cannot post in this channel.")

    # Announcement channels are left out of the picker rather than offered and
    # refused. Unlike an unassignable role -- which /vrcverify_setup accepts and
    # this page only warns about -- /vrcverify_logchannel refuses these
    # outright, so omitting them is matching the bot, not being stricter.
    choices = [
        (str(channel.get("id")), f"#{channel.get('name') or channel.get('id')}")
        for channel in (channels or [])
        if not channel.get("is_news")
    ]

    return Field(
        "verification_log_channel_id",
        "Verification log channel",
        "Every verification attempt, including the ones that fail silently.",
        "channel",
        display,
        empty=empty,
        warnings=warnings,
        choices=choices,
        value="" if raw is None else str(raw),
        **_plan(state),
    )


# Discord's own deep link to a SKU's Store page. It takes an application and a
# SKU and NOTHING ELSE -- there is no guild parameter, which is the answer to
# the open question issue #65 raised before this was built. So a link from here
# can show an admin what Premium costs and includes, but it cannot pre-select
# the server they are looking at, and for a guild-scoped SKU picking the wrong
# server means billing the wrong server.
#
# Hence the split below: /vrcverify_subscription is the primary path, because
# running it inside a server produces Discord's native purchase button already
# bound to that guild, and the store page is offered as reading material.
STORE_URL = "https://discord.com/application-directory/{app_id}/store/{sku_id}"


def build_upgrade(settings: dict, application_id: Optional[str]) -> Optional[dict]:
    """How this server buys Premium, or None when there is nothing to sell.

    Three cases end in None, and they are different: the tier is switched off
    entirely (`enforced` false, so every gate answers "allowed" and an upgrade
    button would be selling a thing that is already free), the server already
    subscribes, or the bot reported no SKU. The last one matters most -- it is
    what a misconfigured deployment looks like, and a link built around a
    missing id would 404 into Discord rather than fail here.

    Grandfathered servers DO get this. They keep three features permanently and
    lose nothing by ignoring it, but the premium-only set -- log channel,
    branded panel, shorter cooldown, queue priority -- is still closed to them,
    so treating them as already-sold would be wrong. The copy differs instead.
    """
    premium = settings.get("premium") or {}
    if not premium.get("enforced"):
        return None
    if premium.get("premium"):
        return None

    sku_id = premium.get("sku_id")
    if not sku_id or not application_id:
        return None

    return {
        "grandfathered": bool(premium.get("grandfathered")),
        "store_url": STORE_URL.format(app_id=application_id, sku_id=sku_id),
    }


# Labels for the audit list. Deliberately the same words the settings above
# use, so a line of history is recognisably about a control on this page.
AUDIT_LABELS = {
    "role_id": "Verified role",
    "unverified_role_id": "Unverified role",
    "auto_verify_new_members": "Auto-verify on join",
    "auto_nickname_change": "Nickname sync",
    "custom_verification_requested_message": "Custom verification message",
    "instructions_locale": "Language",
    "panel_embed_color": "Panel colour",
    "panel_show_icon": "Server icon on the panel",
    "verification_log_channel_id": "Verification log channel",
    "vrchat_group_id": "VRChat group",
    "vrchat_group_invite_enabled": "VRChat group invites",
    # An action rather than a setting, like instructions_panel above: the
    # bot stores (what it did, which group), not (old value, new value).
    "group_verify": "VRChat group setup check",
    # Not a setting but an action, and the only row here whose pair is not
    # (old value, new value) -- the bot stores (what it did, where). Without
    # this entry the branch's own headline feature rendered its history as the
    # raw column name and a bare channel id.
    "instructions_panel": "Instructions panel",
}

# What the bot writes into old_value for an instructions_panel row, as a phrase
# rather than a state it moved out of.
PANEL_ACTIONS = {
    "posted": "posted in",
    "moved": "moved to",
    "refreshed": "refreshed in",
    "replaced": "replaced in",
}

# Long enough to recognise a message, short enough that one entry cannot push
# the rest of the history off the screen.
AUDIT_VALUE_MAX = 80


def build_audit(
    entries: Optional[list],
    roles: Optional[list],
    channels: Optional[list],
) -> Optional[list]:
    """The change history, with ids resolved and values fit to read.

    None means the bot could not answer, which the page says out loud -- an
    empty history and an unavailable one are different facts, and a trail that
    quietly renders as "nothing happened" is worse than one that admits it does
    not know.
    """
    if entries is None:
        return None
    rows = []
    for entry in entries:
        field = entry.get("field")
        # An instructions_panel row is (what happened, where) rather than
        # (before, after), so its halves resolve differently -- the second one
        # is a channel id, not another action.
        new_field = "instructions_panel_channel" if field == "instructions_panel" else field
        rows.append(
            {
                "label": AUDIT_LABELS.get(field, field),
                "actor": entry.get("actor_name") or f"ID {entry.get('actor_id')}",
                "old": _audit_value(field, entry.get("old_value"), roles, channels),
                "new": _audit_value(new_field, entry.get("new_value"), roles, channels),
                "when": entry.get("changed_at"),
                # Formatted here, not in the template. The template used to slice
                # this string, which is the one place bot data was subscripted
                # rather than printed -- a non-string would have 500'd the page.
                "when_text": _audit_when(entry.get("changed_at")),
            }
        )
    return rows


def _audit_when(raw) -> str:
    """`2026-08-11T07:11:36...` as `2026-08-11 07:11 UTC`, defensively."""
    if not isinstance(raw, str) or len(raw) < 16:
        return ""
    return raw[:16].replace("T", " ") + " UTC"


def _audit_value(field, raw, roles, channels) -> str:
    """One stored value, as something an admin can read.

    `roles`/`channels` are None when that read failed, which is "we could not
    check" and NOT "it is gone" -- the same distinction _role_field draws above,
    and the one this function used to lose. Telling an admin their verified role
    was deleted when it was not is exactly the kind of false statement the rest
    of this module is written to avoid.
    """
    if raw is None or raw == "":
        return "not set"
    if field == "instructions_panel":
        return PANEL_ACTIONS.get(str(raw), str(raw))
    if field in {"role_id", "unverified_role_id"}:
        if roles is None:
            return f"role {raw}"
        role = _lookup(roles, raw)
        if role is None:
            return f"a role that no longer exists ({raw})"
        return role.get("name") or f"Role {raw}"
    if field in {"verification_log_channel_id", "instructions_panel_channel"}:
        if channels is None:
            return f"channel {raw}"
        channel = _lookup(channels, raw)
        if channel is None:
            return f"a channel that no longer exists ({raw})"
        return f"#{channel.get('name') or raw}"
    if field == "panel_embed_color":
        return _hex(raw) or str(raw)
    if field == "instructions_locale":
        return locale_label(str(raw))
    if raw in {"True", "False"}:
        return "on" if raw == "True" else "off"
    text = str(raw)
    return text if len(text) <= AUDIT_VALUE_MAX else text[: AUDIT_VALUE_MAX - 1] + "…"


def _panel_channels(channels: Optional[list], panel: Optional[dict]) -> list:
    """Channels the panel button may target.

    Somewhere the bot cannot send is not a real choice, so it is left out --
    unless the panel already lives there, because then the button refreshes an
    existing message instead of sending a new one. Dropping that option would
    make this page refuse a thing the bot does unprompted on every restart.
    """
    here = str((panel or {}).get("channel_id") or "") if (panel or {}).get("posted") else ""
    return [
        (str(channel.get("id")), f"#{channel.get('name') or channel.get('id')}")
        for channel in (channels or [])
        # can_embed, not can_send: the panel is an embed, and a channel with
        # Send Messages but no Embed Links accepts the log and refuses this.
        # A bot that predates the flag reports it as None, which is "unknown"
        # rather than "no" -- offering it and letting the bot refuse is better
        # than hiding every channel from an older deployment.
        if channel.get("can_embed") is not False or str(channel.get("id")) == here
    ]


def panel_summary(panel: Optional[dict]) -> dict:
    """The instructions panel's whereabouts, as a template-ready dict.

    `posted: false` is not proof there is no panel -- load_instruction_panel in
    the bot returns None both for "never posted" and for "the row could not be
    read". The copy says "no panel found" rather than "no panel exists" for
    exactly that reason, and step 6 must confirm before it offers to post one.
    """
    if panel is None:
        return {"known": False}
    if not panel.get("posted"):
        return {"known": True, "posted": False}

    warnings = []
    if panel.get("channel_exists") is False:
        warnings.append(
            "The channel this panel was posted in no longer exists, so members "
            "have no way to start verifying."
        )
    elif panel.get("channel_postable") is False:
        # Deliberately narrow. The panel itself is fine: buttons are
        # interactions, and refreshing it edits a message VRCVerify already
        # owns, neither of which needs Send Messages. Only replacing it does.
        # Both permissions are named because the panel is an embed, so Embed
        # Links alone being off produces this with no other symptom.
        warnings.append(
            "VRCVerify can't post a new message in that channel — it needs "
            "both Send Messages and Embed Links there. The panel still works "
            "and can still be refreshed; it just can't be replaced."
        )

    name = panel.get("channel_name")
    return {
        "known": True,
        "posted": True,
        "channel": f"#{name}" if name else f"channel {panel.get('channel_id')}",
        "warnings": warnings,
    }
