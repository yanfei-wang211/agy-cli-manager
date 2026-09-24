"""End-to-end behavior for the managed Antigravity session supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from agy_cli_manager import supervisor
from agy_cli_manager import manager as account_manager


class SupervisorIntegrationTests(unittest.TestCase):
    def test_named_launchers_use_isolated_profiles_without_switching_shared_account(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-launchers-test-") as raw:
            base = Path(raw)
            paths = account_manager.build_paths(base / "manager")
            account_manager.ensure_layout(paths)
            account_manager.set_live_dir(paths, base / "live" / ".gemini")
            for name in ("primary", "backup"):
                source = base / name
                token = source / ".gemini/antigravity-cli/antigravity-oauth-token"
                token.parent.mkdir(parents=True)
                token.write_text(name, encoding="utf-8")
                account_manager.add_account(paths, name, source)
            account_manager.mark_bad(paths, "backup", "quota", 60)

            fake_agy = base / "fake-agy"
            fake_agy.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                "flags = {x.split('=', 1)[0]: x.split('=', 1)[1] for x in sys.argv[1:] if x.startswith('--') and '=' in x}\n"
                "p = pathlib.Path(flags['--gemini_dir']) / flags['--app_data_dir'] / 'antigravity-oauth-token'\n"
                "print(p.read_text(encoding='utf-8'))\n"
                "print('args=' + repr(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o700)
            env = os.environ.copy()
            env.update({
                "AGY_MANAGER_ROOT": str(paths.root),
                "AGY_BINARY": str(fake_agy),
            })
            for launcher, expected in (("agy1", "primary"), ("agy2", "backup")):
                result = subprocess.run(
                    [str(Path.home() / ".local/bin" / launcher), "--model", "fake"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout.splitlines())
                self.assertIn("'--model', 'fake'", result.stdout)
                self.assertEqual(account_manager.load_state(paths)["active"], "primary")
                self.assertEqual(
                    (base / "live/.gemini/antigravity-cli/antigravity-oauth-token").read_text(encoding="utf-8"),
                    "primary",
                )

            missing = subprocess.run(
                [str(Path.home() / ".local/bin/agy3")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("third", missing.stderr)

    def test_explicit_account_selection_clears_cooldown_and_switches_before_start(self) -> None:
        status = {
            "active": "primary",
            "accounts": {
                "primary": {"enabled": True, "cooldown_until": None},
                "backup": {"enabled": True, "cooldown_until": "2099-01-01T00:00:00+00:00"},
            },
        }
        commands = []

        def fake_manager(manager, root, *args, json_output=False):
            commands.append(args)
            if args == ("status",):
                return status
            if args == ("clear-bad", "backup"):
                status["accounts"]["backup"]["cooldown_until"] = None
            if args == ("switch", "backup"):
                status["active"] = "backup"
            return {}

        with mock.patch.object(supervisor, "_run_manager", side_effect=fake_manager):
            supervisor._select_initial_account("manager", Path("/tmp/agy-test"), "backup")
        self.assertEqual(
            commands,
            [("status",), ("clear-bad", "backup"), ("switch", "backup")],
        )
        self.assertEqual(status["active"], "backup")

    def test_named_launcher_keeps_agy_arguments(self) -> None:
        self.assertEqual(
            supervisor._parse_launcher_args(["--account", "primary", "--", "--model", "fake"]),
            ("primary", ["--model", "fake"]),
        )

    def test_manager_binary_falls_back_to_current_virtualenv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-supervisor-bin-") as raw:
            bin_dir = Path(raw) / "bin"
            bin_dir.mkdir()
            python = bin_dir / "python"
            manager = bin_dir / "agy-cli-manager"
            manager.touch(mode=0o700)
            with mock.patch.dict(os.environ, {"AGY_MANAGER_BINARY": ""}), \
                 mock.patch.object(supervisor.shutil, "which", return_value=None), \
                 mock.patch.object(supervisor.sys, "executable", str(python)):
                self.assertEqual(
                    supervisor._binary("AGY_MANAGER_BINARY", "agy-cli-manager"),
                    str(manager),
                )

    def test_quota_rotation_restarts_and_continues_latest_conversation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-supervisor-test-") as raw:
            base = Path(raw)
            manager = base / "fake-manager"
            agy = base / "fake-agy"
            calls = base / "agy-calls.jsonl"
            state = base / "state.json"
            profile_root = base / "manager-root" / "accounts"
            for name in ("primary", "backup"):
                (profile_root / name / ".gemini" / "antigravity-cli").mkdir(parents=True)

            manager.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, pathlib, subprocess, sys, time
                    args = sys.argv[1:]
                    command = args[2] if args[:1] == ['--root'] else args[0]
                    state_path = pathlib.Path(os.environ['FAKE_MANAGER_STATE'])
                    data = json.loads(state_path.read_text()) if state_path.exists() else {'rotated': False, 'restart': False}
                    if command == 'status':
                        print(json.dumps({'active': 'backup' if data['rotated'] else 'primary', 'accounts': {'primary': {}, 'backup': {}}, 'log_watch': {'restart_required': data['restart']}}))
                    elif command == 'ensure-active':
                        print(json.dumps({'active': 'primary'}))
                    elif command == 'watch':
                        if '--once' in args:
                            print(json.dumps({'events': [], 'restart_required': data['restart']}))
                        else:
                            if not data['rotated']:
                                time.sleep(0.8)
                                data = {'rotated': True, 'restart': True}
                                state_path.write_text(json.dumps(data))
                                hook = args[args.index('--on-rotate') + 1]
                                subprocess.run(hook, shell=True, check=False)
                            time.sleep(30)
                    elif command == 'ack-restart':
                        data['restart'] = False
                        state_path.write_text(json.dumps(data))
                        print(json.dumps({'restart_required': False}))
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            agy.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, pathlib, sys, time
                    path = pathlib.Path(os.environ['FAKE_AGY_CALLS'])
                    with path.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(sys.argv[1:]) + '\\n')
                    if '--continue' not in sys.argv:
                        time.sleep(30)
                    """
                ),
                encoding="utf-8",
            )
            manager.chmod(0o700)
            agy.chmod(0o700)

            env = os.environ.copy()
            env.update(
                {
                    "AGY_MANAGER_BINARY": str(manager),
                    "AGY_BINARY": str(agy),
                    "AGY_MANAGER_ROOT": str(base / "manager-root"),
                    "FAKE_MANAGER_STATE": str(state),
                    "FAKE_AGY_CALLS": str(calls),
                }
            )
            result = subprocess.run(
                [str(Path.home() / ".local/bin/agy"), "--model", "fake"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                invocations[0],
                [
                    f"--gemini_dir={profile_root / 'primary' / '.gemini'}",
                    "--app_data_dir=antigravity-cli",
                    "--model",
                    "fake",
                ],
            )
            self.assertIn("--continue", invocations[1])
            self.assertIn("--prompt-interactive", invocations[1])
            self.assertEqual(
                invocations[1][:4],
                [
                    f"--gemini_dir={profile_root / 'backup' / '.gemini'}",
                    "--app_data_dir=antigravity-cli",
                    "--model",
                    "fake",
                ],
            )


if __name__ == "__main__":
    unittest.main()
