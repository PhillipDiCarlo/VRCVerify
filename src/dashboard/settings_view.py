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

import re
from typing import Callable, Optional

# The no-op translation marker (#97). Tables in this module are built at
# import, so they hold msgids; the lookup happens per request against the
# `gettext` callable the caller passes in.
from dashboard.i18n import DEFAULT_LANGUAGE, N_, format_timestamp


def _untranslated(text: str) -> str:
    """The default `t`: hand back the English, unchanged.

    Every entry point here takes `t` as a keyword defaulting to this, which is
    what keeps this module's promise. It stays callable with no request in
    sight -- the tests that assert which fields a lapsed plan renders read-only
    need a settings payload, not a language.
    """
    return text

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

# The URL slug for each settings group, in the order the groups are built, plus
# one for the audit log -- which is not a group and never has been, so it gets
# its own slug rather than being attached to whichever group it happened to sit
# below (#140).
#
# ONE TABLE, because a sub-nav that names a slug no route serves is a link to a
# 404. That is the same argument `SECTIONS` in app.py already makes for the top
# level, and it holds one level down: the routes, the sidebar sub-nav and the
# groups below all read these names from here rather than each spelling them
# out.
#
# These become a URL contract the moment phase 2 ships them. An admin who
# bookmarks /settings/vrchat-group keeps that link, so renaming one later is a
# redirect to add, not a string to edit.
SETTINGS_GROUPS = (
    ("verification", N_("Verification")),
    ("after-verifying", N_("After verifying")),
    ("panel", N_("Instructions panel")),
    ("vrchat-group", N_("VRChat group")),
    ("logging", N_("Logging")),
)

# The names, not a second copy of them. `build_groups()` titles its cards from
# this table and the sub-nav labels its links from it, so a group renamed on
# the page is renamed in the nav by the same edit -- which is the whole point
# of #140's "not a second hand-maintained list that can drift from either".
SETTINGS_TITLES = dict(SETTINGS_GROUPS)

SETTINGS_SLUGS = tuple(slug for slug, _title in SETTINGS_GROUPS)

# Where /guild/<id>/settings with no group sends an admin. First rather than a
# landing page: the bot posts the bare URL as Discord link buttons that live in
# message history and cannot be edited, so it has to resolve to something
# forever, and one canonical URL per group is worth more than an index.
SETTINGS_DEFAULT_SLUG = SETTINGS_SLUGS[0]

# The audit log's own sub-page. One word in the nav and the same word at the
# top of the page: a reader who taps "Activity" and lands on something headed
# differently has to stop and check they got where they meant to.
ACTIVITY_SLUG = "activity"
ACTIVITY_TITLE = N_("Activity")

LOCALE_NAMES = {
    "en-US": N_("English"),
    "es-ES": N_("Spanish"),
    "zh-CN": N_("Chinese (Simplified)"),
    "ja": N_("Japanese"),
    "de": N_("German"),
    "nl": N_("Dutch"),
    "hi-IN": N_("Hindi"),
    "ar": N_("Arabic"),
    "bn": N_("Bengali"),
    "pt-BR": N_("Portuguese (Brazil)"),
    "ru": N_("Russian"),
    "pa-IN": N_("Punjabi"),
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
    t: Callable[[str], str] = _untranslated,
) -> Field:
    """`label`, `description`, `unassignable_hint` and `empty_warning` arrive
    as msgids from `build_groups` and are looked up here, so a caller never has
    to remember which of its four strings need translating."""
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
        display, empty = t(N_("Not set")), True
        if empty_warning:
            warnings.append(t(empty_warning))
    else:
        empty = False
        role = _lookup(roles, raw)
        if role is None:
            # roles is None when the read failed; that is "we could not check",
            # which is a different claim from "this role is gone".
            display = t(N_("Unknown role (%(id)s)")) % {"id": raw}
            if roles is not None:
                warnings.append(t(N_(
                    "This role no longer exists in the server. VRCVerify will "
                    "not be able to use it."
                )))
        else:
            display = role.get("name") or f"Role {raw}"
            # Colour 0 is Discord's "no colour", which renders as default grey.
            if role.get("color"):
                swatch = _hex(role["color"])
            assignable = role.get("assignable")
            if assignable is False:
                warnings.append(t(unassignable_hint))
            elif assignable is None and roles is not None:
                warnings.append(t(N_(
                    "Couldn't check whether VRCVerify can manage this role."
                )))

    return Field(
        name,
        t(label),
        t(description),
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
    settings: dict,
    name: str,
    label: str,
    description: str,
    *,
    on: str,
    off: str,
    t: Callable[[str], str] = _untranslated,
) -> Field:
    state = _state(settings, name)
    value = bool(state.get("value"))
    return Field(
        name,
        t(label),
        t(description),
        "bool",
        t(on) if value else t(off),
        value=value,
        **_plan(state),
    )


