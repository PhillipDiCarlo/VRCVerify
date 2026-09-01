"""The homelab's one voice to the outside world (issue #170 phase 2).

Reads the heartbeat files the bot, the checker and the inviter drop into a
shared volume, and posts one signed summary to status.vrcverify.com every
minute. That is the whole job.

WHAT IT DELIBERATELY DOES NOT HOLD

No database URL. No broker credentials. No Discord token. No Docker socket.
Every fact it reports was established by a process that already had the right
to establish it, and written to a file; this container reads files and makes
one outbound call. Its only secret is the key it signs with, and the worst a
stolen copy of that can do is lie to a status page.

That is not squeamishness. The reason the dashboard holds no database
credential (SECURITY_AUDIT section 2) applies with more force to a container
whose purpose is to talk to the public internet on a schedule.

WHY SILENCE IS THE SIGNAL

A homelab that is off, unplugged, or has lost its internet cannot send
anything, so a report that stops arriving has to mean something. The status
page treats a stale report as `down` rather than `unknown` for exactly that
reason. Everything here follows from it:

  * The interval is short (60s) and the page's patience is three times that,
    so one lost request is not an outage.
  * A part whose heartbeat file is stale is reported down, with the age in the
    detail, rather than omitted. Omitting it would let a stopped container look
    like a feature that was never deployed.
  * Failures posting are logged and retried on the next tick. This process
    never exits on a network error; it is the thing that reports network
    errors.

Run it with:

    python -m status_reporter        (or as its container; see docker/)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("status_reporter")

# The services expected to be writing heartbeats, and what a missing file
# means. A name listed here that never appears is reported as down once the
# grace period has passed -- see _read_heartbeats.
EXPECTED = ("discord-bot", "vrc-online-checker", "vrc-group-inviter")

DEFAULT_INTERVAL = 60.0

# How old a heartbeat file may be before its parts are reported down. The
# writers tick every 30 seconds, so this is three missed writes: long enough
# that a slow disk or a busy host is not an outage, short enough that the page
# is wrong for at most a minute and a half.
STALE_AFTER = 95


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _read_heartbeats(directory: pathlib.Path, now: int) -> dict[str, dict[str, Any]]:
    """Every part any service claims, with stale files answered as down.

    A part reported by more than one service (the queue, which both workers
    watch) is resolved to the WORST answer. One worker still holding a broker
    connection does not make the other one's dropped connection acceptable,
    and taking the best answer would hide exactly the half-broken state that is
    hardest to notice from outside.
    """
    parts: dict[str, dict[str, Any]] = {}

    def record(name: str, up: bool, detail: Optional[str]) -> None:
        existing = parts.get(name)
        if existing is None or (existing["up"] and not up):
            parts[name] = {"up": up, "detail": detail}

    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        service = path.stem
        seen.add(service)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = now - int(payload["at"])
            written = payload.get("parts") or {}
        except Exception as error:
            # An unreadable heartbeat is not a healthy service. It is also not
            # a mystery worth hiding: name the file and the error.
            record(service, False, f"unreadable heartbeat ({type(error).__name__})")
            continue

        if age > STALE_AFTER:
            for name in written or {service: None}:
                record(name, False, f"no heartbeat from {service} for {age}s")
            continue

        for name, answer in written.items():
            record(name, bool(answer.get("up")), answer.get("detail"))

    for service in EXPECTED:
        if service not in seen:
            # Never written at all. On a first deploy this is true and
            # temporary; if it persists it is a container that is not running,
            # which is precisely what this is for.
            record(service, False, "no heartbeat file")

    return parts


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    """Stripe's scheme, because it is the one this project already reasons about.

    The timestamp is signed ALONGSIDE the body rather than merely sent with it.
    Signing the body alone would leave a captured report replayable forever,
    which for this endpoint means holding the page at "everything is fine"
    while the homelab burns -- the one lie the whole design exists to prevent.
    """
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def post_report(url: str, secret: str, parts: dict[str, Any], timeout: float = 10.0) -> bool:
    body = json.dumps({"parts": parts}, separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(time.time())
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-vrcverify-signature": f"t={timestamp},v1={_sign(secret, timestamp, body)}",
            "user-agent": "vrcverify-status-reporter/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        # 401 here means the key does not match, which is worth shouting about:
        # the page will be showing the homelab as down and the homelab is fine.
        logger.error("Status report rejected: HTTP %s %s", error.code, error.reason)
    except Exception as error:
        logger.warning("Status report failed: %s: %s", type(error).__name__, error)
    return False


def main() -> int:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper() or "INFO",
        format="%(asctime)s %(levelname)s %(message)s",
    )

    url = _env("STATUS_REPORT_URL")
    secret = _env("STATUS_REPORT_SECRET")
    directory = pathlib.Path(_env("HEARTBEAT_DIR", "/heartbeats"))
    interval = float(_env("STATUS_REPORT_INTERVAL", str(DEFAULT_INTERVAL)) or DEFAULT_INTERVAL)

    if not url or not secret:
        # Exits 0, matching the invite worker's unprovisioned path: a feature
        # that has not been switched on is not a crash, and compose must not
        # restart-loop a container with nothing to do.
        logger.error(
            "STATUS_REPORT_URL and STATUS_REPORT_SECRET are unset, so there is "
            "nowhere to report to. Exiting; nothing else is affected."
        )
        return 0

    logger.info("Reporting %s every %ss to %s", directory, interval, url)
    while True:
        parts = _read_heartbeats(directory, int(time.time()))
        ok = post_report(url, secret, parts)
        broken = sorted(name for name, part in parts.items() if not part["up"])
        logger.log(
            logging.INFO if ok and not broken else logging.WARNING,
            "Reported %d parts%s%s",
            len(parts),
            f", down: {', '.join(broken)}" if broken else "",
            "" if ok else " (POST failed)",
        )
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
