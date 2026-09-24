"""Run Antigravity CLI with quota-triggered account failover and resume."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .manager import account_dir, build_paths


RESUME_PROMPT = (
    "Continue the task interrupted by the quota limit. "
    "First verify the current workspace state, then resume from the last incomplete step."
)


class SupervisorError(RuntimeError):
    pass


def _binary(env_name: str, fallback: str) -> str:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured
    resolved = shutil.which(fallback)
    if resolved:
        return resolved
    sibling = Path(sys.executable).with_name(fallback)
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    raise SupervisorError(f"找不到 {fallback}，请检查安装或设置 {env_name}。")


def _manager_command(manager: str, root: Path, *args: str) -> list[str]:
    return [manager, "--root", str(root), *args]


def _run_manager(
    manager: str,
    root: Path,
    *args: str,
    json_output: bool = False,
) -> dict:
    command = _manager_command(manager, root, *args)
    if json_output:
        command.append("--json")
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise SupervisorError(f"账号管理器执行失败：{detail}")
    if not json_output:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SupervisorError("账号管理器返回了无效状态。") from exc
    if not isinstance(data, dict):
        raise SupervisorError("账号管理器返回了无效状态。")
    return data


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _agy_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 1"
    return env


def _resume_arguments(original: list[str]) -> list[str]:
    return [*original, "--continue", "--prompt-interactive", RESUME_PROMPT]


def _account_count(status: dict) -> int:
    accounts = status.get("accounts")
    if isinstance(accounts, dict):
        return len(accounts)
    if isinstance(accounts, list):
        return len(accounts)
    return 0


def _parse_launcher_args(args: list[str]) -> tuple[str | None, list[str]]:
    if not args or args[0] != "--account":
        return None, args
    if len(args) < 2 or args[1] not in {"primary", "backup"}:
        raise SupervisorError("--account 只能指定 primary 或 backup。")
    remaining = args[2:]
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    return args[1], remaining


def _select_initial_account(manager: str, root: Path, name: str) -> None:
    status = _run_manager(manager, root, "status", json_output=True)
    account = (status.get("accounts") or {}).get(name)
    if not isinstance(account, dict):
        raise SupervisorError(f"管理器中没有账号 {name}。")
    if not account.get("enabled", True):
        raise SupervisorError(f"账号 {name} 已禁用。")
    if account.get("cooldown_until"):
        _run_manager(manager, root, "clear-bad", name)
    _run_manager(manager, root, "switch", name)


def format_exhausted_report(status: dict, as_json: bool = False) -> str:
    accounts_info = {}
    accounts = status.get("accounts") or {}
    for name, meta in sorted(accounts.items()):
        identity = meta.get("identity") if isinstance(meta.get("identity"), dict) else {}
        email = identity.get("email") or identity.get("account_name") or "unknown"
        windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
        short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
        weekly = windows.get("weekly") if isinstance(windows.get("weekly"), dict) else {}
        accounts_info[name] = {
            "account": name,
            "email": email,
            "status": meta.get("status", "unknown"),
            "health_status": meta.get("health_status", "unknown"),
            "last_error": meta.get("last_error"),
            "cooldown_until": meta.get("cooldown_until"),
            "short_quota": {
                "status": short.get("status", "unknown"),
                "remaining_percent": short.get("value"),
                "reset_at": short.get("reset_at"),
            },
            "weekly_quota": {
                "status": weekly.get("status", "unknown"),
                "remaining_percent": weekly.get("value"),
                "reset_at": weekly.get("reset_at"),
            },
        }

    report = {
        "status": "all_exhausted",
        "message": "所有配置的账号额度均已耗尽或处于冷却中，无法继续执行任务。",
        "accounts": accounts_info,
    }
    if as_json:
        return json.dumps(report, indent=2, ensure_ascii=False)

    lines = [
        "[!] 所有账号额度均已耗尽或处于冷却中，任务已终止。",
        "各账号额度状态如下：",
    ]
    for name, info in accounts_info.items():
        short_val = info["short_quota"]["remaining_percent"]
        short_rem = f"{short_val}%" if short_val is not None else "0%"
        reset_time = info["short_quota"]["reset_at"] or info["cooldown_until"] or "待刷新"
        lines.append(
            f"  - [{name}] ({info['email']}): 状态={info['status']}, "
            f"剩余额度={short_rem}, 重置时间={reset_time}, 上次原因={info['last_error'] or '无'}"
        )
    lines.append("\n结构化 JSON 数据：")
    lines.append(json.dumps(report, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def supervise(
    manager: str,
    agy: str,
    root: Path,
    original_args: list[str],
    initial_account: str | None = None,
) -> int:
    _run_manager(manager, root, "init")
    if initial_account:
        _select_initial_account(manager, root, initial_account)
    else:
        _run_manager(manager, root, "ensure-active", json_output=True)
    status = _run_manager(manager, root, "status", json_output=True)
    if _account_count(status) < 2:
        raise SupervisorError(
            "还需要先绑定两个账号：运行 `agy-cli-manager login primary`，退出后再运行 "
            "`agy-cli-manager login backup`。"
        )
    if not status.get("active"):
        report = format_exhausted_report(status, as_json="--json" in original_args)
        print(report, file=sys.stderr)
        return 2

    _run_manager(manager, root, "apply-active")
    _run_manager(manager, root, "watch", "--once", "--account", status["active"], json_output=True)

    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        runtime_dir.chmod(0o700)
    watch_log_path = runtime_dir / "supervisor-watch.log"
    watch_log_path.touch(exist_ok=True)
    if os.name != "nt":
        watch_log_path.chmod(0o600)

    resumed = False
    child: subprocess.Popen | None = None
    watcher: subprocess.Popen | None = None
    try:
        while True:
            current_account = status["active"]
            profile_dir = account_dir(build_paths(root), current_account) / ".gemini"
            args = _resume_arguments(original_args) if resumed else list(original_args)
            child = subprocess.Popen(
                [agy, f"--gemini_dir={profile_dir}", "--app_data_dir=antigravity-cli", *args],
                env=_agy_environment(),
            )
            hook = f"/bin/kill -TERM {child.pid}"
            with watch_log_path.open("ab") as watch_log:
                watcher = subprocess.Popen(
                    _manager_command(
                        manager,
                        root,
                        "watch",
                        "--account",
                        current_account,
                        "--poll-seconds",
                        "1",
                        "--force-switch",
                        "--on-rotate",
                        hook,
                    ),
                    stdout=watch_log,
                    stderr=subprocess.STDOUT,
                )
                while child.poll() is None:
                    if watcher.poll() is not None:
                        _terminate(child)
                        raise SupervisorError(
                            f"额度监控意外退出；详情见 {watch_log_path}。"
                        )
                    time.sleep(0.1)
                child_code = child.returncode or 0
                _terminate(watcher)

            _run_manager(manager, root, "watch", "--once", "--account", current_account, json_output=True)
            status = _run_manager(manager, root, "status", json_output=True)
            watch_state = status.get("log_watch") or {}
            if not watch_state.get("restart_required"):
                if not status.get("active"):
                    report = format_exhausted_report(status, as_json="--json" in original_args)
                    print("\n[!] 额度已用完，且所有备用账号均已耗尽或在冷却中。", file=sys.stderr)
                    print(report, file=sys.stderr)
                    return 2
                return child_code

            _run_manager(manager, root, "ack-restart", json_output=True)
            new_active = status.get("active")
            if not new_active:
                report = format_exhausted_report(status, as_json="--json" in original_args)
                print("\n[!] 额度已用完，且所有备用账号均已耗尽或在冷却中。", file=sys.stderr)
                print(report, file=sys.stderr)
                return 2

            print(
                f"\n[!] 额度已用完，已自动切换到账号 [{new_active}]。\n"
                f"[*] 正在以断点引导模式（--continue --prompt-interactive）重新启动任务...\n",
                flush=True,
            )
            status = _run_manager(manager, root, "status", json_output=True)
            resumed = True
    except KeyboardInterrupt:
        _terminate(child)
        _terminate(watcher)
        return 130
    finally:
        _terminate(child)
        _terminate(watcher)


def main() -> int:
    try:
        initial_account, agy_args = _parse_launcher_args(sys.argv[1:])
        manager = _binary("AGY_MANAGER_BINARY", "agy-cli-manager")
        agy = _binary("AGY_BINARY", "agy")
        root = Path(os.getenv("AGY_MANAGER_ROOT", "~/.agy-cli-manager")).expanduser()
        return supervise(manager, agy, root, agy_args, initial_account)
    except SupervisorError as exc:
        print(f"agy-managed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
