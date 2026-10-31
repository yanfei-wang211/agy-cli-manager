# 运行问题排查与解决方案总结 (Troubleshooting & Solutions)

本文档总结了在部署与运行 `agy-cli-manager` 及 `agy-managed` 守护进程过程中遇到的核心问题、根本原因分析以及对应的完整解决方案。

---

## 1. 架构与命令职责分工

在实际多账号协作环境中，各命令的定位与分工如下：

| 命令 | 底层对应程序 | 职责说明 |
| :--- | :--- | :--- |
| **`agym`** | `agy-cli-manager` | **管理后台**：直接运行进入全屏 TUI 仪表盘，查看各账号健康状态、额度、配额重置倒计时；亦可运行子命令（如 `agym status`、`agym list`）。 |
| **`agy`** | `agy-managed` | **智能启动与守护**：哪个账号有额度就用哪个。在会话过程中实时监听日志，额度耗尽时自动切号并以断点引导模式继续任务。 |
| **`agy1`** | 主账号独立入口 | **单独运行主账号**（`primary`），在隔离目录中独立会话，不触发自动故障转移。 |
| **`agy2`** | 备用账号1独立入口 | **单独运行备用账号1**（`backup`），支持未配置时引导独立登录，配置后独立进入该账号。 |
| **`agy3`** | 备用账号2独立入口 | **单独运行备用账号2**（`backup2`），支持未配置时引导独立登录，配置后独立进入该账号。支持继续扩展至更多账号。 |

---

## 2. 核心问题与解决方案

### 问题一：macOS Keychain 钥匙串穿透导致双账号“串号”
- **现象**：
  配置好两个账号后，分别执行 `agy1` 和 `agy2`，发现两个命令最终进入的都是主账号（`yanfeiwang252`），备用账号无法生效。
- **根本原因**：
  macOS 下 Antigravity CLI 默认会读取系统的钥匙串（Keyring/Keychain）。系统钥匙串在全局只维护一份默认授权凭据。即使通过命令行传入 `--gemini_dir` 隔离目录，CLI 的内部凭据选择器优先级仍是：`Keyring > 本地文件`。因此，普通桌面终端启动时会无视独立目录，直接穿透回系统钥匙串中记录的主账号。
- **解决方案**：
  在 `agy1`、`agy2` 以及 `agy-managed` 启动环境中，注入 `SSH_CONNECTION="127.0.0.1 1 127.0.0.1 1"` 环境变量。
  Antigravity CLI 内部检测到该环境变量后，会自动进入远程/无头模式，**彻底禁用 macOS Keychain，强制使用基于文件的凭据存储（file-based token storage）**，从而确保各账号严格读取各自 `--gemini_dir` 下的 token 文件，实现真正的物理隔离。

---

### 问题二：配置文件复制不全导致认证方式识别失败（Unknown Auth Method）
- **现象**：
  首次导入账号或启动时，CLI 报 `Unknown auth method: ""` 及 `keyringAuth: failed to get oauth params: unknown auth method:`，并退回到欢迎/登录选择界面。
- **根本原因**：
  Antigravity CLI (1.3.1+) 在独立目录下运行时，不仅需要 `antigravity-cli/antigravity-oauth-token`，还必须读取同级目录下的 `settings.json`（其中声明了 `"security": {"auth": {"selectedType": "oauth-personal"}}`）、`oauth_creds.json`、`google_accounts.json` 以及 `antigravity-cli/settings.json`。
  原代码中的 `MANAGED_PROFILE_FILES` 只声明了单个 token 文件，导致复制出的隔离 profile 缺失这些关键文件，CLI 无法解析认证类型。
- **解决方案**：
  修改 `src/agy_cli_manager/manager.py`，将全套 OAuth 和设置文件纳入管理与复制清单：
  ```python
  MANAGED_PROFILE_FILES = (
      "antigravity-cli/antigravity-oauth-token",
      "settings.json",
      "oauth_creds.json",
      "google_accounts.json",
      "antigravity-cli/settings.json",
  )
  LOGIN_ARTIFACT_SETS = (
      ("antigravity-cli/antigravity-oauth-token",),
      ("oauth_creds.json",),
  )
  ```
  补齐后，CLI 即可从各自目录中读取正确的 `authMethod=consumer` 并完成静默认证。

---

### 问题三：终端 PATH 顺序与命令别名未对齐
- **现象**：
  直接输入 `agy` 调用的是 Homebrew 原生程序 `/opt/homebrew/bin/agy`，未走守护进程；输入 `agym` 提示 `command not found`。
