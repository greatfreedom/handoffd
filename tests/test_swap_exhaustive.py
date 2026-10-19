# 测试 H42–H44、H48：handoffd S2a 的不变量验收（穷举 / 崩溃点重启 / 交错 / 目标核对），不是场景清单。
#   H42 commit 条件矩阵：a_signed × b_signed × B 三元组 × B 存活 × A 签后 Stop 数组 × A 存活 × holder 槽 × phase，断言「commit ⇔ 设计:5 条件」
#   H43 崩溃点重启：正常交接每个落盘点注入 daemon 重启（J2①：开窗 / OPEN / BATON / commit / SIGTERM 各至多一次，并断言最终结局）
#   H44 A、B、第三会话与 daemon tick 的 ≤4 步交错（含杀任一进程、Owner off），每步守 J1、J5（S1 的 I、II）
#   H48 外部动作的目标核对（J2②）：pid 复用 / cwd 不在项目下 ⇒ 不发信号、不投递
import itertools
import os
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import require, run_cases  # noqa: E402
from relay_helpers import Rings, call_relay, ident_fn, is_denied, read_baton, write_baton  # noqa: E402
from swap_helpers import OPENING, b_comes_online, full_handoff_until_sealed, scenario, start_handoff  # noqa: E402
from test_authz import check_call  # noqa: E402
from cdm import baton, sessions, util  # noqa: E402


class CrashPoint(BaseException):
    """模拟 daemon 进程在某个落盘点崩溃（BaseException：穿过 tick 的 except Exception）。"""


# ---------------------------------------------------------------- H42

A_SIGNED = ("none", "yes")
B_SIGNED = ("none", "ok", "wrong_sid", "wrong_triple")
B_STATE = ("live", "dead", "unknown")
A_STATE = ("live", "dead", "unknown")
STOP = ("none", "before_sign", "after_tasks", "after_crons", "after_empty", "after_unknown", "other_proc_empty")   # other_proc_empty：同 sid 的他进程签后出现过空数组 Stop（K2：不算）
HOLDER = ("is_a", "other")
PHASE = ("arming", "committed")
ROLL = ("same_sid", "rolled_sid")   # 签字时 A 在另一个会话号里（/clear、/resume 换过号）：Stop 序号与会话号无关，结论不变（K2 补全）


def oracle_commit(a_signed, b_signed, b_state, a_state, stop, holder, phase, roll="same_sid"):
    """设计:5 的 commit 条件（独立于实现的手写判定）：a_signed ∧ b_signed（会话号 == B.uuid ∧ 三元组 == b 槽）∧ B live ∧
    （A 已死 ∨ （A live ∧ A 签后出现一次 Stop 且 tasks / crons 皆空））∧ holder 槽三元组 == a_signed 三元组 ∧ phase 还没 committed。"""
    return (phase == "arming" and a_signed == "yes" and b_signed == "ok" and b_state == "live" and holder == "is_a"
            and (a_state == "dead" or (a_state == "live" and stop == "after_empty")))


