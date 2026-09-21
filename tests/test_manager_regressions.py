"""Regression coverage for manager state and account lifecycle bugs."""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from agy_cli_manager import manager as m


TOKEN_PATH = Path(".gemini/antigravity-cli/antigravity-oauth-token")


class ManagerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-manager-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.live_home = self.base / "live"
        self.paths = m.build_paths(self.base / "manager")
        live_patch = mock.patch.object(m, "default_live_dir", return_value=self.live_home / ".gemini")
        live_patch.start()
        self.addCleanup(live_patch.stop)
        m.ensure_layout(self.paths)

    @staticmethod
    def token(home: Path) -> Path:
        return home / TOKEN_PATH

    def add(self, name: str) -> None:
        source = self.base / f"source-{name}"
        token = self.token(source)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(f"token-{name}", encoding="utf-8")
        m.add_account(self.paths, name, source)

    def test_account_paths_cannot_escape_or_follow_symlink(self) -> None:
        self.add("a")
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        for name in ("../../outside", "../outside", ".", "..", "bad\\name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                m.save_account_profile(self.paths, name, self.base / "source-a", overwrite=True)
        (self.paths.accounts_dir / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            m.save_account_profile(self.paths, "linked", self.base / "source-a", overwrite=True)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_interrupted_state_write_keeps_previous_state(self) -> None:
        self.add("a")
        old_state = self.paths.state_file.read_text(encoding="utf-8")

        def interrupted_dump(data, handle, **kwargs):
            handle.write('{"active":')
            raise OSError("simulated interrupted write")

        with mock.patch.object(m.json, "dump", side_effect=interrupted_dump):
            with self.assertRaises(OSError):
                m.save_state(self.paths, m.load_state(self.paths))
        self.assertEqual(self.paths.state_file.read_text(encoding="utf-8"), old_state)

    def test_status_read_does_not_revert_concurrent_switch(self) -> None:
        self.add("a")
        self.add("b")
        original_sync = m.sync_state_from_disk
        switched = False

        def interleaved_switch(paths, state):
            nonlocal switched
            result = original_sync(paths, state)
            if not switched:
                switched = True
                m.switch_account(paths, "b")
            return result

        with mock.patch.object(m, "sync_state_from_disk", side_effect=interleaved_switch):
            m.format_status(self.paths)
        self.assertEqual(m.load_state(self.paths)["active"], "b")
