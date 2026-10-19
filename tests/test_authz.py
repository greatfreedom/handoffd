# 测试 H30–H36：handoffd S1 R3「唯一授权入口」的不变量验收（不是场景清单）。
#   I  放行写 ⇒ 调用返回时登记册 holder 槽（或 handoff.b 槽）的 (pid, lstart) == 本进程（或祖先链）；
#   II 本次调用没有把 proc_state≠dead 的他进程从槽位换掉。
# 唯一例外：本进程身份 / 会话号取不到（规格:79、K2）——放行，但登记册不得被写。
# H30 有限状态穷举（直接从 PreToolUse 进入的格子都在）；H31 两会话 ≤4 步交错 × 中途杀其一 × 有无交接；
# H32–H34 R2 三条 P0 的复现序列（真实进程）；H35 响铃 / 副作用异常不改放行决定；H36 classify 的代码结构核对。
import contextlib
import itertools
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import MVP_ROOT, process_pool, require, run_cases, temp_dir  # noqa: E402
from relay_helpers import (Rings, call_relay, handoff_with_b, holder_of, ident_fn, is_blocked, is_denied, read_baton,  # noqa: E402
                           set_auto, tool_call, write_baton)
from cdm import baton, procs, sessions, util  # noqa: E402

ME = {"session_id": "sME", "pid": 40001, "lstart": "Thu Oct  1 10:00:01 2026"}
OL = {"session_id": "x", "pid": 40002, "lstart": "Thu Oct  1 10:00:02 2026"}       # 他进程，活
OD = {"session_id": "x", "pid": 40003, "lstart": "Thu Oct  1 10:00:03 2026"}       # 他进程，已死
OU = {"session_id": "x", "pid": 40004, "lstart": "Thu Oct  1 10:00:04 2026"}       # 他进程，判活不确定
ANC = {"session_id": "x", "pid": 40005, "lstart": "Thu Oct  1 10:00:05 2026"}      # 本进程的祖先（活）
BL = {"session_id": "x", "pid": 40012, "lstart": "Thu Oct  1 10:00:12 2026"}       # B 槽里的他进程，活
BD = {"session_id": "x", "pid": 40013, "lstart": "Thu Oct  1 10:00:13 2026"}       # B 槽里的他进程，已死
LIVENESS = {ME["pid"]: "live", OL["pid"]: "live", OD["pid"]: "dead", OU["pid"]: "unknown", ANC["pid"]: "live", BL["pid"]: "live", BD["pid"]: "dead"}

HOLDER_STATES = ("absent", "me", "other_live", "other_dead", "other_unknown", "ancestor")
B_STATES = ("no_handoff", "b_empty", "b_me", "b_other_live", "b_other_dead")
SIDS = {"sH": "sH", "uB": "uB", "sX": "sX"}
EVENTS = (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {}),
          ("PreToolUse", tool_call("Write", file_path="/x", content="c")), ("PreToolUse", tool_call("SendMessage", to="a")))


@contextlib.contextmanager
def fake_liveness(table):
    """用模拟的 pid 存活表替换 procs.proc_state（hermetic、快）；真实进程的用例见 H32–H34。"""
    original = procs.proc_state
    procs.proc_state = lambda record, deadline=None: table.get(record["pid"], "dead")
    try:
        yield
    finally:
        procs.proc_state = original


def slot_record(rec, sid="sH"):
    return {"sid": sid, "pid": rec["pid"], "lstart": rec["lstart"], "claimed_ms": 1}


def build_state(holder_state, b_state):
    holder = {"absent": None, "me": slot_record(ME), "other_live": slot_record(OL), "other_dead": slot_record(OD),
              "other_unknown": slot_record(OU), "ancestor": slot_record(ANC)}[holder_state]
    handoff = None
    if b_state != "no_handoff":
        handoff = handoff_with_b("uB")
        rec = {"b_me": ME, "b_other_live": BL, "b_other_dead": BD}.get(b_state)
        if rec:
            handoff["b"].update(pid=rec["pid"], lstart=rec["lstart"], online_ms=1)
    return {"version": 1, "holder": holder, "handoff": handoff, "retire": []}


def same(slot, rec_or_pair):
    pair = (rec_or_pair["pid"], rec_or_pair["lstart"]) if isinstance(rec_or_pair, dict) else rec_or_pair
    return bool(slot) and slot.get("pid") is not None and (slot["pid"], slot["lstart"]) == pair