def build_cell(sc, hid, a, b, c, cell):
    a_signed, b_signed, b_state, a_state, stop, holder, phase, roll = cell
    now = sc.clock.ms
    for rec, state in ((a, a_state), (b, b_state)):
        entry = sc.world.table[rec["pid"]]
        entry["alive"] = state != "dead"
        if state == "unknown":
            sc.world.unknown.add(rec["pid"])
        else:
            sc.world.unknown.discard(rec["pid"])
    handoff = baton.new_handoff(hid, "goal", {"sid": "sA", "pid": a["pid"], "lstart": a["lstart"]}, "a-name", "uB", "b-name", now - 60_000)
    handoff["launch"].update(state="opened", attempt=1, started_ms=now - 50_000, exit=0)
    handoff["b"].update(pid=b["pid"], lstart=b["lstart"], online_ms=now - 40_000)
    handoff["kit"] = {"sha": "0" * 64, "opening": OPENING, "sealed_ms": now - 30_000, "path": "kits/%s.md" % hid}
    handoff["open"] = {"state": "sent", "ms": now - 20_000, "fails": 0}
    if a_signed == "yes":
        handoff["a_signed"] = {"ms": now - 5_000, "by": "A", "sid": "sA" if roll == "same_sid" else "sOld", "pid": a["pid"], "lstart": a["lstart"],
                               "stop_n": 1 if stop == "before_sign" else 0}
    if b_signed != "none":
        signed = {"ms": now - 4_000, "sid": "uB", "pid": b["pid"], "lstart": b["lstart"]}
        if b_signed == "wrong_sid":
            signed["sid"] = "someone-else"
        elif b_signed == "wrong_triple":
            signed["lstart"] = "Mon Jan  1 00:00:00 2001"
        handoff["b_signed"] = signed
    if phase == "committed":
        handoff["phase"] = "committed"
        handoff["committed_ms"] = now - 1_000
        handoff["post"] = {"a_old": {"sid": "sA", "pid": a["pid"], "lstart": a["lstart"]},
                           "kill": {"state": "sent", "ms": now - 900, "reason": None}, "baton": {"state": "sent", "ms": now - 800, "fails": 0}}
    holder_slot = {"sid": "sA", "pid": a["pid"], "lstart": a["lstart"], "claimed_ms": 1} if holder == "is_a" else \
        {"sid": "sC", "pid": c["pid"], "lstart": c["lstart"], "claimed_ms": 1}
    write_baton(sc.sdir, holder=holder_slot, handoff=handoff)
    def put(rec, n, tasks, crons):
        util.atomic_write_json(sessions.proc_stop_path(sc.sdir, rec["pid"], rec["lstart"]), {"n": n, "tasks": tasks, "crons": crons, "ms": now - 1_000})

    for rec in (a, c):
        try:
            os.unlink(sessions.proc_stop_path(sc.sdir, rec["pid"], rec["lstart"]))
        except FileNotFoundError:
            pass
    spec = {"before_sign": (1, 0, 0), "after_tasks": (1, 2, 0), "after_crons": (1, 0, 1), "after_empty": (1, 0, 0), "after_unknown": (1, None, None)}
    if stop in spec:   # A 的进程与 C 的进程都放同样的记录：holder 槽指向谁，实现若不核三元组就会拿错的那个去放行
        for rec in (a, c):
            put(rec, *spec[stop])
    elif stop == "other_proc_empty":
        put(c if holder == "is_a" else a, 1, 0, 0)   # 不是 holder 槽进程的另一个进程的 Stop（同会话号）


def case_h42():
    """H42：commit 条件矩阵 2×4×3×3×7×2×2×2 = 4032 格：每格先直接问 baton.commit_check、再跑一次 daemon tick，断言「commit ⇔ 设计:5 条件」，且 commit 只写 holder=B 并至多一次；
    不空转（既有 commit 也有不 commit）。committed 格里 holder 仍指向 A 是为了探「同一 handoff 至多一次」。"""
    cells = commits = no_commits = 0
    bad = []
    with scenario() as sc:
        a = sc.spawn("sA")
        b = sc.spawn("uB")
        c = sc.spawn("sC")
        dm = sc.daemon()
        for index, cell in enumerate(itertools.product(A_SIGNED, B_SIGNED, B_STATE, A_STATE, STOP, HOLDER, PHASE, ROLL)):
            hid = "h-cell-%d" % index
            build_cell(sc, hid, a, b, c, cell)
            before = read_baton(sc.sdir)
            expected = oracle_commit(*cell)
            # 条件判定函数本身（不经 daemon 的前置门）也必须与设计:5 一致
            checked = baton.commit_check(before, sc.world.proc_state, lambda pid, lstart: sessions.read_proc_stop(sc.sdir, pid, lstart))[0]
            if checked != expected:
                bad.append("%r：commit_check=%s，期望 %s" % (cell, checked, expected))
            sc.tick(dm)
            after = read_baton(sc.sdir)
            did_commit = baton.triple(after["holder"]) == (b["pid"], b["lstart"]) and baton.triple(before["holder"]) != baton.triple(after["holder"])
            retired = len(after["retire"]) - len(before["retire"])
            cells += 1
            if did_commit != expected or retired != (1 if expected else 0):
                bad.append("%r：期望 commit=%s，实际 holder %r → %r，retire +%d" % (cell, expected, baton.triple(before["holder"]), baton.triple(after["holder"]), retired))
            elif not expected and baton.triple(after["holder"]) != baton.triple(before["holder"]):
                bad.append("%r：不该 commit 但 holder 变了" % (cell,))
            if expected:
                commits += 1
            else:
                no_commits += 1
    require(cells == 4032, "格数不对：%d" % cells)
    require(not bad, "commit ⇔ 设计:5 条件：%d 格违反，例：%r" % (len(bad), bad[:3]))
    require(commits == 16 and no_commits == 4016, "不应空转：应 commit 恰 8 格（A 已死 × Stop 7 态，加 A 活 × 自己进程的空 Stop，各 × 会话号 2 态），实际 commit %d 格、不 commit %d 格" % (commits, no_commits))
    return ["%d 格（a_signed 2 × b_signed 4 × B 存活 3 × A 存活 3 × Stop 7（含同 sid 他进程的 Stop）× holder 槽 2 × phase 2 × 签字时会话号 2）：违反 0；应 commit %d 格、不应 commit %d 格" % (cells, commits, no_commits)]


