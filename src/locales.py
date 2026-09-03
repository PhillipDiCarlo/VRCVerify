# locales.py
#
# Every string the bot says, in English, marked for translation.
#
# WHY THIS IS NOT A DICT OF TWELVE LANGUAGES ANY MORE
# ---------------------------------------------------
# It was, until #231: a 1,221-line dict-of-dicts holding all twelve. That shape
# had one fatal property -- the translations could not be opened by a
# translator. 24,937 words of Spanish, Japanese, Bengali and eight more sat in
# a Python literal that cannot be loaded into Poedit, pushed to Crowdin or
# Weblate, or handed to a volunteer from the Discord without asking that person
# to edit Python and hoping they balance the quotes.
#
# The dashboard has had gettext catalogues since #97, and #97 predicted the
# cost of running two systems for one job. #231 is the paying down. The twelve
# languages now live in src/translations/bot/<lang>/LC_MESSAGES/bot.po, in the
# format the translation industry actually speaks, and `pybabel update` merges
# a new string into all eleven mechanically instead of somebody hand-writing it
# eleven times and a test catching the one they forgot.
#
# WHAT IS LEFT HERE, AND WHY IT IS STILL A FILE
# ---------------------------------------------
# The English. gettext keys on the source text, so the English string IS the
# msgid, and this stays the one place a bot string is written down.
#
# The alternative was inlining each English string at its call site, which is
# the idiomatic gettext shape and was rejected on the 26 multi-line strings:
# INSTRUCTIONS_DESC is a six-line block and SUPPORT_INFO carries a URL and a
# bullet list. Putting those in bot.py at the point of use makes a 9,000-line
# file longer and buries the control flow under prose.
#
# So the strings are constants marked with `N_`, the no-op the extractor keys
# on, and the lookup happens per interaction in `get_message`. That is exactly
# the pattern the dashboard's view modules already use for their label tables,
# and `scripts/i18n.sh` already extracts with `-k N_` for it.
#
# ADDING OR CHANGING A STRING
# ---------------------------
# Add the constant here, use it in bot.py, then run ./scripts/i18n.sh. That
# extracts it into bot.pot, merges it into all eleven .po files as untranslated
# and compiles what is already there. The new string renders in English until
# somebody translates it -- never blank, and never the bare constant name.
#
# Changing the *wording* of an existing string changes its msgid, which orphans
# all eleven translations of it. pybabel will offer a fuzzy match; i18n.sh
# deliberately does not compile fuzzy entries, so the string reverts to English
# until a person confirms each one. That is the intended cost of a reword.

from i18n_core import N_

# -- list of supported language codes --
#
# tests/test_i18n.py pins this equal to the dashboard's UI_LANGUAGES, so a
# thirteenth language cannot be added to one surface and forgotten on the
# other. en-US leads and has no catalogue directory: its "translation" is the
# msgids in this file.
LANGUAGE_CODES = [
    "en-US", "es-ES", "zh-CN", "ja", "de", "nl",
    "hi-IN", "ar", "bn", "pt-BR", "ru", "pa-IN",
]

# -- the strings --

NOT_VERIFIED = N_(
    "You haven't verified yet. Please click **Begin Verification** first."
)

ALREADY_VERIFIED = N_("You're already verified! Role assigned (or re-assigned).")

RECHECK_STARTED = N_(
    "We're re-checking your VRChat 18+ status. If you've updated your VRChat age verification, you'll get a DM soon!"
)

DM_ROLE_SUCCESS = N_("You've been verified and given **{role}** in **{server}**!")

NICKNAME_UPDATE_REQUESTED = N_("Nickname update requested. I'll DM you once it's done!")

VERIFICATION_REQUESTED = N_(
    "Verification request received! We'll DM you with the results. Please make sure your DMs for this server are open so you can receive the message."
)

SETUP_MISSING = N_(
    "This server hasn't set up a verification role yet. Please contact an admin."
)

NOT_18_PLUS = N_(
    "You are not 18+ according to VRChat. Contact an admin if this is an error."
)