def check_call(before, after, event, tool, out, sid, me, chain, identity_available, table):
    """对一次钩子调用检查不变量 I、II，返回 (I 违反列表, II 违反列表)。me=(pid, lstart)。"""
    v1, v2 = [], []
    allowed = event == "PreToolUse" and not is_denied(out)
    if allowed and identity_available:
        holder_ok = same(after["holder"], me) or any(same(after["holder"], pair) for pair in chain)
        b_ok = (tool == "SendMessage" and after["handoff"] is not None and after["handoff"]["b"]["uuid"] == sid
                and same(after["handoff"]["b"], me))
        if not (holder_ok or b_ok):
            v1.append("放行了 %s 但登记册槽位不是本进程：holder=%r b=%r" % (tool, after["holder"], (after["handoff"] or {}).get("b")))
    if not identity_available and after != before:
        v1.append("取不到本进程身份时写了登记册（fail-open 只放行、不写）")
    for label, slot_before, slot_after in (
            ("holder", before["holder"], after["holder"]),
            ("b", (before["handoff"] or {}).get("b") if before["handoff"] else None, (after["handoff"] or {}).get("b") if after["handoff"] else None)):
        if not slot_before or slot_before.get("pid") is None or same(slot_before, me):
            continue
        if table.get(slot_before["pid"]) in ("live", "unknown"):
            if not slot_after or (slot_after.get("pid"), slot_after.get("lstart")) != (slot_before["pid"], slot_before["lstart"]):
                v2.append("%s 槽里 proc_state=%s 的他进程被换掉：%r → %r" % (label, table.get(slot_before["pid"]), slot_before, slot_after))
    return v1, v2


def run_cell(project, sdir, holder_state, b_state, available, sid, event, extra):
    state = build_state(holder_state, b_state)
    write_baton(sdir, holder=state["holder"], handoff=state["handoff"])
    sessions.write_heartbeat(sdir, util.now_ms())
    chain_pairs = [(ME["pid"], ME["lstart"])] + ([(ANC["pid"], ANC["lstart"])] if holder_state == "ancestor" else [])
    me = (ME["pid"], ME["lstart"])
    ident = (lambda deadline: me) if available else None
    chain = (lambda deadline: chain_pairs) if available else None
    code, out = call_relay(event, project, sid, ident, chain=chain, rings=Rings(), extra=extra)
    after = read_baton(sdir)
    before = {"version": 1, "holder": state["holder"], "handoff": state["handoff"], "retire": []}
    # 比较用：丢掉 retire（认领会追加）与 qa 之类的非槽位字段
    v1, v2 = check_call(before, after, event, extra.get("tool_name"), out, sid, me, chain_pairs, available, LIVENESS)
    return code, out, v1, v2


def exhaustive():
    """穷举 holder 槽 6 态 × b 槽 5 态 × 身份 2 × 会话号 3 × 事件 4，返回 (格数, I 违反格, II 违反格, 放行写格数, 拒绝写格数)。"""
    cells = bad1 = bad2 = allowed = denied = 0
    v1_list, v2_list = [], []
    with temp_dir() as work, fake_liveness(LIVENESS):
        project = os.path.join(work, "p")
        os.makedirs(project)
        sdir = set_auto(project)
        for holder_state, b_state, available, sid_key, (event, extra) in itertools.product(
                HOLDER_STATES, B_STATES, (True, False), SIDS, EVENTS):
            code, out, v1, v2 = run_cell(project, sdir, holder_state, b_state, available, SIDS[sid_key], event, extra)
            cells += 1
            label = "holder=%s b=%s identity=%s sid=%s %s/%s" % (holder_state, b_state, available, sid_key, event, extra.get("tool_name", ""))
            if code != 0:
                v1.append("退出码 %r" % code)
            if v1:
                bad1 += 1
                v1_list.append(label + " :: " + "; ".join(v1))
            if v2:
                bad2 += 1
                v2_list.append(label + " :: " + "; ".join(v2))
            if event == "PreToolUse" and extra.get("tool_name") == "Write":
                allowed += 0 if is_denied(out) else 1
                denied += 1 if is_denied(out) else 0
    return cells, bad1, bad2, allowed, denied, v1_list, v2_list


