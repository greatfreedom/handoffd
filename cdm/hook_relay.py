# 钩子唯一入口：hook_relay.py <事件>（设计:30、§5.2）。manual / auto 都装、常驻。
# 硬纪律：任何异常一律放行（规格:79；与旧 hook_lock 的拦截式失败相反）；manual 下立即放行、零写入（仅 run/ 去重标记，规格:75-77）。
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdm import baton, config, ctx as ctx_module, hook_nudge, lockcore, notify, policy, procs, sessions, util  # noqa: E402

CLEAR_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "StopFailure")
WAIT_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "quota_auto_resume_stale")
# StopFailure 归类（H:320；归类自定，设计:59）：撞额度 / 临时错误 / 其余（需人处理，含 unknown）
QUOTA_ERRORS = ("rate_limit",)
TEMP_ERRORS = ("overloaded", "server_error", "max_output_tokens")
HEARTBEAT_JUMP_MS = 3600 * 1000  # 设计:7：年龄为负或跳变 > 1 h ⇒ 计时从当前时刻重算
HEARTBEAT_SKEW_MS = 5000  # hook 先取 now、daemon 随后写心跳 ⇒ 年龄为几毫秒的负数：这是竞态不是时钟回拨，按心跳正常处理（H107）


class Ctx:
    """一次钩子调用的上下文。"""

    def __init__(self, event, inp, proj, cfg, sdir, mode, since_ms, now, deadline, identity, chain, ring_fn):
        self.event = event
        self.inp = inp
        self.sid = inp.get("session_id") if isinstance(inp.get("session_id"), str) else None
        self.proj = proj
        self.cfg = cfg
        self.sdir = sdir
        self.mode = mode
        self.since_ms = since_ms
        self.now = now
        self.deadline = deadline
        self._identity = identity
        self._chain = chain
        self.ring_fn = ring_fn
        self._identity_cache = None
        self._chain_cache = None
        self.role = None
        self.state = None

    def identity(self):
        """本会话所在 claude 进程 (pid, lstart)；取不到返回 (None, "")。"""
        if self._identity_cache is None:
            try:
                pid, lstart = self._identity(self.deadline)
            except Exception:
                pid, lstart = None, ""
            self._identity_cache = (pid, lstart or "")
        return self._identity_cache

    def chain(self):
        if self._chain_cache is None:
            try:
                self._chain_cache = list(self._chain(self.deadline) or [])
            except Exception:
                self._chain_cache = []
        return self._chain_cache

    def ring(self, kind, fields):
        """启动响铃子进程；失败只记 events、返回 False，绝不把异常抛进放行决定（响铃是副作用，C 类不变量）。"""
        try:
            self.ring_fn(kind, fields["what"], fields["why"], fields["ask"], fields["recommend"], fields["if_no_reply"])
            return True
        except Exception as exc:
            if self.mode == "auto" and self.sdir:
                sessions.append_event(self.sdir, {"t_ms": self.now, "sid": self.sid, "event": self.event, "role": None,
                                                  "summary": "ring_failed", "error": repr(exc)})
            return False

    def tool(self):
        return self.inp.get("tool_name") if isinstance(self.inp.get("tool_name"), str) else ""

    def tool_input(self):
        value = self.inp.get("tool_input")
        return value if isinstance(value, dict) else {}


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "⛔ " + reason}}


def context(event, text):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# ---- 等输入响铃（manual / auto 共用，规格:77）----

def wait_text(c, reason):
    """等输入通知的四段文案（规格:50、:54 行：选项 1 去该窗口处理）。"""
    who = "会话 %s（项目 %s）" % ((c.sid or "?")[:8], os.path.basename(c.proj.rstrip("/")) or c.proj)
    return dict(
        what="%s在等你输入：%s" % (who, reason),
        why="Claude Code 弹出了需要你操作的对话框，会话停在那里等你。",
        ask="1) 去该窗口处理", recommend=1,
        if_no_reply="该会话会一直停在那里，不会自己继续。",
    )