SUPPORT_INFO = N_(
    "Need help with verification?\n"
    "- Contact a server admin for assistance\n"
    "- Or visit our support page at https://esattotech.com/contact-us/\n"
    "\n"
    "If this is an error, please let us know!"
)

SUPPORT_INVITE_LINE = N_(
    "Get VRCVerify updates in your own server: join {invite} and follow the announcements channel."
)

SUBSCRIPTION_INFO = N_(
    "I've decided to offer this free of charge however if you wish to still support me, you can find my Ko-fi here:{kofi_link}. Thank you for your continued support"
)

SETTINGS_SAVED = N_("Settings saved!")

SETTINGS_UNREADABLE = N_(
    "Couldn't read this server's settings just now. Try again shortly — nothing has changed."
)

INVALID_VRC_ID_INPUT = N_(
    "It looks like you entered your display name instead of your VRChat userID.\n"
    "Please enter either the full profile URL or your userID (which always starts with `usr_`).\n"
    "https://imgur.com/a/EEl6ekH"
)

CODE_NOT_FOUND = N_(
    "We couldn't find your code in your VRChat bio. Please try again. \n"
    "**Double check that the code is on its own line.**"
)

VERIFY_BUTTON_EXPIRED = N_(
    "This verification link has expired or was replaced by a newer one. Please run `/vrcverify` again to get a fresh code."
)

NICKNAME_UPDATED = N_("Your nickname was updated to {display_name}.")

NICKNAME_UPDATE_FAILED = N_("We could not update your nickname.")

SETUP_SUCCESS = N_(
    "Successfully {action} server config.\n"
    "Verified Role set to: `{role}` (ID={role_id})"
)

SETUP_UNVERIFIED_SET = N_(
    "\n"
    "Unverified Role to remove: `{role}` (ID={role_id})"
)

SETUP_UNVERIFIED_MISSING = N_(
    "\n"
    "(Unverified role not set; no role will be removed on verification.)"
)

INSTRUCTIONS_TITLE = N_("How to Use the VRChat Verification Bot")

INSTRUCTIONS_DESC = N_(
    "**Follow these steps** to verify your 18+ status:\n"
    "\n"
    "1. Click the **Begin Verification** button (if shown) or type `/vrcverify` anywhere.\n"
    "2. If you're new, you'll be asked for your VRChat username\n"
    "3. The bot will give you a unique code - put this in your VRChat bio **ON ITS OWN LINE**\n"
    "4. Press **Verify** in Discord once your bio is updated\n"
    "\n"
    "If you need additional help, contact an admin or type `/vrcverify_support`."
)

BIO_VERIFY_INSTRUCTIONS1 = N_("**1)** Add the code to your VRChat bio on its own line.")

BIO_VERIFY_INSTRUCTIONS2 = N_(
    "**2)** Once your bio is updated, click **Verify** in Discord (within 10 minutes)."
)

BTN_BEGIN_VERIFICATION = N_("Begin Verification")

BTN_UPDATE_NICKNAME = N_("Update Nickname")

SETTINGS_INTRO = N_(
    "**VRChat Verify Settings**\n"
    "\n"
    "1.) **Enable auto nickname change**\n"
    "   Automatically update users' Discord nicknames to match their VRChat display names.\n"
    "   Current: **{current}**"
)

DM_ROLE_FAILED_BOT_POSITION = N_(
    "I couldn't assign the '{role}' role in {server}. This usually happens when the VRCVerify bot's role is not above the verified (and unverified) roles in the server's role list. Please ask a server admin to move the VRCVerify bot role above those roles and try again."
)

DM_UNVERIFIED_FAILED_BOT_POSITION = N_(
    "Could not remove the {role} role in {server}. This usually happens when the VRCVerify bot's role is not above the unverified role. Ask a server admin to verify that the VRCVerify bot's role is above both the verified and unverified (if applicable)."
)

CUSTOM_MSG_CLEARED = N_(
    "Custom verification request message cleared. Default will be used."
)

CUSTOM_MSG_SAVED = N_("Custom verification request message saved.")

