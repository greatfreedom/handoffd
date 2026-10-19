# handoffd S1 测试的公共工具：进程内调钩子、响铃记录器、目录快照、可控时钟、写登记册。
import contextlib
import hashlib
import io
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import patched_env, require  # noqa: E402
from cdm import baton, hook_relay, sessions, util  # noqa: E402


class Rings:
    """替代 sessions.spawn_ring：只记录，不出声、不起子进程。"""

    def __init__(self):
        self.calls = []

    def __call__(self, project, kind, what, why, ask, recommend, if_no_reply):
        self.calls.append({"project": project, "kind": kind, "what": what, "why": why, "ask": ask,
                           "recommend": recommend, "if_no_reply": if_no_reply})

    def count(self, kind=None):
        return sum(1 for call in self.calls if kind is None or call["kind"] == kind)


class Clock:
    """可控时钟：替换 util.now_ms，退出时恢复。"""

    def __init__(self, start_ms):
        self.ms = start_ms

    def advance(self, seconds):
        self.ms += int(seconds * 1000)


@contextlib.contextmanager
def fixed_clock(start_ms=None):
    original = util.now_ms
    clock = Clock(original() if start_ms is None else start_ms)
    util.now_ms = lambda: clock.ms
    try:
        yield clock
    finally:
        util.now_ms = original


def ident_fn(record):
    """由 ProcessPool.identity 的记录造 identity(deadline) → (pid, lstart)。"""
    return lambda deadline: (record["pid"], record["lstart"])


def call_relay(event, project, sid, ident=None, chain=None, rings=None, extra=None, env=None, raw=None):
    """进程内调一次 hook_relay.main；返回 (退出码, 输出 JSON 或 None)。
    ident：identity(deadline)；None 表示"取不到本进程身份"。chain：祖先链函数，缺省为 [ident]。"""
    payload = {"hook_event_name": event, "session_id": sid, "cwd": project}
    payload.update(extra or {})
    out = io.StringIO()
    identity = ident or (lambda deadline: (None, ""))
    chain_fn = chain if chain is not None else (lambda deadline: [identity(deadline)] if identity(deadline)[0] else [])
    changes = {"CLAUDE_PROJECT_DIR": project}
    changes.update(env or {})
    with patched_env(**changes):
        code = hook_relay.main(
            [event], stdin=io.StringIO(raw if raw is not None else json.dumps(payload)), stdout=out,
            identity=identity, chain=chain_fn, ring_fn=rings or Rings(),
        )
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None)


def is_denied(output):
    return bool(output and output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


def is_blocked(output):
    return bool(output and output.get("decision") == "block")


def tool_call(tool, **tool_input):
    return {"tool_name": tool, "tool_input": tool_input}


def sdir_of(project):
    """非 git 测试项目的 <S>。"""
    return os.path.join(project, ".claude", "handoffd")


def set_auto(project, since_ms=None):
    """把项目切到 auto：mode 的 since 缺省为"10 秒前"，这样心跳文件缺失也不会立刻触发心跳铃。"""
    sdir = sdir_of(project)
    sessions.write_mode(sdir, "auto", util.now_ms() - 10_000 if since_ms is None else since_ms)
    return sdir


def fresh_heartbeat(sdir, clock_ms):
    sessions.write_heartbeat(sdir, clock_ms - 1000)


def tree_snapshot(root):
    """目录树快照：{相对路径: 内容 sha256 或 "<dir>"}；含空目录，用来断言"零写入"。"""
    snapshot = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            snapshot[os.path.relpath(os.path.join(base, name), root)] = "<dir>"
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as stream:
                snapshot[os.path.relpath(path, root)] = hashlib.sha256(stream.read()).hexdigest()
    return snapshot


def read_baton(sdir):
    status, state = baton.read(sdir)
    require(status in ("ok", "missing"), "登记册读不出：%s" % status)
    return state


def write_baton(sdir, holder=None, handoff=None, retire=None):
    """直接写登记册（S1 没有发起交接的命令，测试用它造 handoff≠null 的状态）。"""
    state = {"version": 1, "holder": holder, "handoff": handoff, "retire": retire or []}
    util.atomic_write_json(baton.baton_path(sdir), state)
    return state


def holder_of(record, claimed_ms=1):
    return {"sid": record["session_id"], "pid": record["pid"], "lstart": record["lstart"], "claimed_ms": claimed_ms}


def handoff_with_b(uuid, phase="arming", **extra):
    return dict({"id": "h-1", "reason": "goal", "phase": phase,
                 "b": {"uuid": uuid, "name": "b-window", "pid": None, "lstart": None, "online_ms": None},
                 "qa_count": 0}, **extra)
