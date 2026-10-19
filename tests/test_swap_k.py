# 测试 H51–H55：handoffd S2a R1 整合账的不变量验收（K1 意图必有结局、K2 换棒输入按进程身份、K3 动作前即时重核）与 A7–A11。
# 预期全部从规格 §3–§5 与整合账写出，**不调用被测实现的判定函数**（不用 commit_check / stop_after_sign / _target_ok 等）：
# 只看世界里发生了什么（信号、投递、进程存活、登记册里谁是 holder）与 notices.jsonl 里有没有「已响」的 notice。
#   H51  K1 崩溃点：对流程里**每一次**落盘写入（daemon / CLI / 钩子都算）前后各注入一次重启
#   H52  K1 注入：在流程每一步之间注入 Owner off / off 再 on / A 死 / B 死
#   H53  K2：A、同 sid 的第二进程 A2、B、daemon tick、时钟回拨的 ≤4 步交错
#   H54  K3：标记落盘后、动作发出前 PID 被复用（OPEN / SIGTERM A / BATON 各一条路径）
#   H55  A7（B 白名单带 --config）、A9（adopt 不补「签后崩」）、A10（重新 seal 清 b_signed）、A11（UserPromptSubmit 也登记 B 上线）
import itertools
import json
import os
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import require, run_cases  # noqa: E402
from swap_helpers import KIT_TEXT, OPENING, b_comes_online, full_handoff_until_sealed, scenario, start_handoff  # noqa: E402
from test_swap_exhaustive import init_state  # noqa: E402
from cdm import baton, policy, sessions, util  # noqa: E402


class CrashPoint(BaseException):
    """模拟进程在某次落盘写入前 / 后死亡（BaseException：穿过各处 except Exception）。"""


class WriteCrash:
    """拦截 util.atomic_write_text / util.append_jsonl（所有状态落盘都经这两个出口）：数写入次数，并在第 n 次写入前 / 后抛 CrashPoint。"""

    def __init__(self, plan=None):
        self.plan = plan          # (n, "before" | "after") 或 None
        self.count = 0
        self.fired = False

    def __enter__(self):
        self.orig_text, self.orig_append = util.atomic_write_text, util.append_jsonl

        def hit(kind):
            self.count += 1
            return bool(self.plan and self.plan[0] == self.count and not self.fired)

        def text(path, content):
            fire = hit("text")
            if fire and self.plan[1] == "before":
                self.fired = True
                raise CrashPoint("before write #%d" % self.count)
            self.orig_text(path, content)
            if fire and self.plan[1] == "after":
                self.fired = True
                raise CrashPoint("after write #%d" % self.count)

        def append(path, record):
            fire = hit("append")
            if fire and self.plan[1] == "before":
                self.fired = True
                raise CrashPoint("before write #%d" % self.count)
            result = self.orig_append(path, record)
            if fire and self.plan[1] == "after":
                self.fired = True
                raise CrashPoint("after write #%d" % self.count)
            return result

        util.atomic_write_text, util.append_jsonl = text, append
        return self

    def __exit__(self, *exc):
        util.atomic_write_text, util.append_jsonl = self.orig_text, self.orig_append
        return False


MAX_ITER = 34          # 每轮 5 秒（共 170 s）：覆盖 120 s / 60 s 的计时门槛
STUCK_RING = ("b_not_online", "b_died", "launcher_failed", "preflight_failed", "a_crash_unsealed", "a_crash_sealed", "open_ambiguous")


