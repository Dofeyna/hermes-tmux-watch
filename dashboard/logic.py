"""tmux-watch pure logic - stdlib + asyncio only, no FastAPI import.

Deliberately separated from plugin_api.py so this module can be imported
and unit-tested with a plain `python3`, independent of whatever FastAPI/
Hermes environment the real gateway runs in. plugin_api.py is a thin
FastAPI wrapper around what's defined here.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

SSH_TIMEOUT_S = 8
SCROLLBACK_LINES = 40
ACTIVE_THRESHOLD_S = 10   # output changed within this window -> "active"
STALL_THRESHOLD_S = 300  # unchanged for this long -> "possibly stuck"

DEFAULT_CONFIG = {
    "hosts": [
        {
            "label": "example-host",
            "ssh_host": "example-host",
            "remote_path_prefix": "",
            "allow_control": False,
        }
    ]
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "") or "~/.config").expanduser()
    return base / "hermes-tmux-watch" / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        except OSError:
            pass
        return {"hosts": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"hosts": []}
    hosts = data.get("hosts")
    if not isinstance(hosts, list):
        return {"hosts": []}
    cleaned = []
    for h in hosts:
        if not isinstance(h, dict):
            continue
        ssh_host = str(h.get("ssh_host") or "").strip()
        if not ssh_host:
            continue
        cleaned.append(
            {
                "label": str(h.get("label") or ssh_host).strip(),
                "ssh_host": ssh_host,
                "remote_path_prefix": str(h.get("remote_path_prefix") or ""),
                # Opt-in, defaults OFF: lets the Desktop pane offer a
                # confirm-gated "kill session" button for this host. Off by
                # default because killing a session can lose unsaved work in
                # whatever it was running.
                "allow_control": bool(h.get("allow_control", False)),
            }
        )
    return {"hosts": cleaned}


# ---------------------------------------------------------------------------
# ANSI handling
#
# tmux capture-pane -e includes raw escape sequences (colors, styles). A
# remote session can print whatever it wants, so before that text reaches
# the Desktop renderer it is sanitized down to SGR (color/style) codes only;
# every other control sequence (cursor moves, OSC window-title sets,
# screen clears, etc.) is stripped. This is a security boundary, not just
# cosmetics: a compromised or malicious remote process cannot use terminal
# escape sequences to manipulate the pane beyond picking text colors.
# ---------------------------------------------------------------------------

_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CSI_RE = re.compile(r"\x1b\[[0-9;]*([A-Za-z])")
_LONE_ESC_RE = re.compile(r"\x1b(?!\[)")


def sanitize_ansi_sgr(text: str) -> str:
    """Strip every control sequence except SGR (ESC[...m) color/style codes."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub(lambda m: m.group(0) if m.group(1) == "m" else "", text)
    text = _LONE_ESC_RE.sub("", text)
    return text


def strip_all_ansi(text: str) -> str:
    """Remove every control sequence including SGR - for plain-text heuristics
    (e.g. "does the last line look like a shell prompt") that must not be
    confused by color codes trailing the visible characters."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _LONE_ESC_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Activity classification: active / idle / stalled
#
# A fixed "does the last line look like a shell prompt" heuristic doesn't
# generalize: Claude Code's TUI, for one, doesn't end its idle screen in a
# plain $/>/❯ character, so that check silently never fires and a fully
# idle session reads as "active" forever. The generic, app-agnostic signal
# is simply: how long has the WHOLE captured screen gone unchanged? Same
# spirit as the hermes-monitor plugin's worker states (active/stalled/loop).
# ---------------------------------------------------------------------------


def classify_activity(
    state: dict[tuple[str, str], dict[str, Any]],
    key: tuple[str, str],
    screen_signature: str,
    now: float,
    active_threshold_s: float = ACTIVE_THRESHOLD_S,
    stall_threshold_s: float = STALL_THRESHOLD_S,
) -> tuple[str, float]:
    """Mutates `state` in place; returns (status, idle_seconds) where status
    is 'active' | 'idle' | 'stalled'. `screen_signature` should be the full
    plain-text (ANSI-stripped) captured pane content, not just the last
    line - a change anywhere on screen counts, not only at the bottom.
    Deterministic given its inputs - no asyncio/SSH/FastAPI, easy to test."""
    prev = state.get(key)
    if prev is None or prev["signature"] != screen_signature:
        state[key] = {"signature": screen_signature, "unchanged_since": now}
        idle_seconds = 0.0
    else:
        idle_seconds = now - prev["unchanged_since"]

    if idle_seconds >= stall_threshold_s:
        status = "stalled"
    elif idle_seconds < active_threshold_s:
        status = "active"
    else:
        status = "idle"
    return status, idle_seconds


# ---------------------------------------------------------------------------
# SSH + tmux
# ---------------------------------------------------------------------------


async def run_ssh(ssh_host: str, remote_cmd: str, path_prefix: str = "") -> tuple[int, str, str]:
    """Run one command on ssh_host. Never raises; returns (rc, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_TIMEOUT_S}",
            ssh_host,
            path_prefix + remote_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "ssh binary not found on gateway host"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SSH_TIMEOUT_S + 5)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "ssh timed out"
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def kill_session(ssh_host: str, session_name: str, path_prefix: str = "") -> tuple[bool, str]:
    """Kill one tmux session. Caller MUST have already checked that host's
    allow_control is true - this function trusts its caller completely and
    does no capability check itself, by design (single enforcement point
    in plugin_api.py, not duplicated/drifting logic)."""
    cmd = f"tmux kill-session -t {shlex.quote(session_name)}"
    rc, out, err = await run_ssh(ssh_host, cmd, path_prefix)
    if rc == 0:
        return True, f"killed session {session_name!r}"
    return False, (err.strip() or out.strip() or f"tmux kill-session exited with code {rc}")


