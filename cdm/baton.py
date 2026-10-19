# 棒登记册 <S>/baton.json（设计:5、:38）：谁持棒、有没有交接、B 是谁。
# 纪律：所有读改写都在同一把 flock（baton.json.guard）内、原子替换；holder 字段只有两条路径能写：
# claim()（认领 CAS，仅无交接时，S1）与 commit()（daemon 的换棒，S2a）；transact() 在写盘前断言其余路径没动 holder（不变量 J1）。
# 读（classify）不取锁：写者总是整文件原子替换，读到的不是旧版就是新版。
import contextlib
import copy
import fcntl
import json
import os
import re
import time
import uuid

from cdm import procs, sessions, util

GUARD_WAIT_SECONDS = 1.5
ROLES = ("holder", "holder_sub", "b", "vacant", "blocked", "spectator", "unknown")


def baton_path(sdir):
    return os.path.join(sdir, "baton.json")


def empty_state():
    return {"version": 1, "holder": None, "handoff": None, "retire": []}


def _valid_holder(value):
    return (isinstance(value, dict) and isinstance(value.get("sid"), str) and value["sid"]
            and isinstance(value.get("pid"), int) and not isinstance(value["pid"], bool)
            and isinstance(value.get("lstart"), str) and value["lstart"])


def read(sdir):
    """返回 ("missing"|"ok"|"error", 状态)。缺失 = 空登记册（从未认领）；结构不对 = error，调用方不得据此认领。"""
    status, value = util.read_json(baton_path(sdir))
    if status == "missing":
        return "missing", empty_state()
    if status != "ok" or not isinstance(value, dict):
        return "error", None
    holder, handoff = value.get("holder"), value.get("handoff")
    if holder is not None and not _valid_holder(holder):
        return "error", None
    if handoff is not None and not (isinstance(handoff, dict) and isinstance((handoff.get("b") or {}).get("uuid"), str)):
        return "error", None
    if not isinstance(value.get("retire", []), list):
        return "error", None
    state = dict(empty_state(), **value)
    return "ok", state


@contextlib.contextmanager
def guarded(sdir, wait=GUARD_WAIT_SECONDS):
    """取 baton.json.guard 的独占 flock（有界等待）；拿不到 yield False。guard 文件本身永不删除。"""
    os.makedirs(sdir, exist_ok=True)
    fd = os.open(baton_path(sdir) + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.01)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _same_process(slot, pid, lstart):
    """槽位里记的进程 == 本进程：只比 (pid, lstart)；会话号可以变（/clear、/resume 同进程换号，RT:T1b/T1c）。"""
    return bool(pid) and slot.get("pid") == pid and slot.get("lstart") == lstart


def _identity_ok(pid, lstart, sid):
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 1
            and isinstance(lstart, str) and bool(lstart) and isinstance(sid, str) and bool(sid))


def _default_proc_state(record):
    """槽位记录（holder / b / a_old，会话号键名 sid 或 uuid）的判活：只比 (pid, lstart)，会话号仅占位。"""
    return procs.proc_state({"session_id": record.get("sid") or record.get("uuid") or "x", "pid": record["pid"], "lstart": record["lstart"]},
                            procs.Deadline(2.0))


default_proc_state = _default_proc_state


def classify(state, sid, identity_fn, chain_fn):
    """唯一授权判据（不变量 I）：只有「登记册槽位里的进程 == 本进程（或祖先链）」才产出可放行角色，纯读、不写。

    返回 (role, info)：
    - holder：holder 槽的 (pid, lstart) == 本进程（info["roll"]=True 表示会话号不同，即同进程换号）；
    - holder_sub：holder 槽的进程在本进程祖先链里（主控跑的 `claude -p` 等子任务，设计:90）；
    - b：会话号 == handoff.b.uuid 且 b 槽的 (pid, lstart) == 本进程；
    - not_me：其余一律（无主、死主、同会话号的另一个进程、重新挂靠、B 未上线……），info["slot"] ∈ {"holder","b"}
      指明本会话可去试认领哪个槽——认领不是放行，必须进 guard 做 CAS（claim）并成功后才算数；
    - fail_open：取不到本进程身份或会话号（规格:79 钩子出错放行，K2）——唯一不看登记册的分支，由调用方单独处理。"""
    pid, lstart = identity_fn()
    if not _identity_ok(pid, lstart, sid):
        return "fail_open", {}
    holder, handoff = state["holder"], state["handoff"]
    if holder is not None and _same_process(holder, pid, lstart):
        return "holder", {"roll": holder["sid"] != sid}
    if holder is not None and any(_same_process(holder, chain_pid, chain_lstart) for chain_pid, chain_lstart in (chain_fn() or [])):
        return "holder_sub", {}
    if handoff is not None and handoff["b"]["uuid"] == sid:
        if _same_process(handoff["b"], pid, lstart):
            return "b", {}
        return "not_me", {"slot": "b"}
    return "not_me", {"slot": "holder"}


