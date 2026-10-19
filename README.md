# handoffd — Automatic main-session relay for Claude Code（后台自动交接主会话）

> **macOS + Warp only · experimental (v0.1.0).** Not affiliated with the unrelated GitHub repositories `kdykdy7991/handoffd` and `Kriso1337/handoffd`.
> Runtime messages (banners, hook notices, CLI output) are in **Chinese** in this first release. 中文说明在下方。

## What it does

When the main Claude Code session (“A”) is running out of context or has reached its goal, `handoffd` starts a fresh session (“B”) in a new Warp window, lets B read a hand-over kit (a Markdown summary written by A), has both sign it, and then swaps the baton — with **zero clicks from you** in the normal case. It only calls you (banner + sound) when a human decision is really needed. It is a background daemon plus Claude Code hooks; **the daemon never calls a model**. Default mode is `manual`, in which it does nothing and never blocks you.

- Triggers: goal reached → hand over; context ≥ 65% → reminds A to find a good point and blocks *new long tasks*; ≥ 76% → the hook initiates the handover and only wrap-up is allowed.
- Pure Python 3 standard library (macOS system `/usr/bin/python3`, 3.9 is fine). No dependencies to install.
- Needs: macOS, [Warp](https://www.warp.dev/) terminal, `git`, Claude Code ≥ 2.1.284 (see `min_claude_version` in `cdm/config.py`; the basis for this number is not documented).

## Install

```text
/plugin marketplace add greatfreedom/handoffd
/plugin install handoffd@handoffd
```

A plugin cannot set `statusLine` or `permissions.allow`, and its `bin/` is only on the PATH of Claude's Bash tool, so also: (1) put a `handoffd` wrapper script on your terminal PATH, (2) add `Bash(handoffd:*)`, `SendMessage`, `ListAgents`, `Read(//private/tmp/claude-<uid>/**)` to the project's `permissions.allow`, (3) point `statusLine` at `bin/statusline_tap.sh` (required: without it the 65%/76% triggers never fire; keep your existing status line by wrapping it, see INSTALL.md), (4) check with `handoffd status`, then run `!handoffd on` yourself. **Full steps with backup/undo for every settings edit, the update procedure (paths change on plugin update) and uninstall: [docs/INSTALL.md](docs/INSTALL.md).**

**Known limitation (0.1.0, “plan B”):** the plugin directory name contains the version, so after a plugin update the wrapper script, the `statusLine` command and the launchd job still point at the old directory. 0.1.0 handles this with a documented manual procedure (`handoffd off` → fix the paths → `/reload-plugins` → `handoffd on`, see “Update the plugin” in docs/INSTALL.md); there is no automatic detection or stable-path mechanism yet.

## Usage

- `handoffd` is an ordinary terminal command, **not** a `/` slash command: `handoffd status` in your terminal, `!handoffd status` inside a Claude Code session (the leading `!` runs it directly), or ask the main session to run it for you.
- Modes: `manual` (default) behaves as if handoffd were not installed. `handoffd on` (human only; typing `!handoffd on` in the main session is fine) switches the current project to `auto` and starts the daemon; `handoffd off` cancels any handover in progress and returns to manual.
- One handover: context ≥ 65% → A is reminded to pick a good point and new long tasks are blocked; ≥ 76% (or goal reached) → the hand-over starts → a new Warp window starts B → B reads A's kit and reviews it → both sign → the daemon swaps the baton and ends A.
- Where to look: `handoffd status` (mode, daemon heartbeat, baton holder, pending alerts); its second line prints the state directory (`baton.json`, `events.jsonl`, `notices.jsonl`, `handoffs.jsonl`, `kits/`).

## Commands, sounds, tests

`handoffd on` (human only) / `off` / `status` (read-only); `notify`, `reply`, `seal`, `sign`, `request` are used by A and B during a handover (see `skills/handoffd/SKILL.md`). The release ships **no audio**: defaults are the macOS sounds Glass and Ping (`assets/voice/README.md` to use your own). Tests: `HOME=$(mktemp -d) CDM_CLAUDE_SESSIONS_DIR=$(mktemp -d) bash tests/run_all.sh` prints `MVP-OFFLINE-ALL-PASS` (about 10 minutes; fake `git`/`ps`/`claude`/`launchctl`, silent via `HANDOFFD_SILENT=1`). License: MIT.

## Using another terminal

The daemon opens B's window through one script, `bin/open_warp.sh` (Warp Launch Configuration in 0.1.0). For Ghostty, iTerm2, Terminal.app, … replace that script yourself, keeping its contract (arguments, exit codes, one new window running the generated command file); a configurable launcher is planned for 0.2. Warp-only parts (`warp_open`, temporary configs in `~/.warp/`) do nothing for other terminals; notifications use macOS `osascript`/`afplay`/`say` and are terminal-independent. Contract and caveats: [docs/TERMINALS.md](docs/TERMINALS.md).

---

# 中文说明

> 首版运行时文案为中文。以下 `Owner` 指**你本人（人类用户）**。安装、卸载的完整步骤见 [docs/INSTALL.md](docs/INSTALL.md)（英文）；下面是使用说明。

## 一句话

在项目里开 `handoffd on` 之后，主控（Claude Code 会话）的上下文快满或目标达成时，由一个不调用模型的后台程序（daemon）自动开一个新窗口、让新主控读交接包、双方签字后换棒，你一般 0 次操作；只有真要你拍板时才响铃叫你。不开（默认 manual）就和平时完全一样。

## 怎么用

| 命令 | 效果 |
|---|---|
| `handoffd on` | 当前项目开自动接力（auto），并拉起 daemon |
| `handoffd off` | 取消进行中的交接、回到 manual；没有没收尾的事就停 daemon |
| `handoffd status` | 看模式、daemon（在不在 launchd 里、心跳几秒前）、谁持棒、有没有待处理的铃 |

- 它是普通终端命令，不是 `/` 斜杠命令。在你自己的 Warp 里直接敲 `handoffd status`；在任意 Claude Code 窗口里敲 `!handoffd status`（感叹号开头 = 直接执行）；或用中文让主控替你做。
- **`on` 只能你自己敲**（可以在主控窗口敲 `!handoffd on`）；`off` 和 `status` 主控也可以替你执行。
- 命令入口是 `bin/handoffd`（需要放进你终端的 PATH，见上面「安装」）。命令从当前目录向上找 `.claude/handoffd.json`，找不到就用全默认配置；也可以 `--config <路径>` 指定。
- `off` 如果提示「daemon 保留，还有没收尾的事」，是因为交接已经换棒、或有没响完的铃：daemon 会自己做完，做完后（manual 下没有未收尾的事）它自己退出，不用再敲 `off`。之后再 `on`，已加载但没在跑的任务会被 `launchctl kickstart` 拉起。**要卸载 / 停用时**：daemon 自己退出后 launchd 任务仍是「已加载」，收尾完成后要再敲一次 `off` 才会卸载它（见 docs/INSTALL.md）。
- 试听提醒声音：`handoffd test-alert <waiting|problem|quiet>`（会真的出声，每次都从第一个铃声开始）。换话术 / 音色 / 铃声见 `assets/voice/README.md`。
- daemon **不开机自启**：它的 launchd 配置写在状态目录 `<S>/launchd/` 里（不进 `~/Library/LaunchAgents`），label 是 `local.handoffd.<项目名>.<路径哈希>`；只在你 `on` 之后运行，运行中崩溃会被 launchd 自动拉起。

配置文件 `<项目>/.claude/handoffd.json` 可选，最小例子（写了不认识的键会直接报错；`"warp_open"` 默认 `"launch_config"`，开 B 的窗口不带空白标签页，出问题可改 `"tab_config"` 回退）：

```json
{"project_dir": "/绝对路径/你的项目", "session_name_prefix": "toy"}
```

## 一次正常交接

1. 到了触发点（目标达成；或上下文到 65% 时提醒主控找节点交接并拦新的长任务；到 76% 还没发起，钩子替它发起），主控 A 向 daemon 申请交接，daemon 在**单独的新 Warp 窗口**起接手方 B。
2. A 写交接包（kit），等在途任务跑完，把结果用消息交给 B；A 用 `handoffd seal` 封存 kit。
3. B 读 kit 复核，把结论回给 A；有异议 A、B 自己来回讨论，超过上限才叫你。
4. A 运行 `handoffd sign out`、B 运行 `handoffd sign in`；daemon 核对会话号和 A 已无在途任务后一次性换棒，并结束 A 的进程。A 若是你手动在标签页里起的，进程退出后标签页会留在 shell 提示符（要你自己关）；B 由机器起的会随之关闭。
5. B 收到「BATON」消息，按 kit 继续。全程你只需要在需要时看一眼窗口。

## 什么时候会响铃

响铃（先弹一条带编号选项的横幅，再响铃声，写明「发生了什么 / 为什么停 / 要你做什么 / 不回复会怎样」）只在需要你时：

- 主控或接手方用 `handoffd notify` 叫你（需要你授权、决定、出了事故、卡住、对不上、碰到规则）。
- 交接卡住：A 崩了而 kit 还没签字、B 120 秒没上线、B 开了又退出、问答超过上限、换棒后旧 A 60 秒还没退、复核第 3 轮仍有 P0。
- 主控在「等你输入」（权限请求、提问框）。
- daemon 心跳停了约 2 分钟（钩子替它报警）。
- 看护（auto 下 daemon 每轮做）：
  - daemon 每 `idle_ask_minutes`（默认 30，项目可在 handoffd.json 配）问一次「在途」的会话（有后台任务、这一轮还没结束、或交接中；平时空闲不问）在等什么；它回复「在等 Owner」会马上响铃；回复「在等任务 / 对方消息 / 额度」daemon 会核对，核得上就继续等，核不上先纠正一次，连续 2 次核不上才响铃。回复 task 时可附预计时长，如 `handoffd reply task 60m`（15m–180m，只对 task 有效），核得上则这一次等到该时长才再问。
  - 问了 5 分钟没人回复（包括消息没投递成功）响铃一次；B 已上线、kit 已封存后开场指令 5 分钟都送不到 B 也响铃一次。
  - 复核第 3 轮仍有 P0（同一产物同一轮只响一次）；B 向 A 提问超过 12 条（每次交接响一次）。
  - 平时（没在交接）主控窗口没了、又没有正常退出的记录（比如点 × 关窗）响一次，新开窗口就是新主控，基本无需处理。
  - daemon 自己连续 3 轮出错，或连续 3 轮读不出 baton.json / review.json，响一次。
  - 不响铃：API 临时错误按 3→6→12→24→48→60 分钟（封顶）自动发「继续」，永不停发；撞额度时 daemon 不打扰，收到「额度自动续跑被关」或 60 分钟没动静才转入同样的「继续」循环。

不响铃只记录：A 签字之后才崩、claude 版本变了等，只写进下面的记录。

## 记录在哪

状态目录 `<S>`：git 项目是 `<git 公共目录>/handoffd/`（即 `.git/handoffd/`），否则是 `<项目>/.claude/handoffd/`；`handoffd status` 第二行会打印它。manual 下只有 `mode`，什么都不记；auto 下有：

| `<S>/` 下 | 内容 |
|---|---|
| `mode` | auto 还是 manual、自什么时候起 |
| `baton.json` | 唯一真相：谁持棒、有没有交接、B 是谁、走到哪一步 |
| `handoffs.jsonl` | 每次交接的结局（换棒完成 / 放弃） |
| `events.jsonl` | 事件流水（钩子与 daemon 做过什么） |
| `notices.jsonl` | 叫过你的记录（每条四段文案） |
| `kits/` | 每次交接的 kit 存档 |
| `sessions/`、`review.json`、`daemon_state.json`、`heartbeat` | 各会话状态、复核轮次账、daemon 的小账本（含铃声轮换位置）、daemon 心跳 |
| `launchd/` | daemon 的 launchd 配置与日志（`daemon.out.log`、`daemon.err.log`） |
| `launch/` | 开 B 窗口用的命令文件 |

## 新会话怎么了解发生了什么

新开一个会话想弄清楚发生过什么：让它读 `handoffd status` 的输出，再按顺序读上表里的 `handoffs.jsonl`、`notices.jsonl`、`events.jsonl` 和 `kits/` 里最新的 kit，不需要你转述。

## 已知限制

- 钩子出故障时放行（不拦）：入口脚本运行中出错 ⇒ 内部兜底放行；入口文件缺失 / 不可读（例如插件目录被清理）⇒ 钩子启动层直接退出 0（`hooks/hooks.json`，测试 X6）。按官方的退出码规则，退出码 2 才是「阻断」约定（退出 0 时钩子仍可以靠输出的 JSON 决策阻断）；本插件入口缺失时返回 0 + 空输出，属放行。解释器本身起不来（找不到 /usr/bin/python3 等）给的不是 2，只是报错；这一类没有在本机逐一演练。多个罕见故障叠加不防。
- 钩子「已判放行、尚未返回」的毫秒窗口里，如果主控恰好崩溃、另一会话恰好接棒，旧的放行仍会生效一次（Owner 已接受，不修）。
- 你自己点 × 关主控窗口（Warp 确认关闭）：Claude Code 会发出结束事件，按正常退出处理、不响铃（2026-10-01 S2b 演练实测，规格 §11 原推断「会响一次铃」不成立）。
- 主控用**文字**向你提问，机器检测不到；规矩是要问你就用提问框（AskUserQuestion）或 `handoffd notify`。
- 主控被强杀（kill -9）或崩溃：没有结束事件，平时（非交接）会响一次铃（S2b 演练实测）。额度自动继续只在真撞额度时能验证；auto 权限模式只在少数模型上实测过。
- 零确认需要 allow 规则含 `Read(//private/tmp/claude-<你的uid>/**)`（`<你的uid>` 用 `id -u` 查）；前台 `sleep` 会被 Claude Code 拦下。
- 已在玩具项目真机验证：Warp Launch Configuration 开窗、从 `<S>/launchd/` bootstrap 的 launchd 任务、云夏人声（S3、S2b 演练）。仍未真机验证：launchctl 对「没有这个任务」的退出码（代码按 113 判）；B 用带 `[1m]` 的 Opus 模型名起窗（首次真交接时看 B 状态栏）。
- 离线测试不出声、不碰真实 launchd：`tests/run_all.sh` 设了 `HANDOFFD_SILENT=1`（全局静音闸，通知器不调 osascript / afplay / say）和假的 launchctl。单跑某个测试脚本时同样默认静音。真实铃声、开窗、launchd 要在玩具项目里实际演练。
- 只管从项目主目录起的会话：从 linked worktree 目录独立起的 Claude 会话没有项目设置，不受「同项目只有一个会话能写」约束（Owner 只在主目录开会话，2026-10-03 记为已知限制）。
- 插件更新后目录名里的版本号会变：终端包装脚本、`statusLine` 命令和 launchd 任务仍指向旧目录。0.1.0 按「方案 B」处理——只提供手动更新步骤（`handoffd off` → 改路径 → `/reload-plugins` → `handoffd on`，见 docs/INSTALL.md「Update the plugin」），没有自动检测，也没有稳定路径机制。
- 横幅点「显示」会打开「脚本编辑器」（通知由 osascript 发出），看完点「关闭」即可；要横幅不自动消失，在 系统设置 → 通知 里把它的提醒样式改为「持续」。
- 平时（非交接）响过的铃在 `handoffd status` 里一直显示待处理，不会自动解除。

## 离线测试

```
bash tests/run_all.sh
```

只有每个用例都恰好通过一次，最后一行才会打印 `MVP-OFFLINE-ALL-PASS`。测试用假 `git` / `ps` / `claude` / `open` / `launchctl`，不会调模型、联网、弹窗或出声。

## 换用其他终端

交接时 daemon 通过唯一一个脚本 `bin/open_warp.sh` 开新窗口（0.1.0 用 Warp Launch Configuration）。要换成 Ghostty / iTerm2 / Terminal.app 等，0.1.0 需要你自己替换这个脚本，保持它的契约（参数、退出码、开一个运行命令文件的新窗口）；可配置的开窗脚本计划在 0.2。Warp 专有的部分（`warp_open`、`~/.warp/` 里的临时配置）对其他终端不起作用；通知用 macOS 自带的 `osascript` / `afplay` / `say`，与终端无关。契约与注意事项见 [docs/TERMINALS.md](docs/TERMINALS.md)。
