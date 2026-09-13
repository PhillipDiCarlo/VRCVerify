"""Merge translations into a site catalog, and report what is still missing.

    python scripts/merge_site_locale.py de < some.json
    python scripts/merge_site_locale.py de --missing

Exists because 243 msgids in eleven languages is more than fits in one edit.
A merge is additive and idempotent: an entry already present is overwritten
only if the incoming value differs, so a partial pass can be repeated.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
LOCALES = REPO / "site_locales"


def load(name: str) -> dict:
    path = LOCALES / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    locale = sys.argv[1]
    inventory = load("messages.json")
    catalog = load(f"{locale}.json")

    if "--missing" in sys.argv:
        missing = [k for k in inventory if k not in catalog]
        json.dump(missing, sys.stdout, ensure_ascii=False, indent=1)
        print(f"\n# {len(missing)} of {len(inventory)} still to translate", file=sys.stderr)
        return 0

    incoming = json.load(sys.stdin)
    unknown = [k for k in incoming if k not in inventory]
    if unknown:
        # A msgid that is not in the inventory is a typo in the key, which
        # would otherwise sit in the catalog forever translating nothing.
        for k in unknown:
            print(f"  NOT A MSGID: {k[:70]!r}", file=sys.stderr)
        return 1

    catalog.update(incoming)
    ordered = {k: catalog[k] for k in inventory if k in catalog}
    (LOCALES / f"{locale}.json").write_text(
        json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    left = len(inventory) - len(ordered)
    print(f"{locale}: {len(ordered)}/{len(inventory)} translated, {left} to go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
