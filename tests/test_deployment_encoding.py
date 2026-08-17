"""The files that get hand-copied between machines must stay ASCII-only.

`docker compose pull` on the VPS once died with "invalid leading UTF-8 octet
(value: 151)". 0x97 is an em dash in Windows-1252: a compose file was copied
from a Windows workstation to a Linux host and something in transit saved it
as ANSI, so a comment character became a byte the YAML parser rejects. It
refuses the whole file, and the error names neither the line nor the
character.

Normalising the content once was not enough. That was done on 2026-08-03 and
by 2026-08-16 six new em dashes had arrived in `.env.example` and
docker-compose.dashboard.yml, because nothing was watching. Hence a test: the
guarantee is worth exactly as much as the thing enforcing it.

Scope is deliberately narrow. These are the artefacts that travel by scp, by
clipboard, or through an editor with an encoding menu -- everything else in
this repo moves through git, which is byte-exact and cannot introduce this.
Prose in Python docstrings is read by Python, not shuttled between hosts, and
is left alone.
"""

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Globs, not a fixed list: a compose file added next year is exactly as
# copyable as the ones here, and would otherwise be covered by nobody.
HAND_COPIED = (
    ".env.example",
    "config/other_configs/*.env.example",
    "config/other_configs/docker-compose*.yml",
    "docker/Dockerfile*",
    "scripts/**/*.sh",
    "scripts/**/*.ps1",
    "scripts/**/*.service",
    "scripts/**/*.timer",
    "tag_and_push_images.sh",
    "tag_and_push_images.ps1",
)


def hand_copied_files() -> list[Path]:
    found: set[Path] = set()
    for pattern in HAND_COPIED:
        found.update(p for p in REPO_ROOT.glob(pattern) if p.is_file())
    return sorted(found)


def test_the_glob_still_matches_something():
    """A typo in a path would turn this whole module into a no-op that passes."""
    names = {p.name for p in hand_copied_files()}
    assert "docker-compose.dashboard.yml" in names
    assert "Dockerfile-bot" in names
    assert ".env.example" in names
    assert "deploy_dashboard.sh" in names


@pytest.mark.parametrize(
    "path", hand_copied_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_byte_above_ascii(path: Path):
    raw = path.read_bytes()
    offenders = []
    for number, line in enumerate(raw.split(b"\n"), start=1):
        for column, byte in enumerate(line, start=1):
            if byte > 0x7F:
                offenders.append((number, column, byte))

    # Say where it is, which is the one thing the compose parser will not do.
    assert not offenders, "\n".join(
        [f"{path.relative_to(REPO_ROOT)} has bytes above 0x7f:"]
        + [
            f"  line {number}, column {column}: 0x{byte:02x}"
            for number, column, byte in offenders[:20]
        ]
        + [
            "Use ASCII punctuation here: an em dash becomes '--', curly quotes",
            "become straight ones. See this module's docstring for why.",
        ]
    )
