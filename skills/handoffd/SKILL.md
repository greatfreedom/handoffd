---
name: handoffd
description: 当用户提到 handoffd、自动接力、主控交接、"上下文快满了想自动换新会话"，或看到 handoffd 的提醒/拦截信息（例如上下文到 65%/76%、"你是接手方 B"、"只读旁观会话"）时使用。说明怎么查看状态、交接怎么走、主控（A）与接手方（B）各自该运行哪些 handoffd 命令。
---

# handoffd — 主控自动接力

handoffd 是后台守护进程（daemon）加一组钩子：在**已开 auto 的项目**里，主控（A）上下文快满或目标达成时，自动在新的 Warp 窗口起接手方（B），双方读交接包（kit）、签字后换棒。默认 manual：什么都不做，也不拦你。仅限 macOS + Warp，实验性。

## 先看状态（任何时候都安全）
`handoffd status` —— 模式（auto/manual）、daemon 是否在跑、谁持棒、有没有待处理的铃。只读。

## 谁能敲什么
- **`handoffd on` / `adopt` 只能 Owner（用户）自己敲**（可在主控窗口里 `!handoffd on`）。你（AI）不要代为执行，钩子也会拦。
- `handoffd off`：取消进行中的交接、回到 manual。用户明确要求时可以执行。
- 主控 A：`request`（申请交接，开 B 的窗口）→ 写 kit → 等在途任务结束 → `seal`（封存 kit）→ 与 B 来回复核 → `sign out`。
- 接手方 B：只读、`SendMessage`、`handoffd status|notify|reply`、`handoffd sign in`，以及用户在 `handoffd.json` 的 `b_verify_commands` 里**逐字**列出的复核命令（多一个空格都会被拒）。B 想找 A 的会话名可用 `ListAgents`。
- 需要用户拍板或授权时：用提问框（AskUserQuestion）或 `handoffd notify`；不要只用文字问——机器检测不到。
- daemon 问"在等什么"时：`handoffd reply task|peer|quota|owner`（`task` 可带预计时长，如 `handoffd reply task 60m`，15–180 分钟）。

## 一次正常交接
1. 到触发点（目标达成；上下文 65% 提醒并拦新的长任务；76% 还没发起则钩子替你发起，此后只准收尾）。
2. A 申请交接 → daemon 开 B；A 写 kit，等在途任务结束，把结果用消息交给 B，`seal`。
3. B 读 kit 复核，把结论回给 A；有异议 A、B 自己来回，超限才叫用户。
4. A `sign out`、B `sign in`；daemon 核对会话号后一次性换棒，结束 A 的进程；B 收到 BATON 消息，按 kit 继续。

## 常见情况
- **被拦了** "上下文已过提醒档"：别再开新的长任务（Agent/Workflow/Monitor/定时任务/后台 Bash），收尾并发起交接。
- **"你是只读旁观会话"**：本项目已有主控会话或正在交接，你只能读和答。
- **响铃/横幅**：只在需要用户时出现；怎么自定义见 `assets/voice/README.md`。
- 记录与状态目录：`handoffd status` 第二行会打印路径（git 项目在 `.git/handoffd/`）。

更多：README.md。