def case_h30():
    """H30：不变量 I、II 的有限状态穷举（720 格，PreToolUse 直接入格）；修后 0 红，且不是空转（既有放行也有拒绝）。"""
    cells, bad1, bad2, allowed, denied, v1_list, v2_list = exhaustive()
    require(cells == len(HOLDER_STATES) * len(B_STATES) * 2 * len(SIDS) * len(EVENTS), "格数不对：%d" % cells)
    require(bad1 == 0, "不变量 I 违反 %d 格，例：%r" % (bad1, v1_list[:3]))
    require(bad2 == 0, "不变量 II 违反 %d 格，例：%r" % (bad2, v2_list[:3]))
    require(allowed > 20 and denied > 20, "穷举不应空转：放行写 %d 格、拒绝写 %d 格" % (allowed, denied))
    return ["%d 格（holder 槽 %d 态 × b 槽 %d 态 × 身份 2 × 会话号 3 × 事件 4，含 PreToolUse 直入）：I 违反 %d、II 违反 %d；Write 放行 %d 格 / 拒绝 %d 格"
            % (cells, len(HOLDER_STATES), len(B_STATES), bad1, bad2, allowed, denied)]


STEPS = tuple((session, name) for session in (1, 2) for name in ("SS", "UP", "PW"))
STEP_EVENT = {"SS": ("SessionStart", {"source": "startup"}), "UP": ("UserPromptSubmit", {}),
              "PW": ("PreToolUse", tool_call("Write", file_path="/x", content="c"))}
P1 = {"session_id": "s1", "pid": 41001, "lstart": "Thu Oct  1 11:00:01 2026"}
P2 = {"session_id": "s2", "pid": 41002, "lstart": "Thu Oct  1 11:00:02 2026"}


INITIALS = (
    # (名字, 登记册里是否有 P1 持棒, 是否有交接, 会话 2 用的会话号)
    ("empty", False, False, "s2"),
    ("p1_holder", True, False, "s2"),
    ("p1_holder_handoff_s2_is_B", True, True, "s2"),
    ("p1_holder_same_sid", True, False, "s1"),              # 会话 2 是 `claude --resume s1` 开出的第二个进程
    ("p1_holder_handoff_same_sid", True, True, "s1"),
)


def interleavings(max_len=4):
    """两会话 ≤max_len 步交错 × 5 种初始登记册（含「会话 2 与主控同会话号」）× 中途（第 2 步后）杀 P1 / 杀 P2 / 不杀
    （长度 4 只试 不杀 / 杀 P1，以控制时长）。每步检查 I、II；每个序列结束再探测：两个存活进程按两种顺序各试一次 Write，
    获准的不得超过 1 个。返回 (序列数, 违反列表)。"""
    count, violations = 0, []
    with temp_dir() as work:
        project = os.path.join(work, "p")
        os.makedirs(project)
        sdir = set_auto(project)
        for length in range(1, max_len + 1):
            kills = (None,) if length < 2 else ((None, 1, 2) if length <= 3 else (None, 1))
            for seq in itertools.product(STEPS, repeat=length):
                for initial, has_holder, has_handoff, sid2 in INITIALS:
                    recs = {1: P1, 2: dict(P2, session_id=sid2)}
                    for kill in kills:
                        count += 1
                        table = {P1["pid"]: "live", P2["pid"]: "live"}
                        original = procs.proc_state
                        procs.proc_state = lambda record, deadline=None, table=table: table.get(record["pid"], "dead")
                        try:
                            write_baton(sdir, holder=slot_record(P1, "s1") if has_holder else None,
                                        handoff=handoff_with_b("s2" if sid2 == "s2" else "uB") if has_handoff else None)
                            sessions.write_heartbeat(sdir, util.now_ms())
                            for index, (session, name) in enumerate(seq):
                                rec = recs[session]
                                if table[rec["pid"]] == "dead":
                                    continue
                                before = read_baton(sdir)
                                event, extra = STEP_EVENT[name]
                                me = (rec["pid"], rec["lstart"])
                                code, out = call_relay(event, project, rec["session_id"], lambda d, me=me: me, chain=lambda d, me=me: [me],
                                                       rings=Rings(), extra=extra)
                                v1, v2 = check_call(before, read_baton(sdir), event, extra.get("tool_name"), out, rec["session_id"], me, [me], True, table)
                                if v1 or v2 or code != 0:
                                    violations.append("初始=%s 杀=%s 序列=%r 第 %d 步：%r %r" % (initial, kill, seq, index + 1, v1, v2))
                                if kill is not None and index == 1:
                                    table[(P1 if kill == 1 else P2)["pid"]] = "dead"
                            alive = [rec for session, rec in sorted(recs.items()) if table[rec["pid"]] != "dead"]
                            for order in (alive, list(reversed(alive))):
                                saved = open(baton.baton_path(sdir), "rb").read()
                                permitted = []
                                for rec in order:
                                    me = (rec["pid"], rec["lstart"])
                                    code, out = call_relay("PreToolUse", project, rec["session_id"], lambda d, me=me: me, chain=lambda d, me=me: [me],
                                                           rings=Rings(), extra=STEP_EVENT["PW"][1])
                                    if not is_denied(out):
                                        permitted.append(rec["pid"])
                                with open(baton.baton_path(sdir), "wb") as stream:
                                    stream.write(saved)
                                if len(permitted) > 1:
                                    violations.append("初始=%s 杀=%s 序列=%r：结束探测 %r 都获准写" % (initial, kill, seq, permitted))
                        finally:
                            procs.proc_state = original
    return count, violations


