# 测试 H83–H100：handoffd S2b「看护」（假世界 + 可控时钟；不起 claude、不开窗、不发信号、不向真实会话投递、不出声——msg / kill / 判活 / agents / 时钟全部注入）。
#   H83 :41 在途询问 ASK（30 min 首问与重问、普通钩子事件不重置、平时空闲不问、目标核对不过不投）
#   H84 :42 / :58 回复核对（owner 立即响铃；task / peer / quota 各核得上与核不上；连 2 次核不上响铃；核得上清零连续计数）
#   H85 :47① ASK 5 min 无回复响铃（含投递失败）；OPEN 持续不可达 5 min 响铃（S2a R1 整合账挂起项）
#   H86 :40 API 临时错误退避 NUDGE 3→6→12→24→48→60→60（NUDGE 后 UserPromptSubmit / 再 StopFailure 不清零，正常 Stop 清零，永不响铃）
#   H87 :39 撞额度（rate_limit 不问不 NUDGE 不响铃；disabled / 60 min 无信号转入退避、无铃；正常 Stop 清 quota_disabled_ms）
#   H88 :59 B 问答超上限只响一次    H89 :47② 非交接时主控没了（无 SessionEnd 响一次、有 SessionEnd 不响）    H90 :65 复核轮次账 round≥3 ∧ p0>0 同（产物, 轮次）一次
#   H94–H100 R1 整合账 F1–F7 回归（off→退出→on 不响 / 额度等待不被普通事件结束 / 暂停期不纠正 / b_died 不叠铃 / OPEN 先于 ASK / 5 min 门核 busy 与 live）
#   H93 Esc 打断（Stop 不触发）：仅因 prompt>stop 在途时先核 agents busy
#   H108–H113 reply task 可带预计时长（15–180 分钟）与询问间隔可配：59/60 分钟、之后回到 idle_ask_minutes、非法时长报错不写状态、非 task 带时长报错、idle_ask_minutes=90、核不上时长不生效
#   H114–H116 R1 裁定回归：时长自回复时刻起算（daemon 停顿不拖后）、核不上清旧时长、持久化时长核 180 分钟上限
#   H91 :47⑥③ tick 异常 / 登记册读不出连 3 轮响一次，中间一轮正常重新计数    H92 设计:7 时钟回拨 / 跳变 > 1 h 重算、不响铃
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import require, run_cases  # noqa: E402
from swap_helpers import OPENING, b_comes_online, full_handoff_until_sealed, scenario, start_handoff  # noqa: E402
from cdm import review, util  # noqa: E402


def new_a(sc, sid="sA", prompt=True, **spawn_kw):
    """A 起来并认领 holder；prompt=True ⇒ 提交了一轮提示、还没 Stop（在途）。"""
    a = sc.spawn(sid, **spawn_kw)
    sc.hook("SessionStart", a, source="startup")
    if prompt:
        sc.hook("UserPromptSubmit", a, prompt="go")
    sc.world.agents = busy_list(a)      # 默认：agents 显示它在 busy（真的在跑）；Esc 打断的用例（H93）另设
    return a


def busy_list(rec):
    return [{"pid": rec["pid"], "status": "busy"}]


def idle_list(rec):
    return [{"pid": rec["pid"], "status": "idle"}]


def msgs(sc, rec, tag):
    return [c for c in sc.world.messages_to(rec) if c.startswith("[handoffd] " + tag)]


def run(sc, dm, minutes):
    for _ in range(minutes):
        sc.tick(dm, 60)


def touch(sc, rec):
    """一个普通钩子事件（PostToolUse）：只刷新 last_event_ms，不该重置任何看护计时。"""
    sc.hook("PostToolUse", rec, tool_name="Read", tool_input={})


def reply(sc, rec, kind):
    sc.clock.advance(1)       # 回复必须晚于 ASK 发出的那一毫秒才算
    code, out, err = sc.cli(["reply", kind], rec)
    require(code == 0, "reply %s 失败：%r %s" % (kind, code, err))
    return out


def reprompt(sc, rec):
    sc.clock.advance(1)       # 提示必须晚于上一次 Stop 才算「本轮未 Stop」
    sc.hook("UserPromptSubmit", rec, prompt="again")


def stop(sc, rec, tasks=()):
    sc.hook("Stop", rec, background_tasks=list(tasks), session_crons=[])


def first_ask(sc, dm, a):
    """从在途起算 30 min 得到第一条 ASK。"""
    sc.tick(dm)
    run(sc, dm, 30)
    require(len(msgs(sc, a, "ASK")) == 1, "30 min 后应恰有 1 条 ASK：%r" % msgs(sc, a, "ASK"))


def notice_kinds(sc):
    return [item.get("kind") for item in sc.notices()]    # 钩子写的 notice（心跳等）没有 kind


def kinds_like(sc, prefix):
    return [item["kind"] for item in sc.notices() if str(item.get("kind")).startswith(prefix)]


def watch_session(sc, sid):
    status, value = util.read_json(os.path.join(sc.sdir, "daemon_state.json"))
    return (value.get("watch") or {}).get("sessions", {}).get(sid, {}) if status == "ok" else {}


