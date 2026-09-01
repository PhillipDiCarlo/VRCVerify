"""The local preview's bot stub must keep answering everything the app asks.

`scripts/preview_bot.py` is a dev tool and gets no test coverage for its
contents -- what it returns is invented and only has to look plausible. One
thing about it is worth pinning, though: it stands in for `BotAPIClient`, and
if the real client grows a method the dashboard calls, the preview starts
failing with an `AttributeError` deep inside a request.

That failure is cheap to prevent and expensive to meet: it appears the next
time somebody opens the preview to look at a page, which is exactly when they
are trying to think about something else.
"""

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("flask")

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from dashboard.botapi import BotAPIClient, BotAPIError  # noqa: E402
from preview_bot import (  # noqa: E402
    FREE,
    GUILDS,
    INSTALLED,
    NOT_ADDED,
    PREMIUM,
    UNREACHABLE,
    PreviewBotAPI,
)


def public_methods(cls) -> set:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name))
    }


def test_the_stub_answers_everything_the_real_client_does():
    """The one thing that can silently break the preview."""
    missing = public_methods(BotAPIClient) - public_methods(PreviewBotAPI)
    assert not missing, f"preview_bot.PreviewBotAPI is missing: {sorted(missing)}"


def test_the_stub_has_not_grown_methods_the_client_lacks():
    """The other direction, which is a subtler kind of wrong: a stub answering
    a call the real client cannot means work done against the preview that
    cannot possibly work in production."""
    extra = public_methods(PreviewBotAPI) - public_methods(BotAPIClient)
    assert not extra, f"PreviewBotAPI answers what the bot cannot: {sorted(extra)}"


def test_every_preview_server_is_obviously_invented():
    """The one risk the stub adds is mistaking canned output for real output.

    Names carry the warning, because the name is what is on screen.
    """
    for guild in GUILDS:
        assert guild["name"].startswith("Preview:"), guild["name"]


def test_the_two_failing_servers_fail_for_different_reasons():
    """They are not interchangeable, and the difference is the point.

    "Bot not added" must be absent from `admin_guild_ids`, because that is
    what the picker draws its un-installed card from. "Bot unreachable" must
    be present: a server the bot is in but cannot answer for is an outage,
    which is a real production state and the one `error.html` exists for.
    """
    stub = PreviewBotAPI()
    installed = stub.admin_guild_ids(1, [g["id"] for g in GUILDS])

    assert installed == INSTALLED
    assert NOT_ADDED not in installed
    assert UNREACHABLE in installed

    # Both refuse every read, by different statuses that render differently.
    for guild_id, status in ((NOT_ADDED, 404), (UNREACHABLE, 503)):
        with pytest.raises(BotAPIError) as refusal:
            stub.settings(1, guild_id)
        assert refusal.value.status == status

    # And the ones that are genuinely usable answer.
    for guild_id in (PREMIUM, FREE):
        assert stub.settings(1, guild_id)["guild_id"] == guild_id