def ring_once(c, reason):
    """同会话同次等待只响一次：run/waiting.<sid> 标记存在即去重；auto 另写 notice（规格:85 auto 才记录）。
    去重标记与 notice 都只在响铃子进程成功启动之后才落（F4）：启动失败不留标记，下一个事件重试，不会把这段等待吞掉。"""
    fields = wait_text(c, reason)
    notice_id = sessions.new_notice_id(c.now)
    marker = sessions.wait_marker(c.sdir, c.sid) if c.sdir else None
    if marker and os.path.exists(marker):
        return False
    if not c.ring("waiting", fields):
        return False
    if marker:
        sessions.create_marker(marker, notice_id if c.mode == "auto" else "")
    if c.mode == "auto" and c.sdir:
        sessions.append_notice(c.sdir, {
            "type": "notice", "id": notice_id, "t_ms": c.now, "sid": c.sid, "cat": "waiting", "ring": True,
            "status": "pending", "text": notify.format_notice(**fields), "options": ["去该窗口处理"],
        })
    return True


def clear_wait(c):
    """任一非等待类事件清「同次等待」标记（设计:58）；auto 下把对应 notice 标为已解除。标记不存在时不产生任何写入。"""
    if not c.sdir:
        return
    content = sessions.clear_marker(sessions.wait_marker(c.sdir, c.sid))
    if content and c.mode == "auto":
        sessions.append_notice(c.sdir, {"type": "resolve", "id": content, "t_ms": c.now})


def waiting_reason(c):
    event = c.event
    if event == "PermissionRequest":
        return "需要你批准工具调用（%s）" % (c.tool() or "?")
    if event == "Elicitation":
        return "MCP 服务器向你索要输入"
    if event == "PreToolUse":
        return "向你提问（AskUserQuestion）"
    kind = c.inp.get("notification_type")
    if kind == "quota_auto_resume_stale":
        return "额度已重置但电脑睡眠过久，Claude Code 在等你按 Enter 继续"
    return "等你处理（%s）" % kind


# ---- manual ----

def manual(c):
    """manual：立即放行，不拦、不记录；只做「等输入响铃」与 Bash 里 `handoffd on|adopt` 的窄拦（设计 §9 §7-1）。"""
    event = c.event
    if event == "PreToolUse":
        if c.tool() == "Bash" and policy.invokes_owner_only(c.tool_input().get("command")):
            return deny(owner_only_reason())
        if c.tool() == "AskUserQuestion":
            ring_once(c, waiting_reason(c))
            return None
        clear_wait(c)
    elif event in ("PermissionRequest", "Elicitation"):
        ring_once(c, waiting_reason(c))
    elif event == "Notification":
        if c.inp.get("notification_type") in WAIT_NOTIFICATIONS:
            ring_once(c, waiting_reason(c))
    elif event in CLEAR_EVENTS:
        clear_wait(c)
    return None


def owner_only_reason():
    return "`handoffd on` / `handoffd adopt` 只能 Owner 在自己的终端敲（规格:19、:57）；请告诉 Owner 在终端或用 `!handoffd on` 执行。"


# ---- auto ----

def log_event(c, role, summary=None, **extra):
    """事件流水（auto 才写）。"""
    record = {"t_ms": c.now, "sid": c.sid, "role": role, "event": c.event}
    if summary:
        record["summary"] = summary
    record.update(extra)
    sessions.append_event(c.sdir, record)


def safe(c, label, fn):
    """副作用（写事件 / 会话状态 / 标记 / 心跳 / 响铃）一律走这里：异常只记 events，不改放行决定（放行先算、副作用后做）。"""
    try:
        return fn()
    except Exception as exc:
        try:
            sessions.append_event(c.sdir, {"t_ms": c.now, "sid": c.sid, "event": c.event, "role": None,
                                           "summary": "side_effect_failed:" + label, "error": repr(exc)})
        except Exception:
            pass
        return None


