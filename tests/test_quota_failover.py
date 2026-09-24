"""Tests verifying quota failover, 429 exhausted marking, candidate exclusion, and all-exhausted structured reporting."""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agy_cli_manager import manager
from agy_cli_manager import supervisor
from agy_cli_manager import watch


def _create_test_profile(source_dir: Path, name: str) -> Path:
    """Helper to create a valid profile source directory with JSON token."""
    account_source = source_dir / name
    token_file = account_source / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_payload = {
        "token": {
            "access_token": f"ya29.test-access-token-{name}",
            "refresh_token": f"1//test-refresh-token-{name}",
            "expiry": "2099-01-01T00:00:00Z",
        }
    }
    token_file.write_text(json.dumps(token_payload), encoding="utf-8")
    return account_source


class QuotaFailoverTests(unittest.TestCase):
    def test_parse_reset_hint_minutes(self) -> None:
        self.assertEqual(watch.parse_reset_hint_minutes("~2h", 60), 120)
        self.assertEqual(watch.parse_reset_hint_minutes("2h", 60), 120)
        self.assertEqual(watch.parse_reset_hint_minutes("1h45m7s", 60), 106)
        self.assertEqual(watch.parse_reset_hint_minutes("45m", 60), 45)
        self.assertEqual(watch.parse_reset_hint_minutes("30s", 60), 1)  # minimum 1 minute
        self.assertEqual(watch.parse_reset_hint_minutes("", 60), 60)
        self.assertEqual(watch.parse_reset_hint_minutes(None, 60), 60)
        self.assertEqual(watch.parse_reset_hint_minutes("invalid", 45), 45)

    def test_rotate_after_quota_marks_short_window_exhausted_without_percentage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-test-") as tmp:
            base = Path(tmp)
            paths = manager.build_paths(base / "manager")
            manager.ensure_layout(paths)

            sources_dir = base / "sources"
            for name in ("primary", "backup"):
                src = _create_test_profile(sources_dir, name)
                manager.add_account(paths, name, src)

            manager.switch_account(paths, "primary")
            state = manager.load_state(paths)
            self.assertEqual(state["active"], "primary")

            # 429 quota failure occurs on primary with no percentage in text
            with manager.manager_lock(paths):
                result = manager.rotate_after_failure_locked(
                    paths,
                    reason="quota",
                    cooldown_minutes=60,
                    force_switch=True,
                    dedupe_seconds=0,
                    trigger="test",
                )

            self.assertEqual(result.switched_to, "backup")
            self.assertEqual(result.outcome, "switched")
            updated_state = manager.load_state(paths)
            self.assertEqual(updated_state["active"], "backup")

            # Verify primary is explicitly marked exhausted with 0.0% remaining
            primary_meta = updated_state["accounts"]["primary"]
            short_window = primary_meta["usage_windows"]["short"]
            self.assertEqual(short_window["status"], "exhausted")
            self.assertEqual(short_window["value"], 0.0)
            self.assertIsNotNone(primary_meta["cooldown_until"])
            self.assertEqual(short_window["reset_at"], primary_meta["cooldown_until"])

            # Verify _is_short_window_exhausted returns True
            self.assertTrue(manager._is_short_window_exhausted(primary_meta))

    def test_candidate_selection_skips_exhausted_and_cooldown_accounts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-test-") as tmp:
            base = Path(tmp)
            paths = manager.build_paths(base / "manager")
            manager.ensure_layout(paths)

            sources_dir = base / "sources"
            for name in ("primary", "backup"):
                src = _create_test_profile(sources_dir, name)
                manager.add_account(paths, name, src)

            manager.switch_account(paths, "primary")

            # Put primary in cooldown and mark exhausted via quota rotation
            with manager.manager_lock(paths):
                result = manager.rotate_after_failure_locked(
                    paths,
                    reason="quota",
                    cooldown_minutes=60,
                    force_switch=True,
                    dedupe_seconds=0,
                )
            self.assertEqual(result.switched_to, "backup")

            state = manager.load_state(paths)
            # Candidate excluding backup must be None, because primary is in cooldown
            best = manager._best_switch_candidate(paths, state, exclude="backup")
            self.assertIsNone(best)

            # Even if cooldown_until is manually cleared, primary has status="exhausted"
            with manager.manager_lock(paths):
                state = manager.load_state(paths)
                state["accounts"]["primary"]["cooldown_until"] = None
                manager.save_state(paths, state)

            state = manager.load_state(paths)
            primary_meta = state["accounts"]["primary"]
            self.assertTrue(manager._is_short_window_exhausted(primary_meta))

            # When ranking all accounts, backup (healthy) should win over primary (exhausted)
            best_all = manager._best_switch_candidate(paths, state, exclude=None)
            self.assertEqual(best_all, "backup")

            # Restore primary cooldown to simulate both accounts in cooldown when backup fails
            with manager.manager_lock(paths):
                state = manager.load_state(paths)
                state["accounts"]["primary"]["cooldown_until"] = (manager.utc_now() + timedelta(minutes=60)).isoformat()
                manager.save_state(paths, state)

            # Now fail backup as well with quota (dedupe_seconds=0 simulates distinct 429 event)
            with manager.manager_lock(paths):
                result2 = manager.rotate_after_failure_locked(
                    paths,
                    reason="quota",
                    cooldown_minutes=60,
                    force_switch=True,
                    dedupe_seconds=0,
                )
            self.assertEqual(result2.outcome, "no_candidate")
            self.assertIsNone(result2.switched_to)
            final_state = manager.load_state(paths)
            self.assertIsNone(final_state.get("active"))

    def test_watch_poll_no_candidate_arms_restart_and_triggers_hook(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-test-") as tmp:
            base = Path(tmp)
            paths = manager.build_paths(base / "manager")
            manager.ensure_layout(paths)

            # Setup live dir with session log containing 429
            live_dir = base / "live" / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "cli-test.log"
            log_file.write_text(
                "ERROR: run.go:371] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): "
                "Individual quota reached. Resets in ~2h.)\n",
                encoding="utf-8",
            )
            manager.set_live_dir(paths, live_dir)

            sources_dir = base / "sources"
            # Add only one account so failover immediately has no candidate
            src = _create_test_profile(sources_dir, "primary")
            manager.add_account(paths, "primary", src)
            manager.switch_account(paths, "primary")

            hook_called = []
            hook_file = base / "hook_ran.txt"
            hook_cmd = f"touch '{hook_file}'"

            res = watch.poll_quota_logs(
                paths,
                from_start=True,
                rotate=True,
                force_switch=True,
                on_rotate=hook_cmd,
            )

            self.assertFalse(res.rotated)
            self.assertEqual(res.rotation.outcome, "no_candidate")
            self.assertTrue(res.restart_required)
            watch_state = watch.load_log_watch_state(paths.root)
            self.assertTrue(watch_state.get("restart_required"))
            self.assertIsNone(watch_state.get("restart_armed_account"))
            self.assertTrue(hook_file.exists())

    def test_supervisor_all_exhausted_outputs_structured_report_and_exits_code_2(self) -> None:
        status_exhausted = {
            "active": None,
            "accounts": {
                "primary": {
                    "enabled": True,
                    "status": "cooldown",
                    "cooldown_until": "2026-09-23T22:00:00+00:00",
                    "identity": {"email": "primary@example.com"},
                    "usage_windows": {"short": {"status": "exhausted", "value": 0.0, "reset_at": "2026-09-23T22:00:00+00:00"}},
                },
                "backup": {
                    "enabled": True,
                    "status": "cooldown",
                    "cooldown_until": "2026-09-23T23:00:00+00:00",
                    "identity": {"email": "backup@example.com"},
                    "usage_windows": {"short": {"status": "exhausted", "value": 0.0, "reset_at": "2026-09-23T23:00:00+00:00"}},
                },
            },
            "log_watch": {"restart_required": False},
        }

        with mock.patch.object(supervisor, "_run_manager", return_value=status_exhausted):
            stderr_buf = StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                exit_code = supervisor.supervise("fake-manager", "fake-agy", Path("/tmp"), [])
                self.assertEqual(exit_code, 2)
                err_output = stderr_buf.getvalue()
                self.assertIn("所有账号额度均已耗尽", err_output)
                self.assertIn("primary@example.com", err_output)
                self.assertIn("backup@example.com", err_output)

    def test_format_exhausted_report_outputs_structured_data(self) -> None:
        status = {
            "active": None,
            "accounts": {
                "primary": {
                    "enabled": True,
                    "status": "cooldown",
                    "health_status": "cooldown",
                    "last_error": "quota",
                    "cooldown_until": "2026-09-23T19:00:00+00:00",
                    "identity": {"email": "primary@example.com"},
                    "usage_windows": {
                        "short": {"status": "exhausted", "value": 0.0, "reset_at": "2026-09-23T19:00:00+00:00"},
                        "weekly": {"status": "known", "value": 85.0, "reset_at": None},
                    },
                },
                "backup": {
                    "enabled": True,
                    "status": "cooldown",
                    "health_status": "cooldown",
                    "last_error": "quota",
                    "cooldown_until": "2026-09-23T20:00:00+00:00",
                    "identity": {"email": "backup@example.com"},
                    "usage_windows": {
                        "short": {"status": "exhausted", "value": 0.0, "reset_at": "2026-09-23T20:00:00+00:00"},
                        "weekly": {"status": "known", "value": 90.0, "reset_at": None},
                    },
                },
            },
        }

        # Test text report contains structured details
        text_report = supervisor.format_exhausted_report(status, as_json=False)
        self.assertIn("[!] 所有账号额度均已耗尽", text_report)
        self.assertIn("primary@example.com", text_report)
        self.assertIn("backup@example.com", text_report)
        self.assertIn("0.0%", text_report)

        # Test JSON report parseable
        json_report_str = supervisor.format_exhausted_report(status, as_json=True)
        data = json.loads(json_report_str)
        self.assertEqual(data["status"], "all_exhausted")
        self.assertIn("primary", data["accounts"])
        self.assertEqual(data["accounts"]["primary"]["short_quota"]["remaining_percent"], 0.0)
        self.assertEqual(data["accounts"]["primary"]["short_quota"]["status"], "exhausted")

    def test_antigravity_oauth_token_and_ssh_connection_environment(self) -> None:
        # Verify SSH_CONNECTION environment bypass for macOS Keychain
        env = supervisor._agy_environment()
        self.assertEqual(env.get("SSH_CONNECTION"), "127.0.0.1 1 127.0.0.1 1")

        # Verify LOGIN_ARTIFACT_SETS contains antigravity-oauth-token
        self.assertTrue(
            any("antigravity-cli/antigravity-oauth-token" in s for s in manager.LOGIN_ARTIFACT_SETS)
        )


if __name__ == "__main__":
    unittest.main()