def case_h83():
    """H83：:41 ASK——在途 30 min 首问（末句写明 handoffd reply task|peer|quota|owner）、答复核对后 30 min 重问、普通钩子事件不重置计时；平时（非交接）空闲不问；目标核对不过不投。"""
    details = []
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 10)
        touch(sc, a)
        run(sc, dm, 10)
        touch(sc, a)
        run(sc, dm, 9)
        require(not msgs(sc, a, "ASK"), "29 min 不该问")
        run(sc, dm, 1)
        asks = msgs(sc, a, "ASK")
        require(len(asks) == 1, "普通钩子事件不得重置计时：30 min 应首问一次（%d）" % len(asks))
        require("handoffd reply task|peer|quota|owner" in asks[0].split("。")[-2], "末句要写明回复命令：%r" % asks[0])
        stop(sc, a, tasks=["t1"])
        reply(sc, a, "task")
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "task 核得上：不追问、不响铃")
        run(sc, dm, 10)
        touch(sc, a)
        run(sc, dm, 10)
        touch(sc, a)
        run(sc, dm, 9)
        require(len(msgs(sc, a, "ASK")) == 1, "核得上后 29 min 不重问")
        run(sc, dm, 1)
        require(len(msgs(sc, a, "ASK")) == 2 and sc.ring_count() == 0, "核得上后满 30 min 重问一次（共 2 条），全程不响铃")
        details.append("在途 30 min 首问、普通钩子事件（PostToolUse）不重置、核得上后 30 min 重问；末句带 `handoffd reply task|peer|quota|owner`；响铃 0")
    with scenario() as sc:
        a = new_a(sc)
        stop(sc, a)       # 本轮已 Stop、数组空、没有交接 ＝ 平时空闲
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 120)
        require(not sc.world.messages and sc.ring_count() == 0, "平时空闲不问不响：%r" % sc.world.messages)
        details.append("平时（非交接）空闲 120 min：不问、不响铃（:43）")
    with scenario() as sc:
        a = new_a(sc, cwd="/nonexistent/elsewhere")
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 31)
        require(not sc.world.messages and sc.ring_count() == 0, "cwd 不在项目内：不投")
        require(any(e.get("event") == "daemon:ask_target_refused" for e in sc.events()), "目标核对不过要记 events")
        details.append("目标核对（三元组 + cwd 在项目内）不过：不投、记 events（ask_target_refused）")
    return details


def case_h84():
    """H84：:42 / :58 回复核对——owner 立即响铃；task（Stop 数组 / agents busy）、peer（交接中对方 live 且 30 min 内有事件）、quota（quota_wait 在）各核得上与核不上；
    核不上先纠正一次、连续 2 次响铃；核得上清零连续计数。"""
    details = []
    with scenario() as sc:                         # owner
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        reply(sc, a, "owner")
        sc.tick(dm)
        require(sc.ring_count() == 1 and kinds_like(sc, "reply_owner"), "owner 立即响铃一次：%r" % notice_kinds(sc))
        run(sc, dm, 10)
        require(sc.ring_count() == 1, "同一条回复不重复响")
        details.append("owner：立即响铃（选项 1 去该窗口处理），一次")
    agents = []
    with scenario() as sc:                         # task：Stop 数组非空
        a = new_a(sc)
        agents[:] = busy_list(a)
        dm = sc.daemon(agents_fn=lambda: agents)
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply(sc, a, "task")
        sc.tick(dm)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 1, "task 核得上（Stop 数组非空）：无声无追问")
    with scenario() as sc:                         # task：agents busy
        a = new_a(sc)
        stop(sc, a)
        reprompt(sc, a)
        agents[:] = busy_list(a)
        dm = sc.daemon(agents_fn=lambda: agents)
        first_ask(sc, dm, a)
        reply(sc, a, "task")
        sc.tick(dm)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 1, "task 核得上（agents busy）")
    details.append("task 核得上两路：最近 Stop 数组非空 / agents status==busy")
    with scenario() as sc:                         # task 核不上 ×2 ⇒ 响铃；第 1 次先纠正
        a = new_a(sc)
        stop(sc, a)
        reprompt(sc, a)
        agents[:] = busy_list(a)
        dm = sc.daemon(agents_fn=lambda: agents)
        first_ask(sc, dm, a)
        agents[:] = []                              # ASK 之后它其实没在跑：task 回复核不上
        reply(sc, a, "task")
        sc.tick(dm)
        asks = msgs(sc, a, "ASK")
        require(len(asks) == 2 and "核对不上" in asks[1] and sc.ring_count() == 0, "第 1 次核不上：投纠正 ASK、不响铃：%r" % asks)
        reply(sc, a, "task")
        sc.tick(dm)
        require(sc.ring_count() == 1 and kinds_like(sc, "reply_mismatch") and len(msgs(sc, a, "ASK")) == 2, "连续 2 次核不上才响铃，且不再追问")
        run(sc, dm, 3)
        require(sc.ring_count() == 1, "同一条回复不重复响")
        details.append("task 核不上：第 1 次纠正 ASK（不响），连续第 2 次响铃一次（选项 1 去该窗口看 2 放弃交接）")
    with scenario() as sc:                         # 核得上清零连续计数
        a = new_a(sc)
        stop(sc, a)
        reprompt(sc, a)
        agents[:] = busy_list(a)
        dm = sc.daemon(agents_fn=lambda: agents)
        first_ask(sc, dm, a)
        agents[:] = []                              # ASK 之后它其实没在跑：task 回复核不上
        reply(sc, a, "task")
        sc.tick(dm)                                 # 核不上 #1
        stop(sc, a, tasks=["t1"])
        reply(sc, a, "task")
        sc.tick(dm)                                 # 核得上：清零
        require(sc.ring_count() == 0, "此处不该响")
        stop(sc, a)
        reprompt(sc, a)
        agents[:] = busy_list(a)
        run(sc, dm, 30)
        n = len(msgs(sc, a, "ASK"))
        agents[:] = []
        reply(sc, a, "task")
        sc.tick(dm)                                 # 核不上：若没清零这是第 2 次 ⇒ 会响
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == n + 1, "核得上清零计数：再核不上只算第 1 次（纠正、不响）")
        details.append("核得上清零连续计数：之后再核不上只算第 1 次，不响")
    with scenario() as sc:                         # peer：交接中对方 live 且 30 min 内有事件
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, _hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        run(sc, dm, 30)
        require(msgs(sc, a, "ASK"), "交接中 A 应已被问")
        touch(sc, b)
        reply(sc, a, "peer")
        sc.tick(dm)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 1, "peer 核得上（B live 且刚有事件）：无声无追问：%r" % notice_kinds(sc))
        details.append("peer 核得上：交接中对方 live 且 30 min 内有事件")
    with scenario() as sc:                         # peer 核不上（没有交接）
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        reply(sc, a, "peer")
        sc.tick(dm)
        asks = msgs(sc, a, "ASK")
        require(len(asks) == 2 and "核对不上" in asks[1] and sc.ring_count() == 0, "peer 核不上（无交接）：纠正 ASK")
        details.append("peer 核不上：无交接 ⇒ 纠正 ASK")
    with scenario() as sc:                         # quota：核得上（quota_wait 在）
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        sc.clock.advance(1)
        sc.hook("StopFailure", a, error="rate_limit")
        reply(sc, a, "quota")
        sc.tick(dm)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 1, "quota 核得上：无声无追问")
    with scenario() as sc:                         # quota：核不上
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        reply(sc, a, "quota")
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 2 and sc.ring_count() == 0, "quota 核不上（没有 rate_limit）：纠正 ASK")
        details.append("quota：quota_wait 在 ⇒ 核得上；不在 ⇒ 纠正 ASK")
    return details