def test_the_preview_never_reads_the_real_environment():
    """The guarantee dev_dashboard.py has carried since it was written, now
    with more moving parts behind it. A stub cannot dial out, but a config read
    from a real .env could still point the *store* somewhere real."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    assert "dotenv" not in source
    assert "load_dotenv" not in source
    # `.env` may be discussed in the docstring; it must not be opened.
    assert 'open(".env"' not in source and "open('.env'" not in source


# --- what starting the preview actually imports ---------------------------
#
# `scripts/preview_bot.py` puts REPO/tests on sys.path and imports two modules
# from it, so the preview's import graph now runs through code nobody thinks of
# as part of the preview. Its docstring calls that the cheaper of two mistakes
# and it is, but it comes with one sharp edge: five modules under src/ call
# `load_dotenv()` at import, and `tests/conftest.py`'s env pinning only runs
# under pytest. A single ordinary `from bot import SOMETHING` added to
# `tests/test_dashboard.py` would put the real `.env` into `os.environ` before
# `create_app()` reads it. The fake credentials would survive -- load_dotenv
# does not override what is already set -- and everything `dev_dashboard.py`
# does *not* pin would quietly come from production. See #162.
#
# Grepping one more filename would guard that one file and wait for the third,
# so what is asserted is the closure: whatever route it arrives by, it is seen.

_ROOTS = (REPO / "scripts", REPO / "src", REPO / "tests")


def _module_file(name):
    """Where this repo would import `name` from, or None for anything else.

    Only the three roots the preview puts on `sys.path` are searched, in that
    order. The standard library, flask and requests all resolve to None and
    the walk stops there, which is the intent -- third-party code is not ours
    to police and none of it reads this repository's `.env`.
    """
    parts = name.split(".")
    for root in _ROOTS:
        candidate = root.joinpath(*parts)
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def _package_of(path):
    """The dotted package `path` sits in, for resolving `from . import x`.

    Nothing in this repo uses a relative import today. Handled anyway, because
    a walker that silently skipped one would leave the guarantee below looking
    green over a graph it had stopped following -- which is the exact shape of
    failure this whole section exists to prevent.
    """
    for root in _ROOTS:
        if root in path.parents:
            return ".".join(path.relative_to(root).parts[:-1])
    return ""


def _imported(tree, package):
    """Every module name an AST asks for, including inside function bodies.

    Deliberately not limited to module scope. A deferred import is still a
    dependency -- it fires on a request rather than at start-up -- and
    `load_dotenv()` reached at request time pollutes `os.environ` just as
    thoroughly, only later and more confusingly.

    `from X import a, b` yields `X`, `X.a` and `X.b`, because the names may be
    submodules or may be attributes and only the filesystem can say which.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = [p for p in package.split(".") if p]
                parts = parts[: len(parts) - node.level + 1]
                module = ".".join(parts + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module:
                continue
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"


def _preview_import_closure():
    """Every file of this repo's own code that starting the preview pulls in.

    Static, so it needs neither a subprocess nor a running app, and so it sees
    imports that only fire on some code path. Returns path -> parsed AST; the
    callers below want both.
    """
    entry = REPO / "scripts" / "dev_dashboard.py"
    found, queue = {}, [entry]
    while queue:
        path = queue.pop()
        if path in found:
            continue
        found[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported(found[path], _package_of(path)):
            target = _module_file(name)
            if target is not None and target not in found:
                queue.append(target)
    return found


def test_the_import_walk_reaches_the_modules_it_exists_to_watch():
    """The guard below is only as good as the walk under it, and a walk that
    quietly reached nothing would pass every assertion in this file.

    So pin the far end. `preview_bot` is one hop out, the two test modules are
    two, and `config.py` is three and inside a package -- if any of those stops
    being found, the walker broke rather than the dependency.
    """
    reached = {path.relative_to(REPO).as_posix() for path in _preview_import_closure()}
    for name in (
        "scripts/dev_dashboard.py",
        "scripts/preview_bot.py",
        "tests/test_dashboard.py",
        "tests/test_subscription_page.py",
        "src/dashboard/app.py",
        "src/dashboard/config.py",
    ):
        assert name in reached, f"the import walk never reached {name}"


def _repo_directories_named_in(path):
    """Every `REPO / "x"` in a file where x is a directory of this repo."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.right.value
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Name)
        and node.left.id == "REPO"
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
        and (REPO / node.right.value).is_dir()
    }


def test_the_import_walk_searches_every_root_the_preview_adds():
    """`_ROOTS` is a hardcoded list, which is the same shape of thing as the
    grep this section replaced.

    `preview_bot.py` adding `tests/` to `sys.path` is what created the hazard
    in the first place. A fourth root added the same way would not break any
    assertion above -- the walk would simply stop following imports into it and
    stay green over a graph it had given up on. So the roots are checked
    against what the two scripts actually reach for.
    """
    named = set()
    for name in ("dev_dashboard.py", "preview_bot.py"):
        named |= _repo_directories_named_in(REPO / "scripts" / name)

    unwatched = named - {root.name for root in _ROOTS}
    assert not unwatched, (
        f"the preview reaches into {sorted(unwatched)}, which the import walk"
        " above does not search. Add it to _ROOTS."
    )


def test_nothing_the_preview_imports_reads_dotenv():
    """dev_dashboard.py's standing promise, asserted over the whole graph
    rather than the one file that makes it.

    `dotenv` is what is banned rather than `bot`, because `bot` is only the
    likeliest of five modules that call `load_dotenv()` at import and any of
    them has the same effect. Read from the import statements rather than
    grepped for as a string, so the several docstrings here that discuss the
    hazard by name cannot fail this.
    """
    guilty = {}
    for path, tree in _preview_import_closure().items():
        wants = sorted(
            {
                name
                for name in _imported(tree, _package_of(path))
                if name == "dotenv" or name.startswith("dotenv.")
            }
        )
        if wants:
            guilty[path.relative_to(REPO).as_posix()] = wants

    assert not guilty, (
        f"the local preview would load the real .env through {guilty} -- every"
        " variable dev_dashboard.py does not pin would then come from"
        " production. Move the import rather than relaxing this; see #162."
    )


def test_the_bot_is_not_in_the_previews_import_closure():
    """The route the hazard would actually arrive by, named on its own so the
    failure says what to do.

    The invariant is already written down in tests/test_overview.py, which
    keeps its own `test_dashboard` import lazy and inside a method precisely to
    hold this line. Until now nothing enforced it.
    """
    closure = {p.relative_to(REPO).as_posix() for p in _preview_import_closure()}
    assert "src/bot.py" not in closure, (
        "something the preview imports now imports the bot, which calls"
        " load_dotenv() at import. See #162."
    )


def test_the_preview_binds_only_to_loopback():
    """It runs on laptops on untrusted networks, and it is now signed in by
    default -- so a bind to 0.0.0.0 would put a session-carrying dashboard on
    the coffee shop wifi."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    assert 'HOST = "127.0.0.1"' in source
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "0.0.0.0" not in code


def test_the_stub_remembers_what_was_saved():
    """A preview where nothing persists cannot show the difference between a
    switch that saved and one that only looked like it -- which is the whole
    hazard #133 phase 4 is about."""
    stub = PreviewBotAPI()
    guild_id = sorted(INSTALLED)[0]

    before = stub.settings(1, guild_id)["fields"]["auto_verify_new_members"]["value"]
    stub.update_settings(1, guild_id, {"auto_verify_new_members": not before})
    after = stub.settings(1, guild_id)["fields"]["auto_verify_new_members"]["value"]

    assert after is not before


def test_the_launch_config_offers_every_preview_state():
    """Three, because three states of the app cannot be reached from one
    another: signed in (most of the work), signed out (#134 redesigns that
    page), and the bot refusing everything (the picker's unknown cards and
    error.html, which have no other local route)."""
    import json
    import re

    raw = (REPO / ".vscode" / "launch.json").read_text(encoding="utf-8")
    configs = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))["configurations"]
    previews = [
        c.get("env", {})
        for c in configs
        if c.get("program", "").endswith("dev_dashboard.py")
    ]

    assert {} in previews
    assert {"PREVIEW_SIGNED_IN": "0"} in previews
    assert {"PREVIEW_BOT_DOWN": "1"} in previews
    assert len(previews) == 3


