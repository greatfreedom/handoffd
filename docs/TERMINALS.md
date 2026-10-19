# Using another terminal / 换用其他终端

Everything below is read from the source (`cdm/daemon.py` `run_launcher`, `bin/open_warp.sh`, `cdm/baton.py` `record_launch`). Anything not backed by the source is marked **unverified**. No terminal other than Warp has been tried.

## Why a terminal integration exists

During a handover the daemon has to open a **new terminal window**, in the project directory, that starts B's `claude` session. handoffd 0.1.0 does this with Warp: `bin/open_warp.sh` writes a Warp *Launch Configuration* (`warp_open` = `launch_config`, default) or *Tab Config* (`tab_config`, fallback) under `~/.warp/` and opens it with `open -g warp://…`.

## The only integration point: `bin/open_warp.sh`

`cdm/daemon.py` (`run_launcher`, about lines 91–116) runs it as `/bin/bash <install-dir>/bin/open_warp.sh <args>` with a **45-second timeout** (`LAUNCHER_TIMEOUT_SECONDS`). The path is hard-coded.

### Arguments the daemon passes (in this order)

| Argument | Value | Notes |
|---|---|---|
| `--name` | B’s window/session name, `<session_name_prefix>-handoff-<id>` | must match `^[A-Za-z0-9._-]+$`; also the title of the window and the base name of the temporary files |
| `--dir` | `project_dir` | an existing directory (absolute for `launch_config`); B must start here |
| `--command-dir` | `<state dir>/launch` | the script creates it and writes `<name>.command` into it |
| `--min-version` | `min_claude_version` (`x.y.z`) | the target `claude` must be at least this |
| `--required-env` | the `required_env` names joined by spaces; **may be an empty string** (default) | the login shell must have these variables set |
| `--claude-bin` | `claude_bin` (absolute path; default `~/.local/bin/claude`) | the script resolves the directory with `pwd -P` |
| `--extra-arg` | repeated; first `--session-id=<B’s uuid>`, then `claude_extra_args` (minus any `--model`/`--effort` that is replaced), then `--model=…` / `--effort=…` inherited from A when A’s status-line snapshot exists | each must match `^[A-Za-z0-9._=:/@+-]+(\[[0-9a-z]+\])?$` |
| `--warp-open` | `launch_config` or `tab_config` | Warp-only; any other value makes the script exit 2 |

The daemon does **not** pass `--prompt-file`: B starts idle, and the opening instruction is delivered later by message after A seals the kit.

### What the script must do

1. **Pre-flight** (before opening anything): `claude` exists, is executable and its first `x.y.z` version is ≥ `--min-version`; a login `zsh -lic` can run `claude --version` and sees every `--required-env` variable; the project directory exists.
2. **Write the command file** `<command-dir>/<name>.command` (the Warp-independent part, lines 121–166 of `open_warp.sh`): it `cd`s to `--dir`, unsets `CLAUDE*`, `GIT_*` and `MAIN_LOCK_OVERRIDE` so B does not inherit the daemon’s environment, and finally runs `exec /bin/zsh -lic '… "$CDM_TARGET" ${=CDM_EXTRA} --name "$CDM_NAME"'`, i.e. effectively `claude --session-id=<uuid> [--model=…] [--effort=…] --name <name>` in a login zsh in the project directory.
3. **Open one new window that runs that command file.** For Warp this is the Launch Configuration/Tab Config + `open -g`; the window closes when `claude` exits because the config uses `exec`. For another terminal this is the only part you replace.

### Exit codes and output

| Exit code | Meaning (script) | What the daemon does (`record_launch` in `cdm/baton.py`) |
|---|---|---|
| `0` | the open command returned 0 | handover state `opened` |
| `2` | pre-flight failed **and no window was opened** (message on stderr, `CDM preflight failed: …`) | state `retry`: tried again after `preflight_retry_seconds` (default 60); after 3 consecutive failures you are alerted |
| anything else (the script uses `3` when `open` fails), or no result within 45 s | the window may or may not exist | state `uncertain`: **never retried automatically** (to avoid two windows), you are alerted |

Output: the daemon only parses a stdout line `CDM_LAUNCHER_VERSION=<x.y.z>` (optional; recorded as information). The other lines Warp’s script prints (`CDM_LAUNCHER_TARGET=`, `CDM_LAUNCHER_COMMAND=`, `CDM_LAUNCHER_LAUNCH=` / `CDM_LAUNCHER_TAB=`) are checked by the tests but not used by the daemon. The last 500 characters of stdout+stderr go into the alert text.

### How the daemon decides the window really worked

Exit code `0` only means “the open command was accepted”. B counts as **online** when a Claude Code session whose id equals the pre-assigned `--session-id` runs its `SessionStart` hook in the project (the plugin’s hooks register it). If B is not online within `launch_confirm_timeout_seconds` (default 120) you are alerted. So any terminal is fine as long as the command file runs, in the project directory, with the hooks active.

## Switching to Ghostty, iTerm2, Terminal.app, …