def case_h31():
    """H31：两会话 ≤4 步（SessionStart / UserPromptSubmit / PreToolUse Write）交错 × 中途杀其一 × 有无交接：每步守 I、II，任一时刻获准写的进程 ≤1。"""
    count, violations = interleavings()
    require(count > 15000, "序列数过少：%d" % count)
    require(not violations, "交错违反 %d 条，例：%r" % (len(violations), violations[:3]))
    return ["%d 个序列（长度 1–4 × 5 种初始登记册（含会话 2 与主控同会话号）× 杀 P1 / 杀 P2 / 不杀）：0 违反" % count]


def case_h32():
    """H32（R2 P0-1）：主控崩溃（无交接）后，同会话号的两个旧窗口与另一个新窗口**直接 PreToolUse**——必须经 guard 认领，只有一个获准，登记册换成赢者。"""
    details = []
    write = tool_call("Write", file_path="/x", content="c")
    with temp_dir() as work, process_pool(work) as pool:
        project = os.path.join(work, "p")
        os.makedirs(project)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        p1_proc = pool.spawn_claude()
        p1 = pool.identity(p1_proc, "S")
        p2 = pool.identity(pool.spawn_claude(), "S")      # claude --resume S
        p3 = pool.identity(pool.spawn_claude(), "T")
        call_relay("SessionStart", project, "S", ident_fn(p1), extra={"source": "startup"})
        for sid, rec in (("S", p2), ("T", p3)):
            require(is_denied(call_relay("PreToolUse", project, sid, ident_fn(rec), extra=write)[1]), "P1 活着时 %s 应被拒" % sid)
        pool.kill(p1_proc)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(p2), extra=write)
        require(out is None and read_baton(sdir)["holder"]["pid"] == p2["pid"], "P2 直接写：应在 guard 内挂靠成 holder（登记册变成 P2）：%r" % read_baton(sdir)["holder"])
        code, out = call_relay("PreToolUse", project, "T", ident_fn(p3), extra=write)
        require(is_denied(out) and read_baton(sdir)["holder"]["pid"] == p2["pid"], "P3 应被拒（P2 已是 holder 且活着）")
        details.append("A 死 → P2（同 sid）直接写挂靠成 holder；P3（他 sid）被拒；登记册=P2")
        # 复现 B：P1 死后两个 --resume S 进程交替直接写
        project = os.path.join(work, "p2")
        os.makedirs(project)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        q1_proc = pool.spawn_claude()
        q1 = pool.identity(q1_proc, "S")
        r1 = pool.identity(pool.spawn_claude(), "S")
        r2 = pool.identity(pool.spawn_claude(), "S")
        call_relay("SessionStart", project, "S", ident_fn(q1), extra={"source": "startup"})
        pool.kill(q1_proc)
        outcomes = [not is_denied(call_relay("PreToolUse", project, "S", ident_fn(rec), extra=write)[1]) for rec in (r1, r2, r1, r2)]
        require(outcomes == [True, False, True, False], "r1,r2,r1,r2 应只有 r1 获准：%r" % outcomes)
        details.append("P1 死后 r1、r2（同 sid）交替直接写：只有先到的 r1 获准")
        # 交接中：同 sid 新窗口被 block
        project = os.path.join(work, "p3")
        os.makedirs(project)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "S")
        a2 = pool.identity(pool.spawn_claude(), "S")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("uB"))
        pool.kill(a_proc)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(a2), extra=write)
        require(is_denied(out) and read_baton(sdir)["holder"]["pid"] == a["pid"], "交接中 A 死后同 sid 直接写：拒绝、登记册不动")
        code, out = call_relay("UserPromptSubmit", project, "S", ident_fn(a2))
        require(is_blocked(out), "UserPromptSubmit 被 block")
        details.append("交接中 A 死后同 sid 新窗口：直接写被拒、提示被 block")
    return details