CUSTOM_MSG_TOO_LONG = N_("Message too long (max 1000 characters).")

CUSTOM_MSG_INVALID_LINKS = N_(
    "Blocked: Only discord.com or vrchat.com links allowed. Invalid link(s):\n"
    "{invalid_list}"
)

VRC_ID_ALREADY_LINKED = N_(
    "The VRChat profile you tried to use is already registered to a different Discord account. If you believe this is a mistake, please contact a server admin."
)

VRCHAT_ISSUE_USER_NOT_FOUND = N_(
    "We could not find that VRChat account. Please double-check that you pasted your VRChat profile URL or `usr_...` user ID correctly."
)

VRCHAT_ISSUE_RATE_LIMITED = N_(
    "VRChat is rate limiting verification lookups right now. Please wait a minute and try again."
)

VRCHAT_ISSUE_TEMP_UNAVAILABLE = N_(
    "VRCVerify is temporarily unable to talk to VRChat right now. Please try again in a little while."
)

VRCHAT_ISSUE_OUTAGE_CONFIRMED = N_(
    "VRChat is currently reporting a service issue that is affecting verification. Please try again later.\n"
    "\n"
    "Status page: {status_page}"
)

VRCHAT_ISSUE_OUTAGE_CONFIRMED_WITH_STATUS = N_(
    "VRChat is currently reporting a service issue that is affecting verification. Please try again later.\n"
    "\n"
    "Status page: {status_page}\n"
    "\n"
    "Reported status: {status_message}"
)

VRCHAT_ISSUE_OUTAGE_SUSPECTED = N_(
    "VRChat appears to be having temporary API issues, so verification could not be completed right now. Please try again later.\n"
    "\n"
    "Status page: {status_page}"
)

VRCHAT_ISSUE_UNEXPECTED = N_(
    "Verification could not be completed because VRChat returned an unexpected error. Please try again later."
)

COOLDOWN_ACTIVE = N_(
    "You're doing that too fast. Please wait {seconds} seconds and try again."
)

BTN_DONATE = N_("Donate")

SETUP_DONATE_HINT = N_(
    "\n"
    "\n"
    "☕ VRCVerify is free thanks to donations. If it helps your community, you can support it here: {kofi_link}"
)

MILESTONE_OWNER_DM = N_(
    "🎉 **{server}** has reached {count} completed verifications with VRCVerify!\n"
    "The bot is free and runs on donations — if it's been useful to your community, you can support it here: {kofi_link}\n"
    "(This is a one-time message.)"
)

SETUP_PANEL_NUDGE = N_(
    "\n"
    "\n"
    "📌 **One more step:** members can't verify until you post the instructions panel.\n"
    "Run `/vrcverify_instructions` in the channel you want them to verify from. Use a normal text channel everyone can see — not a thread, since threads auto-archive and quietly break the panel.\n"
    "You can run `/vrcverify_status` any time to check on it."
)

PANEL_NUDGE_DM = N_(
    "👋 You set up VRCVerify in **{server}**, but no instructions panel has been posted yet — so members there still have no way to start verifying.\n"
    "\n"
    "Run `/vrcverify_instructions` in the channel you want them to verify from. Use a normal text channel everyone can see rather than a thread, since threads auto-archive and quietly break the panel.\n"
    "\n"
    "Run `/vrcverify_status` in your server for a quick health check.\n"
    "(This is a one-time message.)"
)

STATUS_HEADER = N_("**VRCVerify status for {server}**")

STATUS_ROLE_OK = N_("✅ Verified role: **{role}**")

STATUS_ROLE_MISSING = N_(
    "❌ No verified role set — run `/vrcverify_setup` to choose one."
)

STATUS_ROLE_DELETED = N_(
    "❌ The configured verified role no longer exists — run `/vrcverify_setup` to pick a new one."
)

STATUS_PANEL_OK = N_("✅ Instructions panel is posted and the bot can still update it.")

STATUS_PANEL_MISSING = N_(
    "❌ No instructions panel posted — run `/vrcverify_instructions` in the channel members should verify from."
)

