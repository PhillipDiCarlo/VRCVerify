"""A file on disk that says this process is still alive (issue #170 phase 2).

WHY A FILE, AND NOT A REQUEST TO THE STATUS PAGE

Nothing on the homelab is reachable from the internet, by design, so the status
page cannot probe these services and they have to speak outward instead. They
could each post their own heartbeat, and the first sketch had them doing
exactly that. They do not, for three reasons:

  * It would put the status page's signing key inside the bot, the checker and
    the inviter -- three copies of a credential, in the three processes that
    already hold the ones worth stealing.
  * It would add an outbound HTTP call to the public internet from processes
    whose network access is otherwise entirely inward. That is a new hole in
    the shape of the thing, for bookkeeping.
  * A process wedged in a way that still lets a background thread post is a
    process that would keep reporting itself healthy. Writing to a file has
    the same weakness, but at least the weakness sits in one place -- see the
    limit below -- rather than being distributed across three services.

So each service drops a small JSON file in a shared volume, and one reporter
container reads them and makes a single signed call out. See
src/status_reporter.py.

THE LIMIT, STATED PLAINLY: this proves the PROCESS is alive, not that it is
doing its job. The writer is a daemon thread, so a consumer deadlocked inside
a callback would still be reported as up. That is why the probe callback
exists: each service answers with what it can actually check -- the gateway
being ready, the broker connection being open, a SELECT reaching the database
-- rather than with the mere fact that this thread is running. A service that
supplies no probe is reporting only "the interpreter is still executing", and
should say so in its detail.

DISABLED BY DEFAULT. With HEARTBEAT_DIR unset nothing starts, no thread runs
and no file is written. A deployment that has not been given the volume is not
a deployment that should start failing to write to it every thirty seconds.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import threading
import time
from typing import Callable, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# One part's answer: is it up, and one short line about why. The line is
# PRIVATE -- it reaches the status page's alerting, never its public page --
# so it may and should name the thing that broke.
ProbeResult = Mapping[str, Tuple[bool, Optional[str]]]

DEFAULT_INTERVAL = 30.0


def heartbeat_dir() -> Optional[pathlib.Path]:
    """Where heartbeats go, or None if this deployment has not asked for them."""
    raw = os.getenv("HEARTBEAT_DIR", "").strip()
    return pathlib.Path(raw) if raw else None


def write_heartbeat(path: pathlib.Path, service: str, parts: ProbeResult) -> None:
    """One atomic write. Never raises.

    Atomic because the reporter reads these files on its own schedule and has
    no way to lock against a writer: a torn read would be a JSON parse error,
    which the reporter would have to interpret as "cannot tell", which is one
    step from "down" on a page people trust. os.replace on the same filesystem
    is the cheapest way to make that impossible.
    """
    payload = {
        "service": service,
        "at": int(time.time()),
        "pid": os.getpid(),
        "parts": {
            name: {"up": bool(up), "detail": detail}
            for name, (up, detail) in parts.items()
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        os.replace(temporary, path)
    except Exception:
        # A heartbeat that cannot be written must never be able to stop the
        # service it is reporting on. Bookkeeping is not allowed to be the
        # reason a verification fails -- the same rule the audit log follows.
        logger.debug("Could not write the heartbeat to %s", path, exc_info=True)


def start_heartbeat(
    service: str,
    probe: Callable[[], ProbeResult],
    interval: float = DEFAULT_INTERVAL,
) -> Optional[threading.Thread]:
    """Start writing `<HEARTBEAT_DIR>/<service>.json` until the process ends.

    Returns None when HEARTBEAT_DIR is unset, which is the ordinary case for a
    deployment that has not turned the status page on.

    A daemon thread, so it cannot hold a shutdown open, and matching
    vrc_session.start_relogin_thread which every one of these services already
    starts at boot.
    """
    directory = heartbeat_dir()
    if directory is None:
        return None

    path = directory / f"{service}.json"

    def loop() -> None:
        while True:
            try:
                parts = probe()
            except Exception as error:
                # The probe failing IS a finding, and reporting the service as
                # up because we could not ask would be the same lie the status
                # page refuses to tell.
                parts = {service: (False, f"probe raised {type(error).__name__}")}
                logger.debug("Heartbeat probe for %s raised", service, exc_info=True)
            write_heartbeat(path, service, parts)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name=f"heartbeat-{service}", daemon=True)
    thread.start()
    logger.info("Heartbeat for %s every %ss -> %s", service, interval, path)
    return thread


# -------------------------------------------------------------------
# The watchdog (issue #325)
# -------------------------------------------------------------------
# A process that is running but stuck is invisible to Docker: `restart:`
# only acts when the process exits. After a long network outage the bot sat
# "Up" and offline until somebody restarted it by hand. So each service names
# the parts of itself that a restart can actually fix, and when one of them has
# been down without a break for WATCHDOG_STALL_SECONDS the process exits and
# Docker starts a fresh one.
#
# ITS OWN THREAD, NOT THE HEARTBEAT'S. The heartbeat only runs when
# HEARTBEAT_DIR is set, and the bot's heartbeat probe runs a real SELECT that a
# dead database connection can hold for minutes. A watchdog sharing that thread
# would be switched off with the status page and frozen by the very hang it is
# meant to notice.
#
# NOTHING A RESTART CANNOT FIX. The database being down, or the bot API being
# off, is not watched: a fresh process would find them exactly as broken.

DEFAULT_WATCHDOG_STALL_SECONDS = 900
WATCHDOG_INTERVAL = 30.0


def watchdog_stall_seconds() -> int:
    """WATCHDOG_STALL_SECONDS, 900 by default. 0 (or less) turns it off."""
    raw = os.getenv("WATCHDOG_STALL_SECONDS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_WATCHDOG_STALL_SECONDS
    except ValueError:
        return DEFAULT_WATCHDOG_STALL_SECONDS


class StallTracker:
    """How long each watched part has been down without a break.

    Every part starts out down as of `started`: a service that never becomes
    healthy (the network is still gone when it boots) is restarted once the
    stall time has passed since boot, not straight away and not never. One
    healthy reading resets a part, so a part that flaps is not a stall.
    """

    def __init__(self, parts, stall_seconds: float, started: float):
        self.stall_seconds = stall_seconds
        self.down_since = {part: started for part in parts}

    def observe(self, readings: ProbeResult, now: float) -> Optional[Tuple[str, float, Optional[str]]]:
        """Record one probe. Returns (part, seconds down, detail) for a stall, else None.

        A watched part missing from the readings counts as down: a probe that
        stopped mentioning it has stopped vouching for it.
        """
        stalled = None
        for part in self.down_since:
            up, detail = readings.get(part, (False, "not reported"))
            if up:
                self.down_since[part] = None
                continue
            if self.down_since[part] is None:
                self.down_since[part] = now
            down_for = now - self.down_since[part]
            if down_for >= self.stall_seconds and (stalled is None or down_for > stalled[1]):
                stalled = (part, down_for, detail)
        return stalled


def exit_for_restart(service: str, part: str, down_for: float, detail: Optional[str]) -> None:
    """Log why, then leave with code 1 so Docker starts the service again.

    os._exit, not sys.exit. A normal exit joins every thread of the default
    executor at interpreter shutdown, and the RabbitMQ consumers there loop
    forever, so sys.exit would hang in exactly the state it is meant to end.
    Code 1 because both `unless-stopped` and `on-failure` restart on it.
    """
    logger.error(
        "Watchdog: %s part %r has been down for %ds (%s). Exiting so Docker restarts it.",
        service,
        part,
        int(down_for),
        detail or "no detail",
    )
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass
    os._exit(1)


def start_watchdog(
    service: str,
    probe: Callable[[], ProbeResult],
    parts,
    stall_seconds: Optional[int] = None,
    interval: float = WATCHDOG_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_stall: Callable[[str, str, float, Optional[str]], None] = exit_for_restart,
) -> Optional[threading.Thread]:
    """Watch `parts` of `probe` on a daemon thread. None when turned off.

    `probe` must answer quickly and must not touch anything a restart cannot
    fix. A probe that raises counts as every watched part being down.
    """
    stall = watchdog_stall_seconds() if stall_seconds is None else stall_seconds
    parts = tuple(parts)
    if stall <= 0 or not parts:
        return None
    tracker = StallTracker(parts, stall, clock())

    def loop() -> None:
        while True:
            try:
                readings = probe()
            except Exception as error:
                readings = {part: (False, f"probe raised {type(error).__name__}") for part in parts}
            found = tracker.observe(readings, clock())
            if found is not None:
                on_stall(service, *found)
            sleep(interval)

    thread = threading.Thread(target=loop, name=f"watchdog-{service}", daemon=True)
    thread.start()
    logger.info("Watchdog for %s: restarts after %ss down: %s", service, stall, ", ".join(parts))
    return thread