# ---------------------------------------------------------------- H43

def run_normal_flow(sc, dm_factory, crash_plan=None, restart_every_tick=False, max_steps=70):
    """脚本化的正常交接，会话侧动作按「看到了什么」触发；返回 (统计 dict, 各落盘点出现次数)。
    crash_plan = (标签, 第几次出现)：到那一处抛 CrashPoint，随后「重启」= 换一个新的 Daemon 对象继续跑。"""
    counts = {}
    crashed = {"n": 0}

    def point(label):
        counts[label] = counts.get(label, 0) + 1
        if crash_plan and crash_plan == (label, counts[label]):
            crashed["n"] += 1
            raise CrashPoint(label)

    state = {"dm": dm_factory(point)}
    a = sc.spawn("sA", name="a-name")
    sc.hook("SessionStart", a, source="startup")
    hid, uuid = start_handoff(sc, a)
    box = {}
    sc.world.on_launch = lambda handoff: box.update(b=b_comes_online(sc, uuid))
    progress = {"sealed": False, "signed": False, "stopped": False}
    for step in range(max_steps):
        sc.clock.advance(5)
        try:
            state["dm"].tick(sc.clock.ms)
        except CrashPoint:
            state["dm"] = dm_factory(lambda label: counts.__setitem__(label, counts.get(label, 0) + 1))   # 重启：新对象，不再崩
            continue
        if restart_every_tick:
            state["dm"] = dm_factory(point)
        b = box.get("b")
        handoff = sc.handoff()
        if b and not progress["sealed"] and handoff and handoff["b"].get("online_ms"):
            require(sc.cli(["seal", "--kit", sc.write_kit()], a)[0] == 0, "seal")
            progress["sealed"] = True
        if b and progress["sealed"] and not progress["signed"] and OPENING in sc.world.messages_to(b):
            require(sc.cli(["sign", "in"], b)[0] == 0 and sc.cli(["sign", "out"], a)[0] == 0, "双签")
            progress["signed"] = True
            progress["signed_step"] = step
        if progress["signed"] and not progress["stopped"] and step > progress["signed_step"]:   # A 的 Stop 晚一个 tick：commit 先「等」一次再成立
            sc.stop(a)
            progress["stopped"] = True
        if progress["stopped"] and sc.handoff() is None:
            break
    stats = {
        "launches": len(sc.world.launches),
        "opens": sum(1 for _pid, content in sc.world.messages if content == OPENING),
        "batons": sum(1 for _pid, content in sc.world.messages if "BATON" in content),
        "kills": len(sc.world.kills),
        "commits": sc.count_commits(),
        "complete": sc.handoff() is None and progress["stopped"],
        "holder_is_b": bool(box.get("b")) and sc.state()["holder"] is not None and sc.state()["holder"]["pid"] == box["b"]["pid"],
        "notice_kinds": sc.notice_kinds(),
        "crashed": crashed["n"],
        "a_alive": sc.world.table[a["pid"]]["alive"],
    }
    return stats, counts


EXPECT = {   # 标签 → 崩在此处之后的最终结局：complete = 交接最终完成；stuck:<响铃> = 动作丢了、交接停住并响该铃（让 Owner 知道）
    "launch:marker": ("stuck", "b_not_online"),
    "launch:acted": ("complete", None),
    "launch:recorded": ("complete", None),
    "open:marker": ("stuck", "open_ambiguous"),
    "open:acted": ("complete", "open_ambiguous"),
    "open:recorded": ("complete", None),
    "commit:before": ("complete", None),
    "commit:after": ("complete", None),
    "kill:marker": ("complete", "old_a_alive"),
    "kill:acted": ("complete", None),
    "kill:recorded": ("complete", None),
    "baton:marker": ("complete", "baton_ambiguous"),
    "baton:acted": ("complete", "baton_ambiguous"),
    "baton:recorded": ("complete", None),
    "complete:before": ("complete", None),
    "complete:after": ("complete", None),
}