- **根本原因**：
  1. 系统 PATH 中 `/opt/homebrew/bin` 排在 `~/.local/bin` 前面；
  2. `~/.zshrc` 中的 PATH 导出语句存在连接符缺失（两行命令拼合在了一起）；
  3. 未在 shell 配置中建立全局快捷别名。
- **解决方案**：
  1. 修复 `~/.zshrc` 中的 PATH 设置，确保 `~/.local/bin` 和 `~/bin` 优先；
  2. 在 `~/.zshrc` 中建立标准快捷别名：
     ```bash
     alias agym='agy-cli-manager'
     alias agy='agy-managed'
     ```
  3. 在 `~/.local/bin` 和 `~/bin` 同时部署独立脚本 `agy`、`agym`、`agy1`、`agy2`，确保在交互式终端、非交互式子 shell 或脚本调用时均可正常生效。

---

### 问题四：主账号从外部导入时缺乏真实文件凭据
- **现象**：
  由于主账号最早是在 macOS 原生环境下通过 Keychain 登录的，其 `~/.gemini` 下的文件 token 中存在 dummy expiry（如 2099 年占位符）或缺失 `auth_method` 字段。当切换至 `SSH_CONNECTION` 文件模式时，由于无法向 Google 刷新 access_token，可能返回 401 UNAUTHENTICATED。
- **解决方案**：
  1. 规范化 token 结构，补齐 `auth_method: consumer`；
  2. 提供 `agy1 login` / `agym login primary` 交互入口，当需要重置或刷新主账号真实文件凭据时，可直接进行原生独立登录。

---

### 问题五：多账号登录时误调守护脚本造成递归调用 (Supervisor Recursion during Login)
- **现象**：
  在运行 `agy3`（或 `agym login <name>`）为新账号登录时，终端报错：
  `agy-managed: 还需要先绑定两个账号：运行 agy-cli-manager login primary，退出后再运行 agy-cli-manager login backup。`
  随后抛出 `error: agy login did not produce a usable auth profile.`。
- **根本原因**：
  `resolve_agy_binary` 默认通过 `shutil.which("agy")` 查找二进制。由于用户的 PATH 中 `~/bin` 优先，它命中了 `~/bin/agy`（即 `agy-managed` 守护进程包装脚本）。在临时隔离目录登录时，由于隔离目录尚未生成完整账号记录，守护进程直接阻断并提示错误，导致未拉起底层的真实 Google Antigravity 二进制程序。
- **解决方案**：
  1. 在 `resolve_agy_binary` 和 `supervisor._binary` 中优先探测原生二进制路径（如 `/opt/homebrew/bin/agy`、`/usr/local/bin/agy`），若从 PATH 中查找则主动过滤包含 `agy-managed` 的包装脚本。
  2. 在启动脚本 `agy1`、`agy2`、`agy3` 的登录调用中，显式传入已解析的原生二进制参数 `--agy-binary "$AGY_BIN"`。
  3. 解除 `supervisor.py` 中 `--account` 只能接收 `primary` 或 `backup` 的硬编码限制，使其支持任意名称的账号无缝启动。

---

## 3. 额度监听与自动故障转移机制

当通过 `agy`（`agy-managed`）启动任务时，其自动轮换与断点续接流程如下：

```
[用户启动 agy]
       │
       ▼
[评估最佳健康账号 (ensure-active)]
       │
       ├─► 启动 agy 子进程 (注入 SSH_CONNECTION + 隔离 profile)
       │
       └─► 启动后台监控线程 (watch --account <active> --poll-seconds 1)
                 │
                 ▼
       [实时扫描 session log]
       - 匹配 5 小时短窗口耗尽: "Individual quota reached" / "RESOURCE_EXHAUSTED (code 429)"
       - 匹配周额度耗尽: "weekly quota reached"
       - 自动提取重置时间: "Resets in ~2h" -> 设定精确冷却期
                 │
                 ▼ (命中额度上限)
       [执行轮换故障转移]
       1. 发送 kill -TERM 终止卡住的旧进程
       2. 将耗尽账号标记为 cooldown (锁定至恢复时间)
       3. 切换至下一个健康的 standby 账号
       4. 以断点引导模式重新启动新进程:
          --continue --prompt-interactive "<RESUME_PROMPT>"
                 │
                 ▼
       [模型加载历史会话并检查当前工作区，接着上一轮断点继续完成任务]
```