def case_h85():
    """H85：:47① ASK（含投递失败）5 min 无回复响铃一次；S2a R1 挂起项：B 已上线、kit 已封存后 OPEN 持续不可达 5 min 响铃一次。"""
    details = []
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        sc.world.agents = idle_list(a)      # 5 min 到点时 agents 不在 busy（R1-F6：busy 才算「还在跑」）
        run(sc, dm, 4)
        require(sc.ring_count() == 0, "4 min 不响")
        run(sc, dm, 1)
        require(sc.ring_count() == 1 and kinds_like(sc, "ask_noreply"), "5 min 无回复响一次：%r" % notice_kinds(sc))
        run(sc, dm, 20)
        require(sc.ring_count() == 1, "同一条 ASK 只响一次")
        details.append("ASK 5 min 无回复：恰响 1 次（4 min 不响；之后同一条 ASK 不再响）")
    with scenario() as sc:
        a = new_a(sc)
        sc.world.msg_fail = True
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 30)
        sc.world.agents = idle_list(a)
        require(not sc.world.messages and any(e.get("event") == "daemon:ask" and e.get("delivered") is False for e in sc.events()), "ASK 投递失败要记 events")
        run(sc, dm, 4)
        require(sc.ring_count() == 0, "投递失败也按 5 min 门")
        run(sc, dm, 1)
        require(sc.ring_count() == 1, "ASK 投递失败 5 min 后响一次")
        details.append("ASK 投递失败（连不上收件箱）：同样 5 min 后响铃一次")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.world.msg_fail = True
        sc.tick(dm)
        run(sc, dm, 3)
        require(sc.ring_count() == 0, "OPEN 不可达不满 5 min 不响")
        run(sc, dm, 3)
        require(sc.ring_count() == 1 and kinds_like(sc, "open_unreachable"), "OPEN 持续不可达 ≥ 5 min 响一次：%r" % notice_kinds(sc))
        run(sc, dm, 10)
        require(sc.ring_count() == 1, "只响一次")
        details.append("B 已上线 ∧ kit 已封存 ∧ OPEN 持续投递失败：满 5 min 恰响 1 次（选项 1 去 B 的窗口看 2 放弃交接）")
    return details


def case_h86():
    """H86：:40 API 临时错误退避——NUDGE「继续」按 3→6→12→24→48→60→60 分钟；NUDGE 引起的 UserPromptSubmit、NUDGE 后又来的 StopFailure 都不清零；正常 Stop 清零；永不响铃、期间不问。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        sc.hook("StopFailure", a, error="server_error")
        sent = []
        for want in (3, 6, 12, 24, 48, 60, 60):
            before = len(msgs(sc, a, "NUDGE"))
            for k in range(1, want + 1):
                sc.tick(dm, 60)
                got = len(msgs(sc, a, "NUDGE")) - before
                require(got == (1 if k == want else 0), "第 %d 分钟：NUDGE 应%s（间隔 %d）" % (k, "发出" if k == want else "未发", want))
            sent.append(want)
            sc.clock.advance(1)
            sc.hook("UserPromptSubmit", a, prompt="继续")      # NUDGE 引起的 UserPromptSubmit
            sc.clock.advance(1)
            sc.hook("StopFailure", a, error="server_error")    # NUDGE 之后又来的 StopFailure
        require(all(m == "[handoffd] NUDGE 继续" for m in msgs(sc, a, "NUDGE")), "NUDGE 内容")
        require(not msgs(sc, a, "ASK") and sc.ring_count() == 0, "退避期间不问、永不响铃")
        require(watch_session(sc, "sA").get("bo_step") == 7, "步数应累计到 7：%r" % watch_session(sc, "sA"))
        stop(sc, a)                                             # 正常 Stop ＝ 明确恢复
        sc.tick(dm)
        require("bo_step" not in watch_session(sc, "sA"), "正常 Stop 清零")
        sc.hook("StopFailure", a, error="overloaded")
        before = len(msgs(sc, a, "NUDGE"))
        run(sc, dm, 2)
        require(len(msgs(sc, a, "NUDGE")) == before, "清零后 2 min 不发")
        run(sc, dm, 1)
        require(len(msgs(sc, a, "NUDGE")) == before + 1 and sc.ring_count() == 0, "清零后又从 3 min 起")
    return ["NUDGE 间隔 %r 分钟（逐分钟断言：未到不发、到点恰发一条）；NUDGE 后 UserPromptSubmit + 再 StopFailure 步数继续累计（bo_step=7）；正常 Stop 清零后又从 3 min；全程不问、响铃 0" % sent]


def case_h87():
    """H87：:39 撞额度——rate_limit 不问、不 NUDGE、不响铃；收到 quota_auto_resume_disabled 或 60 min 无信号转入退避 NUDGE（无铃）；正常 Stop 清 quota_disabled_ms。"""
    details = []
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        sc.hook("StopFailure", a, error="rate_limit")
        run(sc, dm, 59)
        require(not sc.world.messages and sc.ring_count() == 0, "rate_limit 59 min：不问不 NUDGE 不响：%r" % sc.world.messages)
        for k in range(60, 63):
            sc.tick(dm, 60)
            require(not msgs(sc, a, "NUDGE"), "转入退避后还要等 3 min（第 %d 分钟）" % k)
        sc.tick(dm, 60)
        require(len(msgs(sc, a, "NUDGE")) == 1 and not msgs(sc, a, "ASK") and sc.ring_count() == 0, "60 min 无信号转入退避：第 63 分钟发出第一条 NUDGE，无铃")
        details.append("rate_limit：前 59 min 不问不 NUDGE；60 min 无 resume 无新事件 ⇒ 转入退避，3 min 后第一条 NUDGE，无铃")
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        sc.hook("StopFailure", a, error="rate_limit")
        sc.clock.advance(1)
        sc.hook("Notification", a, notification_type="quota_auto_resume_disabled")
        n = 0
        while not msgs(sc, a, "NUDGE") and n < 10:
            sc.tick(dm, 60)
            n += 1
        require(3 <= n <= 5 and sc.ring_count() == 0 and not msgs(sc, a, "ASK"), "disabled ⇒ 转入退避：约 3 min 后 NUDGE（%d），无铃" % n)
        stop(sc, a)
        sc.tick(dm)
        session = json.load(open(os.path.join(sc.sdir, "sessions", "sA.json"), encoding="utf-8"))
        require(session.get("quota_disabled_ms") is None and "xfer" not in watch_session(sc, "sA"), "正常 Stop 清 quota_disabled_ms 与转入状态：%r" % session)
        details.append("quota_auto_resume_disabled ⇒ 转入退避 NUDGE（约 %d min 后），无铃；正常 Stop 清 quota_disabled_ms" % n)
    return details


def case_h88():
    """H88：:59 B 问答超上限（qa_count > qa_limit=12）响铃一次；≤ 上限不响。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        for _ in range(12):
            sc.hook("PostToolUse", b, tool_name="SendMessage", tool_input={})
        require(sc.handoff()["qa_count"] == 12, "qa_count=%r" % sc.handoff()["qa_count"])
        run(sc, dm, 2)
        require(sc.ring_count() == 0, "=12 条不响")
        sc.hook("PostToolUse", b, tool_name="SendMessage", tool_input={})
        run(sc, dm, 2)
        require(sc.ring_count() == 1 and kinds_like(sc, "qa_over"), "13 条响一次：%r" % notice_kinds(sc))
        for _ in range(3):
            sc.hook("PostToolUse", b, tool_name="SendMessage", tool_input={})
        run(sc, dm, 5)
        require(sc.ring_count() == 1, "每次交接只响一次")
    return ["qa_count 12 不响；13 响 1 次（选项 1 放弃交接 2 去两个窗口看，推荐 2）；继续增长、多轮 tick 仍只 1 次"]


