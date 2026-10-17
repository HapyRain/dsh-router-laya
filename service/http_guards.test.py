"""Plain-script check for the two guards in http_guards.py (repo convention: no framework).

    python service/http_guards.test.py

Both guards are the defects reported in issues #2 and #3, and both are stdlib-only, so this runs
without torch or a checkpoint: a second binder must be refused the port, and the desktop shell
origin must be allowed to read /state.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http.server import BaseHTTPRequestHandler  # noqa: E402

from http_guards import ExclusiveHTTPServer, is_allowed_origin  # noqa: E402

passed = 0
failed = []


def check(name, got, want=True):
    global passed
    if got == want:
        passed += 1
    else:
        failed.append("%s -> got %r, want %r" % (name, got, want))


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


# --- the CORS allowlist (issue #3: the desktop origin was missing, chip read "AUTO · 离线") ---
check("web profile origin allowed", is_allowed_origin("http://127.0.0.1:3080"))
check("desktop shell origin allowed", is_allowed_origin("dsh-app://app"))
check("ipv6 loopback allowed", is_allowed_origin("http://[::1]:3080"))
check("external origin refused", is_allowed_origin("https://example.com"), False)
check("lookalike scheme refused", is_allowed_origin("dsh-appx://app"), False)
check("missing origin refused", is_allowed_origin(None), False)

# --- one owner per port (issue #2: SO_REUSEADDR let 11 Windows judges bind :8765 at once) ---
first = ExclusiveHTTPServer(("127.0.0.1", 0), Quiet)
port = first.server_address[1]
check("first binder owns a port", port > 0)
try:
    second = ExclusiveHTTPServer(("127.0.0.1", port), Quiet)
except OSError:
    check("second binder is refused the same port", True)
else:
    second.server_close()
    check("second binder is refused the same port", False)
finally:
    first.server_close()

print("%d passed, %d failed" % (passed, len(failed)))
for line in failed:
    print("  FAIL " + line)
sys.exit(1 if failed else 0)