def case_h43():
    """H43（J2①）：在正常交接的每个落盘点注入 daemon 重启（标记已落 / 动作已做 / 结果已记各一处；首次与最后一次出现各一轮），
    再加「每个 tick 之间都重启」一轮：开窗 / OPEN / BATON / commit / SIGTERM 各至多一次；结局符合 EXPECT（完成，或动作丢了且响铃）。"""
    details = []
    runs = 0

    def factory_for(sc):
        return lambda point: sc.daemon(point_fn=point)

    with scenario() as sc:
        base_stats, base_counts = run_normal_flow(sc, factory_for(sc))
    require(base_stats["complete"] and base_stats["launches"] == base_stats["opens"] == base_stats["batons"] == base_stats["kills"] == base_stats["commits"] == 1,
            "基线：不崩的正常交接各动作恰好 1 次且完成：%r" % base_stats)
    require(set(EXPECT) <= set(base_counts), "落盘点标签与实现不符：%r" % sorted(set(EXPECT) ^ set(base_counts)))
    bad = []
    outcomes = {}
    for label in EXPECT:
        occurrences = sorted({1, base_counts[label]})
        for occurrence in occurrences:
            runs += 1
            with scenario() as sc:
                stats, _counts = run_normal_flow(sc, factory_for(sc), crash_plan=(label, occurrence))
                key = "%s#%d" % (label, occurrence)
                if stats["crashed"] != 1:
                    bad.append("%s：没有真的崩到" % key)
                    continue
                for name in ("launches", "opens", "batons", "kills", "commits"):
                    if stats[name] > 1:
                        bad.append("%s：%s 做了 %d 次（至多一次）" % (key, name, stats[name]))
                want, ring = EXPECT[label]
                if want == "complete" and not stats["complete"]:
                    bad.append("%s：应最终完成：%r" % (key, stats))
                if want == "stuck" and stats["complete"]:
                    bad.append("%s：动作丢了应停住：%r" % (key, stats))
                if ring and ring not in stats["notice_kinds"]:
                    bad.append("%s：应响铃 %s：%r" % (key, ring, stats["notice_kinds"]))
                if not ring and any(kind in stats["notice_kinds"] for kind in ("open_ambiguous", "b_not_online", "old_a_alive", "baton_ambiguous")):
                    bad.append("%s：不该响铃：%r" % (key, stats["notice_kinds"]))
                if label.startswith("launch:marker"):
                    pass
                outcomes[key] = (want if not (want == "complete" and ring) else "complete+铃", stats["launches"], stats["opens"], stats["batons"], stats["kills"], stats["commits"])
    require(not bad, "崩溃点重启：%d 处违反：%r" % (len(bad), bad[:4]))
    details.append("%d 轮崩溃点重启（%d 个落盘点标签 × 首次 / 最后一次出现）：开窗 / OPEN / BATON / commit / SIGTERM 均 ≤1 次；结局：完成 %d 轮，动作丢失且响铃 %d 轮"
                   % (runs, len(EXPECT), sum(1 for v in outcomes.values() if v[0].startswith("complete")), sum(1 for v in outcomes.values() if v[0] == "stuck")))
    runs += 1
    with scenario() as sc:
        stats, _counts = run_normal_flow(sc, factory_for(sc), restart_every_tick=True)
    require(stats["complete"] and stats["launches"] == stats["opens"] == stats["batons"] == stats["kills"] == stats["commits"] == 1,
            "每个 tick 之间都重启：各动作恰好 1 次且完成：%r" % stats)
    details.append("每个 tick 之间都重启（新 Daemon 对象）：各动作恰好 1 次，交接完成")
    return details


# ---------------------------------------------------------------- H44

