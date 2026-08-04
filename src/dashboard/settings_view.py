"""Turns the bot API's settings payload into something a template can render.

Pure: no Flask, no network, no clock. Everything it needs arrives as arguments,
which is what lets the awkward cases -- a role that was deleted, a log channel
the bot can no longer post in, a plan that lapsed -- be tested directly instead
of through a request.

TWO KINDS OF "not on your plan", AND THE DIFFERENCE MATTERS
-----------------------------------------------------------
The bot gates in two different places, and `SettingsField` in bot.py records
which is which. This module must render both honestly:

* `locked` -- the bot refuses the *save*. PagedSettingsView disables the
  control and leaves the stored value alone, so a later subscriber gets their
  original choice back. Shown as "Premium only".
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
            # The only group with a save path so far. The template renders a
            # form when a group names an endpoint AND the bot said at least one
            # of its fields is writable, so opening the next group is a change
            # here and in DASHBOARD_WRITABLE_FIELDS, not in the template.
            "save_endpoint": "save_panel_settings",
        },
        {
            "title": "Logging",
            "blurb": "A record of verification activity for your moderators.",
            "fields": [log_channel],
            "save_endpoint": "save_logging_settings",
        },
    ]


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
    elif panel.get("channel_reachable") is False:
        warnings.append(
            "VRCVerify can no longer post in that channel, so it cannot refresh "
            "this panel."
        )

    name = panel.get("channel_name")
    return {
        "known": True,
        "posted": True,
        "channel": f"#{name}" if name else f"channel {panel.get('channel_id')}",
        "warnings": warnings,
    }