def resolve_role(c, claim, sync=False):
    """唯一授权入口（不变量 I、II）。返回 (role, note)；只有 holder / holder_sub / b 可放行写，它们都来自
    「登记册槽位里的进程 == 本进程（或祖先链）」；其余（spectator / blocked）不可写。

    - 先无锁读登记册、baton.classify；三元组相等 ⇒ 直接得角色（holder 同进程换号只在 sync=True 时写回会话号）；
    - not_me：claim=False ⇒ 旁观；claim=True ⇒ 进 guard 做槽位 CAS（baton.claim），认领成功才得 holder / b，
      对方存活 / 判活不确定 / guard 忙 ⇒ 旁观（本次拒写），交接中持棒者已死 ⇒ blocked（规格:44）；
    - fail_open（单列）：本进程身份 / 会话号取不到，或登记册读不出、认领时抛异常（规格:79、K2）——钩子出错放行，不看登记册、不写登记册。"""
    status, state = baton.read(c.sdir)
    if status == "error":
        return "fail_open", "baton_unreadable"
    c.state = state
    role, info = baton.classify(state, c.sid, c.identity, c.chain)
    if role == "fail_open":
        return "fail_open", "no_identity"
    if role == "holder":
        if info.get("roll") and sync:
            baton.claim(c.sdir, c.sid, c.identity, c.chain, c.now)
        return "holder", None
    if role in ("holder_sub", "b"):
        return role, None
    if not claim:
        return "spectator", "not_me"
    try:
        result, new_role = baton.claim(c.sdir, c.sid, c.identity, c.chain, c.now)
    except Exception as exc:  # 状态目录不可写等：钩子自己出错 ⇒ 放行（规格:79）；登记册没有被写
        return "fail_open", "claim_exception:" + type(exc).__name__
    status, fresh = baton.read(c.sdir)
    if status == "ok":
        c.state = fresh
    if result in ("fail_open", "error"):
        return "fail_open", "claim_" + result
    if result in ("claimed", "rolled", "b_online", "ok"):
        return new_role, (None if result == "ok" else result)
    # denied / unknown / blocked / busy：本次不得写
    return ("blocked" if result == "blocked" else "spectator"), "claim_" + result


def register_b_online(c):
    """B 上线接线（设计:93 行、派单包 §1）：会话号 == handoff.b.uuid 的会话在 SessionStart 做一次 b 槽 CAS（槽空 / 旧进程确已死才写；活的他进程不换）。
    只写 b 槽（pid、lstart、online_ms），不动 holder；失败 / 不是 B 都不影响后面的 resolve_role。"""
    pid, lstart = c.identity()
    if baton.b_online(c.sdir, c.sid, pid, lstart, c.now):
        log_event(c, "b", "b_online")