STEPS = ("A.pre", "A.sign", "A.stop", "B.pre", "B.sign", "X.pre", "D.tick", "kill A", "kill B", "off")
# 第二套词汇：聚焦 B 上线接线（b_online）——B 的 SessionStart、同会话号的第二个进程 B2（hostile）的 SessionStart / 写、B 签字、daemon tick、杀 B
STEPS_B = ("B.start", "B2.start", "B2.pre", "B.sign", "D.tick", "kill A", "kill B", "adopt")
# 第三套词汇（K2）：同 sid 的第二进程 A2 的 Stop / SessionStart、签字、A 自己的 Stop、时钟回拨、daemon tick
STEPS_K = ("A.sign", "B.sign", "A.stop", "A2.stop", "A2.start", "clock-10s", "D.tick")
WRITE = {"file_path": "/x", "content": "c"}


def init_state(sc, a, b, signed, hid):
    """交错的初始登记册：A 持棒，B 已上线、kit 已封存、OPEN 已发；signed=True 时双签都已完成。"""
    now = sc.clock.ms
    handoff = baton.new_handoff(hid, "goal", {"sid": "sA", "pid": a["pid"], "lstart": a["lstart"]}, "a-name", "uB", "b-name", now - 60_000)
    handoff["launch"].update(state="opened", attempt=1, started_ms=now - 50_000, exit=0)
    handoff["b"].update(pid=b["pid"], lstart=b["lstart"], online_ms=now - 40_000)
    handoff["kit"] = {"sha": "0" * 64, "opening": OPENING, "sealed_ms": now - 30_000, "path": "kits/%s.md" % hid}
    handoff["open"] = {"state": "sent", "ms": now - 20_000, "fails": 0}
    if signed:
        handoff["a_signed"] = {"ms": now - 5_000, "by": "A", "sid": "sA", "pid": a["pid"], "lstart": a["lstart"], "stop_n": 0}
        handoff["b_signed"] = {"ms": now - 4_000, "sid": "uB", "pid": b["pid"], "lstart": b["lstart"]}
    write_baton(sc.sdir, holder={"sid": "sA", "pid": a["pid"], "lstart": a["lstart"], "claimed_ms": 1}, handoff=handoff)


def table_of(sc, recs):
    return {rec["pid"]: sc.world.proc_state(rec) for rec in recs}


def commit_oracle_state(sc, state, model):
    """test 侧独立的判定：「此刻 daemon tick 应不应 commit」。Stop 条件不读会话文件，而用 test 自己记的模型（model["latest"]：
    A **自己的进程**签字后最近一次 Stop 的数组是否为空；同 sid 他进程的 Stop、时钟都不进模型）。"""
    handoff, holder = state["handoff"], state["holder"]
    if not handoff or handoff.get("phase") != "arming" or not holder:
        return False
    a_signed, b_signed = handoff.get("a_signed"), handoff.get("b_signed")
    if not a_signed or not b_signed:
        return False
    if baton.triple(holder) != baton.triple(a_signed):
        return False
    slot = handoff["b"]
    if b_signed["sid"] != slot["uuid"] or baton.triple(b_signed) != baton.triple(slot) or sc.world.proc_state(slot) != "live":
        return False
    a_state = sc.world.proc_state(holder)
    if a_state == "dead":
        return True
    return a_state == "live" and model["latest"] == "empty"


def run_step(sc, dm, step, actors):
    """执行一步，返回 (事件, 工具, 钩子输出, 身份记录)；不是钩子步骤返回 (None, None, None, None)。"""
    a, b = actors["A"], actors["B"]
    if step.endswith(".pre") or step.endswith(".start"):
        rec = actors[step.split(".")[0]]
        if sc.world.proc_state(rec) == "dead":
            return None, None, None, None
        if step.endswith(".pre"):
            out = call_relay("PreToolUse", sc.project, rec["session_id"], ident_fn(rec), rings=Rings(),
                             extra={"tool_name": "Write", "tool_input": WRITE})[1]
            return "PreToolUse", "Write", out, rec
        out = call_relay("SessionStart", sc.project, rec["session_id"], ident_fn(rec), rings=Rings(), extra={"source": "startup"})[1]
        return "SessionStart", None, out, rec
    if step == "A.sign":
        if sc.world.proc_state(a) != "dead":
            sc.cli(["sign", "out"], a)
    elif step == "B.sign":
        if sc.world.proc_state(b) != "dead":
            sc.cli(["sign", "in"], b)
    elif step == "A.stop":
        if sc.world.proc_state(a) != "dead":
            sc.stop(a)
    elif step == "A2.stop":
        if sc.world.proc_state(actors["A2"]) != "dead":
            sc.stop(actors["A2"])
    elif step == "clock-10s":
        sc.clock.ms -= 10_000
    elif step == "D.tick":
        sc.tick(dm)
    elif step == "kill A":
        sc.world.die(a)
    elif step == "kill B":
        sc.world.die(b)
    elif step == "off":
        sc.cli(["off"], None)
    elif step == "adopt":
        sc.cli(["adopt"], None)
    return None, None, None, None