def case_h89():
    """H89：:47② 非交接时主控没了——无 SessionEnd 响一次（每个 holder 三元组一次）；有 SessionEnd 的正常退出不响；交接中的崩溃不重复（S2a 的 :56/:57/:64 管）。"""
    details = []
    with scenario() as sc:
        a = new_a(sc, prompt=False)
        dm = sc.daemon()
        sc.tick(dm)
        sc.world.die(a)
        run(sc, dm, 3)
        require(sc.ring_count() == 1 and kinds_like(sc, "holder_gone"), "无 SessionEnd：响一次：%r" % notice_kinds(sc))
        run(sc, dm, 10)
        require(sc.ring_count() == 1, "只响一次")
        details.append("holder 死 ∧ 无 SessionEnd ∧ 无交接：响 1 次（选项 1 无需处理 2 去看看，推荐 1）")
    with scenario() as sc:
        a = new_a(sc, prompt=False)
        dm = sc.daemon()
        sc.tick(dm)
        sc.hook("SessionEnd", a, reason="prompt_input_exit")
        sc.world.die(a)
        run(sc, dm, 5)
        require(sc.ring_count() == 0, "有 SessionEnd 的正常退出不响")
        details.append("有 SessionEnd：不响")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.world.die(a)
        run(sc, dm, 3)
        require(not kinds_like(sc, "holder_gone") and kinds_like(sc, "a_crash_unsealed") and sc.ring_count() == 1, "交接中崩溃只走 S2a 的铃：%r" % notice_kinds(sc))
        details.append("交接中 A 崩溃：只有 S2a 的 a_crash_unsealed 一铃，不重复")
    return details


def case_h90():
    """H90：:65 复核轮次账——round≥3 ∧ p0>0 响「卡住了」，同一（产物, 轮次）一次；同轮重复登记不再响；round 2 / p0=0 不响。"""
    with scenario() as sc:
        dm = sc.daemon()
        sc.tick(dm)
        review.register(sc.sdir, "kit-x", 2, 3, sc.clock.ms)
        sc.tick(dm)
        review.register(sc.sdir, "kit-x", 3, 0, sc.clock.ms)
        sc.tick(dm)
        require(sc.ring_count() == 0, "round 2 或 p0=0 不响")
        review.register(sc.sdir, "kit-x", 3, 2, sc.clock.ms)
        run(sc, dm, 3)
        require(sc.ring_count() == 1 and kinds_like(sc, "review_stuck-kit-x-3"), "round 3 ∧ p0>0 响一次：%r" % notice_kinds(sc))
        review.register(sc.sdir, "kit-x", 3, 1, sc.clock.ms)
        run(sc, dm, 3)
        require(sc.ring_count() == 1, "同一（产物, 轮次）重复登记不再响")
        review.register(sc.sdir, "kit-x", 4, 1, sc.clock.ms)
        run(sc, dm, 3)
        require(sc.ring_count() == 2, "新轮次是新的一条")
    return ["round 2 / p0=0 不响；round 3 ∧ p0>0 响 1 次；同轮重复登记不再响；round 4 仍有 P0 再响 1 次（共 2）"]


