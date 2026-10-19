# 进程身份与判活（os.kill + ps）；查询出错一律判"不确定"，不当作已死。
import os
import re
import subprocess
import time


# ps 的 lstart 随 TZ 变化，所以所有 ps 调用固定 TZ=UTC，让不同时区的进程得到同一个字符串
PS_ENV = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"}


class Deadline:
    """一次钩子调用的总时间预算。"""

    def __init__(self, seconds):
        self.ends = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.ends - time.monotonic())


def run_limited(args, deadline, **kwargs):
    """在剩余预算内运行子进程；预算用完抛 TimeoutExpired。"""
    remaining = deadline.remaining()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run(args, timeout=remaining, **kwargs)


def valid_identity(record):
    """身份三元组（会话号 + pid + lstart）齐全才有效。"""
    return bool(
        isinstance(record, dict)
        and isinstance(record.get("session_id"), str) and record["session_id"]
        and isinstance(record.get("pid"), int) and 1 < record["pid"] < 2 ** 31
        and isinstance(record.get("lstart"), str) and record["lstart"]
    )


def is_claude(comm):
    """进程名是 claude*，或是版本号形式的二进制文件名。"""
    name = os.path.basename(comm.split()[0]) if comm.split() else ""
    return name.startswith("claude") or bool(re.fullmatch(r"\d+\.\d+\.\d+", name))


def pid_exists(pid):
    """True 存在 / False 确实不存在 / None 查询出错。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return None
    return True


def ps_field(pid, field, deadline):
    """取 ps 的一个字段，返回 ("ok", 值) 或 ("fail", "")；任何异常都是 fail。"""
    if not isinstance(pid, int) or pid <= 1:
        return "fail", ""
    try:
        result = run_limited(
            ["ps", "-o", field + "=", "-p", str(pid)], deadline,
            capture_output=True, text=True,
            env=dict(os.environ, **PS_ENV),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return "fail", ""
    if result.returncode != 0:
        return "fail", ""
    value = result.stdout.strip()
    return ("ok", value) if value else ("fail", "")


def _state_after_ps_failure(pid):
    """ps 失败后再看一次进程是否存在：确实不存在才判 dead，否则不确定。"""
    return "dead" if pid_exists(pid) is False else "unknown"


def proc_state(record, deadline):
    """返回 live / dead / unknown。unknown 永远不授权接管。"""
    if not valid_identity(record):
        return "unknown"
    pid = record["pid"]
    exists = pid_exists(pid)
    if exists is False:
        return "dead"
    if exists is None:
        return "unknown"
    status, comm = ps_field(pid, "comm", deadline)
    if status != "ok":
        return _state_after_ps_failure(pid)
    if not is_claude(comm):
        return "dead"
    status, lstart = ps_field(pid, "lstart", deadline)
    if status != "ok":
        return _state_after_ps_failure(pid)
    return "live" if lstart == record["lstart"] else "dead"