def explore(sc, dm, actors, steps, counter):
    """steps 词汇上长度 1–4 的全部序列 × 2 种初始登记册；counter 累计 (序列数, 步数, commit 次数)；返回违反列表。"""
    a, b = actors["A"], actors["B"]
    recs = tuple(actors.values())
    violations = []
    base_ms = sc.clock.ms
    for length in range(1, 5):
        for seq in itertools.product(steps, repeat=length):
            for signed in (False, True):
                counter[0] += 1
                sc.clock.ms = base_ms
                model = {"latest": None}
                for name in ("notices.jsonl", "handoffs.jsonl", "events.jsonl"):   # 这些日志每轮都会被读（补响 / 归档去重），不清会随序列数二次增长
                    try:
                        os.unlink(os.path.join(sc.sdir, name))
                    except FileNotFoundError:
                        pass
                for rec in recs:
                    sc.world.table[rec["pid"]]["alive"] = True
                sc.world.messages.clear()
                sc.world.kills.clear()
                sessions.write_mode(sc.sdir, "auto", sc.clock.ms - 10_000)
                path = sessions.session_path(sc.sdir, "sA")
                if os.path.exists(path):
                    os.unlink(path)
                shutil.rmtree(os.path.join(sc.sdir, "procs"), ignore_errors=True)   # 每进程 Stop 记录也清（否则上一条序列的 Stop 会被当成签后 Stop）
                init_state(sc, a, b, signed, "h-seq-%d" % counter[0])
                commits_here = 0
                for index, step in enumerate(seq):
                    before = read_baton(sc.sdir)
                    should_commit = step == "D.tick" and sessions.read_mode(sc.sdir)[0] == "auto" and commit_oracle_state(sc, before, model)
                    event, tool, out, rec = run_step(sc, dm, step, actors)
                    signed_before = bool(before["handoff"] and before["handoff"].get("a_signed"))
                    counter[1] += 1
                    after = read_baton(sc.sdir)
                    signed_after = bool(after["handoff"] and after["handoff"].get("a_signed"))
                    if step == "A.sign" and not signed_before and signed_after:
                        model["latest"] = None                        # 刚签字：之前的 Stop 不算
                    elif step == "A.stop" and sc.world.proc_state(a) != "dead" and signed_after:
                        model["latest"] = "empty"                     # A 自己的进程签后的 Stop（数组为空）
                    label = "初始=%s 序列=%r 第 %d 步 %s" % ("双签" if signed else "未签", seq, index + 1, step)
                    if event and sessions.read_mode(sc.sdir)[0] == "auto":   # off 之后是 manual：规格:76 立即放行，不适用单写者判据
                        me = (rec["pid"], rec["lstart"])
                        v1, v2 = check_call(before, after, event, tool, out, rec["session_id"], me, [me], True, table_of(sc, recs))
                        if v1 or v2:
                            violations.append("%s：%r %r" % (label, v1, v2))
                    holder_changed = baton.triple(before["holder"]) != baton.triple(after["holder"])
                    if holder_changed:
                        to_b = baton.triple(after["holder"]) == (b["pid"], b["lstart"]) and before["handoff"] is not None
                        if to_b and step == "D.tick" and should_commit:
                            commits_here += 1
                        elif event and before["handoff"] is None and before["holder"] is not None and sc.world.proc_state(before["holder"]) == "dead":
                            pass   # 死者的认领 CAS（S1 路径）
                        elif event and before["holder"] is None:
                            pass
                        else:
                            violations.append("%s：holder 不该变：%r → %r" % (label, before["holder"], after["holder"]))
                    if step == "D.tick" and should_commit and not (holder_changed and baton.triple(after["holder"]) == (b["pid"], b["lstart"])):
                        violations.append("%s：commit 条件成立但没换棒" % label)
                    if step == "D.tick" and not should_commit and holder_changed:
                        violations.append("%s：条件不成立却换了 holder" % label)
                counter[2] += commits_here
                if commits_here > 1 or sum(1 for item in read_baton(sc.sdir)["retire"] if item.get("reason") == "handoff_commit") > 1:
                    violations.append("序列=%r：commit 多于 1 次" % (seq,))
                killed_a = [k for k in sc.world.kills if k["pid"] == a["pid"]]
                killed_b = [k for k in sc.world.kills if k["pid"] == b["pid"]]
                if (len(killed_a) > 1 or len(killed_b) > 1 or len(killed_a) + len(killed_b) != len(sc.world.kills)
                        or (killed_b and "off" not in seq)):
                    violations.append("初始=%s 序列=%r：SIGTERM 次数 / 目标不对（A 只由 daemon 的 commit 后一次；B 只由 off 一次）：%r" % (signed, seq, sc.world.kills))
                if killed_a and any(k["phase"] != "committed" or baton.triple(k["holder"]) != (b["pid"], b["lstart"]) for k in killed_a):
                    violations.append("初始=%s 序列=%r：向 A 发信号时登记册不是已换棒状态：%r" % (signed, seq, killed_a))
                if any(pid != b["pid"] for pid, _content in sc.world.messages) or len(sc.world.messages) > 1:
                    violations.append("初始=%s 序列=%r：投递只应给 B 且至多 1 条（BATON）：%r" % (signed, seq, sc.world.messages))
                if sessions.read_mode(sc.sdir)[0] == "auto":
                    saved = open(baton.baton_path(sc.sdir), "rb").read()
                    alive = [rec for rec in recs if sc.world.proc_state(rec) != "dead"]
                    for order in (alive, list(reversed(alive))):
                        permitted = []
                        for rec in order:
                            probe = call_relay("PreToolUse", sc.project, rec["session_id"], ident_fn(rec), rings=Rings(),
                                               extra={"tool_name": "Write", "tool_input": WRITE})[1]
                            if not is_denied(probe):
                                permitted.append(rec["pid"])
                        with open(baton.baton_path(sc.sdir), "wb") as stream:
                            stream.write(saved)
                        if len(permitted) > 1:
                            violations.append("初始=%s 序列=%r：结束探测 %r 都获准写" % (signed, seq, permitted))
    return violations