def case_h91():
    """H91：:47⑥ tick 抛未预料异常连 3 轮响铃一次，中间一轮正常则重新计数；:47③ 登记册读不出连 3 轮响一次。"""
    details = []
    with scenario() as sc:
        dm = sc.daemon()
        healthy = dm._request_pending

        def boom(state):
            raise RuntimeError("boom")

        dm._request_pending = boom
        sc.tick(dm)
        sc.tick(dm)
        require(sc.ring_count() == 0, "2 轮不响")
        dm._request_pending = healthy
        sc.tick(dm)                                  # 中间一轮正常
        dm._request_pending = boom
        sc.tick(dm)
        sc.tick(dm)
        require(sc.ring_count() == 0, "中间一轮正常后重新计数：再 2 轮仍不响")
        sc.tick(dm)
        require(sc.ring_count() == 1 and kinds_like(sc, "tick_bad"), "连续 3 轮响一次：%r" % notice_kinds(sc))
        for _ in range(4):
            sc.tick(dm)
        require(sc.ring_count() == 1, "同一段异常只响一次")
        dm._request_pending = healthy
        sc.tick(dm)
        dm._request_pending = boom
        for _ in range(3):
            sc.tick(dm)
        require(sc.ring_count() == 2, "恢复后再连 3 轮可再响（第二段）")
        details.append("tick 异常：连 2 轮不响；中间一轮正常重新计数；连 3 轮响 1 次；同段不重复；恢复后再连 3 轮再响")
    with scenario() as sc:
        dm = sc.daemon()
        sc.tick(dm)
        with open(os.path.join(sc.sdir, "baton.json"), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        sc.tick(dm)
        sc.tick(dm)
        require(sc.ring_count() == 0, "读不出 2 轮不响")
        sc.tick(dm)
        require(sc.ring_count() == 1 and kinds_like(sc, "tick_bad"), "baton.json 连 3 轮读不出：响一次")
        details.append("baton.json 读不出：连 3 轮响 1 次（并入同一计数）")
    return details


def case_h92():
    """H92：设计:7 时钟回拨 / 跳变 > 1 h ⇒ 计时从当前时刻重算、不响铃，下一周期照常询问。"""
    details = []
    for label, jump_ms in (("回拨 2 h", -2 * 3600 * 1000), ("向前跳 2 h", 2 * 3600 * 1000)):
        with scenario() as sc:
            a = new_a(sc)
            dm = sc.daemon()
            first_ask(sc, dm, a)                     # ASK 已发、等回复（5 min 门快到之前）
            run(sc, dm, 2)
            sc.clock.ms += jump_ms
            sc.tick(dm)
            run(sc, dm, 10)
            require(sc.ring_count() == 0, "%s：不响铃" % label)
            require(len(msgs(sc, a, "ASK")) == 1, "%s：重算后不立刻重问" % label)
            run(sc, dm, 18)
            require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "%s：从跳变那一刻起 30 min 内不问" % label)
            run(sc, dm, 3)
            require(len(msgs(sc, a, "ASK")) == 2, "%s：下一周期照常询问（%d）" % (label, len(msgs(sc, a, "ASK"))))
            details.append("%s：未决 ASK 不响铃，30 min 后照常重问" % label)
    return details


def case_h93():
    """H93：Owner 按 Esc 打断时 Stop 不触发（hooks.md:2525），prompt>stop 会一直成立——「在途」仅因 prompt>stop 时发 ASK 前核 agents：不在 busy ⇒ 视为空闲不问（30 / 60 min 都不投、零铃）；
    agents 查询失败 ⇒ 本轮不问、不清计时（恢复后立刻问）；busy ⇒ 照常问。"""
    details = []
    with scenario() as sc:
        a = new_a(sc)
        sc.world.agents = [{"pid": a["pid"], "status": "idle"}]
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 30)
        require(not msgs(sc, a, "ASK") and sc.ring_count() == 0, "Esc 打断（agents idle）30 min 不问")
        run(sc, dm, 30)
        require(not msgs(sc, a, "ASK") and sc.ring_count() == 0, "60 min 仍不问、零铃")
        sc.world.agents = busy_list(a)
        run(sc, dm, 3)
        require(len(msgs(sc, a, "ASK")) == 1, "agents 显示 busy ⇒ 照常问（%d）" % len(msgs(sc, a, "ASK")))
        details.append("agents idle：30 / 60 min 都不投 ASK、零铃；改为 busy 后下一次到点照常投 1 条")
    with scenario() as sc:
        a = new_a(sc)
        sc.world.agents = None
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 40)
        require(not msgs(sc, a, "ASK") and sc.ring_count() == 0, "agents 查询失败：不问、不响")
        sc.world.agents = busy_list(a)
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 1, "查询失败期间不清计时：恢复后下一轮就问")
        details.append("agents 查询失败（None）：本轮不问、不响铃、不清计时")
    return details



# ---- R1 整合账（handoffd_S2b_R1_integration_20261001.md）F1–F7 的回归用例 ----

def case_h94():
    """H94（R1-F1）：auto 下 sA 持棒 → `handoffd off` → sA 在 manual 下正常退出（manual 零写入、没有 ended_ms）→ 隔天 `handoffd on` → daemon 首轮不得响「主控没了」；
    同一个 auto 期间亲眼见过活着的 holder 死了仍响（H89）。"""
    with scenario() as sc:
        a = new_a(sc, prompt=False)
        dm = sc.daemon()
        sc.tick(dm)
        code, out, err = sc.cli(["off"], None)
        require(code == 0, "off 失败：%s" % err)
        sc.hook("SessionEnd", a, reason="prompt_input_exit")      # manual：零写入
        sc.world.die(a)
        sc.clock.advance(12 * 3600)
        code, out, err = sc.cli(["on"], None)
        require(code == 0, "on 失败：%s" % err)
        run(sc, dm, 5)
        require(sc.ring_count() == 0 and not kinds_like(sc, "holder_gone"), "off→正常退出→on：不得响铃：%r" % notice_kinds(sc))
    return ["off → manual 下正常退出 → 12 h 后 on：daemon 首轮起 5 轮 0 铃（holder 没在本次 auto 期间被亲眼见过活着）"]