STATUS_PANEL_UNREACHABLE = N_(
    "⚠️ The instructions panel exists but the bot can't update it. Check that it still has **View Channel**, **Send Messages** and **Embed Links** in that channel."
)

STATUS_PANEL_ARCHIVED = N_(
    "⚠️ The instructions panel is in a thread that has been archived, so the bot can't update it. Un-archive the thread, or post a fresh panel in a normal text channel."
)

STATUS_PANEL_GONE = N_(
    "❌ The saved instructions panel no longer exists — it or its channel was deleted. Run `/vrcverify_instructions` again to post a new one."
)

STATUS_TIPS = N_(
    "\n"
    "**Tips**\n"
    "• Post the panel in a normal text channel everyone can see — threads auto-archive and silently break it.\n"
    "• Keep the bot's **View Channel**, **Send Messages** and **Embed Links** permissions in that channel.\n"
    "• If you delete or recreate that channel, run `/vrcverify_instructions` again."
)

GUILD_JOIN_WELCOME_DM = N_(
    "👋 Thanks for adding VRCVerify to **{server}**!\n"
    "\n"
    "To get set up:\n"
    "1. Run `/vrcverify_setup` to choose the role members get once verified.\n"
    "2. Run `/vrcverify_instructions` in the channel you want members to verify from — use a normal text channel everyone can see, not a thread, since threads auto-archive and quietly break the panel.\n"
    "\n"
    "Need a hand? `/vrcverify_support` has you covered."
)

PREMIUM_STATUS_ACTIVE = N_(
    "✅ **VRCVerify Premium is active on {server}.**\n"
    "\n"
    "Unlocked here:\n"
    "• Verification activity log — set it up with `/vrcverify_logchannel`\n"
    "• Priority in the verification queue when there's a backlog\n"
    "• Automatic removal of the unverified role\n"
    "• Automatic nickname sync with VRChat\n"
    "• Custom post-verification message\n"
    "• Your colour and server icon on the instructions panel\n"
    "• Reduced verification cooldown\n"
    "• Invite verified members straight into your server's VRChat group\n"
    "\n"
    "Appearance and automation settings live in `/vrcverify_settings`.\n"
    "\n"
    "You can manage or cancel this any time from Discord's **User Settings → Subscriptions**.\n"
    "Thank you for supporting VRCVerify. 💜"
)

PREMIUM_STATUS_ACTIVE_CARD = N_(
    "✅ **VRCVerify Premium is active on {server}.**\n"
    "\n"
    "Unlocked here:\n"
    "• Verification activity log — set it up with `/vrcverify_logchannel`\n"
    "• Priority in the verification queue when there's a backlog\n"
    "• Automatic removal of the unverified role\n"
    "• Automatic nickname sync with VRChat\n"
    "• Custom post-verification message\n"
    "• Your colour and server icon on the instructions panel\n"
    "• Reduced verification cooldown\n"
    "• Invite verified members straight into your server's VRChat group\n"
    "\n"
    "Appearance and automation settings live in `/vrcverify_settings`.\n"
    "\n"
    "This server pays by card, so manage or cancel it on the **VRCVerify website** — it won't appear in Discord's subscription settings.\n"
    "Thank you for supporting VRCVerify. 💜"
)

PREMIUM_STATUS_ACTIVE_BOTH = N_(
    "⚠️ **{server} is paying for VRCVerify Premium twice.**\n"
    "\n"
    "There's an active **Discord** subscription and an active **card** subscription for this server. Premium is on and stays on — but you're being charged for both.\n"
    "\n"
    "Nothing has been cancelled for you, deliberately: cancelling a subscription and issuing a refund without a person deciding is not something this bot should do on its own.\n"
    "\n"
    "Keep whichever suits you and cancel the other:\n"
    "• **Discord** — User Settings → Subscriptions\n"
    "• **Card** — the Subscriptions page on the website\n"
    "\n"
    "If you're unsure: the website has 6- and 12-month plans that work out cheaper, and Discord can only bill monthly."
)