def case_h44():
    """H44（J1、J5）：A、B、第三会话 X、同会话号的第二个 B 进程 B2 与 daemon tick 的 ≤4 步交错 × 2 种初始登记册（未签 / 双签已完成）：
    词汇一：A.pre / A.sign / A.stop / B.pre / B.sign / X.pre / daemon tick / 杀 A / 杀 B / Owner off；词汇二（聚焦 b_online 与 adopt）：B.start / B2.start / B2.pre / B.sign / tick / 杀 A / 杀 B / Owner adopt；词汇三（K2）：A.sign / B.sign / A.stop / A2.stop / A2.start / 时钟回拨 / tick。
    每步守 S1 的 I、II（check_call）；holder 只在「daemon tick 且 commit 条件成立」时变成 B、其余变化只能是死者的认领 CAS；commit ⇔ 条件；
    每个序列 commit ≤1、SIGTERM A ≤1 且只在已换棒之后、off 的 SIGTERM 只打未接棒的 B、投递只发给 B；序列末探测：非 manual 时获准写的进程 ≤1。"""
    counter = [0, 0, 0]
    violations = []
    with scenario() as sc:
        actors = {"A": sc.spawn("sA"), "B": sc.spawn("uB"), "X": sc.spawn("sX")}
        actors["B2"] = sc.spawn("uB")   # 同会话号的第二个进程
        actors["A2"] = sc.spawn("sA")   # 同会话号的第二个进程（K2）
        dm = sc.daemon()
        violations += explore(sc, dm, actors, STEPS, counter)
        first = counter[0]
        violations += explore(sc, dm, actors, STEPS_B, counter)
        second = counter[0] - first
        violations += explore(sc, dm, actors, STEPS_K, counter)
        third = counter[0] - first - second
    require(first == 2 * sum(len(STEPS) ** n for n in range(1, 5)) and second == 2 * sum(len(STEPS_B) ** n for n in range(1, 5))
            and third == 2 * sum(len(STEPS_K) ** n for n in range(1, 5)), "序列数不对：%d + %d + %d" % (first, second, third))
    require(not violations, "交错违反 %d 条，例：%r" % (len(violations), violations[:3]))
    require(counter[2] > 500, "不应空转：commit 出现 %d 次" % counter[2])
    return ["%d + %d + %d 个序列 / %d 步（词汇一 10 步 + 词汇二 8 步 + 词汇三 7 步（同 sid 第二进程 A2 的 Stop / SessionStart、时钟回拨），长度 1–4 × 2 种初始登记册）：违反 0；其中 commit 出现 %d 次" % (first, second, third, counter[1], counter[2])]