def case_h33():
    """H33（R2 P0-2）：项目里预植同名 `./handoffd`——旁观会话没有 Bash；B 只认裸 handoffd 或安装绝对路径，别处同名文件一律拒。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = os.path.join(work, "p")
        os.makedirs(project)
        script = os.path.join(project, "handoffd")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\nprintf changed > side-effect.txt\n")
        os.chmod(script, 0o755)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        s = pool.identity(pool.spawn_claude(), "sS")
        b = pool.identity(pool.spawn_claude(), "uB")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        for command in ("./handoffd status", "%s status" % script, "handoffd status", "bin/handoffd status"):
            require(is_denied(call_relay("PreToolUse", project, "sS", ident_fn(s), extra=tool_call("Bash", command=command))[1]), "旁观没有 Bash：%r" % command)
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("uB"))
        call_relay("SessionStart", project, "uB", ident_fn(b), extra={"source": "startup"})
        for command in ("./handoffd status", "%s status" % script, "bin/handoffd status"):
            require(is_denied(call_relay("PreToolUse", project, "uB", ident_fn(b), extra=tool_call("Bash", command=command))[1]), "B 不认别处的同名文件：%r" % command)
        require(call_relay("PreToolUse", project, "uB", ident_fn(b), extra=tool_call("Bash", command="handoffd status"))[1] is None, "B 的裸 handoffd 放行")
        require(not os.path.exists(os.path.join(project, "side-effect.txt")), "测试本身不执行命令")
    return ["旁观：任何 Bash 都拒；B：./handoffd、项目内绝对路径、bin/handoffd 全拒，裸 handoffd 放行"]


def case_h34():
    """H34（R2 P0-3）：B 已上线且存活，另一进程沿用 B 的会话号——在三种入口（SessionStart / UserPromptSubmit / PreToolUse）都不能覆盖 b 槽、不能得到 B 的写例外；B 死后才可接替。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = os.path.join(work, "p")
        os.makedirs(project)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b_proc = pool.spawn_claude()
        b = pool.identity(b_proc, "uB")
        b2 = pool.identity(pool.spawn_claude(), "uB")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("uB"))
        call_relay("SessionStart", project, "uB", ident_fn(b), extra={"source": "startup"})
        require(read_baton(sdir)["handoff"]["b"]["pid"] == b["pid"], "B 上线登记")
        send = tool_call("SendMessage", to="sA")
        verify = tool_call("Bash", command="handoffd status")
        for event, extra in (("SessionStart", {"source": "resume"}), ("UserPromptSubmit", {}), ("PreToolUse", send), ("PreToolUse", verify)):
            code, out = call_relay(event, project, "uB", ident_fn(b2), extra=extra)
            require(read_baton(sdir)["handoff"]["b"]["pid"] == b["pid"], "%s：b 槽里的活进程不得被覆盖" % event)
            if event == "PreToolUse":
                require(is_denied(out), "第二个 B 进程不得得到 B 的例外：%r" % (extra,))
        for extra in (send, verify):
            require(call_relay("PreToolUse", project, "uB", ident_fn(b), extra=extra)[1] is None, "真 B 照常")
        pool.kill(b_proc)
        code, out = call_relay("PreToolUse", project, "uB", ident_fn(b2), extra=send)
        require(out is None and read_baton(sdir)["handoff"]["b"]["pid"] == b2["pid"], "B 已死：第二个进程可接替（CAS 换槽）")
    return ["b 槽活进程不被同 sid 的第二进程覆盖（SessionStart / UserPromptSubmit / PreToolUse×2 全拒）；B 死后才可接替"]


