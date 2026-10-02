"""Plugin HTTP calls must not go out as ``Python-urllib``.

Cloudflare answers that User-Agent with error 1010 on ``*.workers.dev``, so a
self-hosted Cognee behind a Worker rejects every hook call.
"""

from __future__ import annotations

import http.server
import ssl
import threading
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def _claude_code_only(suite):
    if suite.name != "claude-code":
        pytest.skip("the User-Agent default lives in the claude-code integration")


@pytest.fixture
def seen_agents():
    agents: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            agents.append(self.headers.get("User-Agent", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/", agents
    server.shutdown()


def test_plugin_requests_carry_the_plugin_user_agent(suite, isolated_modules, seen_agents):
    isolated_modules(suite, "_plugin_common")
    url, agents = seen_agents
    urllib.request.urlopen(url, timeout=5).read()
    # context= makes urlopen build a fresh opener, the path most plugin calls take.
    urllib.request.urlopen(url, timeout=5, context=ssl.create_default_context()).read()
    assert len(agents) == 2
    assert all(a.startswith("cognee-memory-plugin") and "Python-urllib" not in a for a in agents), agents


def test_an_explicit_user_agent_still_wins(suite, isolated_modules, seen_agents):
    isolated_modules(suite, "_plugin_common")
    url, agents = seen_agents
    urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "custom/1"}), timeout=5).read()
    assert agents == ["custom/1"]


def test_loading_twice_does_not_stack_wrappers(suite, isolated_modules):
    isolated_modules(suite, "_plugin_common")
    first = urllib.request.OpenerDirector.__init__
    isolated_modules(suite, "_plugin_common")
    assert urllib.request.OpenerDirector.__init__ is first
