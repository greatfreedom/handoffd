# 单主控锁核心：由 poc/q6_single_main_lock.py 派生，修复 MVP-1（git 探测出错不退回本地锁）和 MVP-2（判活区分"不存在"与"查询出错"）。
import fcntl
import json
import os
import subprocess
import tempfile

from cdm import procs
from cdm.procs import valid_identity

SUCCESS_CONTEXT = (
    "本次提示已通过项目主控锁检查，当前会话是持锁主控。此前由本锁钩子发出的"
    "“你不是主控，不要做任何改动”状态提示已失效；本消息不扩大 Owner 授权范围。"
)


def in_git_repo(proj):
    """从 realpath(proj) 逐级向上找 .git（文件或目录都算）；环境里有 GIT_DIR 也算。"""
    if os.environ.get("GIT_DIR"):
        return True
    current = os.path.realpath(proj)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _git_common_dir(proj, deadline):
    """返回 ("ok", git 公共目录) 或 ("error", 原因)；只在 git 仓库内调用。"""
    try:
        result = procs.run_limited(
            ["git", "-C", proj, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            deadline, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return "error", "git 调用失败：%s" % type(exc).__name__
    output = result.stdout.strip()
    if result.returncode != 0:
        return "error", "git 退出码 %d" % result.returncode
    if not output:
        return "error", "git 没有输出"
    if not os.path.isdir(output):
        return "error", "git 公共目录不存在：%s" % output
    return "ok", output


def lock_path(proj, deadline):
    """返回 ("ok", 锁路径) 或 ("error", 原因)。git 仓库里探测失败一律 error，绝不退回本地锁。"""
    if not in_git_repo(proj):
        return "ok", os.path.join(proj, ".claude", "main-controller.lock")
    status, common = _git_common_dir(proj, deadline)
    if status != "ok":
        return "error", common
    return "ok", os.path.join(common, "claude-main-controller.lock")


def state_dir(proj, deadline):
    """handoffd 状态目录 <S>（设计:9）：git 项目用 <git 公共目录>/handoffd，否则 <项目>/.claude/handoffd。返回 ("ok", 路径) 或 ("error", 原因)。"""
    if not in_git_repo(proj):
        return "ok", os.path.join(proj, ".claude", "handoffd")
    status, common = _git_common_dir(proj, deadline)
    if status != "ok":
        return "error", common
    return "ok", os.path.join(common, "handoffd")


def ps_identity(deadline):
    """找到本钩子所属 claude 进程的 pid；父进程链任一步查询失败返回 None。"""
    pid = os.getppid()
    for _ in range(6):
        if not pid or pid <= 1:
            break
        status, comm = procs.ps_field(pid, "comm", deadline)
        if status != "ok":
            return None
        if procs.is_claude(comm):
            return pid
        status, parent = procs.ps_field(pid, "ppid", deadline)
        if status != "ok" or not parent.isdigit():
            return None
        pid = int(parent)
    return None


def claude_chain(deadline, limit=12):
    """本钩子（或命令）所在进程的祖先链里所有 claude 进程，由近及远：[(pid, lstart)]。

    祖先链含 holder 的 claude 进程 ⇒ 本进程是 holder 的子任务（例如主控跑 `claude -p`，设计:90）。
    任何一步 ps 查询失败都返回已找到的部分（可能为空），由调用方按"无法判定"处理。"""
    found = []
    pid = os.getppid()
    for _ in range(limit):
        if not pid or pid <= 1:
            break
        status, comm = procs.ps_field(pid, "comm", deadline)
        if status != "ok":
            break
        if procs.is_claude(comm):
            status, lstart = procs.ps_field(pid, "lstart", deadline)
            if status == "ok":
                found.append((pid, lstart))
        status, parent = procs.ps_field(pid, "ppid", deadline)
        if status != "ok" or not parent.isdigit():
            break
        pid = int(parent)
    return found


def my_identity(deadline):
    """返回 (pid 或 None, lstart)；取不到时 lstart 为空串。"""
    pid = ps_identity(deadline)
    if not pid:
        return None, ""
    status, lstart = procs.ps_field(pid, "lstart", deadline)
    return pid, (lstart if status == "ok" else "")


def same_identity(left, right):
    """会话号、pid、lstart 三者都相同才算同一持锁者。"""
    return bool(
        valid_identity(left) and valid_identity(right)
        and left["session_id"] == right["session_id"]
        and left["pid"] == right["pid"]
        and left["lstart"] == right["lstart"]
    )


def read_lock(lock):
    """返回 ("missing"|"ok"|"error", 记录)。"""
    try:
        with open(lock, encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, TypeError, RecursionError):
        return "error", None
    return ("ok", value) if valid_identity(value) else ("error", None)


def publish_lock(lock, record):
    """临时文件 + os.replace 原子发布锁记录。"""
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
    """对固定的 .guard 文件取非阻塞 flock；拿不到返回 None。"""
    fd = os.open(lock + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_guard(fd):
    """释放 guard（文件本身永不删除）。"""
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def acquire(lock, record, deadline):
    """在 guard 内读判改：返回 (acquired|held_by_me|conflict|busy|error, 持锁记录)。"""
    os.makedirs(os.path.dirname(lock), exist_ok=True)
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
            # 同一个 claude 进程可能开启新生命周期（/clear 或进程内 resume），旧会话号不能卡住新提示（L9）。
            same_process = holder["pid"] == record["pid"] and holder["lstart"] == record["lstart"]
            state = "dead" if same_process else procs.proc_state(holder, deadline)
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
    """只有持锁身份能释放：返回 released|not_holder|busy|error。"""
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
    """给用户看的冲突说明。"""
    same = " （与本会话同一会话号，但在另一个进程里运行）" if (holder or {}).get("session_id") == sid else ""
    return (
        "本项目已有主控在运行"
        "（pid %s，会话 %s）%s。同一项目只允许一个主控。"
        % ((holder or {}).get("pid", "?"), str((holder or {}).get("session_id", "?"))[:8], same)
    )


def inspect(proj, deadline=None):
    """只读查看锁状态（守护程序用）：不拿 guard，不改任何文件。"""
    deadline = deadline or procs.Deadline(2.0)
    status, lock = lock_path(proj, deadline)
    if status != "ok":
        return {
            "lock_status": "error", "lock": None, "record_status": "error",
            "holder": None, "holder_state": "unknown", "reason": lock,
        }
    record_status, holder = read_lock(lock)
    if record_status == "missing":
        holder_state = "none"
    elif record_status == "ok":
        holder_state = procs.proc_state(holder, deadline)
    else:
        holder_state = "unknown"
    return {
        "lock_status": "ok", "lock": lock, "record_status": record_status,
        "holder": holder, "holder_state": holder_state,
    }