def case_h95():
    """H95（R1-F2）：rate_limit 后后台任务 / 子代理的普通 PostToolUse 抬高 last_event_ms，不得结束额度等待（不问、不响）；60 min 无信号的起点推后到最后一次事件。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        sc.hook("StopFailure", a, error="rate_limit")
        run(sc, dm, 10)
        touch(sc, a)                                  # t=10：普通事件
        run(sc, dm, 35)                               # t=45
        require(not sc.world.messages and sc.ring_count() == 0, "额度等待期间有普通事件：不问不响：%r" % sc.world.messages)
        run(sc, dm, 24)                               # t=69：距最后事件 59 min
        require(not msgs(sc, a, "NUDGE") and sc.ring_count() == 0, "起点推后到最后一次事件：59 min 还不转入退避")
        run(sc, dm, 7)                                # t=76：70 转入，73 发第一条
        require(len(msgs(sc, a, "NUDGE")) == 1 and not msgs(sc, a, "ASK") and sc.ring_count() == 0, "最后事件后 60 min 转入退避 NUDGE，零铃")
    return ["rate_limit 后 t=10 一个普通事件：t=45 不问不响；t=69 仍无 NUDGE；t=76 已转入退避发出第一条 NUDGE；零铃"]


def case_h96():
    """H96（R1-F3）：ASK 后答 task（核不上），下一轮前撞 rate_limit / server_error ⇒ 暂停期间不投纠正 ASK、不计核不上；owner 回复仍立即响铃。"""
    details = []
    for error in ("rate_limit", "server_error"):
        with scenario() as sc:
            a = new_a(sc)
            dm = sc.daemon()
            first_ask(sc, dm, a)
            sc.world.agents = []                       # task 回复核不上
            reply(sc, a, "task")
            sc.clock.advance(1)
            sc.hook("StopFailure", a, error=error)     # 下一轮 tick 之前撞上
            sc.tick(dm)
            require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "%s：暂停期间不投纠正 ASK：%r" % (error, msgs(sc, a, "ASK")))
            require(not watch_session(sc, "sA").get("mismatch"), "%s：不计核不上" % error)
            details.append("%s：暂停期间核不上的 task 回复不投纠正 ASK、不计数、零铃" % error)
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        reply(sc, a, "owner")
        sc.clock.advance(1)
        sc.hook("StopFailure", a, error="server_error")
        sc.tick(dm)
        require(sc.ring_count() == 1, "暂停期间 owner 回复仍立即响铃")
        details.append("暂停期间 owner 回复仍立即响铃")
    return details


def case_h97():
    """H97（R1-F4）：B 上线后、seal 前 Owner 关掉 B 的窗口 ⇒ 只有 S2a 的 b_died 一铃，5 min 后不再叠 open_unreachable。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.world.die(b)
        run(sc, dm, 8)
        require(kinds_like(sc, "b_died") and not kinds_like(sc, "open_unreachable") and sc.ring_count() == 1, "只应有 b_died：%r" % notice_kinds(sc))
    return ["B 死后 8 min：仅 b_died 一铃，无 open_unreachable"]


def case_h98():
    """H98（R1-F5）：A 从 request 到 seal 超过 30 min ⇒ B 的第一条消息是 OPEN，不是 ASK；OPEN 送达后 B 才被看护。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        holder = {}
        sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = holder["b"]
        run(sc, dm, 35)
        require(not sc.world.messages_to(b), "seal 之前 B 一条消息都不该收到：%r" % sc.world.messages_to(b))
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit()], a)
        require(code == 0, "seal 失败：%s" % err)
        sc.tick(dm)
        first = sc.world.messages_to(b)
        require(first and first[0] == OPENING, "B 的首条消息应是 OPEN 原文：%r" % first[:1])
    return ["A 35 min 才 seal：seal 前 B 零消息；seal 后 B 的第一条是 OPEN 原文"]


def case_h99():
    """H99（R1-F6，规格:41「还在跑就继续等」）：5 min 到点 agents 显示 busy ⇒ 不响，按核得上重新计时（30 min 后再问）；agents 查询失败 ⇒ 本轮不判，之后 idle 才响。"""
    details = []
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        run(sc, dm, 5)
        require(sc.ring_count() == 0, "busy：5 min 到点不响")
        run(sc, dm, 29)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 1, "busy：重新计时后 30 min 内不重问")
        run(sc, dm, 1)
        require(sc.ring_count() == 0 and len(msgs(sc, a, "ASK")) == 2, "核得上处理：重新计时，满 30 min 再问一次")
        details.append("agents busy：5 min 无回复不响；按核得上重新计时，30 min 后重问")
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        sc.world.agents = None
        run(sc, dm, 10)
        require(sc.ring_count() == 0, "agents 查询失败：本轮不判")
        sc.world.agents = idle_list(a)
        sc.tick(dm)
        require(sc.ring_count() == 1, "之后 idle：响一次")
        details.append("agents 查询失败：不判不响；恢复后 idle 才响")
    return details


def case_h100():
    """H100（R1-F7）：ASK 未读时 Owner 正常 /exit ⇒ 5 min 后不响「无回复」（目标会话已不是 live；崩溃由 :47② 与 S2a 的铃覆盖）。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        sc.world.agents = idle_list(a)
        sc.hook("SessionEnd", a, reason="prompt_input_exit")
        sc.world.die(a)
        run(sc, dm, 8)
        require(sc.ring_count() == 0, "目标已退出：5 min 无回复不响：%r" % notice_kinds(sc))
    return ["ASK 后会话正常退出：8 min 内 0 铃"]


# ---- H108–H113：reply task 可带预计时长（15–180 分钟）+ 询问间隔可配（2026-10-15，Owner 批） ----

def reply_eta(sc, rec, kind, *extra):
    """带额外参数的 reply（reply() 只带 kind）；返回 (退出码, 输出, 错误)。"""
    sc.clock.advance(1)
    return sc.cli(["reply", kind] + list(extra), rec)


def file_bytes(path):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError:
        return None


def state_snapshot(sc, rec):
    """reply 可能写的全部状态：会话记录 + events.jsonl（二者一字节都不能变）。"""
    from cdm import sessions
    return (file_bytes(sessions.session_path(sc.sdir, rec["session_id"])), file_bytes(os.path.join(sc.sdir, "events.jsonl")))


def case_h108():
    """H108：reply task 60m 核得上 ⇒ 59 分钟不问、60 分钟问（首问之后）；不带时长仍是 30（H83 钉住）。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        code, out, err = reply_eta(sc, a, "task", "60m")
        require(code == 0, "reply task 60m 应成功：%r %s" % (code, err))
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "核得上：不追问、不响铃")
        run(sc, dm, 29)
        require(len(msgs(sc, a, "ASK")) == 1, "带 60m：30 分钟时不该问")
        touch(sc, a)
        run(sc, dm, 30)
        require(len(msgs(sc, a, "ASK")) == 1, "带 60m：59 分钟不问")
        run(sc, dm, 1)
        require(len(msgs(sc, a, "ASK")) == 2 and sc.ring_count() == 0, "带 60m：满 60 分钟问一次（共 2 条），不响铃")
    return ["reply task 60m 核得上：59 分钟不问、60 分钟问；全程不响铃"]


def case_h109():
    """H109：带时长的那次重问之后回到 idle_ask_minutes（只影响紧接着的一次）：无回复路径与再次不带时长回复路径都是 30 分钟间隔。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply_eta(sc, a, "task", "60m")
        sc.tick(dm)
        run(sc, dm, 60)
        require(len(msgs(sc, a, "ASK")) == 2, "60 分钟重问")
        require("eta_ms" not in watch_session(sc, "sA"), "重问发出后时长应作废：%r" % watch_session(sc, "sA"))
        reply(sc, a, "task")            # 这次不带时长
        sc.tick(dm)
        run(sc, dm, 29)
        require(len(msgs(sc, a, "ASK")) == 2, "不带时长：29 分钟不问")
        run(sc, dm, 1)
        require(len(msgs(sc, a, "ASK")) == 3, "不带时长：回到 30 分钟间隔（共 3 条）：%d" % len(msgs(sc, a, "ASK")))
    with scenario() as sc:              # 无回复路径：5 分钟门核 busy ⇒ 重新计时，之后仍按 30 而不是 60
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply_eta(sc, a, "task", "60m")
        sc.tick(dm)
        run(sc, dm, 60)
        require(len(msgs(sc, a, "ASK")) == 2, "60 分钟重问")
        run(sc, dm, 34)
        require(len(msgs(sc, a, "ASK")) == 2, "重问后无回复：5 分钟门 + 30 分钟，34 分钟时不问")
        run(sc, dm, 2)
        require(len(msgs(sc, a, "ASK")) == 3, "重问后无回复：约 35 分钟再问（按 30 而不是 60）：%d" % len(msgs(sc, a, "ASK")))
    return ["带时长的重问之后：再次回复不带时长 ⇒ 30 分钟；无回复 ⇒ 5 分钟门后 30 分钟（共约 35），都不再用 60"]


