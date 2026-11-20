"""tmux-watch dashboard plugin backend - thin FastAPI wrapper.

Mounted at /api/plugins/tmux-watch/ by the Hermes gateway. All real logic
lives in logic.py (stdlib + asyncio only, no FastAPI dependency) so it can
be unit-tested with a plain `python3`, independent of whichever FastAPI
install the Hermes gateway happens to use.

Three endpoints: GET /status, GET /config (read-only, always available),
and POST /kill - the one command-executing endpoint, gated per-host behind
`allow_control: true` in config.json (default false). The enforcement check
lives HERE, in exactly one place, not duplicated into logic.py - see
kill_session()'s docstring.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import logic  # noqa: E402  (after sys.path fix-up, see above)

from fastapi import APIRouter

router = APIRouter()

# Per-process, in-memory stall-tracking state: resets on gateway restart,
# which is fine: a freshly-restarted gateway has no history to judge "stuck"
# against anyway.
_STALL_STATE: dict[tuple[str, str], dict[str, Any]] = {}


@router.get("/status")
async def status() -> dict[str, Any]:
    cfg = logic.load_config()
    hosts = cfg["hosts"]
    if not hosts:
        return {
            "ok": True,
            "hosts": [],
            "checked_at": time.time(),
            "note": f"no hosts configured - edit {logic.config_path()}",
        }
    results = await asyncio.gather(*(logic.host_status(h, _STALL_STATE) for h in hosts))
    return {"ok": True, "hosts": results, "checked_at": time.time()}


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """Host labels/addresses only - never secrets; config.json holds none by design."""
    cfg = logic.load_config()
    return {"config_path": str(logic.config_path()), "hosts": cfg["hosts"]}


@router.post("/kill")
async def kill(ssh_host: str, session_name: str) -> dict[str, Any]:
    """Kill one tmux session on one host. Refuses unless that host has
    allow_control: true in config.json - opt-in, off by default, because
    this can lose unsaved work in whatever the session was running."""
    cfg = logic.load_config()
    host = next((h for h in cfg["hosts"] if h["ssh_host"] == ssh_host), None)
    if host is None:
        return {"ok": False, "error": f"host {ssh_host!r} is not in config.json"}
    if not host.get("allow_control"):
        return {
            "ok": False,
            "error": f"control is not enabled for {ssh_host!r} - set allow_control: true in {logic.config_path()}",
        }
    ok, message = await logic.kill_session(ssh_host, session_name, host["remote_path_prefix"])
    return {"ok": ok, "message": message}