async def host_status(
    host: dict[str, str], stall_state: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    ssh_host = host["ssh_host"]
    prefix = host["remote_path_prefix"]
    rc, out, err = await run_ssh(
        ssh_host, "tmux list-sessions -F '#{session_name}:#{session_attached}' 2>/dev/null", prefix
    )
    if rc != 0 or not out.strip():
        return {
            "label": host["label"],
            "ssh_host": ssh_host,
            "reachable": rc not in (124, 127, 255),
            "sessions": [],
            "note": err.strip() or "no tmux sessions running",
            "allow_control": bool(host.get("allow_control", False)),
        }

    now = time.time()
    sessions: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        name, _, attached = line.partition(":")
        name = name.strip()
        if not name:
            continue
        # -e includes color/style escape sequences in the captured text.
        pane_cmd = f"tmux capture-pane -p -e -S -{SCROLLBACK_LINES} -t {shlex.quote(name)}"
        _, pane_out, _ = await run_ssh(ssh_host, pane_cmd, prefix)
        raw_lines = pane_out.rstrip("\n").splitlines()
        colored_lines = [sanitize_ansi_sgr(l) for l in raw_lines]
        plain_lines = [strip_all_ansi(l) for l in raw_lines]
        last_nonblank_plain = next((l for l in reversed(plain_lines) if l.strip()), "")
        screen_signature = "\n".join(plain_lines)
        activity, idle_seconds = classify_activity(stall_state, (ssh_host, name), screen_signature, now)
        sessions.append(
            {
                "name": name,
                "attached": attached.strip() == "1",
                "tail": colored_lines[-SCROLLBACK_LINES:],
                "last_line": last_nonblank_plain,
                "activity": activity,  # 'active' | 'idle' | 'stalled'
                "idle_seconds": round(idle_seconds),
            }
        )

    return {"label": host["label"], "ssh_host": ssh_host, "reachable": True, "sessions": sessions, "note": "",
            "allow_control": bool(host.get("allow_control", False))}