def build_groups(
    settings: dict,
    roles: Optional[list],
    channels: Optional[list],
    panel: Optional[dict] = None,
    t: Callable[[str], str] = _untranslated,
) -> list:
    """The settings page, grouped the way an admin thinks about them.

    The panel's whereabouts rides along with the panel's appearance, because
    "what does it look like" and "where is it, and can the bot still reach it"
    are one question to the person reading. It stays a separate key rather than
    a tenth field: it is a status, not a setting, and nothing will ever save it.

    Each group carries its `slug` from SETTINGS_SLUGS, which is what the split
    into a page per group is routed and navigated by (#140). Nothing reads it
    while Settings is still one page; it is here first so the routes and the
    sub-nav can share this list rather than keeping one of their own.
    """
    verified = _role_field(
        settings,
        roles,
        "role_id",
        N_("Verified role"),
        N_("Granted once a member's VRChat account is confirmed as 18+."),
        unassignable_hint=N_(
            "VRCVerify cannot grant this role. Move the VRCVerify role above it "
            "in Server Settings -> Roles, or verification will fail for every "
            "member."
        ),
        empty_warning=N_(
            "No verified role is set, so verification cannot complete. Members "
            "are told to contact an admin."
        ),
        required=True,
        t=t,
    )

    unverified = _role_field(
        settings,
        roles,
        "unverified_role_id",
        N_("Unverified role"),
        N_("Removed automatically once a member verifies."),
        unassignable_hint=N_(
            "VRCVerify cannot remove this role. Move the VRCVerify role above "
            "it in Server Settings -> Roles."
        ),
        t=t,
    )

    auto_verify = _bool_field(
        settings,
        "auto_verify_new_members",
        N_("Auto-verify on join"),
        N_(
            "Members already verified with VRCVerify elsewhere get the role as "
            "soon as they join. Free for every server, always."
        ),
        on=N_("On"),
        off=N_("Off"),
        t=t,
    )

    nickname = _bool_field(
        settings,
        "auto_nickname_change",
        N_("Nickname sync"),
        N_("Sets a member's server nickname to their VRChat display name."),
        on=N_("On"),
        off=N_("Off"),
        t=t,
    )

    custom_dm = _custom_dm_field(settings, t)
    locale = _locale_field(settings, t)
    log_channel = _log_channel_field(settings, channels, t)
    color, icon = _panel_fields(settings, t)
    vrchat_group, group_enabled = _group_invite_fields(settings, t)

    return [
        {
            "title": t(SETTINGS_TITLES["verification"]),
            "slug": "verification",
            "blurb": t(N_("The core of the bot. These are free for every server.")),
            "fields": [verified, unverified, auto_verify],
            "save_endpoint": "save_verification_settings",
        },
        {
            "title": t(SETTINGS_TITLES["after-verifying"]),
            "slug": "after-verifying",
            "blurb": t(N_("What happens once a member is confirmed.")),
            "fields": [nickname, custom_dm],
            "save_endpoint": "save_member_settings",
        },
        {
            "title": t(SETTINGS_TITLES["panel"]),
            "slug": "panel",
            "blurb": t(N_("The message members use to start verification.")),
            "fields": [locale, color, icon],
            "panel": panel_summary(panel, t),
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
            "title": t(SETTINGS_TITLES["vrchat-group"]),
            "slug": "vrchat-group",
            "blurb": t(N_("Invite members to your group once they're verified.")),
            "fields": [vrchat_group, group_enabled],
            "group_setup": group_setup_summary(settings, t),
            "save_endpoint": "save_group_settings",
        },
        {
            "title": t(SETTINGS_TITLES["logging"]),
            "slug": "logging",
            "blurb": t(N_("A record of verification activity for your moderators.")),
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
        N_("Not checked yet"),
        N_("Put the setup code in your group's description, invite the bot to the "
        "group, then run the check."),
    ),
    "checking": (
        "pending",
        N_("Checking\u2026"),
        N_("The bot is talking to VRChat. Reload this page in a moment."),
    ),
    "timed_out": (
        "warn",
        N_("No answer from the checker"),
        N_("The check was sent but nothing came back. Try again shortly."),
    ),
    "worker_unreachable": (
        "warn",
        N_("Couldn't start the check"),
        N_("The bot couldn't reach the part of itself that talks to VRChat. Try "
        "again shortly."),
    ),
    "seat_released": (
        "warn",
        N_("The bot left your group"),
        N_("A VRChat account can only be in so many groups, so after a long time "
        "without a subscription the bot left yours to free the space. Invite "
        "the account below back and run the check again \u2014 your group and "
        "setup code are still saved, so there is nothing else to redo."),
    ),
    "ready": (
        "ok",
        N_("Ready"),
        N_("The bot is in your group and can send invites."),
    ),
    "join_requested": (
        "pending",
        N_("Waiting for a moderator"),
        N_("The bot has asked to join and a group moderator needs to approve it. "
        "Run the check again once they have."),
    ),
    "not_invited": (
        "warn",
        N_("The bot hasn't been invited yet"),
        N_("Invite the account below to your group from VRChat, then run the "
        "check again."),
    ),
    "no_invite_permission": (
        "warn",
        N_("In the group, but it can't invite anyone"),
        N_("Give the bot's role the \u201cManage Group Invites\u201d permission in "
        "VRChat. Being an admin is not enough \u2014 it is its own tick box."),
    ),
    "code_missing": (
        "warn",
        N_("The setup code isn't in the group description"),
        N_("Paste the code below anywhere in your VRChat group's description, "
        "then run the check again. You can remove it once the check passes."),
    ),
    "group_not_found": (
        "warn",
        N_("No VRChat group with that ID"),
        N_("Check the ID, or paste the group's vrchat.com link instead."),
    ),
    "banned": (
        "warn",
        N_("The bot is banned or blocked from that group"),
        N_("A group moderator has to lift that before setup can continue."),
    ),
    "bad_job": (
        "warn",
        N_("That group ID wasn't usable"),
        N_("Check the ID, or paste the group's vrchat.com link instead."),
    ),
    "vrchat_unavailable": (
        "warn",
        N_("VRChat didn't answer"),
        N_("Nothing is wrong with your setup. Try the check again shortly."),
    ),
}