def _cas_holder(sdir, state, sid, pid, lstart, now_ms, proc_state_fn):
    """holder 槽 CAS（在 guard 内、state 是刚读的）：只有「槽空或旧进程确已死」且「无进行中的交接」才能写（规格:110、:44）；
    旧进程活着 / 判活不确定 ⇒ 不换（不变量 II）。返回 (结果, 角色)。"""
    holder, handoff = state["holder"], state["handoff"]
    old = None
    if holder is not None:
        holder_state = proc_state_fn(holder)
        if holder_state == "live":
            return "denied", "spectator"
        if holder_state != "dead":
            return "unknown", "spectator"
        old = holder
    if handoff is not None:
        return "blocked", "blocked"
    if old is not None:
        state["retire"] = (state.get("retire") or []) + [dict(old, retired_ms=now_ms, reason="takeover_dead")]
    state["holder"] = {"sid": sid, "pid": pid, "lstart": lstart, "claimed_ms": now_ms}
    util.atomic_write_json(baton_path(sdir), state)
    return "claimed", "holder"


def _cas_b(sdir, state, sid, pid, lstart, now_ms, proc_state_fn):
    """b 槽 CAS：会话号 == handoff.b.uuid（classify 已判）且「槽空或旧进程确已死」才写；活着 / 判活不确定 ⇒ 不换（不变量 II）。"""
    b = state["handoff"]["b"]
    if b.get("pid"):
        b_state = proc_state_fn(b)
        if b_state == "live":
            return "denied", "spectator"
        if b_state != "dead":
            return "unknown", "spectator"
    state["handoff"]["b"] = dict(b, pid=pid, lstart=lstart, online_ms=now_ms)
    util.atomic_write_json(baton_path(sdir), state)
    return "b_online", "b"


def claim(sdir, sid, identity_fn, chain_fn, now_ms, proc_state_fn=None):
    """授权入口：在 guard 内重读、重判、按槽位 CAS。返回 (结果, 角色)，只有角色为 holder / holder_sub / b 才可放行写。

    结果：ok（本来就是，无需写）/ rolled（同进程换号，已更新会话号）/ claimed（认领 holder 槽）/ b_online（认领 b 槽）/
    denied（对方存活）/ unknown（对方判活不确定，本次拒）/ blocked（交接中持棒者已死，规格:44）/
    busy（guard 拿不到，本次拒）/ error（登记册读不出）/ fail_open（本进程身份取不到）。"""
    proc_state_fn = proc_state_fn or _default_proc_state
    with guarded(sdir) as acquired:
        if not acquired:
            return "busy", None
        status, state = read(sdir)
        if status == "error":
            return "error", None
        role, info = classify(state, sid, identity_fn, chain_fn)
        if role == "fail_open":
            return "fail_open", role
        if role == "holder":
            if info.get("roll"):
                state["holder"] = dict(state["holder"], sid=sid)
                util.atomic_write_json(baton_path(sdir), state)
                return "rolled", role
            return "ok", role
        if role in ("holder_sub", "b"):
            return "ok", role
        pid, lstart = identity_fn()
        cas = _cas_b if info["slot"] == "b" else _cas_holder
        return cas(sdir, state, sid, pid, lstart, now_ms, proc_state_fn)


def b_online(sdir, sid, pid, lstart, now_ms, proc_state_fn=None):
    """B 上线登记：只做 b 槽 CAS（会话号必须 == handoff.b.uuid；槽里是别的存活进程就不换）。返回 True 表示槽位现在就是本进程。"""
    if not _identity_ok(pid, lstart, sid):
        return False
    with guarded(sdir) as acquired:
        if not acquired:
            return False
        status, state = read(sdir)
        if status != "ok" or not state["handoff"] or state["handoff"]["b"]["uuid"] != sid:
            return False
        if _same_process(state["handoff"]["b"], pid, lstart):
            return True
        return _cas_b(sdir, state, sid, pid, lstart, now_ms, proc_state_fn or _default_proc_state)[1] == "b"


def bump_qa(sdir, sid):
    """B 的 SendMessage 计数（设计:56）：handoff.qa_count += 1；只认 B。返回新计数或 None。"""
    with guarded(sdir) as acquired:
        if not acquired:
            return None
        status, state = read(sdir)
        if status != "ok" or not state["handoff"] or state["handoff"]["b"]["uuid"] != sid:
            return None
        state["handoff"]["qa_count"] = int(state["handoff"].get("qa_count") or 0) + 1
        util.atomic_write_json(baton_path(sdir), state)
        return state["handoff"]["qa_count"]