# ---------------------------------------------------------------- H48

def case_h48():
    """H48（J2②）：外部动作的目标进程必须 == 登记册槽位三元组且 cwd 在项目下——pid 复用 / cwd 在项目外 ⇒ 不发信号、不投递，记 events。"""
    details = []
    # SIGTERM：commit 之后、kill 之前 A 的 pid 被另一个进程复用
    with scenario() as sc:
        a = sc.spawn("sA")
        def point(label):
            if label == "commit:after":
                sc.world.reuse_pid(a)

        dm = sc.daemon(point_fn=point)
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        sc.tick(dm)
        require(sc.world.kills == [], "pid 被复用（lstart 不同）：绝不向它发 SIGTERM：%r" % sc.world.kills)
        require(sc.state()["holder"]["pid"] == b["pid"], "换棒本身照常完成")
        require(sc.world.table[a["pid"]]["alive"], "被复用的无辜进程仍活着")
        details.append("SIGTERM：A 的 pid 在 commit 后被复用 ⇒ 三元组不符，不发信号")
    # SIGTERM：cwd 在项目外
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.world.table[a["pid"]]["cwd"] = os.path.dirname(sc.project)   # 项目的上一级：不在项目「下」
        sc.stop(a)
        sc.tick(dm)
        require(sc.world.kills == [] and sc.world.table[a["pid"]]["alive"], "cwd 不在项目下：不发 SIGTERM：%r" % sc.world.kills)
        require(sc.events("daemon:kill_skipped") and sc.events("daemon:kill_skipped")[0]["reason"] == "cwd_outside", "记 events：kill_skipped cwd_outside")
        details.append("SIGTERM：A 的 cwd 在项目外（含项目的上一级）⇒ 不发信号、记 events")
    # 子目录算「项目下」
    with scenario() as sc:
        a = sc.spawn("sA", cwd=None)
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.world.table[a["pid"]]["cwd"] = os.path.join(sc.project, "sub", "dir")
        sc.stop(a)
        sc.tick(dm)
        require(len(sc.world.kills) == 1, "cwd 在项目的子目录：仍算项目下，应发信号")
        details.append("cwd 在项目子目录里仍算「项目下」，照常发信号")
    # OPEN：B pid 复用 / B cwd 在项目外
    for label, mutate in (("pid 复用", lambda sc, b: sc.world.reuse_pid(b)),
                          ("cwd 在项目外", lambda sc, b: sc.world.table[b["pid"]].update(cwd="/somewhere/else"))):
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon()
            hid, uuid = start_handoff(sc, a)
            holder = {}
            sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
            sc.tick(dm)
            b = holder["b"]
            sc.cli(["seal", "--kit", sc.write_kit()], a)
            mutate(sc, b)
            for _ in range(3):
                sc.tick(dm)
            require(sc.world.messages == [] and sc.handoff()["open"]["state"] is None, "OPEN：B %s ⇒ 不投、不落标记：%r" % (label, sc.world.messages))
            require(sc.events("daemon:open_target_refused"), "记 events")
    details.append("OPEN：B 的 pid 复用 / cwd 在项目外 ⇒ 不投递、不落 sending 标记、记 events")
    # BATON：commit 后 B 的 cwd 在项目外 ⇒ 放弃（failed），不投
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        sc.world.table[b["pid"]]["cwd"] = "/somewhere/else"
        sc.tick(dm)
        require(sc.world.messages_to(b) == [OPENING], "BATON：B cwd 在项目外 ⇒ 不投：%r" % sc.world.messages)
        require(sc.events("daemon:baton_refused"), "记 events")
        details.append("BATON：B 的 cwd 在项目外 ⇒ 不投，记 baton_refused，交接照样收尾")
    return details


def main():
    """运行 H42、H43、H44、H48。"""
    return run_cases([("H42", case_h42), ("H43", case_h43), ("H44", case_h44), ("H48", case_h48)])


if __name__ == "__main__":
    sys.exit(main())
