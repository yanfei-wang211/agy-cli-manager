"""Launch one saved Antigravity account without changing the shared manager session."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from agy_cli_manager.manager import account_dir, build_paths, load_state


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: agy-account <账号名> [agy 参数]", file=sys.stderr)
        return 2

    name, *agy_args = sys.argv[1:]
    root = Path(os.getenv("AGY_MANAGER_ROOT", "~/.agy-cli-manager")).expanduser()
    paths = build_paths(root)
    state = load_state(paths)
    account = state.get("accounts", {}).get(name)
    if not isinstance(account, dict):
        print(
            f"账号 {name} 尚未绑定。先运行 agy-cli-manager login {name}。",
            file=sys.stderr,
        )
        return 2
    if not account.get("enabled", True):
        print(f"账号 {name} 已禁用。", file=sys.stderr)
        return 2

    gemini_dir = account_dir(paths, name) / ".gemini"
    token = gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
    if not token.is_file():
        print(f"账号 {name} 缺少登录凭据，请重新登录。", file=sys.stderr)
        return 2

    agy = os.getenv("AGY_BINARY", "/opt/homebrew/bin/agy")
    env = os.environ.copy()
    env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 1"
    os.execvpe(
        agy,
        [
            agy,
            f"--gemini_dir={gemini_dir}",
            "--app_data_dir=antigravity-cli",
            *agy_args,
        ],
        env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