# --- who the signed-in preview will answer as -----------------------------
#
# Loopback is not the same as private. The bind keeps the network out; on a
# shared host every local account can still open a socket to 127.0.0.1:5001,
# and the preview session used to be injected on every request with no cookie
# and nothing to present. Everything behind it is invented and CSRF still
# refuses cross-origin writes, so this was small -- but "less able to reach
# production, not more" was being claimed on one axis and was quiet about the
# other. #162, second half.

PROBE_TOKEN = "test-preview-token-not-a-real-one"

PROBE = """
import json, pathlib, sys

REPO = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(REPO / "scripts"))

import dev_dashboard as preview

client = preview.app.test_client()


def signed_in(path):
    return "Preview: premium server" in client.get(path).get_data(as_text=True)


# Order matters. The first three run against an empty cookie jar; the fourth
# is what fills it, which is the whole point of the fifth.
print(json.dumps({
    "token": preview.PREVIEW_TOKEN,
    "anonymous": signed_in("/"),
    "wrong_token": signed_in("/?preview=not-the-token"),
    "with_token": signed_in("/?preview=" + preview.PREVIEW_TOKEN),
    "after_token": signed_in("/"),
}))
"""


@pytest.fixture(scope="module")
def probe():
    """Start the preview in a fresh interpreter and ask it five questions.

    A SUBPROCESS RATHER THAN AN IMPORT, and not for speed. `dev_dashboard.py`
    rewrites `os.environ` at import for whichever process imports it -- fake
    Discord credentials, fake signing keys, a scratch session path. Doing that
    inside the suite would push a preview's environment onto every test that
    ran afterwards, which is the mirror image of the leak the other half of
    this file guards against.

    `PREVIEW_TOKEN` is handed in rather than scraped from the banner, which is
    also how scripts/shoot_pages.py drives it.
    """
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO)],
        capture_output=True,
        text=True,
        env=dict(os.environ, PREVIEW_TOKEN=PROBE_TOKEN),
    )
    assert done.returncode == 0, done.stderr
    # The last line: the preview prints its own hint to stderr, but a stray
    # warning on stdout should not turn a real answer into a JSON error.
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_preview_takes_its_token_from_the_environment_when_given(probe):
    """scripts/shoot_pages.py sends the server's output to /dev/null, so it has
    to choose the token rather than read it. If this stops working the
    screenshots come back as seven copies of the sign-in page."""
    assert probe["token"] == PROBE_TOKEN