def archive_once(sdir, record):
    """把 handoff 的结局记录追加到 handoffs.jsonl；同一 (id, phase) 已有就不再追加（重启后重做不产生两条，A8）。"""
    path = os.path.join(sdir, "handoffs.jsonl")
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("id") == record.get("id") and item.get("phase") == record.get("phase"):
                    return False
    except OSError:
        pass
    return util.append_jsonl(path, record)


def abandon(sdir, now_ms):
    """`off`：guard 内把进行中的交接标 abandoned 并清空 handoff；不动 holder。返回 (结果, 被放弃的 handoff 或 None)。
    结果 ok / busy / error；committed = 棒已换给 B（phase=committed），不能放弃，handoff 原样保留（收尾由 daemon 做完，A3）。
    SIGTERM 未接棒 B 的意图（K1）与清 handoff 在同一次写入里落盘：state["off_kill"] += {hid, uuid, pid, lstart, state:pending}，
    由 daemon（任何 mode 下）核对三元组 + cwd 后执行、再清掉；CLI 做不完（崩了 / 核对不过）也不会丢。"""
    with guarded(sdir) as acquired:
        if not acquired:
            return "busy", None
        status, state = read(sdir)
        if status == "error":
            return "error", None
        handoff = state["handoff"]
        if handoff is None:
            return "ok", None
        if handoff.get("phase") == "committed":
            return "committed", handoff  # 棒已换给 B：不放弃、不清 handoff，后续收尾（SIGTERM A / BATON / 归档）由 daemon 做完
        archive_once(sdir, dict(handoff, phase="abandoned", abandoned_ms=now_ms, abandoned_by="off"))
        b = handoff.get("b") or {}
        launched = ((handoff.get("launch") or {}).get("state") in ("launching", "opened", "uncertain"))
        if b.get("uuid") and ((b.get("pid") and b.get("lstart")) or launched):
            # B 已登记 ⇒ 按三元组落；窗口开了（或开到一半）但 B 还没登记 ⇒ 按预分配的 uuid 落，由执行者等它出现再核对、关掉（B5）
            state["off_kill"] = (state.get("off_kill") or []) + [
                {"hid": handoff.get("id"), "uuid": b.get("uuid"), "pid": b.get("pid"), "lstart": b.get("lstart"), "state": "pending", "ms": now_ms,
                 "config_paths": list((handoff.get("launch") or {}).get("config_paths") or [])}]
        state["handoff"] = None
        util.atomic_write_json(baton_path(sdir), state)
        return "ok", handoff


def _off_item(state, hid):
    for item in state.get("off_kill") or []:
        if item.get("hid") == hid:
            return item
    return None


def off_kill_claim(sdir, hid, token, now_ms):
    """K4：pending → sending 的 guard 内 CAS（只有一个执行者能赢）。赢了返回 True；意图已不是 pending（被别人认领 / 已清）返回 False。"""
    def fn(state):
        item = _off_item(state, hid)
        if item is None or item.get("state") != "pending":
            return False
        item.update(state="sending", owner=token, ms=now_ms)
        return True
    status, result = transact(sdir, fn)
    return status == "ok" and bool(result)


def off_kill_owned(sdir, hid, token):
    """K4：发信号前重读这条意图记录本身——仍在、仍是 sending、认领者仍是我。"""
    status, state = read(sdir)
    item = _off_item(state, hid) if status == "ok" else None
    return bool(item and item.get("state") == "sending" and item.get("owner") == token)


def off_kill_sent(sdir, hid, token, now_ms):
    """K4：信号已发出：sending → sent（记发出时刻）。意图保留到确认 B 已死才清（B2）。"""
    def fn(state):
        item = _off_item(state, hid)
        if item is None or item.get("state") != "sending" or item.get("owner") != token:
            return False
        item.update(state="sent", ms=now_ms)
        return True
    status, result = transact(sdir, fn)
    return status == "ok" and bool(result)


def off_kill_bind(sdir, hid, pid, lstart):
    """B5：B 登记（会话文件出现该 uuid）后，把它的三元组补进还没有 pid 的意图。"""
    def fn(state):
        item = _off_item(state, hid)
        if item is None or item.get("state") != "pending" or item.get("pid"):
            return False
        item.update(pid=pid, lstart=lstart)
        return True
    status, result = transact(sdir, fn)
    return status == "ok" and bool(result)