def check_heartbeat(c):
    """auto 每次调用读心跳（规格:72，设计:64 行）：停超 heartbeat_stale_seconds ⇒ 响铃一次、同段停跳不重复；心跳恢复才清标记。
    文件缺失按 mode.since_ms 起算；时钟回拨（年龄为负）或跳变 > 1 h ⇒ 从当前时刻重算、不响铃（设计:7）。"""
    limit_ms = int(c.cfg["heartbeat_stale_seconds"] * 1000)
    heartbeat = sessions.read_heartbeat(c.sdir)
    base = heartbeat if heartbeat is not None else c.since_ms
    age = c.now - base
    stale_marker = sessions.run_path(c.sdir, "hb_stale")
    rebase_path = sessions.run_path(c.sdir, "hb_rebase")
    if -HEARTBEAT_SKEW_MS <= age <= limit_ms:
        try:  # 心跳正常 ⇒ 上一次回拨 / 跳变留下的重算起点作废，否则下一次竞态会从它起算「停跳 N 秒」误报（H107）
            os.remove(rebase_path)
        except OSError:
            pass
        resolved = sessions.clear_marker(stale_marker)
        if resolved:  # 心跳恢复即解除（设计:64 行）：把对应的 pending 心跳 notice 标为已解除（F5）
            sessions.append_notice(c.sdir, {"type": "resolve", "id": resolved, "t_ms": c.now})
        return
    if age < 0 or age > HEARTBEAT_JUMP_MS:
        try:
            with open(rebase_path, encoding="utf-8") as stream:
                rebase = int(stream.read().strip())
        except (OSError, ValueError):
            rebase = None
        if rebase is None or rebase > c.now or c.now - rebase > HEARTBEAT_JUMP_MS:
            os.makedirs(os.path.dirname(rebase_path), exist_ok=True)
            util.atomic_write_text(rebase_path, str(c.now))
            return
        age = c.now - rebase
        if age <= limit_ms:
            return
    if os.path.exists(stale_marker):
        return
    notice_id = sessions.new_notice_id(c.now)
    fields = dict(
        what="handoffd 守护进程的心跳已停约 %d 秒（超过 %d 秒）。" % (age // 1000, limit_ms // 1000),
        why="守护进程可能卡死或已退出；它不在时自动接力不会发生。",
        ask="1) 重启 daemon（handoffd on） 2) 切回 manual（handoffd off）", recommend=1,
        if_no_reply="自动接力不会触发；主控上下文满了也不会有人替你发起交接。",
    )
    if not c.ring("problem", fields):
        return
    sessions.create_marker(stale_marker, notice_id)  # 响铃子进程已成功启动才落标记（F4）
    sessions.append_notice(c.sdir, {
        "type": "notice", "id": notice_id, "t_ms": c.now, "sid": c.sid, "cat": "heartbeat", "ring": True,
        "status": "pending", "text": notify.format_notice(**fields),
        "options": ["重启 daemon（handoffd on）", "切回 manual（handoffd off）"],
    })


def holder_summary(state):
    """旁观会话看不到 `handoffd status`（没有 Bash），状态由这句话给出：谁是主控、有没有交接。"""
    if not state:
        return ""
    holder, handoff = state.get("holder"), state.get("handoff")
    parts = []
    if holder:
        parts.append("主控会话 %s（pid %s）" % (holder["sid"][:8], holder["pid"]))
    if handoff:
        parts.append("交接进行中（%s 阶段 %s，接手方 %s）" % (handoff.get("id"), handoff.get("phase"), handoff["b"]["uuid"][:8]))
    return "；".join(parts)


def b_role_text(state):
    """B 在 SessionStart 得到的角色说明（派单包 §1）：自己是接棒者、A 的会话名与 sid、等开场指令、复核结论回 A、完成后 sign in。"""
    handoff = (state or {}).get("handoff") or {}
    a = handoff.get("a") or {}
    holder = (state or {}).get("holder") or {}
    a_sid = a.get("sid") or holder.get("sid") or "?"
    a_name = a.get("name")
    return ("你是交接的接手方（B），不是当前主控：A 的会话名 %s，sid %s。你起来后先空闲等待——daemon 会在 A 封存 kit 后给你发一条开场指令"
            "（没有 [handoffd] 前缀的消息），收到后照做：读 kit 复核，复核结论用 SendMessage 发给 A（可来回讨论）；"
            "复核完成后运行 `handoffd sign in` 签字，然后等 daemon 的 BATON 消息，收到前不要自行行动。"
            "交接完成前你只能读、SendMessage，以及 handoffd 的 sign in / status / notify / reply；不能 off、不能改文件。"
            % (a_name or "（未登记）", a_sid))


def mode_notice(role, state=None):
    """SessionStart 注入（规格:95）：模式提示 + 角色说明。"""
    base = "handoffd：auto 模式。你处在 handoffd auto 模式，交接按接力规则，细节见 README。"
    if role == "holder":
        return base + "你是本项目的主控（holder）。"
    if role == "b":
        return base + b_role_text(state)
    if role == "spectator":
        return (base + "本项目已有主控会话在运行：你是只读旁观会话，只能读、答，不能改文件、不能执行 Bash、不参与交接。当前状态：%s。"
                "要看完整状态请让主控或 Owner 运行 handoffd status。" % (holder_summary(state) or "见 handoffd status"))
    if role == "blocked":
        return base + "本项目正在交接且原主控已退出；你不是接手方，不会被认作主控。若要放弃交接，先运行 handoffd off。"
    return None


def auto(c):
    """auto 分派。每个事件都是「先算决定（claim 是决定的一部分），再做副作用（safe）」；心跳检查放在最后，且自身异常不影响决定。"""
    event = c.event
    decision = None
    if event == "SessionStart":
        safe(c, "b_online", lambda: register_b_online(c))
        role, note = resolve_role(c, claim=True, sync=True)
        text = mode_notice(role, c.state)
        decision = context("SessionStart", text) if text else None
        safe(c, "log", lambda: log_event(c, role, note, source=c.inp.get("source")))
        safe(c, "session", lambda: sessions.update_session(
            c.sdir, c.sid, lambda record: record.update(last_event_ms=c.now, role=role, started_ms=c.now)))
    elif event == "UserPromptSubmit":
        safe(c, "b_online", lambda: register_b_online(c))   # OPEN 到达即触发：SessionStart 那次没登上也在这里补（A11）
        role, note = resolve_role(c, claim=True, sync=True)
        if role == "blocked":
            decision = {"decision": "block", "reason": "⛔ 本项目正在交接，但原主控已退出；你不是接手方，不会被认作主控。"
                                                       "请先确认放弃交接：在终端运行 `handoffd off`，再重新开始。"}
        safe(c, "clear_wait", lambda: clear_wait(c))
        safe(c, "log", lambda: log_event(c, role, note))
        safe(c, "session", lambda: sessions.update_session(
            c.sdir, c.sid, lambda record: record.update(last_event_ms=c.now, last_prompt_ms=c.now, role=role)))
    elif event == "PreToolUse":
        decision = auto_pre_tool(c)
    elif event in ("PermissionRequest", "Elicitation"):
        safe(c, "log", lambda: log_event(c, None, waiting_reason(c)))
        safe(c, "ring", lambda: ring_once(c, waiting_reason(c)))
    elif event == "Notification":
        kind = c.inp.get("notification_type")
        safe(c, "log", lambda: log_event(c, None, kind))
        if kind in WAIT_NOTIFICATIONS:
            safe(c, "ring", lambda: ring_once(c, waiting_reason(c)))
        if kind == "quota_auto_resume_fired":
            safe(c, "session", lambda: sessions.update_session(
                c.sdir, c.sid, lambda record: record.update(quota_wait_ms=None, last_event_ms=c.now)))
        elif kind == "quota_auto_resume_disabled":
            safe(c, "session", lambda: sessions.update_session(
                c.sdir, c.sid, lambda record: record.update(quota_wait_ms=None, quota_disabled_ms=c.now, last_event_ms=c.now)))
    elif event in ("PostToolUse", "PostToolUseFailure"):
        role, _note = resolve_role(c, claim=False)
        safe(c, "clear_wait", lambda: clear_wait(c))
        safe(c, "log", lambda: log_event(c, role, c.tool()))
        safe(c, "session", lambda: sessions.update_session(c.sdir, c.sid, lambda record: record.update(last_event_ms=c.now)))
        if event == "PostToolUse":
            decision = safe(c, "post_tool", lambda: post_tool(c, role))
    elif event in ("Stop", "StopFailure"):
        safe(c, "clear_wait", lambda: clear_wait(c))
        safe(c, "stop", lambda: stop(c))
    elif event == "SessionEnd":
        reason = c.inp.get("reason")
        safe(c, "log", lambda: log_event(c, None, reason))
        if reason not in ("clear", "resume"):
            safe(c, "session", lambda: sessions.update_session(
                c.sdir, c.sid, lambda record: record.update(ended_ms=c.now, end_reason=reason, last_event_ms=c.now)))
    safe(c, "heartbeat", lambda: check_heartbeat(c))
    return decision


def over_first_threshold(c):
    """本会话上下文是否已到第一档（默认 65%）。取不到百分比 ⇒ 不拦。"""
    try:
        pct, _source, _detail = ctx_module.context_percent(c.sdir, c.sid, c.inp.get("transcript_path"))
    except Exception:
        return False
    return pct is not None and pct >= min(c.cfg["nudge_thresholds"])


def auto_pre_tool(c):
    """PreToolUse 的全部放行路径（逐行清单见 claudedocs/review/handoffd_S1_R3_allow_paths_20261001.md）：
    (a) AskUserQuestion / 只读工具：不看角色；(b) holder / holder_sub / b：登记册三元组相等或 guard 内认领成功；(c) fail_open（规格:79、K2）。
    其余一律拒绝。注（K2）：认领失败 / 对方判活不明 ⇒ 拒写，与「本进程身份取不到 ⇒ 放行」方向不同是有意的：
    前者是对方死活 / 谁持棒不明，宁可不给第二个写者；后者是本会话自己的身份取不到，按规格:79 fail-open。"""
    tool, tool_input = c.tool(), c.tool_input()
    if tool == "AskUserQuestion":
        safe(c, "log", lambda: log_event(c, None, tool))
        safe(c, "ring", lambda: ring_once(c, waiting_reason(c)))
        return None
    safe(c, "clear_wait", lambda: clear_wait(c))
    if tool == "Bash" and policy.invokes_owner_only(tool_input.get("command")):
        safe(c, "log", lambda: log_event(c, None, "deny_owner_only"))
        return deny(owner_only_reason())
    if tool in policy.READ_ONLY_TOOLS:  # 放行依据 1：只读工具，不认领、不看角色
        safe(c, "log", lambda: log_event(c, None, tool))
        return None
    role, note = resolve_role(c, claim=True)  # 写工具：三元组相等，或 guard 内槽位 CAS 成功
    over = policy.is_new_long_task(tool, tool_input) and role in ("holder", "holder_sub") and over_first_threshold(c)
    allowed, reason = policy.decide_tool(role, tool, tool_input, over, c.cfg["b_verify_commands"])
    safe(c, "log", lambda: log_event(c, role, tool if allowed else "deny:" + tool, **({"note": note} if note else {})))
    if allowed:
        return None
    summary = holder_summary(c.state) if role in ("spectator", "blocked") else ""
    return deny(reason + ("当前状态：%s。" % summary if summary else ""))


def post_tool(c, role):
    tool = c.tool()
    if role == "b" and tool == "SendMessage":
        baton.bump_qa(c.sdir, c.sid)
        return None
    if role != "holder" or c.inp.get("agent_id"):
        return None
    out = io.StringIO()

    def on_fire(level, pct, token):
        if level >= 1:
            sessions.update_session(c.sdir, c.sid, lambda record: record.update(
                request_pending={"reason": "ctx76", "ms": c.now, "pct": pct}))

    hook_nudge.process(dict(c.inp, hook_event_name="PostToolUse"), out, c.sdir, c.cfg["nudge_thresholds"], on_fire)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def stop(c):
    inp = c.inp

    def count(value):
        if isinstance(value, list):
            return len(value)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    if c.event == "Stop":
        tasks, crons = count(inp.get("background_tasks")), count(inp.get("session_crons"))
        log_event(c, None, "tasks=%s crons=%s" % (tasks, crons))
        pid, lstart = c.identity()

        def record_stop(record):
            # S2b：正常 Stop ＝ 明确恢复，一并清额度相关标记（quota_disabled_ms 派单包要求；quota_wait_ms 为 S2b 自定：否则 Stop 后仍被当作在等额度）
            record.update(last_event_ms=c.now, last_stop_ms=c.now, bg_tasks=tasks, bg_crons=crons, last_error=None,
                          quota_wait_ms=None, quota_disabled_ms=None)

        if pid and lstart:   # K2：每进程一个单调 Stop 序号，存在与会话号无关的进程文件里（设计:5 的「A 签后出现 Stop」按它判）
            sessions.record_proc_stop(c.sdir, pid, lstart, tasks, crons, c.now)
        sessions.update_session(c.sdir, c.sid, record_stop)
        return None
    error = inp.get("error") if isinstance(inp.get("error"), str) else "unknown"
    log_event(c, None, error)
    if error in QUOTA_ERRORS:
        sessions.update_session(c.sdir, c.sid, lambda record: record.update(
            last_event_ms=c.now, quota_wait_ms=c.now, last_error={"type": error, "ms": c.now}))
        return None
    if error in TEMP_ERRORS:
        sessions.update_session(c.sdir, c.sid, lambda record: record.update(
            last_event_ms=c.now, last_error={"type": error, "ms": c.now}))
        return None
    sessions.update_session(c.sdir, c.sid, lambda record: record.update(
        last_event_ms=c.now, last_error={"type": error, "ms": c.now}))
    fields = dict(
        what="会话 %s 遇到需要人处理的 API 错误：%s。" % ((c.sid or "?")[:8], error),
        why="这类错误（认证 / 账单 / 账号 / 模型 / 请求）机器修不了，会话已停住。",
        ask="1) 去该窗口处理", recommend=1,
        if_no_reply="该会话会一直停着，不会自己恢复。",
    )
    notice_id = sessions.new_notice_id(c.now)
    sessions.append_notice(c.sdir, {
        "type": "notice", "id": notice_id, "t_ms": c.now, "sid": c.sid, "cat": "api_error", "ring": True,
        "status": "pending", "text": notify.format_notice(**fields), "options": ["去该窗口处理"],
    })
    c.ring("problem", fields)
    return None


# ---- 入口 ----

def handle(event, inp, identity, chain, ring_fn, info):
    """解析项目、状态目录与模式，分派到 manual / auto。info 用来把 sdir / mode 带给异常兜底。"""
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or inp.get("cwd") or os.getcwd()
    if not isinstance(proj, str):
        raise TypeError("项目目录不是字符串")
    cfg, _config_error = config.load_handoffd(proj)
    deadline = procs.Deadline(2.0)
    sdir = None
    if cfg["state_dir_set"]:
        sdir = cfg["state_dir"]
    else:
        status, path = lockcore.state_dir(proj, deadline)
        if status == "ok":
            sdir = path
    mode, since_ms = sessions.read_mode(sdir) if sdir else ("manual", 0)
    info.update(sdir=sdir, mode=mode)
    c = Ctx(event, inp, proj, cfg, sdir, mode, since_ms, util.now_ms(), deadline, identity, chain,
            lambda *args: ring_fn(proj, *args))
    return auto(c) if mode == "auto" else manual(c)


def default_chain_and_identity(identity, chain):
    """只给了 identity（测试）⇒ chain 取 [identity]；都没给 ⇒ 真实祖先链，identity = 最近的 claude 进程。"""
    if chain is None and identity is None:
        cache = {}

        def real_chain(deadline):
            if "chain" not in cache:
                cache["chain"] = lockcore.claude_chain(deadline)
            return cache["chain"]

        def real_identity(deadline):
            found = real_chain(deadline)
            return found[0] if found else (None, "")
        return real_identity, real_chain
    if chain is None:
        return identity, lambda deadline: [identity(deadline)]
    if identity is None:
        return (lambda deadline: (chain(deadline) or [(None, "")])[0]), chain
    return identity, chain


def main(argv=None, stdin=None, stdout=None, identity=None, chain=None, ring_fn=None):
    """钩子入口。返回值恒为 0：任何异常（含输入坏、状态目录读不出、响铃失败）一律放行，不输出阻断。
    identity(deadline)→(pid, lstart)、chain(deadline)→[(pid, lstart)]、ring_fn 只供测试替换。"""
    argv = sys.argv[1:] if argv is None else argv
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if len(argv) != 1:
        return 0
    info = {}
    try:
        inp = json.load(stdin)
        if not isinstance(inp, dict):
            return 0
        identity, chain = default_chain_and_identity(identity, chain)
        result = handle(argv[0], inp, identity, chain, ring_fn or sessions.spawn_ring, info)
        if result:
            stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            stdout.flush()
    except Exception as exc:
        log_exception(argv[0], info, exc)
    return 0


def log_exception(event, info, exc):
    """auto 下把钩子内部异常记一笔（尽力而为）；manual 下什么也不写。"""
    try:
        if info.get("mode") == "auto" and info.get("sdir"):
            sessions.append_event(info["sdir"], {"t_ms": util.now_ms(), "event": event, "role": None,
                                                 "summary": "hook_exception", "error": repr(exc)})
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