PREMIUM_STATUS_INACTIVE = N_(
    "**18+ verification is free on {server}, and always will be.** So is auto-verifying members who are already verified when they join.\n"
    "\n"
    "VRCVerify Premium adds these optional extras for this server:\n"
    "• Verification activity log — every verification in a channel you choose, including the ones that fail silently\n"
    "• Priority in the verification queue when there's a backlog\n"
    "• Automatic removal of the unverified role\n"
    "• Automatic nickname sync with VRChat\n"
    "• Custom post-verification message\n"
    "• Your colour and server icon on the instructions panel\n"
    "• Reduced verification cooldown\n"
    "• Invite verified members straight into your server's VRChat group\n"
    "\n"
    "One subscription covers the whole server, and there are two ways to buy it:\n"
    "• **In Discord** — the button below. Billed monthly.\n"
    "• **By card on the website** — the same Premium, plus 6- and 12-month plans that work out cheaper. Discord can only bill monthly, so the longer plans are website-only."
)

PREMIUM_STATUS_GRANDFATHERED = N_(
    "**18+ verification is free on {server}, and always will be.** So is auto-verifying members who are already verified when they join.\n"
    "\n"
    "Because this server was set up before Premium launched, it also keeps these for free, permanently:\n"
    "• Automatic removal of the unverified role\n"
    "• Automatic nickname sync with VRChat\n"
    "• Custom post-verification message\n"
    "\n"
    "Premium adds these on top:\n"
    "• Verification activity log — every verification in a channel you choose, including the ones that fail silently\n"
    "• Priority in the verification queue when there's a backlog\n"
    "• Your colour and server icon on the instructions panel\n"
    "• Reduced verification cooldown\n"
    "• Invite verified members straight into your server's VRChat group\n"
    "\n"
    "**Premium is available two ways:**\n"
    "• **In Discord** — the button below. Billed monthly.\n"
    "• **By card on the website** — the same Premium, plus 6- and 12-month plans that work out cheaper. Discord can only bill monthly, so the longer plans are website-only."
)

PREMIUM_CUTOVER_DM = N_(
    "👋 A quick heads-up about VRCVerify in **{server}**.\n"
    "\n"
    "VRCVerify now has an optional Premium tier — and to be clear up front, **nothing about your server changes.**\n"
    "\n"
    "18+ verification is free and staying free, permanently, for everyone. So is auto-verifying members who are already verified when they join.\n"
    "\n"
    "And because **{server}** was set up before Premium launched, it keeps these too, at no cost, permanently:\n"
    "• Automatic removal of the unverified role\n"
    "• Automatic nickname sync with VRChat\n"
    "• Custom post-verification message\n"
    "\n"
    "So there is nothing you need to do. If you're ever curious what Premium adds, run `/vrcverify_subscription` in your server.\n"
    "(This is a one-time message.)"
)

LOG_VERIFIED = N_("✅ {user} — verified 18+ · {when}")

LOG_ROLE_FAILED = N_(
    "⚠️ {user} — verified 18+, but the role could not be assigned. Check that the VRCVerify bot's role sits above the verified role. · {when}"
)

LOG_NOT_18 = N_("❌ {user} — not 18+ according to VRChat · {when}")

LOG_ENTRIES_DROPPED = N_("…{count} earlier entries could not be recorded.")

LOG_CHANNEL_READY = N_(
    "📋 Verification activity will be logged here from now on.\n"
    "Entries show the member, the result and the time — never their VRChat name or ID."
)

LOG_CHANNEL_SET = N_("Verification activity will be logged in {channel}.")

LOG_CHANNEL_CLEARED = N_("Verification activity logging is now off.")

LOG_CHANNEL_PREMIUM_ONLY = N_(
    "The verification activity log is a VRCVerify Premium feature. Core 18+ verification stays free for everyone."
)

LOG_CHANNEL_NO_PERMISSION = N_(
    "I can't post in {channel}. Give the bot **View Channel** and **Send Messages** there, then run this command again."
)