# The bot's error sentence is the only free text on this page that the bot
# authored, and most of it is our own wording -- but classify_api_error can
# fold in whatever VRChat replied, which is a third party's string. Jinja
# escapes it, so this is not about injection; it is about a page that stays
# readable when an upstream error turns out to be a wall of JSON.
GROUP_ERROR_MAX_LEN = 200

# States that mean the account is already inside the group. Not the same as
# "setup worked": no_invite_permission is a member that cannot invite, which is
# a permissions problem rather than an invitation one.
GROUP_STATES_ALREADY_IN = frozenset({"ready", "no_invite_permission"})

GROUP_SETUP_FALLBACK = (
    "warn",
    N_("Setup couldn't be confirmed"),
    N_("Run the check again. If it keeps happening, contact support."),
)


def group_setup_summary(
    settings: dict, t: Callable[[str], str] = _untranslated
) -> dict:
    """How far this guild's VRChat group setup has got, ready to render.

    A status rather than a setting: nothing here is ever saved, and the bot is
    the only thing that writes any of it. It sits beside the two fields for the
    same reason the panel's whereabouts sits beside the panel's appearance --
    "what did I set" and "did it work" are one question to the person reading.
    """
    block = settings.get("group_invite") or {}
    state = str(block.get("state") or "unverified")
    tone, headline, detail = GROUP_SETUP_COPY.get(state, GROUP_SETUP_FALLBACK)
    headline, detail = t(headline), t(detail)

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
        detail = t(N_(
            "Group invites are part of VRCVerify Premium. Your group is kept "
            "exactly as it is, and this picks up where it left off if the "
            "subscription is renewed."
        ))

    warnings = []
    if locked:
        # Both of the warnings below ask for an action, and there is nothing
        # here to act with.
        pass
    elif state == "ready" and not block.get("can_see_members"):
        # Not a failure, and the reason is narrower than it used to be. Invites
        # work without it: a member who is already in the group is recognised
        # from the invite attempt itself and told so.
        #
        # What it still buys is the case that attempt CANNOT distinguish -- a
        # member who already has an invite waiting in VRChat. Without this the
        # bot may send them a second one, which is the thing the opt-in design
        # exists to avoid.
        warnings.append(t(N_(
            "Optional: add \u201cView All Members\u201d to the bot's role so "
            "VRCVerify can see that a member already has an invite waiting, "
            "instead of possibly sending them a second one."
        )))
    if group_id and not account and not locked:
        # Two operator problems reach here and neither is the admin's doing:
        # no invite account is provisioned at all, or every one of them has
        # used up its group slots. Deliberately one sentence for both -- the
        # admin's next step is identical, and naming the difference would only
        # invite them to try to work around a capacity limit they cannot see.
        warnings.append(t(N_(
            "No invite account is available to this VRCVerify installation "
            "right now, so the check cannot run. Contact the bot operator."
        )))

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
        # Rendered above the name. Not suppressed while locked, unlike the
        # instructions: a picture of the admin's own group is not a step they
        # are being asked to take.
        #
        # Only ever an https URL on VRChat's own file host, because that is the
        # single origin the CSP allows an image from -- anything else is a
        # broken image rather than a request. Checked here anyway so the page
        # does not emit a src it knows the browser will refuse.
        "icon_url": _vrchat_image(block.get("icon_url")),
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
        # Naming the account is an instruction, so it stops once the bot is
        # in the group. "Invite this account" beside "the bot is in your group
        # and can send invites" is a step somebody has already taken, and a
        # completed instruction left on screen reads as one that did not work.
        "show_account": bool(
            account and not locked and state not in GROUP_STATES_ALREADY_IN
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


# What the API puts in `icon_url`: the raw file, which is NOT usable in an
# <img>. It comes back as `application/octet-stream` -- the same reason
# clicking one in a browser downloads it instead of showing it -- and at full
# upload size, 744KB for a group icon shown at 64 pixels.
_VRCHAT_FILE_RE = re.compile(
    r"^https://api\.vrchat\.cloud/api/1/file/"
    r"(file_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/([0-9]{1,4})/file$",
    re.I,
)

# ...and where the page actually points, which is back at this app. Serving
# the icon from our own origin is what stopped it being a browser-by-browser
# argument about content types. See the /vrchat-icon route -- VRChat's own
# URLs were tried twice and neither is dependable in an <img>:
#
#     /api/1/file/<id>/1/file   application/octet-stream   744 KB
#     /api/1/image/<id>/1/128   image/png                   24 KB
#
# The first is what the API stores, and a browser will not draw it. The second
# is a real image, and still a third party deciding per request what it is
# sending -- so the proxy reads the bytes and says so itself.
VRCHAT_ICON_PATH = "/vrchat-icon/{file_id}/{version}"


def _vrchat_image(url):
    """A path on this site that will serve the icon, or None.

    Built, never echoed. The result is assembled from a file id and a version
    that both had to match the pattern above, so no stored string reaches an
    `src` attribute intact -- and what it produces is same-origin, so the CSP
    needs no exception for it at all.

    None for anything else, because the alternative is emitting an `src` that
    fails, which renders as a broken image with the explanation only in a
    console nobody has open. That is exactly what the first two versions of
    this did.
    """
    if not isinstance(url, str):
        return None
    match = _VRCHAT_FILE_RE.match(url.strip())
    if match is None:
        return None
    return VRCHAT_ICON_PATH.format(
        file_id=match.group(1).lower(), version=match.group(2)
    )


def _clip(text, limit: int):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _group_invite_fields(settings: dict, t: Callable[[str], str] = _untranslated):
    group_state = _state(settings, "vrchat_group_id")
    raw = group_state.get("value")
    group = Field(
        "vrchat_group_id",
        t(N_("VRChat group")),
        t(N_(
            "The group verified members can be invited to. Paste the group's ID "
            "(it starts with grp_) or its vrchat.com link. Leave it empty to "
            "disconnect the group."
        )),
        "line",
        str(raw) if raw else t(N_("No group connected")),
        empty=not raw,
        value="" if raw is None else str(raw),
        maxlength=GROUP_INPUT_MAXLEN,
        **_plan(group_state),
    )

    enabled = _bool_field(
        settings,
        "vrchat_group_invite_enabled",
        N_("Offer group invites"),
        N_(
            "Adds a button to the message a member gets after verifying, which "
            "invites them to your group. Nothing is sent unless they press it."
        ),
        on=N_("On"),
        off=N_("Off"),
        t=t,
    )
    return group, enabled


def _custom_dm_field(settings: dict, t: Callable[[str], str] = _untranslated) -> Field:
    state = _state(settings, "custom_verification_requested_message")
    raw = state.get("value")
    if raw:
        display, empty = str(raw), False
    else:
        display, empty = t(N_("Using the default message")), True
    return Field(
        "custom_verification_requested_message",
        t(N_("Custom verification message")),
        t(N_(
            "Replaces the default DM a member gets when they start verifying. "
            "Links may only point to discord.com or vrchat.com. Leave it empty "
            "to go back to the default."
        )),
        "text",
        display,
        empty=empty,
        value="" if raw is None else str(raw),
        maxlength=CUSTOM_MESSAGE_MAX_LEN,
        **_plan(state),
    )


def locale_label(code: str, t: Callable[[str], str] = _untranslated) -> str:
    """"German (de)", in the language the reader is reading the page in.

    Not the endonym, unlike the header bar's picker (see `i18n.ENDONYMS`).
    That control is read BY the person who cannot read the page, so it has to
    name each language in itself. This one is a setting an admin is choosing
    FOR their members, described in the language they are already reading --
    so a German admin picking Japanese for their server sees "Japanisch (ja)".

    The code rides along in both cases: it is what the bot stores, and it is
    the thing to quote in a support message.
    """
    name = LOCALE_NAMES.get(code)
    return f"{t(name)} ({code})" if name else str(code)


def _locale_field(settings: dict, t: Callable[[str], str] = _untranslated) -> Field:
    state = _state(settings, "instructions_locale")
    code = state.get("value") or "en-US"
    # The options come from the bot, never from LOCALE_NAMES above -- that dict
    # is for display, and a select built from it could offer a language the bot
    # cannot render.
    codes = (settings.get("choices") or {}).get("instructions_locale") or []
    return Field(
        "instructions_locale",
        t(N_("Language")),
        t(N_(
            "The language of the instructions panel and the bot's replies to "
            "members."
        )),
        "locale",
        locale_label(code, t),
        choices=[(one, locale_label(one, t)) for one in codes],
        value=code,
        **_plan(state),
    )


def _panel_fields(settings: dict, t: Callable[[str], str] = _untranslated):
    color_state = _state(settings, "panel_embed_color")
    swatch = _hex(color_state.get("value"))
    color = Field(
        "panel_embed_color",
        t(N_("Panel colour")),
        t(N_("The colour bar down the side of the instructions panel.")),
        "color",
        swatch or t(N_("Default blue")),
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
        N_("Server icon on the panel"),
        N_("Shows your server's icon as the panel's thumbnail."),
        on=N_("Shown"),
        off=N_("Hidden"),
        t=t,
    )
    return color, icon


def _log_channel_field(
    settings: dict,
    channels: Optional[list],
    t: Callable[[str], str] = _untranslated,
) -> Field:
    state = _state(settings, "verification_log_channel_id")
    raw = state.get("value")
    warnings = []

    if raw is None or str(raw) == "":
        display, empty = t(N_("Not set")), True
    else:
        empty = False
        channel = _lookup(channels, raw)
        if channel is None:
            display = t(N_("Unknown channel (%(id)s)")) % {"id": raw}
            if channels is not None:
                warnings.append(
                    t(N_("This channel no longer exists in the server."))
                )
        else:
            display = f"#{channel.get('name') or raw}"
            if channel.get("is_news"):
                # The bot refuses these outright. Other servers can follow an
                # announcement channel, which would republish an age disclosure
                # about a named member into servers they have no relationship
                # with.
                warnings.append(t(N_(
                    "This is an announcement channel. Other servers can follow "
                    "it, which would republish age disclosures about your "
                    "members. VRCVerify will not log here."
                )))
            if channel.get("can_send") is False:
                warnings.append(t(N_("VRCVerify cannot post in this channel.")))

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
        t(N_("Verification log channel")),
        t(N_("Every verification attempt, including the ones that fail silently.")),
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
    "role_id": N_("Verified role"),
    "unverified_role_id": N_("Unverified role"),
    "auto_verify_new_members": N_("Auto-verify on join"),
    "auto_nickname_change": N_("Nickname sync"),
    "custom_verification_requested_message": N_("Custom verification message"),
    "instructions_locale": N_("Language"),
    "panel_embed_color": N_("Panel colour"),
    "panel_show_icon": N_("Server icon on the panel"),
    "verification_log_channel_id": N_("Verification log channel"),
    "vrchat_group_id": N_("VRChat group"),
    "vrchat_group_invite_enabled": N_("VRChat group invites"),
    # An action rather than a setting, like instructions_panel above: the
    # bot stores (what it did, which group), not (old value, new value).
    "group_verify": N_("VRChat group setup check"),
    # Not a setting but an action, and the only row here whose pair is not
    # (old value, new value) -- the bot stores (what it did, where). Without
    # this entry the branch's own headline feature rendered its history as the
    # raw column name and a bare channel id.
    "instructions_panel": N_("Instructions panel"),
}