def case_h35():
    """H35（C 类）：响铃 / 事件 / 会话状态等副作用抛错不改放行决定（旁观写仍拒、block 仍 block）；响铃失败不落标记。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = os.path.join(work, "p")
        os.makedirs(project)
        sdir = set_auto(project)
        a = pool.identity(pool.spawn_claude(), "sA")
        s = pool.identity(pool.spawn_claude(), "sS")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        sessions.write_heartbeat(sdir, util.now_ms() - 300_000)   # 心跳已停 ⇒ 每个事件都会尝试响铃
        write = tool_call("Write", file_path="/x", content="c")

        def failing(*args):
            raise OSError("spawn failed")

        for _ in range(3):
            code, out = call_relay("PreToolUse", project, "sS", ident_fn(s), rings=failing, extra=write)
            require(code == 0 and is_denied(out), "心跳铃起不来：旁观写仍被拒（R2 Opus P1）：%r" % out)
        require(call_relay("PreToolUse", project, "sA", ident_fn(a), rings=failing, extra=write)[1] is None, "holder 仍放行")
        require(not os.path.exists(sessions.run_path(sdir, "hb_stale")), "响铃失败不落标记")
        originals = (sessions.update_session, sessions.append_event, sessions.append_notice, sessions.create_marker, sessions.clear_marker)

        def boom(*args, **kwargs):
            raise OSError("disk")

        sessions.update_session = sessions.append_event = sessions.append_notice = sessions.create_marker = sessions.clear_marker = boom
        try:
            code, out = call_relay("PreToolUse", project, "sS", ident_fn(s), extra=write)
            require(code == 0 and is_denied(out), "副作用全抛错：旁观写仍被拒：%r" % out)
            code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=write)
            require(code == 0 and out is None, "副作用全抛错：holder 仍放行")
            a_dead = {"session_id": "sD", "pid": 41999, "lstart": "x y z"}
            write_baton(sdir, holder=holder_of(a_dead), handoff=handoff_with_b("uB"))
            with fake_liveness({a_dead["pid"]: "dead", s["pid"]: "live"}):
                code, out = call_relay("UserPromptSubmit", project, "sS", ident_fn(s))
            require(is_blocked(out), "副作用全抛错：交接中主控死后仍 block：%r" % out)
        finally:
            (sessions.update_session, sessions.append_event, sessions.append_notice, sessions.create_marker, sessions.clear_marker) = originals
    return ["ring_fn 抛错 ×3 次 PreToolUse：旁观写仍拒、不落标记；update_session / append_* / marker 全抛错：拒写 / 放行 / block 决定不变"]


def case_h36():
    """H36：classify 的代码结构——每个 `return "holder"/"holder_sub"/"b"` 上方 3 行内必有 `_same_process`（放行角色只来自三元组相等）；policy 放行角色集合只有 holder / holder_sub / b / fail_open。"""
    lines = open(os.path.join(MVP_ROOT, "cdm", "baton.py"), encoding="utf-8").read().splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("def classify("))
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("def "))
    found = 0
    for index in range(start, end):
        if 'return "holder"' in lines[index] or 'return "holder_sub"' in lines[index] or 'return "b"' in lines[index]:
            found += 1
            require(any("_same_process" in line for line in lines[max(start, index - 3):index + 1]),
                    "baton.py:%d 产出可放行角色，但 3 行内没有 _same_process：%s" % (index + 1, lines[index].strip()))
    require(found == 3, "classify 里应恰有 3 处产出放行角色，实际 %d" % found)
    policy_source = open(os.path.join(MVP_ROOT, "cdm", "policy.py"), encoding="utf-8").read()
    body = policy_source.split("def decide_tool")[1].split('"""', 2)[2]
    require('"vacant"' not in body and '"unknown"' not in body and '"spectator"' not in body, "decide_tool 不得再把 vacant / unknown / spectator 当放行角色")
    return ["classify 产出放行角色的 3 处都紧随 _same_process；decide_tool 放行集合只有 holder / holder_sub / b（+ 单列的 fail_open）"]


def main():
    """运行 H30–H36。"""
    return run_cases([("H30", case_h30), ("H31", case_h31), ("H32", case_h32), ("H33", case_h33), ("H34", case_h34), ("H35", case_h35), ("H36", case_h36)])


if __name__ == "__main__":
    sys.exit(main())