def read_notices(sc):
    """(notice 记录列表, 已响的 notice id 集合)。"""
    notices, rungs = [], set()
    try:
        with open(sessions.notices_path(sc.sdir), encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("type") == "notice" and item.get("cat") == "handoff":
                    notices.append(item)
                elif item.get("type") == "rung":
                    rungs.add(item.get("id"))
    except OSError:
        pass
    return notices, rungs


def archived(sc):
    out = []
    try:
        with open(os.path.join(sc.sdir, "handoffs.jsonl"), encoding="utf-8") as stream:
            out = [json.loads(line) for line in stream if line.strip()]
    except OSError:
        pass
    return out


def drive(sc, plan=None, inject=None, ignore=False):
    """脚本化的一次交接：A 申请 → 开窗（世界里起 B 进程）→ B 上线 → seal → 收到 OPEN 后 B sign in、A sign out → A 的 Stop → 之后只让 daemon 跑到 160 s。
    会话侧动作都是「看到状态才做」，所以 CLI / 钩子崩了下一轮会自己重做；daemon 崩了换一个新 Daemon 对象（重启）。
    plan=(n, before|after)：第 n 次落盘写入前 / 后崩；inject=(k, kind)：第 k 轮开头（交接已出现之后）注入 off / off_on / killA / killB。
    返回 facts。"""
    world = sc.world
    a = sc.spawn("sA", name="a-name")
    world.kill_ignored = ignore       # A 收到 SIGTERM 也不退出：换棒后的收尾期被拉长（⑤ 的 60 s 窗口），可在其中注入 off / 重启
    world.ignore_pids = {a["pid"]}
    box = {}
    world.on_launch = lambda handoff: box.setdefault("b", sc.spawn(handoff["b"]["uuid"], name=handoff["b"]["name"]))
    ctl = {"dm": sc.daemon(), "seen": False, "stopped": False, "injected": None, "first_seen": None}

    def guarded(fn):
        try:
            return fn()
        except CrashPoint:
            return "crashed"

    with WriteCrash(plan) as wc:
        for it in range(MAX_ITER):
            sc.clock.advance(5)
            handoff = sc.handoff()
            if handoff is not None and not ctl["seen"]:
                ctl["seen"], ctl["first_seen"] = True, it
            if inject and ctl["injected"] is None and ctl["seen"] and it >= ctl["first_seen"] + inject[0]:
                kind = inject[1]
                handoff = sc.handoff()
                ctl["injected"] = {"kind": kind, "it": it, "phase": handoff["phase"] if handoff else None,
                                   "b_registered": bool(handoff and handoff["b"].get("pid")), "b_proc": box.get("b")}
                if kind in ("off", "off_on"):
                    guarded(lambda: sc.cli(["off"], None))
                    if kind == "off_on":
                        guarded(lambda: sc.cli(["on"], None))
                elif kind == "killA":
                    world.die(a)
                elif kind == "killB" and box.get("b"):
                    world.die(box["b"])
            if guarded(lambda: ctl["dm"].tick(sc.clock.ms)) == "crashed":
                ctl["dm"] = sc.daemon()
            handoff = sc.handoff()
            b = box.get("b")
            a_alive = world.proc_state(a) == "live"
            b_alive = bool(b) and world.proc_state(b) == "live"
            if not ctl["seen"] and a_alive and (handoff is None):
                guarded(lambda: sc.hook("SessionStart", a, source="startup"))
                guarded(lambda: sc.cli(["request", "--reason", "goal"], a))
            if handoff and handoff["phase"] == "arming":
                if b_alive and not handoff["b"].get("online_ms"):
                    guarded(lambda: sc.hook("SessionStart", b, source="startup"))
                if handoff["b"].get("online_ms") and not handoff["kit"] and a_alive:
                    guarded(lambda: sc.cli(["seal", "--kit", sc.write_kit()], a))
                if (b_alive and OPENING in [c for pid, c in world.messages if pid == b["pid"]]):
                    if not handoff["b_signed"]:
                        guarded(lambda: sc.cli(["sign", "in"], b))
                    if not handoff["a_signed"] and a_alive:
                        guarded(lambda: sc.cli(["sign", "out"], a))
                if handoff["a_signed"] and handoff["b_signed"] and a_alive and not ctl["stopped"]:
                    sc.clock.advance(1)
                    if guarded(lambda: sc.stop(a)) != "crashed":
                        ctl["stopped"] = True
    notices, _recorded = read_notices(sc)
    state = sc.state()
    b = box.get("b")
    records = archived(sc)
    spoken = {message for _kind, message, _voice in sc.notifier.calls}
    rungs = {n["id"] for n in notices if n["text"] in spoken}   # 「已响」= 通知器真的收到过这条 notice 的文案（不依赖实现怎么记 rung）
    return {
        "a": a, "b": b, "ctl": ctl, "notices": notices, "rungs": rungs, "state": state, "writes": wc.count,
        "launches": len(world.launches),
        "opens": sum(1 for _p, c in world.messages if c == OPENING),
        "batons": sum(1 for _p, c in world.messages if "BATON" in c),
        "kills_a": [k for k in world.kills if k["pid"] == a["pid"]],
        "kills_b": [k for k in world.kills if b and k["pid"] == b["pid"]],
        "commits": sum(1 for r in state.get("retire", []) if r.get("reason") == "handoff_commit"),
        "records": records,
        "a_alive": world.proc_state(a) == "live", "b_alive": bool(b) and world.proc_state(b) == "live",
        "mode": sessions.read_mode(sc.sdir)[0],
    }


def check_k1(facts):
    """K1 的独立 oracle：只用世界里的事实（谁活着、谁收到了什么、登记册里 holder 是谁、notice 与 rung）。返回违反列表。"""
    errors = []
    a, b, ctl, state = facts["a"], facts["b"], facts["ctl"], facts["state"]
    rung_kinds = {n["kind"] for n in facts["notices"] if n["id"] in facts["rungs"] and n.get("ring")}
    for n in facts["notices"]:
        if n["id"] not in facts["rungs"]:
            errors.append("notice %s 写了却没有响（通知器没收到）" % n["kind"])
    for name, limit in (("launches", 1), ("opens", 1), ("batons", 1), ("commits", 1)):
        if facts[name] > limit:
            errors.append("%s=%d（至多一次）" % (name, facts[name]))
    if len(facts["kills_a"]) > 1 or len(facts["kills_b"]) > 1:
        errors.append("SIGTERM 多于一次：A %d B %d" % (len(facts["kills_a"]), len(facts["kills_b"])))
    if any(k["lstart"] != a["lstart"] for k in facts["kills_a"]):
        errors.append("向不是 A 的进程发了信号")
    per_hid = {}
    for record in facts["records"]:
        per_hid.setdefault((record.get("id"), record.get("phase")), 0)
        per_hid[(record.get("id"), record.get("phase"))] += 1
    if any(v > 1 for v in per_hid.values()):
        errors.append("归档重复：%r" % per_hid)
    holder = state["holder"]
    swapped = bool(b) and holder is not None and holder["pid"] == b["pid"]
    injected = ctl["injected"] or {}
    off_done = injected.get("kind") in ("off", "off_on")
    if swapped:
        if state["handoff"] is not None:
            errors.append("已换棒但交接没有收尾（committed 残留）")
        if facts["a_alive"] and "old_a_alive" not in rung_kinds:
            errors.append("换棒后旧 A 仍活且没有响铃（SIGTERM 意图无结局）")
        delivered_baton = facts["batons"] >= 1
        if not delivered_baton and not (rung_kinds & {"baton_ambiguous", "baton_failed", "b_died_after_commit"}):
            errors.append("BATON 没送达且没有响铃（意图无结局）")
        if facts["opens"] == 0 and "open_ambiguous" not in rung_kinds:
            errors.append("OPEN 没送达且没有响铃")
        if injected.get("kind") == "killB" and injected.get("phase") == "committed" and "b_died_after_commit" not in rung_kinds:
            errors.append("换棒收尾期 B 死了，没有响铃")
        if injected.get("kind") == "killA" and injected.get("phase") in ("arming", "committed") and not any(
                n["kind"] == "a_crash_signed" for n in facts["notices"]) and facts["kills_a"] == []:
            # A 在 SIGTERM 之前自己死：要有一条（无声）记录；A 在签字前死的话走不到换棒
            errors.append("A 签后 / 换棒收尾期自己死了，没有 quiet 记录")
    else:
        if off_done and injected.get("phase") == "arming":
            if state["handoff"] is not None:
                errors.append("off 之后交接仍在")
            if injected.get("b_registered") and facts["b_alive"] and not (rung_kinds & {"off_kill_refused", "off_kill_unconfirmed"}):
                errors.append("off 之后已登记的 B 仍活着且没有响铃（SIGTERM B 意图无结局）")
        elif state["handoff"] is None and not off_done:
            errors.append("没有换棒、也没有放弃交接，交接却消失了")
        elif state["handoff"] is not None and not rung_kinds:
            errors.append("交接停住（%s）但没有任何已响的 notice" % state["handoff"].get("phase"))
    return errors


VARIANTS = (
    ("基线", {}),
    ("A 在签字后、换棒前死（killA@2）", {"inject": (2, "killA")}),
    ("B 上线后死（killB@3）", {"inject": (3, "killB")}),
    ("A 不理 SIGTERM（⑤ 路径）", {"ignore": True}),
    ("Owner off（B 已登记，off 的每个落盘写入点都在内）", {"inject": (3, "off")}),
    ("Owner off（换棒收尾期，A 不理 SIGTERM）", {"ignore": True, "inject": (6, "off")}),
)


def case_h51():
    """H51（K1）：对流程里每一次落盘写入——daemon、CLI、钩子的都算——在写入前、写入后各注入一次重启，再让流程自己跑到 170 s：
    各动作至多一次、每条 notice 都真的响过（通知器收到）、已换棒的必须收尾且旧 A / BATON / OPEN 有结局，停住的必须有已响的 notice。
    跑 6 种流程：基线、A 签后死、B 上线后死、A 不理 SIGTERM（这些流程里才有 notice 可写）、Owner off ×2（off 的每个落盘写入点）。"""
    totals = []
    bad = []
    runs = 0
    for name, kwargs in VARIANTS:
        with scenario() as sc:
            base = drive(sc, **kwargs)
        errors = check_k1(base)
        require(not errors, "%s：不崩的基线应满足 K1：%r" % (name, errors))
        for n in range(1, base["writes"] + 1):
            for when in ("before", "after"):
                with scenario() as sc:
                    facts = drive(sc, plan=(n, when), **kwargs)
                runs += 1
                errors = check_k1(facts)
                if errors:
                    bad.append("%s / 写入 #%d %s：%s" % (name, n, when, "; ".join(errors)))
        totals.append("%s %d 次写入" % (name, base["writes"]))
    require(not bad, "K1 崩溃点：%d / %d 处违反，例：%r" % (len(bad), runs, bad[:4]))
    return ["6 种流程（%s）× 前 / 后 = %d 轮重启：违反 0" % ("；".join(totals), runs)]


def case_h52():
    """H52（K1）：在流程每一步之间注入 Owner off、off 再 on、A 死、B 死（各从交接出现后第 0…12 轮开始；另跑「A 不理 SIGTERM」把换棒收尾期拉长），
    再跑到 170 s：同 K1 的 oracle，另有 off 之后不得换棒、已登记的 B 必被关（或响铃）、committed 期 off 不冻结收尾、committed 期 B 死要响、
    A 在 SIGTERM 前自己死要有 quiet 记录。"""
    with scenario() as sc:
        base = drive(sc)
    require(not check_k1(base), "基线应满足 K1：%r" % check_k1(base))
    bad = []
    count = 0
    for ignore in (False, True):
        for k in range(0, 13 if not ignore else 22):
            for kind in ("off", "off_on", "killA", "killB"):
                with scenario() as sc:
                    facts = drive(sc, inject=(k, kind), ignore=ignore)
                count += 1
                errors = check_k1(facts)
                if errors:
                    bad.append("%s第 %d 轮注入 %s：%s" % ("（A 不理 SIGTERM）" if ignore else "", k, kind, "; ".join(errors)))
    require(not bad, "K1 注入：%d / %d 处违反，例：%r" % (len(bad), count, bad[:4]))
    return ["%d 轮注入（交接出现后第 0…12 轮 + 收尾期拉长的 0…21 轮 × off / off+on / A 死 / B 死）：违反 0" % count]


# ---------------------------------------------------------------- H53（K2）

K2_STEPS = ("A.sign", "B.sign", "A.stop", "A.stop_tasks", "A2.stop", "A2.start", "clock-10s", "A.clear", "A.resume", "D.tick")
K2_STEPS_5 = ("A.sign", "B.sign", "A.stop", "A.clear", "A.resume", "D.tick")
K2_STEPS_5C = ("A.sign", "B.sign", "A.stop", "A2.stop", "clock-10s", "D.tick")


def case_h53():
    """H53（K2）：A、同 sid 的第二进程 A2、A 自己 /clear 与 /resume 换号、B、daemon tick、时钟回拨的交错（10 步词汇长度 ≤4；6 步词汇长度 5）（初始：B 已上线、kit 已封存、OPEN 已发、都没签）。
    独立模型：commit ⇔ A 与 B 都签了 ∧ B 活 ∧ A **自己的进程**签后出现过 Stop，且那次 Stop 两数组皆空（A2 的 Stop 不算、时钟回拨不影响）。
    A 在途的 Stop（带 tasks）后不得换棒；换棒前不得有任何 SIGTERM。"""
    count = 0
    bad = []
    commits = 0
    with scenario() as sc:
        a = sc.spawn("sA")
        a2 = sc.spawn("sA")
        b = sc.spawn("uB")
        dm = sc.daemon()
        base = sc.clock.ms
        sequences = [seq for length in range(1, 5) for seq in itertools.product(K2_STEPS, repeat=length)]
        sequences += list(itertools.product(K2_STEPS_5, repeat=5))      # 长度 5：两个签字 + A 的 Stop + /clear、/resume 换号 + 换棒检查放进同一序列
        sequences += list(itertools.product(K2_STEPS_5C, repeat=5))     # 长度 5：两个签字 + 他进程 Stop + 时钟回拨 + A 的 Stop + 换棒检查
        sequences += [("A.stop", "A.clear", "A.sign", "B.sign", "A.resume", "D.tick"),            # R2-01 原序列：签字前在旧会话留过一次 Stop，换号签字，回旧会话
                      ("A.stop", "A.clear", "A.sign", "B.sign", "A.stop_tasks", "A.resume", "D.tick"),
                      ("A.stop", "A.clear", "A.sign", "A.resume", "B.sign", "D.tick"),
                      ("A.sign", "A.clear", "A.stop_tasks", "A.resume", "B.sign", "D.tick")]
        for seq in sequences:
            if True:
                count += 1
                sc.clock.ms = base
                for rec in (a, a2, b):
                    sc.world.table[rec["pid"]]["alive"] = True
                sc.world.kills.clear()
                for sid in ("sA", "sA1"):
                    path = sessions.session_path(sc.sdir, sid)
                    if os.path.exists(path):
                        os.unlink(path)
                shutil.rmtree(os.path.join(sc.sdir, "procs"), ignore_errors=True)    # 每进程 Stop 记录也清
                init_state(sc, a, b, False, "h-k2-%d" % count)
                cur = {"rec": a}                                                       # A 当前所在的会话号（/clear 换到 sA1，/resume 回 sA）
                model = {"a": False, "b": False, "latest": None}
                committed = False
                for index, step in enumerate(seq):
                    label = "序列=%r 第 %d 步 %s" % (seq, index + 1, step)
                    if committed:
                        break
                    if step == "A.sign":
                        before = sc.handoff()["a_signed"]
                        sc.cli(["sign", "out"], cur["rec"])
                        if not before and sc.handoff() and sc.handoff()["a_signed"]:
                            model["a"], model["latest"] = True, None
                    elif step == "B.sign":
                        sc.cli(["sign", "in"], b)
                        if sc.handoff() and sc.handoff()["b_signed"]:
                            model["b"] = True
                    elif step in ("A.stop", "A.stop_tasks"):
                        sc.stop(cur["rec"], tasks=["t1"] if step == "A.stop_tasks" else ())
                        if model["a"]:
                            model["latest"] = "tasks" if step == "A.stop_tasks" else "empty"
                    elif step == "A.clear":          # 同一个 A 进程 /clear 到新会话号（Stop 序号不随会话号走）
                        cur["rec"] = dict(a, session_id="sA1")
                        sc.hook("SessionStart", cur["rec"], source="clear")
                    elif step == "A.resume":         # 同一个 A 进程 /resume 回旧会话号
                        cur["rec"] = a
                        sc.hook("SessionStart", a, source="resume")
                    elif step == "A2.stop":
                        sc.stop(a2)
                    elif step == "A2.start":
                        sc.hook("SessionStart", a2, source="resume")
                    elif step == "clock-10s":
                        sc.clock.ms -= 10_000
                    elif step == "D.tick":
                        expected = model["a"] and model["b"] and model["latest"] == "empty"
                        sc.tick(dm)
                        swapped = sc.state()["holder"]["pid"] == b["pid"]
                        if swapped != expected:
                            bad.append("%s：期望换棒=%s 实际=%s" % (label, expected, swapped))
                        if swapped:
                            committed = True
                            commits += 1
                    if sc.world.kills and not committed:
                        bad.append("%s：换棒前就发了 SIGTERM：%r" % (label, sc.world.kills))
    require(not bad, "K2：%d 处违反，例：%r" % (len(bad), bad[:3]))
    require(commits > 30, "不应空转：换棒出现 %d 次" % commits)
    return ["%d 个序列（10 步词汇长度 1–4 + 两套 6 步词汇长度 5，含同 sid 第二进程 Stop / SessionStart、A 换号与时钟回拨）：违反 0；换棒出现 %d 次" % (count, commits)]


# ---------------------------------------------------------------- H54（K3）

def k3_off_path():
    """SIGTERM B（off 路径）：认领（sending）之后、发信号前 B 的 pid 被复用 ⇒ 不向复用进程发信号。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        hit = {}
        victim = {}

        def point(name):
            if name == "offkill:marker" and not hit:
                hit["done"] = True
                sc.world.reuse_pid(victim["rec"])

        dm = sc.daemon(point_fn=point)
        b, hid = full_handoff_until_sealed(sc, a, dm)
        victim["rec"] = b
        baton.abandon(sc.sdir, sc.clock.ms)
        sessions.write_mode(sc.sdir, "manual", sc.clock.ms)
        for _ in range(3):
            sc.tick(dm)
        reused = sc.world.table[b["pid"]]["lstart"]
        require(hit, "注入没有触发")
        require(not [k for k in sc.world.kills if k["lstart"] == reused], "向被复用的进程发了信号：%r" % sc.world.kills)
    return "offkill：认领后 PID 被复用 ⇒ 不向复用进程发信号"


def k3_path(path):
    with scenario() as sc:
        a = sc.spawn("sA")
        hit = {}
        label = {"open": "open:marker", "kill": "kill:marker", "baton": "baton:marker"}[path]
        victim = {}

        def point(name):
            if name == label and not hit:
                hit["done"] = True
                sc.world.reuse_pid(victim["rec"])

        dm = sc.daemon(point_fn=point)
        hid, uuid = start_handoff(sc, a)
        box = {}
        sc.world.on_launch = lambda handoff: box.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = box["b"]
        victim["rec"] = {"open": b, "kill": a, "baton": b}[path]
        sc.cli(["seal", "--kit", sc.write_kit()], a)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        for _ in range(4):
            sc.tick(dm)
        reused = sc.world.table[victim["rec"]["pid"]]["lstart"]
        bad_msgs = [(p, c[:20]) for (p, c), ls in zip(sc.world.messages, sc.world.sent_lstart) if ls == reused]
        bad_kills = [k for k in sc.world.kills if k["lstart"] == reused]
        require(hit, "注入没有触发")
        require(not bad_msgs and not bad_kills, "向被复用的进程投递 / 发信号了：%r %r" % (bad_msgs, bad_kills))
    return "%s：标记后 PID 被复用 ⇒ 不向复用进程投递 / 发信号" % path


def case_h54():
    """H54（K3）：OPEN、SIGTERM A、BATON、SIGTERM B（off）四条路径各一次——标记已落盘、动作发出前，目标 PID 被另一个进程复用（lstart 不同）：
    绝不向复用进程投递 / 发信号。"""
    return collect([(path, lambda path=path: k3_path(path)) for path in ("open", "kill", "baton")] + [("offkill", k3_off_path)])


# ---------------------------------------------------------------- H55（A7、A9、A10、A11）

def collect(checks):
    """逐项跑，收集失败（修前要数「红了几处」）。"""
    failures, details = [], []
    for name, fn in checks:
        try:
            details.append(fn())
        except Exception as exc:
            failures.append("%s：%s" % (name, exc))
    require(not failures, "；".join(failures))
    return details


def a7():
    ok = ("handoffd sign in", "handoffd sign in --config /c/h.json", "handoffd --config /c/h.json sign in", "handoffd sign --config /c/h.json in",
          "handoffd sign in --config=/c/h.json", "handoffd status --config /c/h.json", "handoffd reply task --config /c/h.json")
    no = ("handoffd sign out --config /c/h.json", "handoffd sign in extra", "handoffd sign in --config", "handoffd off --config /c/h.json",
          "handoffd sign in --config /c/h.json extra")
    for command in ok:
        require(policy.b_may_run_handoffd(policy.literal_handoffd(command)), "B 应能执行 %r" % command)
    for command in no:
        require(not policy.b_may_run_handoffd(policy.literal_handoffd(command)), "B 不应能执行 %r" % command)
    return "A7：B 白名单按解析后的子命令判，`--config` 在命令前 / 子命令后 / `=` 形式都允许；sign out、多余参数、off 仍拒"


def a9():
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        holder = {}
        sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = holder["b"]
        sc.cli(["seal", "--kit", sc.write_kit()], a)
        sc.world.die(a)
        sc.tick(dm)
        sc.tick(dm)
        require(sc.cli(["adopt"], None)[0] == 0, "adopt")
        sc.cli(["sign", "in"], b)
        for _ in range(3):
            sc.tick(dm)
        require("a_crash_signed" not in sc.notice_kinds() and sc.state()["holder"]["pid"] == b["pid"], "adopt（Owner 代签）后不补「A 签字之后崩溃」：%r" % sc.notice_kinds())
    return "A9：adopt 之后不补 a_crash_signed"


def a10():
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        holder = {}
        sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = holder["b"]
        sc.cli(["seal", "--kit", sc.write_kit()], a)
        sc.tick(dm)
        sc.cli(["sign", "in"], b)
        require(sc.handoff()["b_signed"], "前置：B 已签")
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit(KIT_TEXT.replace("第 2 棒", "第二棒"), "k2.md")], a)
        require(code == 0 and sc.handoff()["b_signed"] is None and "重新 sign in" in out, "B 已签后重新 seal ⇒ 清 b_signed 并提示：%r %s" % (out, err))
        sc.cli(["sign", "out"], a)
        sc.stop(a)
        for _ in range(3):
            sc.tick(dm)
        require(sc.state()["holder"]["pid"] == a["pid"] and sc.handoff()["phase"] == "arming", "B 没重新签字前不换棒")
        sc.cli(["sign", "in"], b)
        sc.tick(dm)
        require(sc.state()["holder"]["pid"] == b["pid"], "B 重新 sign in 后换棒")
    return "A10：B 已签后重新 seal ⇒ 清 b_signed、提示 B 重新 sign in；没重签不换棒"


def a11():
    with scenario() as sc:
        a = sc.spawn("sA")
        sc.hook("SessionStart", a, source="startup")
        sc.cli(["request"], a)
        uuid = sc.handoff()["b"]["uuid"]
        b = sc.spawn(uuid)
        sc.hook("UserPromptSubmit", b)
        require(sc.handoff()["b"]["pid"] == b["pid"] and sc.handoff()["b"]["online_ms"], "B 的 UserPromptSubmit 也应登记上线")
    return "A11：B 没经 SessionStart 登上时，UserPromptSubmit（OPEN 到达即触发）也登记 b 槽"


def case_h55():
    """H55：A7 B 的白名单允许 `--config`；A9 adopt 之后不补「A 签字之后崩溃」；A10 B 已签后 A 重新 seal ⇒ 清 b_signed 并提示 B 重新 sign in；
    A11 B 的 UserPromptSubmit 也登记上线（SessionStart 那次没登上时）。"""
    return collect([("A7", a7), ("A9", a9), ("A10", a10), ("A11", a11)])


def k1_notice_write_fail():
    """notice 单次写失败（盘满之类，进程没死）：不发声、不记「已响」；下一轮条件仍成立再写 ⇒ 总共 1 条 notice、1 次响铃（A2 的第二半）。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.world.die(a)
        original = util.append_jsonl
        state = {"failed": 0}

        def flaky(path, record):
            if path.endswith("notices.jsonl") and record.get("type") == "notice" and state["failed"] == 0:
                state["failed"] = 1
                return False
            return original(path, record)

        util.append_jsonl = flaky
        try:
            sc.tick(dm)
            require(state["failed"] == 1 and sc.ring_count() == 0 and sc.notices() == [], "写失败那一轮：不响、没有 notice 记录：rings=%d" % sc.ring_count())
            sc.tick(dm)
            sc.tick(dm)
        finally:
            util.append_jsonl = original
        require(len(sc.notices()) == 1 and sc.ring_count() == 1, "之后：1 条 notice、1 次响铃：%d / %d" % (len(sc.notices()), sc.ring_count()))
    return "notice 单次写失败：先不响，下一轮补写后恰好 1 条 1 铃"


def k1_a_dies_between_commit_and_kill():
    """A 在 commit 落盘后、SIGTERM 之前自己死了：不发信号、BATON 照发、归档照做，但留下一条 quiet 记录（归档会吞掉报警，A4）。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        done = {}

        def point(label):
            if label == "commit:after" and not done:
                done["x"] = True
                sc.world.die(a)

        dm = sc.daemon(point_fn=point)
        hid, uuid = start_handoff(sc, a)
        box = {}
        sc.world.on_launch = lambda handoff: box.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = box["b"]
        sc.cli(["seal", "--kit", sc.write_kit()], a)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        for _ in range(3):
            sc.tick(dm)
        require(done and sc.state()["holder"]["pid"] == b["pid"] and sc.handoff() is None, "前置：已换棒并收尾")
        require(sc.world.kills == [] and sc.notice_kinds() == ["a_crash_signed"] and sc.notices()[0]["ring"] is False and sc.ring_count() == 0,
                "A 先死：不发信号、留下 quiet 记录、不响铃：%r %r" % (sc.world.kills, sc.notice_kinds()))
    return "A 在 commit 后、SIGTERM 前自己死：不发信号，补 quiet 记录，BATON 与归档照常"


def k1_baton_fails():
    """BATON 连续投递失败：放弃（failed）并响铃一次（K1：失败即卡住，卡住必有铃）；notice 不随归档解除（仍在待处理里）。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        box = {}
        sc.world.on_launch = lambda handoff: box.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = box["b"]
        sc.cli(["seal", "--kit", sc.write_kit()], a)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        sc.world.msg_fail = True       # OPEN 已发出；此后任何投递都连不上
        for _ in range(8):
            sc.tick(dm)
        require(sc.handoff() is None and sc.state()["holder"]["pid"] == b["pid"], "前置：已换棒并收尾")
        require("baton_failed" in sc.notice_kinds() and sc.ring_count() == 1, "BATON 放弃后响铃一次：%r rings=%d" % (sc.notice_kinds(), sc.ring_count()))
        require([n["kind"] for n in sessions.pending_notices(sc.sdir)] == ["baton_failed"], "归档后该 notice 仍在待处理里：%r" % sessions.pending_notices(sc.sdir))
    return "BATON 连败 5 次放弃：响铃 1 次，notice 不随归档解除"


def case_h56():
    """H56（K1 的补充）：notice 单次写失败（A2）、A 在 commit 后 SIGTERM 前死（A4）、BATON 放弃要响（A1/K1）。"""
    return collect([("notice 写失败", k1_notice_write_fail), ("A 先死", k1_a_dies_between_commit_and_kill), ("BATON 失败", k1_baton_fails)])


def case_h60():
    """H60（R3-01）：`sign out` 读进程 Stop 文件时单次读错误——文件不存在 ⇒ 基线 0；读不出 ⇒ 拒绝签字（退出码 3、明确错误、登记册不变）；
    之后读取恢复、正常重签：stop_n 取到真实序号，旧 Stop 不会被当成签后 Stop，没有签后 Stop 前不换棒。另验：钩子写 Stop 序号遇读错误不覆盖（序号不倒退）。"""
    with scenario() as sc:
        a = sc.spawn("sA")
        sc.hook("SessionStart", a, source="startup")
        sc.stop(a)                                           # 签字前在旧会话留一次空 Stop（n=1）
        a1 = dict(a, session_id="sA1")
        sc.hook("SessionStart", a1, source="clear")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a1, dm)
        sc.tick(dm)
        path = sessions.proc_stop_path(sc.sdir, a["pid"], a["lstart"])
        original = util.read_json
        state = {"failed": 0}

        def flaky(p):
            if p == path and state["failed"] == 0:
                state["failed"] = 1
                return "error", None
            return original(p)

        util.read_json = flaky
        try:
            before = json.dumps(sc.state(), sort_keys=True)
            code, out, err = sc.cli(["sign", "out"], a1)
        finally:
            util.read_json = original
        require(state["failed"] == 1 and code == 3 and "稍后重新" in err, "单次读错误：拒绝签字（退出码 3，提示稍后重签）：%r %r" % (code, err))
        require(sc.handoff()["a_signed"] is None and json.dumps(sc.state(), sort_keys=True) == before, "登记册不变")
        code, out, err = sc.cli(["sign", "out"], a1)         # 读取恢复，正常重签
        require(code == 0 and sc.handoff()["a_signed"]["stop_n"] == 1, "重签：stop_n 取到真实序号 1：%r" % (sc.handoff()["a_signed"],))
        sc.hook("SessionStart", a, source="resume")          # 回旧会话号；签后没有任何 Stop
        sc.cli(["sign", "in"], b)
        for _ in range(3):
            sc.tick(dm)
        require(sc.state()["holder"]["pid"] == a["pid"] and sc.world.kills == [], "没有签后 Stop：不换棒、不杀 A")
        sc.stop(a)
        sc.tick(dm)
        require(sc.state()["holder"]["pid"] == b["pid"], "A 自己签后 Stop 之后正常换棒")
    # 钩子写序号时读到错误：不覆盖
    with scenario() as sc:
        a = sc.spawn("sA")
        sc.hook("SessionStart", a, source="startup")
        sc.stop(a)
        sc.stop(a)
        path = sessions.proc_stop_path(sc.sdir, a["pid"], a["lstart"])
        original = util.read_json

        def flaky(p):
            return ("error", None) if p == path else original(p)

        util.read_json = flaky
        try:
            sc.stop(a)
        finally:
            util.read_json = original
        require(sessions.read_proc_stop(sc.sdir, a["pid"], a["lstart"]).get("n") == 2, "钩子读不出旧序号：不覆盖，序号不倒退：%r" % sessions.read_proc_stop(sc.sdir, a["pid"], a["lstart"]))
        sc.stop(a)
        require(sessions.read_proc_stop(sc.sdir, a["pid"], a["lstart"]).get("n") == 3, "恢复后继续单调")
    return ["sign out 读 Stop 文件：缺失 ⇒ 基线 0；读错误 ⇒ 拒绝签字（登记册不变），重签后 stop_n 正确、不误换棒；钩子读错误不覆盖序号"]


def main():
    """运行 H51–H56、H60。"""
    return run_cases([("H51", case_h51), ("H52", case_h52), ("H53", case_h53), ("H54", case_h54), ("H55", case_h55), ("H56", case_h56), ("H60", case_h60)])


if __name__ == "__main__":
    sys.exit(main())
