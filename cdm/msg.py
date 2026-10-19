# daemon → 会话的消息投递（设计:31、§5.3；poc/q5_post_to_session.py 派生）：读 ~/.claude/sessions/<pid>.json 取会话收件箱 socket，
# 发一行 {"type":"user","message":{"role":"user","content":...}}（无确认）。
# 本模块只做 I/O；「目标进程是不是登记册里的那个」(J2②) 由调用方 daemon 在调用前核对。
import json
import os
import socket

SESSIONS_ENV = "CDM_CLAUDE_SESSIONS_DIR"   # 测试必须把它指向假目录，避免误投真实会话
PREFIX = "[handoffd] "


def sessions_dir(env=None):
    env = os.environ if env is None else env
    return env.get(SESSIONS_ENV) or os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def session_info(pid, directory=None):
    """读 <sessions>/<pid>.json（只读）；缺失 / 坏 JSON / 不是对象 ⇒ None。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    try:
        with open(os.path.join(directory or sessions_dir(), "%d.json" % pid), encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def find_session(session_id, directory=None):
    """按会话号在 <sessions>/ 里找 claude 的会话文件（B5：B 预分配了 --session-id，登记前只能这样找它）。返回 (pid, 文件内容) 或 None。"""
    directory = directory or sessions_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in sorted(names):
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        info = session_info(int(name[:-5]), directory)
        if info and info.get("sessionId") == session_id:
            return int(name[:-5]), info
    return None


def session_cwd(pid, directory=None):
    """该进程登记的工作目录（session 文件里的 cwd）；取不到 ⇒ None（调用方按「核对不过」处理）。"""
    info = session_info(pid, directory)
    cwd = info.get("cwd") if info else None
    return cwd if isinstance(cwd, str) and cwd else None


def format_message(kind, body=""):
    """除 OPEN 外统一 `[handoffd] <TYPE> …`；OPEN 的 content 就是开场指令原文，无前缀（规格:92、设计:97）。"""
    if kind == "OPEN":
        return body
    return PREFIX + kind + (" " + body if body else "")


def send(pid, content, timeout=3.0, directory=None, info=None):
    """向 pid 的会话收件箱投一条 user 消息。返回 (True, "") / (False, 原因)；只有「已 sendall 成功」才算 True（False ⇒ 确定没发出去）。
    info：调用方（daemon）紧邻发出前重核目标时读到的会话文件内容——给了就只用它的 socket，不再读文件（K3：不让核对与投递之间的文件变化改写目标）。"""
    if not isinstance(content, str) or not content:
        return False, "empty_content"
    if info is None:
        info = session_info(pid, directory)
    path = info.get("messagingSocketPath") if info else None
    if not isinstance(path, str) or not path:
        return False, "no_socket"
    line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(line.encode("utf-8"))
    except OSError as exc:
        return False, type(exc).__name__
    finally:
        sock.close()
    return True, ""
