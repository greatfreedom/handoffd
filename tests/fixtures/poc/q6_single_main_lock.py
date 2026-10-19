#!/usr/bin/env python3
# Q6：同一项目只允许一个主控——固定 guard 串行化持久锁记录的判定、发布和释放。
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

START_PROMPT_BUDGET = 2.0
END_BUDGET = 1.0
SUCCESS_CONTEXT = (
    "本次提示已通过项目主控锁检查，当前会话是持锁主控。此前由本锁钩子发出的"
    "“你不是主控，不要做任何改动”状态提示已失效；本消息不扩大 Owner 授权范围。"
)


class Deadline:
    def __init__(self, seconds):
        self.ends = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.ends - time.monotonic())


def run_limited(args, deadline, **kwargs):
    remaining = deadline.remaining()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run(args, timeout=remaining, **kwargs)


def lock_path(proj, deadline):
    try:
        result = run_limited(
            ["git", "-C", proj, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            deadline, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result and result.returncode == 0 and result.stdout.strip():
        return os.path.join(result.stdout.strip(), "claude-main-controller.lock")
    return os.path.join(proj, ".claude", "main-controller.lock")


def best_effort_log(logf, mode, sid, **kw):
    try:
        with open(logf, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(t=time.strftime("%H:%M:%S"), mode=mode, sid=sid, **kw), ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ps_field(pid, field, deadline):
    if not isinstance(pid, int) or pid <= 1:
        return "dead", ""
    try:
        result = run_limited(
            ["ps", "-o", field + "=", "-p", str(pid)], deadline,
            capture_output=True, text=True,
            env=dict(os.environ, LC_ALL="C", LANG="C"),
        )
    except subprocess.TimeoutExpired:
        return "unknown", ""
    except OSError:
        return "unknown", ""
    if result.returncode != 0:
        return "dead", ""
    value = result.stdout.strip()
    return ("ok", value) if value else ("dead", "")


def is_claude(comm):
    name = os.path.basename(comm.split()[0]) if comm.split() else ""
    return name.startswith("claude") or bool(re.fullmatch(r"\d+\.\d+\.\d+", name))


def my_claude_pid(deadline):
    pid = os.getppid()
    for _ in range(6):
        if not pid or pid <= 1:
            break
        status, comm = ps_field(pid, "comm", deadline)
        if status != "ok":
            return None
        if is_claude(comm):
            return pid
        status, parent = ps_field(pid, "ppid", deadline)
        if status != "ok" or not parent.isdigit():
            return None
        pid = int(parent)
    return None


def valid_identity(record):
    return bool(
        isinstance(record, dict)
        and isinstance(record.get("session_id"), str) and record["session_id"]
        and isinstance(record.get("pid"), int) and record["pid"] > 1
        and isinstance(record.get("lstart"), str) and record["lstart"]
    )


def same_identity(left, right):
    return bool(
        valid_identity(left) and valid_identity(right)
        and left["session_id"] == right["session_id"]
        and left["pid"] == right["pid"]
        and left["lstart"] == right["lstart"]
    )


def proc_state(record, deadline):
    """Return live, dead, or unknown. Unknown never authorizes takeover."""
    if not valid_identity(record):
        return "unknown"
    status, comm = ps_field(record["pid"], "comm", deadline)
    if status != "ok":
        return status
    if not is_claude(comm):
        return "dead"
    status, lstart = ps_field(record["pid"], "lstart", deadline)
    if status != "ok":
        return status
    return "live" if lstart == record["lstart"] else "dead"


def read_lock(lock):
    try:
        with open(lock, encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, json.JSONDecodeError):
        return "error", None
    return ("ok", value) if valid_identity(value) else ("error", None)


def publish_lock(lock, record):
    fd, temporary = tempfile.mkstemp(prefix=".claude-main-controller.", dir=os.path.dirname(lock))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        os.replace(temporary, lock)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def guard_lock(lock):
    fd = os.open(lock + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_guard(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def acquire(lock, record, deadline):
    """All reads and formal-lock changes are serialized by the permanent guard."""
    guard = guard_lock(lock)
    if guard is None:
        return "busy", None
    try:
        status, holder = read_lock(lock)
        if status == "error":
            return "error", None
        if status == "ok" and same_identity(holder, record):
            return "held_by_me", holder
        if status == "ok":
            # Claude may reuse one process for a new lifecycle (/clear or in-process
            # resume).  Its prior SID must not strand the next lifecycle's prompt.
            state = "dead" if holder["pid"] == record["pid"] and holder["lstart"] == record["lstart"] else proc_state(holder, deadline)
            if state == "live":
                return "conflict", holder
            if state != "dead":
                return "error", holder
            record = dict(record, took_over_stale=holder["session_id"], took_over_pid=holder["pid"])
        try:
            publish_lock(lock, record)
        except (OSError, ValueError):
            return "error", holder
        return "acquired", record
    finally:
        release_guard(guard)


def release(lock, record):
    guard = guard_lock(lock)
    if guard is None:
        return "busy"
    try:
        status, holder = read_lock(lock)
        if status == "ok" and same_identity(holder, record):
            os.unlink(lock)
            return "released"
        return "not_holder" if status != "error" else "error"
    except OSError:
        return "error"
    finally:
        release_guard(guard)


def conflict_message(holder, sid):
    same = " （与本会话同一会话号，但在另一个进程里运行）" if (holder or {}).get("session_id") == sid else ""
    return (
        "本项目已有主控在运行"
        f"（pid {(holder or {}).get('pid', '?')}，会话 {str((holder or {}).get('session_id', '?'))[:8]}）{same}。"
        "同一项目只允许一个主控。"
    )


def main(argv=None, stdin=None):
    argv = argv or sys.argv[1:]
    stdin = stdin or sys.stdin
    if len(argv) != 1 or argv[0] not in ("start", "prompt", "end"):
        return 2
    mode = argv[0]
    try:
        inp = json.load(stdin)
    except Exception:
        if mode == "prompt":
            emit({"decision": "block", "reason": "主控锁钩子输入无效，本条提示已被拦截。"})
        return 2
    sid = inp.get("session_id")
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or inp.get("cwd") or os.getcwd()
    deadline = Deadline(END_BUDGET if mode == "end" else START_PROMPT_BUDGET)
    lock = lock_path(proj, deadline)
    logf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "q6_lock_log.jsonl")
    if os.environ.get("MAIN_LOCK_OVERRIDE") == "1":
        best_effort_log(logf, mode, sid, action="override")
        return 0
    pid = my_claude_pid(deadline)
    status, lstart = ps_field(pid, "lstart", deadline) if pid else ("unknown", "")
    record = {"session_id": sid, "pid": pid, "lstart": lstart, "acquired": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not valid_identity(record):
        state, holder = "error", None
    elif mode == "end":
        try:
            state = release(lock, record)
        except OSError:
            state = "error"
        best_effort_log(logf, mode, sid, action=state, pid=pid)
        return 0
    else:
        try:
            state, holder = acquire(lock, record, deadline)
        except OSError:
            state, holder = "error", None
    if state in ("acquired", "held_by_me"):
        best_effort_log(logf, mode, sid, action=state, pid=pid, lock=lock)
        if mode == "prompt":
            emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": SUCCESS_CONTEXT}})
        return 0
    best_effort_log(logf, mode, sid, action=state, pid=pid, holder=(holder or {}).get("session_id"))
    message = conflict_message(holder, sid) if state == "conflict" else "主控锁当前繁忙或状态无法确认，本条提示已被拦截。"
    if mode == "start":
        emit({"systemMessage": "⛔ " + message})
        return 0
    emit({"decision": "block", "reason": "⛔ " + message})
    return 2


if __name__ == "__main__":
    sys.exit(main())