LOG_CHANNEL_ANNOUNCEMENT = N_(
    "{channel} is an announcement channel. Other servers can follow it, which would republish your members' 18+ status outside this server, so it can't be used as a verification log. Please pick a normal text channel."
)

PANEL_COLOR_INVALID = N_(
    "That doesn't look like a hex colour. Use something like `#5865F2` (or `#58F` for short)."
)


# Issue #49 phase 5: the member-facing group invite.

BTN_GROUP_INVITE = N_("Send me an invite")

DM_GROUP_INVITE_OFFER = N_(
    "You're verified in **{server}**! Would you like an invite to their VRChat group, **{group}**?\n"
    "\n"
    "Nothing is sent to VRChat unless you press the button."
)

GROUP_INVITE_WORKING = N_("Asking VRChat for your invite...")

GROUP_INVITE_SENT = N_(
    "Invite sent! Open your VRChat notifications to join **{group}**."
)

GROUP_INVITE_ALREADY_MEMBER = N_(
    "You're already in **{group}**, so there's nothing to send."
)

GROUP_INVITE_ALREADY_INVITED = N_(
    "An invite to **{group}** is already waiting in your VRChat notifications."
)

GROUP_INVITE_BLOCKED = N_(
    "VRChat wouldn't deliver the invite. Group invites may be switched off in your VRChat settings, or the group may be blocked on your account."
)

GROUP_INVITE_BANNED = N_(
    "You can't be invited to **{group}**. Only a group moderator can change that."
)

GROUP_INVITE_SETUP_PROBLEM = N_(
    "**{server}**'s VRChat group isn't set up correctly right now, so the invite couldn't be sent. Please let the server's admins know."
)

GROUP_INVITE_UNAVAILABLE = N_(
    "VRChat didn't answer, so the invite couldn't be sent. Please try again in a few minutes."
)

GROUP_INVITE_TOO_SOON = N_(
    "You've already asked for an invite. Please give it a few minutes before trying again."
)

GROUP_INVITE_ACCOUNT_MISSING = N_(
    "VRChat didn't recognise the account you verified with, so the invite couldn't be sent. Try verifying again to relink your VRChat account."
)

GROUP_INVITE_NOT_A_MEMBER = N_(
    "This invite was for **{server}**, and you're no longer a member there. Join the server and verify again if you'd still like an invite."
)

GROUP_INVITE_NOT_VERIFIED = N_(
    "You're not currently verified as 18+ in **{server}**, so the invite couldn't be sent. Verify again to get a new invite offer."
)

GROUP_INVITE_ACCOUNT_CHANGED = N_(
    "This offer was for a different VRChat account than the one you have linked now. Verify again to get a new invite offer for your current account."
)

# -- every msgid in this file, for the checks that have to iterate them --
#
# The dict this file used to be could be walked with .items(); a module of
# constants cannot, and several checks legitimately need to ask "is this string
# one of ours" or "does every one of these render in Japanese". Built from
# globals() rather than hand-listed for the obvious reason: a hand-list is a
# second place to forget a string, and forgetting one there would silently
# shrink the very checks that exist to catch a forgotten string.
#
# COMPUTED ON FIRST ACCESS, NOT HERE, and that is not a micro-optimisation.
# The natural way to add a string -- the way the README tells you to -- is to
# append a constant to the end of this file. A frozenset built at this line
# would not contain anything written below it, so a newly added string would
# be missing from `ALL_MESSAGES` while being present in the .pot and in every
# catalogue. Every check keyed on this set would then quietly stop covering
# the newest string in the file, which is the one most likely to be wrong.
#
# That is not hypothetical: it is what this module did for one commit, and
# what the .pot-versus-code test caught while #231 was proving out the
# documented "add a string" workflow.
_all_messages = None


def __getattr__(name: str):
    """Resolve `ALL_MESSAGES` lazily, so its value cannot depend on where in
    this file the last constant was written."""
    if name != "ALL_MESSAGES":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _all_messages
    if _all_messages is None:
        _all_messages = frozenset(
            value
            for key, value in globals().items()
            if key.isupper() and isinstance(value, str)
        )
    return _all_messages