def off_kill_drop_paths(sdir, hid, done):
    """已删掉的临时配置路径从意图记录里去掉（剩下的由 daemon 在结束该意图前删）。返回 True 表示已写。"""
    def fn(state):
        item = _off_item(state, hid)
        if item is None:
            return False
        item["config_paths"] = [path for path in item.get("config_paths") or [] if path not in done]
        return True
    status, result = transact(sdir, fn)
    return status == "ok" and bool(result)


def off_kill_clear(sdir, hid):
    """清掉一条意图（B 已死 / 超时已响铃 / 核对不过已响铃 / 视为未开出）。返回 True 表示已写。"""
    def fn(state):
        item = _off_item(state, hid)
        if item is None:
            return False
        state["off_kill"].remove(item)
        return True
    status, result = transact(sdir, fn)
    return status == "ok" and bool(result)


# ================= S2a：交接的状态机（request / seal / sign / adopt / commit / 外部动作落盘标记 / 归档） =================
# handoff 字段（设计:38，S2a 补全；S1 写的最小 handoff 缺这些键时，写路径先 fill() 补默认值）：
#   id、reason、phase(arming|committed)、created_ms、a{sid,name,pid,lstart}、b{uuid,name,pid,lstart,online_ms}、
#   launch{attempt,state(None|launching|opened|retry|uncertain),started_ms,exit,version,preflight_fails,next_attempt_ms}、
#   kit{sha,opening,sealed_ms,path}|None、open{state(None|sending|sent),ms,fails}、a_signed{ms,by,sid,pid,lstart}|None、
#   b_signed{ms,sid,pid,lstart}|None、qa_count、committed_ms、post{a_old{sid,pid,lstart},kill{state,ms,reason},baton{state,ms,fails}}
OPENING_HEADING = "给新 session 的开场指令"
HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
DELIVERY_RETRY_LIMIT = 5      # BATON 连续投递失败这么多次就放弃（只记 events）；OPEN 不设上限（B 没收到开场指令就一直卡着，不能放弃）


def _handoff_defaults():
    return {
        "created_ms": None,
        "a": None,
        "launch": {"attempt": 0, "state": None, "started_ms": None, "exit": None, "version": None,
                   "preflight_fails": 0, "next_attempt_ms": None},
        "kit": None,
        "open": {"state": None, "ms": None, "fails": 0},
        "a_signed": None,
        "b_signed": None,
        "qa_count": 0,
        "post": None,
    }


def fill(handoff):
    """给 handoff 补缺省键（只在写路径用；读路径用 .get）。返回同一对象。"""
    for key, value in _handoff_defaults().items():
        handoff.setdefault(key, copy.deepcopy(value))
    return handoff


def triple(slot):
    return (slot.get("pid"), slot.get("lstart")) if isinstance(slot, dict) else (None, None)