def case_h110():
    """H110：非法时长（300m / 10m / 60 / 60h / abc 等）⇒ 命令报错、退出码非 0、不写任何状态（会话记录与 events 字节不变）；15m / 180m 两端点合法。"""
    bad = ["300m", "10m", "60", "60h", "abc", "14m", "181m", "0m", "-30m", "60M", "1.5m", "m", "６０m",
           "180m\n", "60m ", " 60m", "60m\t", "\n60m", "60m\r\n"]   # 空白维度两端（R3 P1）
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        for text in bad:
            before = state_snapshot(sc, a)
            code, out, err = reply_eta(sc, a, "task", text)
            require(code not in (0, None), "非法时长 %r 应报错（退出码非 0）：%r" % (text, code))
            require(err.strip() != "", "非法时长 %r 应有错误文案" % text)
            require(state_snapshot(sc, a) == before, "非法时长 %r 不得写任何状态" % text)
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "全部非法回复都没有登记：daemon 无动作")
    for text, minutes in (("15m", 15), ("180m", 180)):
        with scenario() as sc:
            a = new_a(sc)
            code, out, err = reply_eta(sc, a, "task", text)
            require(code == 0, "端点 %s 应合法：%r %s" % (text, code, err))
            from cdm import sessions
            last = sessions.read_session(sc.sdir, "sA")["last_reply"]
            require(last.get("eta_minutes") == minutes and last.get("kind") == "task", "端点 %s 应存 eta_minutes=%d：%r" % (text, minutes, last))
    return ["%d 种非法时长全部非 0 退出、会话记录与 events 字节不变；15m / 180m 端点合法并存入 eta_minutes" % len(bad)]


def case_h111():
    """H111：reply owner / peer / quota 带时长 ⇒ 报错、状态不变；owner 照旧不受影响（不带时长仍立即登记）。"""
    with scenario() as sc:
        a = new_a(sc)
        for kind in ("owner", "peer", "quota"):
            before = state_snapshot(sc, a)
            code, out, err = reply_eta(sc, a, kind, "60m")
            require(code not in (0, None), "reply %s 60m 应报错：%r" % (kind, code))
            require(state_snapshot(sc, a) == before, "reply %s 60m 不得写任何状态" % kind)
        code, out, err = reply_eta(sc, a, "owner")
        require(code == 0, "reply owner（不带时长）仍应成功：%r %s" % (code, err))
    return ["owner / peer / quota 带 60m 全部报错且状态不变；不带时长的 owner 仍成功"]


def case_h112():
    """H112：idle_ask_minutes=90（项目配置，代码默认仍 30）⇒ 89 分钟不问、90 分钟首问。"""
    with scenario(cfg_overrides={"idle_ask_minutes": 90}) as sc:
        a = new_a(sc)
        dm = sc.daemon()
        sc.tick(dm)
        run(sc, dm, 89)
        require(not msgs(sc, a, "ASK"), "idle_ask_minutes=90：89 分钟不该问")
        run(sc, dm, 1)
        require(len(msgs(sc, a, "ASK")) == 1, "idle_ask_minutes=90：90 分钟首问一次")
    from cdm import config
    require(config.DEFAULTS["idle_ask_minutes"] == 30, "代码默认值仍是 30")
    return ["idle_ask_minutes=90：89 分钟不问、90 分钟首问；代码默认仍 30"]


def case_h113():
    """H113：带时长但核不上 ⇒ 时长不生效，走现行纠正路径（先纠正一次；之后不按 60 分钟等）。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        sc.world.agents = idle_list(a)      # 没有后台任务、agents 也不 busy ⇒ task 核不上
        code, out, err = reply_eta(sc, a, "task", "60m")
        require(code == 0, "reply 本身合法、应登记：%r %s" % (code, err))
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 2 and "核对不上" in msgs(sc, a, "ASK")[1], "核不上：立刻投一条纠正 ASK：%r" % msgs(sc, a, "ASK"))
        ws = watch_session(sc, "sA")
        require("eta_ms" not in ws and "eta_for_ms" not in ws, "核不上：不得记下时长：%r" % ws)
        sc.world.agents = busy_list(a)      # 之后它确实在跑：间隔应是 30（5 分钟门 + 30），不是 60
        run(sc, dm, 50)
        require(len(msgs(sc, a, "ASK")) == 3, "核不上后不按 60 分钟等：50 分钟内已再问：%d" % len(msgs(sc, a, "ASK")))
    return ["带 60m 但核不上：投纠正 ASK、状态里不记时长、50 分钟内已再问（不按 60 等）"]


def case_h114():
    """H114（R1 裁定 P0-1）：下次询问自「回复时刻」起算，不是 daemon 核对时刻：回复后 daemon 停 59 分钟再核对 ⇒ 带 180m 仍在回复后 180 分钟内问；带 60m 在核对后约 1 分钟问。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply_eta(sc, a, "task", "180m")
        sc.tick(dm, 59 * 60)            # daemon 停了 59 分钟（小于 H92 的 1 小时跳变门槛）才核对这条回复
        require(len(msgs(sc, a, "ASK")) == 1 and sc.ring_count() == 0, "核得上：不追问、不响铃")
        run(sc, dm, 120)
        require(len(msgs(sc, a, "ASK")) == 1, "带 180m、已停 59 分钟：核对后 120 分钟内（回复后约 179 分钟）还不该问")
        run(sc, dm, 3)
        require(len(msgs(sc, a, "ASK")) == 2, "带 180m：回复后 180 分钟内（核对后约 121 分钟）必须问，不是核对后再满 180：%d" % len(msgs(sc, a, "ASK")))
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply_eta(sc, a, "task", "60m")
        sc.tick(dm, 59 * 60)
        require(len(msgs(sc, a, "ASK")) == 1, "停 59 分钟后核对：尚未到点（回复后 59 分钟）")
        run(sc, dm, 2)
        require(len(msgs(sc, a, "ASK")) == 2, "带 60m：回复后满 60 分钟（核对后约 1 分钟）就问：%d" % len(msgs(sc, a, "ASK")))
    return ["回复后 daemon 停 59 分钟再核对：180m ⇒ 核对后约 121 分钟问（不是 180）；60m ⇒ 核对后约 1 分钟问"]


