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
