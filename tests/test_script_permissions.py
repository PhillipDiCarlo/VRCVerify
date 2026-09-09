"""A script the docs tell you to execute has to be executable.

`./scripts/test_postgres.sh` was documented in #275 and committed at mode
100644, so the command in the README returned "permission denied" on every
fresh clone. `./scripts/i18n.sh` had been the same for far longer.

WHY chmod DID NOT CATCH IT. This repo sets `core.fileMode = false`, which tells
git to ignore the executable bit on the filesystem entirely. A local `chmod +x`
then changes the working tree and nothing else: the file stages as 100644,
`git diff` reports no change, and no warning is printed anywhere. The author
sees a working script forever and every checkout after theirs does not.

So the mode has to be asserted against the INDEX rather than against the
working tree -- `os.access(path, os.X_OK)` would pass on the machine of the
person who broke it, which is the one machine where it must not.

Setting the bit needs the index too:

    git update-index --chmod=+x scripts/<name>.sh
"""

import subprocess

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _indexed_modes():
    """{path: mode} straight out of the git index."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts", "tag_and_push_images.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    return modes


@pytest.fixture(scope="module")
def indexed_modes():
    return _indexed_modes()


def _shell_scripts(modes):
    return sorted(p for p in modes if p.endswith(".sh"))


class TestEveryShellScriptIsExecutable:
    """Deliberately every .sh rather than only the ones currently documented.

    A list of "the ones the README mentions" would need updating by whoever
    adds the next script, which is the same person who would forget the bit --
    the two mistakes have the same author and the same moment.
    """

    def test_there_are_scripts_to_check(self, indexed_modes):
        """A glob that silently matches nothing passes forever."""
        assert _shell_scripts(indexed_modes)

    def test_none_is_committed_without_the_bit(self, indexed_modes):
        not_executable = [
            path
            for path in _shell_scripts(indexed_modes)
            if indexed_modes[path] != "100755"
        ]
        assert not not_executable, (
            "These are committed non-executable, so `./<path>` fails on a fresh"
            " clone:\n\n"
            + "\n".join(f"    {path}" for path in not_executable)
            + "\n\nchmod will not fix it while core.fileMode is false. Run:\n\n"
            + "\n".join(
                f"    git update-index --chmod=+x {path}" for path in not_executable
            )
        )


class TestTheCheckReadsTheIndex:
    def test_it_does_not_consult_the_working_tree(self):
        """The bug being prevented is invisible in the working tree, so a check
        that looks there reproduces it rather than catching it."""
        import inspect

        source = inspect.getsource(_indexed_modes)
        assert "git" in source and "ls-files" in source
        assert "os.access" not in source