def case_h115():
    """H115（R1 裁定 P1-1）：先核准 task 60m、之后再答 peer 核不上 ⇒ 旧时长作废，纠正 ASK 之后按 idle_ask_minutes（30）重问，不是 60。"""
    with scenario() as sc:
        a = new_a(sc)
        dm = sc.daemon()
        first_ask(sc, dm, a)
        stop(sc, a, tasks=["t1"])
        reply_eta(sc, a, "task", "60m")
        sc.tick(dm)
        require("eta_ms" in watch_session(sc, "sA"), "前提：60m 已核准并记下")
        run(sc, dm, 10)
        sc.world.agents = idle_list(a)      # 后台任务还在、agents 显示 idle：纠正 ASK 之后 5 分钟无回复会响铃，ok_ms 不被刷新
        reply(sc, a, "peer")                # 没有进行中的交接 ⇒ 核不上
        sc.tick(dm)
        require(len(msgs(sc, a, "ASK")) == 2 and "核对不上" in msgs(sc, a, "ASK")[1], "核不上：投一条纠正 ASK：%r" % msgs(sc, a, "ASK"))
        ws = watch_session(sc, "sA")
        require("eta_ms" not in ws and "eta_for_ms" not in ws, "核不上：先前的时长必须清掉：%r" % ws)
        run(sc, dm, 31)
        require(len(msgs(sc, a, "ASK")) == 3, "纠正 ASK 之后按 30 分钟重问（不是 60）：%d" % len(msgs(sc, a, "ASK")))
    return ["先核准 60m 再答 peer 核不上：状态里不留旧时长，纠正 ASK 后 31 分钟内已按 30 分钟节奏再问"]


def case_h116():
    """H116（R1 裁定 P1-2）：状态文件里的 eta_ms 越界（> 180 分钟）⇒ 不采用，按 idle_ask_minutes 问；180 分钟整仍采用。"""
    def poisoned(eta_ms):
        with scenario() as sc:
            a = new_a(sc)
            dm = sc.daemon()
            first_ask(sc, dm, a)
            stop(sc, a, tasks=["t1"])
            reply_eta(sc, a, "task", "60m")
            sc.tick(dm)
            path = os.path.join(sc.sdir, "daemon_state.json")
            status, value = util.read_json(path)
            require(status == "ok" and isinstance(value["watch"]["sessions"]["sA"].get("eta_ms"), int), "前提：60m 已记下：%r" % value)
            value["watch"]["sessions"]["sA"]["eta_ms"] = eta_ms
            util.atomic_write_json(path, value)
            run(sc, dm, 31)
            return len(msgs(sc, a, "ASK"))
    require(poisoned(10 ** 12) == 2, "eta_ms 被改成极大值：应按 30 分钟问")
    require(poisoned(181 * 60 * 1000 + 1) == 2, "eta_ms 刚过 180 分钟：应按 30 分钟问")
    require(poisoned(180 * 60 * 1000) == 1, "eta_ms 恰为 180 分钟：仍采用，31 分钟内不问")
    return ["持久化 eta_ms 极大值 / 超 180 分钟 ⇒ 按 30 分钟问；恰 180 分钟仍采用"]


def case_h117():
    """H117（R3 检查点诊断 P2）：已核准 task 60m 后时钟跳变 > 1 h ⇒ H92 重算时预计时长随 ok_ms 一起作废，状态里不留 eta_ms / eta_for_ms。"""
    details = []
    for label, jump_ms in (("回拨 2 h", -2 * 3600 * 1000), ("向前跳 2 h", 2 * 3600 * 1000)):
        with scenario() as sc:
            a = new_a(sc)
            dm = sc.daemon()
            first_ask(sc, dm, a)
            stop(sc, a, tasks=["t1"])
            reply_eta(sc, a, "task", "60m")
            sc.tick(dm)
            require("eta_ms" in watch_session(sc, "sA"), "%s 前提：60m 已核准并记下" % label)
            sc.clock.ms += jump_ms
            sc.tick(dm)
            ws = watch_session(sc, "sA")
            require("eta_ms" not in ws and "eta_for_ms" not in ws, "%s：重算后不得留旧时长：%r" % (label, ws))
            details.append("%s：预计时长与 ok_ms 一起清掉" % label)
    return details


def main():
    """运行 H83–H100、H108–H117。"""
    return run_cases([("H83", case_h83), ("H84", case_h84), ("H85", case_h85), ("H86", case_h86), ("H87", case_h87),
                      ("H88", case_h88), ("H89", case_h89), ("H90", case_h90), ("H91", case_h91), ("H92", case_h92), ("H93", case_h93),
                      ("H94", case_h94), ("H95", case_h95), ("H96", case_h96), ("H97", case_h97), ("H98", case_h98), ("H99", case_h99), ("H100", case_h100),
                      ("H108", case_h108), ("H109", case_h109), ("H110", case_h110), ("H111", case_h111), ("H112", case_h112), ("H113", case_h113),
                      ("H114", case_h114), ("H115", case_h115), ("H116", case_h116), ("H117", case_h117)])


if __name__ == "__main__":
    sys.exit(main())
