"""Default User-Agent for every urllib request a plugin process makes.

Cloudflare rejects ``Python-urllib/*`` with error 1010 on ``*.workers.dev``, so
a self-hosted Cognee behind a Worker refused every hook call. Patching
``OpenerDirector.__init__`` (not ``install_opener``) also covers
``urlopen(context=...)``, which builds a fresh opener per call. A header the
caller sets on its ``Request`` still wins. Scoped to plugin processes, unlike a
machine-wide ``usercustomize.py``.
"""

from __future__ import annotations

import json
import os
import urllib.request


def _version() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except (OSError, ValueError):
        return ""


_VERSION = _version()
USER_AGENT = "cognee-memory-plugin" + (f"/{_VERSION}" if _VERSION else "")


def install() -> None:
    init = urllib.request.OpenerDirector.__init__
    # Isolated test imports reload this module; wrapping again would stack.
    if getattr(init, "_cognee_ua", False):
        return

    def _init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.addheaders = [("User-agent", USER_AGENT)]

    _init._cognee_ua = True
    urllib.request.OpenerDirector.__init__ = _init


install()
