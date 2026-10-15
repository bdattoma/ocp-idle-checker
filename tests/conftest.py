"""Pytest fixtures for the ocp_idle_check test suite.

The module under test is a single file at the repo root; pyproject.toml's
`pythonpath` setting puts the repo root (for `import ocp_idle_check`) and
this directory (for `import helpers`) on sys.path before tests run.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

import ocp_idle_check as oic


class _PromHandler(BaseHTTPRequestHandler):
    """Serves canned JSON keyed by (path, promql); 404s anything else.

    POST-only, matching the client: the query rides in the form body,
    never the URL, so a regression to GET query strings fails loudly
    here instead of passing silently.
    """

    responses: ClassVar[dict[tuple[str, str], str]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        query = form.get("query", [""])[0]
        body = self.responses.get((urllib.parse.urlparse(self.path).path, query))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep test output clean


@pytest.fixture
def prom_server():
    """A local HTTP server standing in for thanos-querier.

    ThreadingHTTPServer because the five criteria query it concurrently.
    """
    _PromHandler.responses = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PromHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _PromHandler.responses
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _reset_verbose():
    """run() flips the module-global verbosity; reset it between tests."""
    yield
    oic.set_verbose(False)
