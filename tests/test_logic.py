"""Unit tests for dashboard/logic.py - stdlib unittest only.

Run with plain python3, no venv, no fastapi, no network, no SSH:

    python3 tests/test_logic.py

This deliberately never touches a live Hermes gateway: that's the whole
point of keeping logic.py free of FastAPI imports.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import logic  # noqa: E402


class SanitizeAnsiSgrTests(unittest.TestCase):
    def test_plain_text_passes_through(self):
        self.assertEqual(logic.sanitize_ansi_sgr("hello world"), "hello world")

    def test_sgr_color_code_is_kept(self):
        text = "\x1b[32mPASS\x1b[0m"
        self.assertEqual(logic.sanitize_ansi_sgr(text), text)

    def test_cursor_move_is_stripped(self):
        # ESC[2A moves the cursor up 2 lines - not a color code, must go.
        text = "before\x1b[2Aafter"
        self.assertEqual(logic.sanitize_ansi_sgr(text), "beforeafter")

    def test_osc_window_title_is_stripped(self):
        text = "\x1b]0;evil title\x07visible text"
        self.assertEqual(logic.sanitize_ansi_sgr(text), "visible text")

    def test_clear_screen_is_stripped_but_color_kept(self):
        text = "\x1b[2J\x1b[31mred\x1b[0m"
        self.assertEqual(logic.sanitize_ansi_sgr(text), "\x1b[31mred\x1b[0m")

    def test_lone_truncated_escape_is_dropped(self):
        text = "truncated\x1b"
        self.assertEqual(logic.sanitize_ansi_sgr(text), "truncated")


class StripAllAnsiTests(unittest.TestCase):
    def test_strips_color_too(self):
        text = "\x1b[32mPASS\x1b[0m"
        self.assertEqual(logic.strip_all_ansi(text), "PASS")

    def test_prompt_detection_not_confused_by_trailing_color_reset(self):
        # A colored shell prompt: the '>' is followed by a reset code before
        # the real end of line - plain-text heuristics must see past it.
        text = "user@host \x1b[32m>\x1b[0m"
        plain = logic.strip_all_ansi(text)
        self.assertTrue(plain.strip().endswith(">"))


class ActivityClassificationTests(unittest.TestCase):
    def test_first_sighting_is_active(self):
        state = {}
        status, idle_seconds = logic.classify_activity(state, ("host", "sess"), "screen1", now=1000.0)
        self.assertEqual(status, "active")
        self.assertEqual(idle_seconds, 0.0)

    def test_unchanged_briefly_is_still_active(self):
        state = {}
        logic.classify_activity(state, ("host", "sess"), "screen1", now=1000.0)
        status, _ = logic.classify_activity(
            state, ("host", "sess"), "screen1", now=1000.0 + 5, active_threshold_s=10
        )
        self.assertEqual(status, "active")

    def test_unchanged_past_active_threshold_is_idle(self):
        state = {}
        logic.classify_activity(state, ("host", "sess"), "screen1", now=1000.0)
        status, idle_seconds = logic.classify_activity(
            state, ("host", "sess"), "screen1", now=1000.0 + 30, active_threshold_s=10, stall_threshold_s=300
        )
        self.assertEqual(status, "idle")
        self.assertEqual(idle_seconds, 30.0)

    def test_unchanged_past_stall_threshold_is_stalled(self):
        state = {}
        logic.classify_activity(state, ("host", "sess"), "screen1", now=1000.0)
        status, _ = logic.classify_activity(
            state, ("host", "sess"), "screen1", now=1000.0 + 301, stall_threshold_s=300
        )
        self.assertEqual(status, "stalled")

    def test_any_change_anywhere_in_the_screen_resets_to_active(self):
        # Not just the last line - the whole screen signature changing
        # (e.g. a progress bar mid-screen) must count as activity.
        state = {}
        logic.classify_activity(state, ("host", "sess"), "line1\nline2\nline3", now=1000.0)
        logic.classify_activity(
            state, ("host", "sess"), "line1\nline2\nline3", now=1000.0 + 299, active_threshold_s=10, stall_threshold_s=300
        )
        status, idle_seconds = logic.classify_activity(
            state, ("host", "sess"), "line1\nCHANGED\nline3", now=1000.0 + 300, active_threshold_s=10, stall_threshold_s=300
        )
        self.assertEqual(status, "active")
        self.assertEqual(idle_seconds, 0.0)

    def test_two_sessions_tracked_independently(self):
        state = {}
        logic.classify_activity(state, ("host", "a"), "x", now=0.0)
        logic.classify_activity(state, ("host", "b"), "y", now=0.0)
        status_a, _ = logic.classify_activity(state, ("host", "a"), "x", now=301.0, stall_threshold_s=300)
        status_b, _ = logic.classify_activity(state, ("host", "b"), "y-changed", now=301.0, stall_threshold_s=300)
        self.assertEqual(status_a, "stalled")
        self.assertEqual(status_b, "active")


class ConfigTests(unittest.TestCase):
    def test_load_config_drops_hosts_without_ssh_host(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"hosts": [{"label": "no host here"}, {"ssh_host": "ok-host"}]}))
            cfg = logic.load_config(path)
            self.assertEqual(len(cfg["hosts"]), 1)
            self.assertEqual(cfg["hosts"][0]["ssh_host"], "ok-host")

    def test_load_config_missing_file_returns_empty_and_writes_default(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "config.json"
            cfg = logic.load_config(path)
            self.assertEqual(cfg["hosts"], [])
            self.assertTrue(path.exists())  # default example written for next time

    def test_allow_control_defaults_false(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"hosts": [{"ssh_host": "h1"}]}))
            cfg = logic.load_config(path)
            self.assertFalse(cfg["hosts"][0]["allow_control"])

    def test_allow_control_true_is_preserved(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"hosts": [{"ssh_host": "h1", "allow_control": True}]}))
            cfg = logic.load_config(path)
            self.assertTrue(cfg["hosts"][0]["allow_control"])


class KillSessionTests(unittest.TestCase):
    """kill_session() does no capability check itself (see its docstring) -
    these tests only cover its SSH-result-to-(ok, message) mapping, with
    run_ssh mocked out. No network, no real SSH, no gateway."""

    def test_success_message_names_the_session(self):
        import asyncio
        import unittest.mock as mock

        async def fake_run_ssh(ssh_host, cmd, prefix=""):
            return 0, "", ""

        with mock.patch.object(logic, "run_ssh", fake_run_ssh):
            ok, message = asyncio.run(logic.kill_session("host", "my-session"))
        self.assertTrue(ok)
        self.assertIn("my-session", message)

    def test_failure_surfaces_stderr(self):
        import asyncio
        import unittest.mock as mock

        async def fake_run_ssh(ssh_host, cmd, prefix=""):
            return 1, "", "session not found: my-session"

        with mock.patch.object(logic, "run_ssh", fake_run_ssh):
            ok, message = asyncio.run(logic.kill_session("host", "my-session"))
        self.assertFalse(ok)
        self.assertIn("not found", message)

    def test_session_name_is_shell_quoted(self):
        import asyncio
        import unittest.mock as mock

        seen_cmd = {}

        async def fake_run_ssh(ssh_host, cmd, prefix=""):
            seen_cmd["cmd"] = cmd
            return 0, "", ""

        with mock.patch.object(logic, "run_ssh", fake_run_ssh):
            asyncio.run(logic.kill_session("host", "evil; rm -rf /"))
        # shlex.quote must have wrapped the malicious-looking name so it is
        # one argument to tmux, not a shell metacharacter injection.
        self.assertIn("'evil; rm -rf /'", seen_cmd["cmd"])


if __name__ == "__main__":
    unittest.main()