# What the bot writes into old_value for an instructions_panel row, as a phrase
# rather than a state it moved out of.
PANEL_ACTIONS = {
    "posted": N_("posted in"),
    "moved": N_("moved to"),
    "refreshed": N_("refreshed in"),
    "replaced": N_("replaced in"),
}

# Long enough to recognise a message, short enough that one entry cannot push
# the rest of the history off the screen.
AUDIT_VALUE_MAX = 80


def build_audit(
    entries: Optional[list],
    roles: Optional[list],
    channels: Optional[list],
    t: Callable[[str], str] = _untranslated,
    lang: str = DEFAULT_LANGUAGE,
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
                "label": t(AUDIT_LABELS[field]) if field in AUDIT_LABELS else field,
                "actor": entry.get("actor_name")
                or t(N_("ID %(id)s")) % {"id": entry.get("actor_id")},
                "old": _audit_value(field, entry.get("old_value"), roles, channels, t),
                "new": _audit_value(
                    new_field, entry.get("new_value"), roles, channels, t
                ),
                "when": entry.get("changed_at"),
                # Formatted here, not in the template. The template used to slice
                # this string, which is the one place bot data was subscripted
                # rather than printed -- a non-string would have 500'd the page.
                "when_text": _audit_when(entry.get("changed_at"), lang),
            }
        )
    return rows


