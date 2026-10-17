"""Dependency-free HTTP guards for the judge service.

Neither guard can be exercised through `laya_router.py` itself: importing that module pulls torch in
for `laya`, so a check that imported it could not run in CI. They live here instead, stdlib-only, and
`http_guards.test.py` pins them without a checkpoint.

1. `ExclusiveHTTPServer` -- one listening socket per port, on every platform (issue #2).
2. `is_allowed_origin`   -- the tier chip's CORS allowlist (issue #3).
"""
import os
from http.server import HTTPServer

# Origins the tier chip may poll /state from: the web profile's page on loopback, and the DSH desktop
# shell, which loads the GUI from its own privileged scheme instead of an http port. A prefix
# allowlist rather than `*` on purpose -- /state echoes prompt text back.
ALLOWED_ORIGIN_PREFIXES = (
    "http://127.0.0.1:",
    "http://localhost:",
    "http://[::1]:",
    "dsh-app://",
)


def is_allowed_origin(origin):
    """Whether an `Origin` header value may read /state. A missing origin is refused, not defaulted."""
    return bool(origin) and origin.startswith(ALLOWED_ORIGIN_PREFIXES)


class ExclusiveHTTPServer(HTTPServer):
    """An HTTPServer that a second process cannot share the port with.

    `http.server.HTTPServer` sets `allow_reuse_address = 1` (SO_REUSEADDR). On POSIX that only buys a
    restart the right to rebind a TIME_WAIT port -- two *live* listeners still conflict -- but on
    Windows SO_REUSEADDR is what allows a second socket to bind an address another process is
    already listening on, so the duplicate-start guard the plugin documents in index.js did not
    exist there: every extra `--http` spawn stayed alive and loaded its own copy of the checkpoint
    (issue #2: 11 listeners, ~2.9 GB each).

    Measured on Windows 11 (26300), Python 3.13, loopback: with the owner holding SO_REUSEADDR a
    second SO_REUSEADDR bind succeeds; without it the second bind is refused (WinError 10048, or
    10013 when the second binder sets SO_REUSEADDR itself). Rebinding after the previous owner has
    closed -- the port still in TIME_WAIT -- succeeded in all three combinations, so dropping the
    flag on Windows costs no restart behaviour. POSIX keeps it, where it is still needed for exactly
    that restart.
    """

    allow_reuse_address = os.name != "nt"