- **0.1.0: replace the implementation of `bin/open_warp.sh` yourself**, keeping the contract above (same arguments, same exit codes, one new window running `<command-dir>/<name>.command`). Keep everything up to and including the command-file step and replace the Warp-specific tail (the `write_*_config` functions and the final `open`). *Whether a given terminal can be driven this way (for example `open -a Terminal <file>.command` for Terminal.app) is unverified.*
- If you installed the plugin, files under `~/.claude/plugins/cache/…` are replaced on update — your edit is lost. Use a clone-based install (see INSTALL.md, manual install) for a customised script.
- **Warp-only parts that simply do nothing for other terminals**: the `warp_open` option (still has to be `launch_config` or `tab_config` because the daemon validates and passes it), the temporary configuration files in `~/.warp/`, and their registration (`<state dir>/warp_configs.json`) and clean-up in the daemon — deleting a file that never existed is a no-op (`cdm/daemon.py` `_rm_configs`). *That this has no effect on other functions has not been tested with another terminal.*
- **Notifications do not depend on the terminal**: banners and sound use macOS `osascript`, `afplay` and `say`.
- **Planned for 0.2: a configurable launcher script** (a setting instead of editing `bin/open_warp.sh`).

---

# 换用其他终端

以下内容都取自源码（`cdm/daemon.py` 的 `run_launcher`、`bin/open_warp.sh`、`cdm/baton.py` 的 `record_launch`），源码里没有依据的标「未核实」。**除 Warp 外没有试过任何终端。**

## 为什么需要终端集成
交接时 daemon 要自动开一个**新终端窗口**，在项目目录里启动接手方 B 的 `claude` 会话。0.1.0 用 Warp 实现：`bin/open_warp.sh` 在 `~/.warp/` 下写 Launch Configuration（`warp_open` = `launch_config`，默认）或 Tab Config（`tab_config`，回退），再用 `open -g warp://…` 打开。

## 唯一的集成点：`bin/open_warp.sh`
daemon（`cdm/daemon.py` `run_launcher`，约 91–116 行）以 `/bin/bash <安装目录>/bin/open_warp.sh <参数>` 调用，**超时 45 秒**，路径写死。

- **传入的参数**：`--name`（B 的窗口/会话名 `<前缀>-handoff-<id>`，只许 `A-Za-z0-9._-`）、`--dir`（项目目录，B 必须在这里启动）、`--command-dir`（`<状态目录>/launch`，脚本在里面写 `<名>.command`）、`--min-version`、`--required-env`（`required_env` 用空格连接，**默认是空字符串**）、`--claude-bin`（claude 绝对路径）、重复的 `--extra-arg`（先 `--session-id=<B 的 uuid>`，再是 `claude_extra_args`（被替换的 `--model` / `--effort` 除外），最后是有 A 的状态栏快照时继承的 `--model=` / `--effort=`）、`--warp-open`（`launch_config` 或 `tab_config`，Warp 专用，其他值脚本退出 2）。daemon **不传** `--prompt-file`：B 先空闲，开场指令在 A 封存 kit 后由消息投递。
- **脚本要做的事**：① 预检（claude 可执行且版本不低于最低版本；登录 `zsh -lic` 能跑 `claude --version` 且有所需环境变量；项目目录存在）；② 写命令文件 `<command-dir>/<名>.command`（不依赖 Warp：先 `cd` 到 `--dir`、清掉 `CLAUDE*` / `GIT_*` / `MAIN_LOCK_OVERRIDE`，最终相当于在登录 zsh 里、项目目录下运行 `claude --session-id=<uuid> [--model=…] [--effort=…] --name <名>`）；③ **开一个新窗口来运行这个命令文件**——这一步 Warp 用配置文件 + `open -g`，换终端只需替换这一步。
- **退出码与输出**：`0` = open 命令返回 0 → 状态 `opened`；`2` = 预检失败且**保证没开窗**（stderr 写 `CDM preflight failed: …`）→ 状态 `retry`，隔 `preflight_retry_seconds`（默认 60）重试，连续 3 次失败会叫你；其他（脚本在 `open` 失败时用 `3`）或 45 秒内没结果 → 窗口可能开了也可能没开，状态 `uncertain`，**永不自动重试**（防止开出两个窗口），并叫你。输出里 daemon 只解析 stdout 的 `CDM_LAUNCHER_VERSION=<x.y.z>`（可选）；其余几行测试会检查、daemon 不用；stdout+stderr 的末尾 500 字符会进提醒文案。
- **daemon 怎么判定开窗成功**：退出码 0 只代表"open 命令被接受"。B 算**上线**＝会话号等于预分配的 `--session-id` 的 Claude Code 会话在项目里跑了 `SessionStart` 钩子（由插件的钩子登记）；`launch_confirm_timeout_seconds`（默认 120）内没上线会叫你。所以只要命令文件在项目目录里被执行、钩子生效，任何终端都行。

## 换成 Ghostty / iTerm2 / Terminal.app 等
- **0.1.0 需要你自己替换 `bin/open_warp.sh` 的实现**，保持上面的契约（参数相同、退出码相同、开一个运行 `<command-dir>/<名>.command` 的新窗口）：保留到写命令文件为止，替换 Warp 专有的尾部（`write_*_config` 与最后的 `open`）。*某个终端能否这样驱动（例如 Terminal.app 用 `open -a Terminal <文件>.command`）未核实。*
- 若装的是插件，`~/.claude/plugins/cache/…` 下的文件会在更新时被覆盖，你的改动会丢；要定制脚本请用克隆方式的手动安装（见 INSTALL.md）。
- **对其他终端不起作用的 Warp 专有部分**：`warp_open` 配置项（daemon 仍会校验并传入，所以仍须是 `launch_config` / `tab_config`）、`~/.warp/` 里的临时配置文件及其登记（`<状态目录>/warp_configs.json`）与清扫——删除一个从未存在的文件是空操作（`cdm/daemon.py` `_rm_configs`）。*对其他功能没有影响这一点没有在别的终端上测过。*
- **通知与终端无关**：横幅和声音用 macOS 自带的 `osascript`、`afplay`、`say`。
- **可配置的开窗脚本计划在 0.2。**