def test_the_token_is_random_when_it_is_not_given():
    """The other path, and the one that matters on a shared host: a fixed
    default would be no gate at all."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    assert "secrets.token_urlsafe" in source


def test_a_request_without_the_token_is_served_signed_out(probe):
    """The refusal, and the shape of it matters as much as the fact.

    Signed out rather than 403: somebody else on this host who finds port 5001
    should see exactly what a stranger sees, not a message telling them there
    is a token worth having.
    """
    assert probe["anonymous"] is False
    assert probe["wrong_token"] is False


def test_the_token_signs_you_in_and_is_then_remembered(probe):
    """Presented once in the URL, kept as a cookie. Every link on every page
    has to work afterwards without carrying it, or the preview becomes
    unusable the moment you click anything."""
    assert probe["with_token"] is True
    assert probe["after_token"] is True


def _constant_in(path, name):
    """Read a module-level `NAME = "literal"` without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return node.value.value
    raise AssertionError(f"{path.name} no longer defines {name}")


def test_the_screenshot_script_names_the_cookie_the_preview_sets():
    """`scripts/shoot_pages.py` opens every page as a deep link in a fresh
    browser context, so it plants the preview cookie rather than exchanging the
    token through a query string -- and it writes the cookie's name as a
    literal, because importing dev_dashboard would rewrite its own os.environ.

    Two files holding the same string, which is the drift this whole file is
    about. The failure is silent in the worst way: the run still finishes and
    still reports 42 screenshots, and every one of them is the sign-in page.
    """
    cookie = _constant_in(REPO / "scripts" / "dev_dashboard.py", "PREVIEW_TOKEN_COOKIE")
    shooter = (REPO / "scripts" / "shoot_pages.py").read_text(encoding="utf-8")
    assert f'"{cookie}"' in shooter, (
        f"dev_dashboard.py now sets its preview cookie as {cookie!r};"
        " shoot_pages.py still plants the old name and would shoot 42"
        " screenshots of the sign-in page."
    )


def test_the_scratch_session_file_is_not_inside_the_repo():
    """It holds an authenticated session now. Committing one would be
    harmless -- the credentials are fake -- and still a bad habit to leave
    lying where a real one could follow it."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    line = next(
        one for one in source.splitlines() if "SESSION_DB_PATH" in one
    )
    path = line.split("=", 1)[1].strip().strip('",')
    assert os.path.isabs(path)
    assert not path.startswith(str(REPO))