def _audit_when(raw, lang: str = DEFAULT_LANGUAGE) -> str:
    """`2026-08-11T07:11:36...` as the date and time `lang` writes, marked UTC.

    Was a string slice: `raw[:16].replace("T", " ") + " UTC"`, which never
    parsed anything and so never had a day or a month to put in the wrong
    order. That made it the least wrong of the dashboard's dates when #230
    went looking -- ISO is at least unambiguous -- and still the only one on a
    translated page written in a shape no reader of it chose.

    The parse is `i18n.format_timestamp`'s and it is as defensive as the slice
    was: anything that is not an instant returns "", and the template already
    renders no timestamp for an empty string.
    """
    return format_timestamp(raw, lang) or ""


def _audit_value(
    field, raw, roles, channels, t: Callable[[str], str] = _untranslated
) -> str:
    """One stored value, as something an admin can read.

    `roles`/`channels` are None when that read failed, which is "we could not
    check" and NOT "it is gone" -- the same distinction _role_field draws above,
    and the one this function used to lose. Telling an admin their verified role
    was deleted when it was not is exactly the kind of false statement the rest
    of this module is written to avoid.
    """
    if raw is None or raw == "":
        return t(N_("not set"))
    if field == "instructions_panel":
        key = str(raw)
        return t(PANEL_ACTIONS[key]) if key in PANEL_ACTIONS else key
    if field in {"role_id", "unverified_role_id"}:
        if roles is None:
            return t(N_("role %(id)s")) % {"id": raw}
        role = _lookup(roles, raw)
        if role is None:
            return t(N_("a role that no longer exists (%(id)s)")) % {"id": raw}
        return role.get("name") or t(N_("Role %(id)s")) % {"id": raw}
    if field in {"verification_log_channel_id", "instructions_panel_channel"}:
        if channels is None:
            return t(N_("channel %(id)s")) % {"id": raw}
        channel = _lookup(channels, raw)
        if channel is None:
            return t(N_("a channel that no longer exists (%(id)s)")) % {"id": raw}
        return f"#{channel.get('name') or raw}"
    if field == "panel_embed_color":
        return _hex(raw) or str(raw)
    if field == "instructions_locale":
        return locale_label(str(raw), t)
    if raw in {"True", "False"}:
        return t(N_("on")) if raw == "True" else t(N_("off"))
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


def panel_summary(
    panel: Optional[dict], t: Callable[[str], str] = _untranslated
) -> dict:
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
        warnings.append(t(N_(
            "The channel this panel was posted in no longer exists, so members "
            "have no way to start verifying."
        )))
    elif panel.get("channel_postable") is False:
        # Deliberately narrow. The panel itself is fine: buttons are
        # interactions, and refreshing it edits a message VRCVerify already
        # owns, neither of which needs Send Messages. Only replacing it does.
        # Both permissions are named because the panel is an embed, so Embed
        # Links alone being off produces this with no other symptom.
        warnings.append(t(N_(
            "VRCVerify can't post a new message in that channel — it needs "
            "both Send Messages and Embed Links there. The panel still works "
            "and can still be refreshed; it just can't be replaced."
        )))

    name = panel.get("channel_name")
    return {
        "known": True,
        "posted": True,
        "channel": (
            f"#{name}"
            if name
            else t(N_("channel %(id)s")) % {"id": panel.get("channel_id")}
        ),
        "warnings": warnings,
    }
