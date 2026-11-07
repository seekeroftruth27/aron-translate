"""Shared helpers for the tests: a fresh copy of the server under a chosen
environment, and a stand-in for ProPresenter's network API.

Nothing here reads a real key, opens the browser, writes into the project or calls
a provider.
"""
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

HAVE_FLASK = importlib.util.find_spec("flask") is not None
need_flask = unittest.skipUnless(HAVE_FLASK, "needs the app's Python (Flask)")

# Every setting a test might inherit from a developer's shell or .env is pinned
# here, so a test sees only what it asks for.
_PINNED = {k: "" for k in (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
    "SONIOX_API_KEY", "ELEVENLABS_API_KEY",
    "CT_PUBLISH_URL", "CT_PUBLISH_TOKEN",
    "CT_PROPRESENTER_URL", "CT_PROPRESENTER_MESSAGE", "CT_PROPRESENTER_TOKEN",
    "CT_PROPRESENTER_TYPE", "CT_PROPRESENTER_FOLLOW_MIC", "CT_PROPRESENTER_QUIET_S")}
_PINNED.update(CT_OPEN="0", CT_SESSION_SAVE="0")

_n = itertools.count()
_tmp = tempfile.TemporaryDirectory(prefix="aron-tests-")


def load_server(**env):
    """A new copy of app/server.py, imported under `_PINNED` plus `env`.

    The server reads most of its configuration when it is imported, so a test that
    needs a different setting needs a different copy. Recordings and saved settings
    go to a temporary folder."""
    full = dict(_PINNED, CT_RECORD_DIR=_tmp.name, **{k: str(v) for k, v in env.items()})
    name = f"aron_server_{next(_n)}"
    spec = importlib.util.spec_from_file_location(name, os.path.join(APP, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with mock.patch.dict(os.environ, full), mock.patch.object(sys, "path", [APP] + sys.path):
        spec.loader.exec_module(mod)
    return mod


class FakeProPresenter:
    """ProPresenter's network API, as far as Aron uses it: records every request and
    answers with `status`."""

    def __init__(self, status=204, look=None):
        self.status = status
        self.look = look            # the reply to GET /v1/look/current (a dict), or None
        self.requests = []          # (method, path, parsed JSON body or None)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                outer.requests.append((self.command, self.path, body))
                if self.path == "/v1/look/current" and outer.look is not None:
                    out = json.dumps(outer.look).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_DELETE = _do

            def log_message(self, *a):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