def extract_opening(text):
    """从 kit 文本抽「给新 session 的开场指令」一节：标题行（任意级别 #）含该短语，取到下一个同级或更高级标题之前；
    首尾空白行去掉，其余逐字保留。缺节 / 节空 ⇒ None（seal 拒绝）。"""
    lines = text.split("\n")
    start, level = None, None
    for index, line in enumerate(lines):
        match = HEADING_LINE.match(line)
        if match and OPENING_HEADING in match.group(2):
            start, level = index, len(match.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = HEADING_LINE.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    body = lines[start + 1:end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    opening = "\n".join(body)
    return opening if opening.strip() else None


def transact(sdir, fn, may_change_holder=False, wait=GUARD_WAIT_SECONDS):
    """guard 内读 → fn(state) 就地改 → 有变化才原子写回。返回 ("ok", fn 的返回值) / ("busy", None) / ("error", None)。
    fn 抛异常 ⇒ 不写盘（异常向上抛）。不变量 J1 的结构保证：may_change_holder=False 时，fn 若动了 holder 槽，写盘前即抛 AssertionError——
    所以 holder 只经 claim() 的 CAS 与 commit() 两条路径改变。"""
    with guarded(sdir, wait) as acquired:
        if not acquired:
            return "busy", None
        status, state = read(sdir)
        if status == "error":
            return "error", None
        before = json.dumps(state, sort_keys=True)
        holder_before = json.dumps(state["holder"], sort_keys=True)
        result = fn(state)
        if not may_change_holder and json.dumps(state["holder"], sort_keys=True) != holder_before:
            raise AssertionError("holder 槽只能经认领 CAS 或 commit 改变（不变量 J1）")
        if json.dumps(state, sort_keys=True) != before:
            util.atomic_write_json(baton_path(sdir), state)
        return "ok", result


def new_handoff(hid, reason, holder, a_name, b_uuid, b_name, now_ms):
    """steady → arming 的新 handoff；A = 申请时的 holder 槽。"""
    handoff = {"id": hid, "reason": reason, "phase": "arming", "created_ms": now_ms,
               "a": {"sid": holder["sid"], "name": a_name, "pid": holder["pid"], "lstart": holder["lstart"]},
               "b": {"uuid": b_uuid, "name": b_name, "pid": None, "lstart": None, "online_ms": None}}
    return fill(handoff)


def request(sdir, sid, identity_fn, chain_fn, now_ms, reason, hid, b_uuid, b_name, a_name):
    """`request`：只有 holder（槽位三元组 == 本进程；子任务 holder_sub 不算）能申请；已有交接 ⇒ 拒。返回 (结果, 值)。
    结果 ok(handoff) / exists(handoff) / not_holder(角色) / fail_open / busy / error。写的是 handoff 字段，不动 holder。"""
    def fn(state):
        role, _info = classify(state, sid, identity_fn, chain_fn)
        if role == "fail_open":
            return "fail_open", None
        if role != "holder":
            return "not_holder", role
        if state["handoff"] is not None:
            return "exists", state["handoff"]
        state["handoff"] = new_handoff(hid, reason, state["holder"], a_name, b_uuid, b_name, now_ms)
        return "ok", state["handoff"]
    status, result = transact(sdir, fn)
    return result if status == "ok" else (status, None)


def request_for_holder(sdir, holder_sid, now_ms, reason, hid, b_uuid, b_name, proc_state_fn=None):
    """daemon 代 76% 的 holder 发起交接（hook 只落 request_pending 标记）：holder 槽会话号 == holder_sid 且进程确认存活、无交接才建。"""
    proc_state_fn = proc_state_fn or _default_proc_state

    def fn(state):
        holder = state["holder"]
        if holder is None or holder["sid"] != holder_sid:
            return "not_holder", None
        if state["handoff"] is not None:
            return "exists", state["handoff"]
        if proc_state_fn(holder) != "live":
            return "holder_not_live", None
        state["handoff"] = new_handoff(hid, reason, holder, None, b_uuid, b_name, now_ms)
        return "ok", state["handoff"]
    status, result = transact(sdir, fn)
    return result if status == "ok" else (status, None)


def seal(sdir, sid, identity_fn, chain_fn, now_ms, sha, opening, kit_text):
    """`seal`：holder 封存 kit：存档到 kits/<hid>.md、记 sha、记开场指令原文（OPEN 的 content 取发送那一刻的它，J3）。
    A 签字（a_signed）之前允许重新 seal（B 复核有异议时 A 按意见改 kit，规格:29）：更新存档与 sha；OPEN 已发出则不重发，
    新开场指令与已发出的不同时返回 stale_open=True（由命令提示 A 用 SendMessage 通知 B 重读 kit）。a_signed 之后冻结。
    返回 (结果, 值)；ok 时值 = {"handoff", "resealed", "stale_open", "b_signed_cleared"}。重新 seal 会清掉已有的 b_signed（B 复核的是旧 kit，A10）。"""
    def fn(state):
        role, _info = classify(state, sid, identity_fn, chain_fn)
        if role == "fail_open":
            return "fail_open", None
        if role != "holder":
            return "not_holder", role
        handoff = state["handoff"]
        if handoff is None:
            return "no_handoff", None
        if handoff.get("phase") != "arming":
            return "phase", handoff.get("phase")
        fill(handoff)
        if handoff["a_signed"]:
            return "signed", None
        resealed = handoff["kit"] is not None
        sent = handoff["open"].get("opening") if handoff["open"]["state"] is not None else None
        relative = os.path.join("kits", handoff["id"] + ".md")
        util.atomic_write_text(os.path.join(sdir, relative), kit_text)
        handoff["kit"] = {"sha": sha, "opening": opening, "sealed_ms": now_ms, "path": relative}
        stale = handoff["open"]["state"] is not None and sent != opening
        cleared = handoff["b_signed"] is not None
        handoff["b_signed"] = None   # A10：B 已签字时，B 复核的是旧 kit ⇒ 签字作废，B 须重新 sign in
        return "ok", {"handoff": handoff, "resealed": resealed, "stale_open": stale, "b_signed_cleared": cleared}
    status, result = transact(sdir, fn)
    return result if status == "ok" else (status, None)


def sign(sdir, who, sid, identity_fn, chain_fn, now_ms):
    """`sign out`（who=out，须 holder）/ `sign in`（who=in，须 b 槽三元组 == 本进程 ∧ 会话号 == b.uuid）：记签字（含三元组）。
    kit 未封存 ⇒ 拒（S2a 自定：没有 kit 就没有可复核的东西）；in 另须 B 已上线。返回 (结果, 值)。"""
    def fn(state):
        role, _info = classify(state, sid, identity_fn, chain_fn)
        if role == "fail_open":
            return "fail_open", None
        if role != ("holder" if who == "out" else "b"):
            return "not_allowed", role
        handoff = state["handoff"]
        if handoff is None:
            return "no_handoff", None
        if handoff.get("phase") != "arming":
            return "phase", handoff.get("phase")
        fill(handoff)
        if not handoff["kit"]:
            return "no_kit", None
        pid, lstart = identity_fn()
        key = "a_signed" if who == "out" else "b_signed"
        if handoff[key]:
            return "already", handoff[key]
        record = {"ms": now_ms, "sid": sid, "pid": pid, "lstart": lstart}
        if who == "out":
            record["by"] = "A"
            stop_status, entry = sessions.read_proc_stop_status(sdir, pid, lstart)
            if stop_status == "error":
                return "stop_unreadable", None   # R3-01：读不出进程 Stop 文件不能当基线 0——拒绝签字，登记册不变，请 A 稍后重签
            record["stop_n"] = entry.get("n", 0)   # K2：签字那一刻 A 这个**进程**已有的 Stop 序号（文件不存在 ⇒ 0；与会话号无关）
        elif not handoff["b"].get("online_ms"):
            return "b_not_online", None
        handoff[key] = record
        return "ok", record
    status, result = transact(sdir, fn)
    return result if status == "ok" else (status, None)


def adopt(sdir, now_ms, proc_state_fn=None):
    """`adopt`（仅 Owner；设计:61 行）：仅 :57 情形——交接未换棒、A 未签字、holder 确认已死、kit 已封存、B 已上线。
    补记 a_signed(by=owner, 三元组取 holder 槽)；B 仍须 `sign in`。返回 (结果, 值)。"""
    proc_state_fn = proc_state_fn or _default_proc_state

    def fn(state):
        handoff, holder = state["handoff"], state["holder"]
        if handoff is None:
            return "no_handoff", None
        if handoff.get("phase") != "arming":
            return "phase", handoff.get("phase")
        fill(handoff)
        if handoff["a_signed"]:
            return "already", None
        if holder is None:
            return "no_holder", None
        if proc_state_fn(holder) != "dead":
            return "holder_not_dead", None
        if not handoff["kit"] or not handoff["b"].get("online_ms"):
            return "not_sealed_online", None
        handoff["a_signed"] = {"ms": now_ms, "by": "owner", "sid": holder["sid"], "pid": holder["pid"], "lstart": holder["lstart"]}
        return "ok", handoff["a_signed"]
    status, result = transact(sdir, fn)
    return result if status == "ok" else (status, None)


def stop_after_sign(entry, a_signed):
    """设计:5 的 A 条件之一，按**进程身份**判（K2）：holder 槽那个进程（pid, lstart）自己的 Stop 序号大于签字时记的序号，
    且那次 Stop 的 tasks / crons 数都为 0（None = 不知道，不算空）。序号存在与会话号无关的进程文件里，/clear、/resume 不改变基线；
    同 sid 的他进程的 Stop、墙钟先后都不参与。entry = sessions.read_proc_stop(...) 的返回。"""
    base = a_signed.get("stop_n")
    return (isinstance(entry, dict) and isinstance(entry.get("n"), int) and isinstance(base, int) and entry["n"] > base
            and entry.get("tasks") == 0 and entry.get("crons") == 0)


def commit_check(state, proc_state_fn, stop_fn):
    """设计:5 commit 条件的唯一判定：返回 (是否可换棒, 原因, A 的判活)。
    a_signed ∧ b_signed ∧ holder 槽三元组 == a_signed 的三元组 ∧ b_signed.sid == handoff.b.uuid ∧ b_signed 三元组 == b 槽三元组 ∧
    b 槽进程 live ∧（A 已死 ∨（A live ∧ A 签后出现过 Stop 且两数组皆空））∧ phase == arming。"""
    handoff, holder = state["handoff"], state["holder"]
    if handoff is None:
        return False, "no_handoff", None
    if handoff.get("phase") != "arming":
        return False, "phase", None
    a_signed, b_signed = handoff.get("a_signed"), handoff.get("b_signed")
    if not a_signed:
        return False, "a_unsigned", None
    if not b_signed:
        return False, "b_unsigned", None
    if holder is None or triple(holder) != triple(a_signed):
        return False, "holder_changed", None
    b = handoff["b"]
    if b.get("pid") is None or not b.get("lstart"):
        return False, "b_offline", None
    if b_signed.get("sid") != b.get("uuid"):
        return False, "b_sid", None
    if triple(b_signed) != triple(b):
        return False, "b_triple", None
    if proc_state_fn(b) != "live":
        return False, "b_not_live", None
    a_state = proc_state_fn(holder)
    if a_state == "dead":
        return True, "a_dead", a_state
    if a_state != "live":
        return False, "a_unknown", a_state
    if not stop_after_sign(stop_fn(holder["pid"], holder["lstart"]) or {}, a_signed):
        return False, "a_no_stop", a_state
    return True, "a_stopped", a_state


def commit(sdir, now_ms, proc_state_fn=None, stop_fn=None):
    """daemon 的换棒——holder 槽的两条写路径之一（J1）：guard 内重判 commit_check，成立才一次写入
    holder=B、phase=committed、post.kill=pending（SIGTERM A 的意图随同一次写入落盘）。同一 handoff 至多一次（phase 已 committed 即拒）。
    返回 (结果, 值)：committed({a_old, a_state}) / wait(原因) / busy / error。"""
    proc_state_fn = proc_state_fn or _default_proc_state
    stop_fn = stop_fn or (lambda pid, lstart: sessions.read_proc_stop(sdir, pid, lstart))

    def fn(state):
        ok, reason, a_state = commit_check(state, proc_state_fn, stop_fn)
        if not ok:
            return "wait", reason
        handoff, old, b = state["handoff"], state["holder"], state["handoff"]["b"]
        fill(handoff)
        state["retire"] = (state.get("retire") or []) + [dict(old, retired_ms=now_ms, reason="handoff_commit", handoff=handoff["id"])]
        state["holder"] = {"sid": b["uuid"], "pid": b["pid"], "lstart": b["lstart"], "claimed_ms": now_ms}
        handoff["phase"] = "committed"
        handoff["committed_ms"] = now_ms
        a_old = {"sid": old["sid"], "pid": old["pid"], "lstart": old["lstart"]}
        handoff["post"] = {"a_old": a_old, "kill": {"state": "pending", "ms": None, "reason": None},
                           "baton": {"state": "pending", "ms": None, "fails": 0}}
        return "committed", {"a_old": a_old, "a_state": a_state}
    status, result = transact(sdir, fn, may_change_holder=True)
    return result if status == "ok" else (status, None)


def update_handoff(sdir, hid, fn, phases=("arming",)):
    """按 id 找当前 handoff（须仍是这一次、phase 在 phases 里）→ fill → fn(handoff, state)。不在 ⇒ ("gone", None)。"""
    def wrapper(state):
        handoff = state["handoff"]
        if handoff is None or handoff.get("id") != hid or handoff.get("phase") not in phases:
            return "gone", None
        fill(handoff)
        return fn(handoff, state)
    status, result = transact(sdir, wrapper)
    return result if status == "ok" else (status, None)


def mark_launching(sdir, hid, now_ms, config_paths=None, inherit=None):
    """开窗前先落盘「将要开」（J2①）：launch.state None/retry(到点) → launching，attempt+1。只此一步成功才允许调启动器。
    config_paths：本次开窗将写的临时 Warp 配置文件的准确路径（随 handoff 落盘；off 放弃交接时带进 off_kill，由 CLI / daemon 删除）。
    inherit（S4a 自定）：本次开 B 实际继承的 {"model","effort","src"}，原样记进 launch（status 显示「B 模型 / 档位」）。"""
    def fn(handoff, state):
        launch = handoff["launch"]
        if launch["state"] == "retry" and now_ms >= (launch["next_attempt_ms"] or 0):
            pass
        elif launch["state"] is not None:
            return "skip", launch["state"]
        launch.update(state="launching", attempt=launch["attempt"] + 1, started_ms=now_ms, exit=None, next_attempt_ms=None,
                      config_paths=list(config_paths or []))
        if inherit is not None:
            launch.update(b_model=inherit.get("model"), b_effort=inherit.get("effort"), b_inherit=inherit.get("src"))   # S4a 自定字段
        return "ok", launch["attempt"]
    return update_handoff(sdir, hid, fn)


def record_launch(sdir, hid, now_ms, code, version, retry_seconds, notify=None):
    """启动器返回后记结果：0 → opened；2（预检失败，保证没开窗）→ retry（next_attempt_ms）且 preflight_fails+1；
    其余 / 异常（code=None）→ uncertain（不确定窗口是否已开：永不自动重开）。notify(key, 值) 在 guard 内被调用，由调用方负责「出 guard 后再发声」。"""
    def fn(handoff, state):
        launch = handoff["launch"]
        if launch["state"] != "launching":
            return "skip", launch["state"]
        launch["exit"] = code
        if version:
            launch["version"] = version
        if code == 0:
            launch["state"] = "opened"
        elif code == 2:
            launch.update(state="retry", preflight_fails=launch["preflight_fails"] + 1, next_attempt_ms=now_ms + int(retry_seconds * 1000))
            if notify and launch["preflight_fails"] >= 3:
                notify("preflight_failed", launch["preflight_fails"])
        else:
            launch["state"] = "uncertain"
            if notify:
                notify("launcher_failed", code)
        return "ok", dict(launch)
    return update_handoff(sdir, hid, fn)


def mark_delivery(sdir, hid, kind, now_ms, target):
    """投递前先落盘「将要发」（J2①）。kind=open：phase arming ∧ kit 已封存 ∧ b 槽已上线且三元组 == target ∧ open.state 空 → sending，
    返回 content（= seal 时抽出的开场指令原文）；kind=baton：phase committed ∧ post.baton.state=pending ∧ holder 槽三元组 == target → sending。"""
    def fn(handoff, state):
        if kind == "open":
            if not handoff["kit"] or not handoff["b"].get("online_ms") or triple(handoff["b"]) != tuple(target):
                return "not_ready", None
            if handoff["open"]["state"] is not None:
                return "skip", handoff["open"]["state"]
            handoff["open"].update(state="sending", ms=now_ms, opening=handoff["kit"]["opening"])   # 记下实际发出的原文：之后 reseal 对比用
            return "ok", handoff["kit"]["opening"]
        slot = handoff["post"]["baton"]
        if slot["state"] != "pending":
            return "skip", slot["state"]
        if state["holder"] is None or triple(state["holder"]) != tuple(target):
            return "not_ready", None
        slot.update(state="sending", ms=now_ms)
        return "ok", None
    return update_handoff(sdir, hid, fn, phases=("arming",) if kind == "open" else ("committed",))


def finish_delivery(sdir, hid, kind, now_ms, delivered, give_up=False):
    """投递结果：成功 → sent；明确失败（连不上，消息没发出）→ 退回可重试（open→None；baton→pending，连续失败 DELIVERY_RETRY_LIMIT 次后 failed）；
    give_up（目标已死 / 核对不过，BATON 不可能再送达）→ baton 直接 failed。"""
    def fn(handoff, state):
        slot = handoff["open"] if kind == "open" else handoff["post"]["baton"]
        if slot["state"] != "sending":
            return "skip", slot["state"]
        if delivered:
            slot.update(state="sent", ms=now_ms)
        elif give_up and kind == "baton":
            slot.update(state="failed", ms=now_ms)
        else:
            slot["fails"] = slot.get("fails", 0) + 1
            if kind == "open":
                slot["state"] = None
            else:
                slot["state"] = "failed" if slot["fails"] >= DELIVERY_RETRY_LIMIT else "pending"
        return "ok", dict(slot)
    return update_handoff(sdir, hid, fn, phases=("arming",) if kind == "open" else ("committed",))


def mark_kill(sdir, hid, now_ms, step, reason=None, target=None):
    """SIGTERM A 的落盘标记（J2①）。step=sending：pending → sending（须目标三元组 == post.a_old）；sent：sending → sent；
    skipped：pending → skipped（目标核对不过 / 已死，记 reason，不发信号）。"""
    def fn(handoff, state):
        kill = handoff["post"]["kill"]
        if step == "sending":
            if kill["state"] != "pending" or triple(handoff["post"]["a_old"]) != tuple(target):
                return "skip", kill["state"]
            kill.update(state="sending", ms=now_ms)
        elif step == "sent":
            if kill["state"] != "sending":
                return "skip", kill["state"]
            kill.update(state="sent", ms=now_ms)
        else:
            if kill["state"] not in ("pending", "sending"):   # sending：标记已落、动作发出前的重核没过（K3），信号确定没发
                return "skip", kill["state"]
            kill.update(state="skipped", ms=now_ms, reason=reason)
        return "ok", dict(kill)
    return update_handoff(sdir, hid, fn, phases=("committed",))


def complete(sdir, hid, now_ms):
    """收尾：把已换棒的 handoff 存档到 handoffs.jsonl（phase=completed）并清空 handoff（此后 holder=B 与无交接时的规则一致）。"""
    def fn(handoff, state):
        archive_once(sdir, dict(handoff, phase="completed", completed_ms=now_ms))
        state["handoff"] = None
        return "ok", None
    return update_handoff(sdir, hid, fn, phases=("committed",))


def new_ids(now_ms, name_prefix, rand=os.urandom):
    """交接 id / B 的会话名 / B 的预分配会话号（--session-id）：id 与会话名共用一段随机尾巴，字符集只含 [A-Za-z0-9-]。"""
    tail = rand(3).hex()
    return "h-%d-%s" % (now_ms, tail), "%s-handoff-%s" % (name_prefix, tail), str(uuid.UUID(bytes=rand(16), version=4))
