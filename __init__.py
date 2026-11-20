"""tmux-watch: no agent tools or hooks, dashboard/desktop backend only.

register() is a required no-op for the Hermes plugin loader; the real logic
is in dashboard/plugin_api.py (backend) and desktop/plugin.js (UI pane).
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """No tools, no hooks - this plugin only ships a dashboard/desktop backend."""
    return None
