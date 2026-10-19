# handoffd 状态目录 <S> 的读写（设计:34-46）：mode、运行标记、每会话状态、事件流水、通知记录、心跳。
# 纪律：manual 下只允许 mode 文件读取与 run/ 标记（规格:75、:85）；其余写入都由调用方在 auto 下才调用。
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time

from cdm import util

MVP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_WAIT_SECONDS = 1.0


def mode_path(sdir):
    return os.path.join(sdir, "mode")


def read_mode(sdir):
    """返回 (mode, since_ms)。文件缺失、坏 JSON、值不是 auto ⇒ 一律按 manual（默认 manual，规格:45）。"""
    status, value = util.read_json(mode_path(sdir))
    if status == "ok" and isinstance(value, dict) and value.get("mode") == "auto":
        since = value.get("since_ms")
        return "auto", since if isinstance(since, int) and not isinstance(since, bool) else 0
    return "manual", 0


def write_mode(sdir, mode, since_ms):
    """原子写 mode（只由 on / off 命令调用）。"""
    util.atomic_write_json(mode_path(sdir), {"mode": mode, "since_ms": since_ms})


def run_path(sdir, name):
    return os.path.join(sdir, "run", name)


def wait_marker(sdir, sid):
    """「同次等待只响一次」标记（设计:46）。"""
    return run_path(sdir, "waiting." + (util.safe_id(sid) or "unknown"))


def create_marker(path, content=""):
    """O_EXCL 原子创建标记；已存在返回 False（即去重命中）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def clear_marker(path):
    """标记存在则删除并返回其内容（可能为空串）；不存在返回 None，且不产生任何写入。"""
    try:
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
    except FileNotFoundError:
        return None
    except OSError:
        content = ""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return content


def append_event(sdir, record):
    """事件流水 events.jsonl（auto 才写；尽力而为）。"""
    return util.append_jsonl(os.path.join(sdir, "events.jsonl"), record)


def notices_path(sdir):
    return os.path.join(sdir, "notices.jsonl")


def append_notice(sdir, record):
    """叫过 Owner 的记录 notices.jsonl（auto 才写）。"""
    return util.append_jsonl(notices_path(sdir), record)


def new_notice_id(now_ms):
    return "n-%d-%s" % (now_ms, os.urandom(3).hex())


def notice_exists(sdir, notice_id):
    """notices.jsonl 里是否已有这个 id 的 notice 记录（daemon 用它给每类响铃去重；id 由 handoff id + 情形键确定，重启后仍然成立）。"""
    try:
        with open(notices_path(sdir), encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("type") == "notice" and item.get("id") == notice_id:
                    return True
    except OSError:
        return False
    return False


def append_rung(sdir, notice_id, now_ms):
    """某条 notice「已响」的单独落盘记录（K1）：发声之后才写；daemon 每轮对「有 notice 无 rung」补响（宁可重复一次，不可零次）。"""
    return util.append_jsonl(notices_path(sdir), {"type": "rung", "id": notice_id, "t_ms": now_ms})


def unrung_notices(sdir):
    """daemon 发出的（cat=handoff）、没有 rung 记录的 notice（按写入顺序）。「解除（resolve）」与「已响（rung）」是两回事（B3）：
    off / 归档只能解除，不能让一条没响过的 notice 从补响集合里消失。读不出按空。"""
    notices, done = {}, set()
    try:
        with open(notices_path(sdir), encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "notice" and item.get("cat") == "handoff" and item.get("hid") and item.get("id"):
                    notices[item["id"]] = item
                elif item.get("type") == "rung":
                    done.add(item.get("id"))
    except OSError:
        return []
    return [item for nid, item in notices.items() if nid not in done]


def proc_stop_path(sdir, pid, lstart):
    """每进程的 Stop 记录文件（K2 补全）：与会话号无关，同一个 claude 进程 /clear、/resume 换多少次号都是同一个文件。"""
    digest = hashlib.sha1(("%d:%s" % (pid, lstart)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(sdir, "procs", "%d-%s.json" % (pid, digest))


def read_proc_stop_status(sdir, pid, lstart):
    """某进程最近一次 Stop 的记录，带状态：("missing", {}) 文件不存在（从未 Stop 过）/ ("ok", {n, tasks, crons, ms}) /
    ("error", {}) 读不出、坏 JSON、结构不对。签字路径靠它区分「没有」与「读不出」（R3-01：读不出不能当基线 0）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or not lstart:
        return "missing", {}
    status, value = util.read_json(proc_stop_path(sdir, pid, lstart))
    if status == "missing":
        return "missing", {}
    if status != "ok" or not isinstance(value, dict) or not isinstance(value.get("n"), int) or isinstance(value.get("n"), bool):
        return "error", {}
    return "ok", value


def read_proc_stop(sdir, pid, lstart):
    """commit 判断用：某进程最近一次 Stop 的记录；没有或读不出 ⇒ {}（偏向「不换棒」：读不出时不会误判成签后已 Stop）。"""
    return read_proc_stop_status(sdir, pid, lstart)[1]


def record_proc_stop(sdir, pid, lstart, tasks, crons, now_ms):
    """钩子在 Stop 时调用：该进程的 Stop 序号 +1，并记这次的两个数组数。同一进程的并发写由 flock 串行。"""
    path = proc_stop_path(sdir, pid, lstart)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        end = time.monotonic() + GUARD_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    return False
                time.sleep(0.01)
        status, entry = read_proc_stop_status(sdir, pid, lstart)
        if status == "error":
            return False   # 读不出旧序号：不覆盖（否则序号会倒退成 1，破坏单调）；本次 Stop 不记，偏向「不换棒」
        util.atomic_write_json(path, {"n": entry.get("n", 0) + 1, "tasks": tasks, "crons": crons, "ms": now_ms})
        return True
    finally:
        os.close(fd)


def pending_notices(sdir):
    """未解除的响铃通知：type=notice 且 ring=true，减去已有 resolve 记录的 id。读不出按空。"""
    pending = {}
    try:
        with open(notices_path(sdir), encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "notice" and item.get("ring") and item.get("id"):
                    pending[item["id"]] = item
                elif item.get("type") == "resolve":
                    pending.pop(item.get("id"), None)
    except OSError:
        return []
    return list(pending.values())


def resolve_notices_for(sdir, hid, now_ms, keep=()):
    """把某次交接名下所有未解除的响铃通知标为已解除（放弃 / 完成交接时）；kind 在 keep 里的不解除（「卡住了」类要留到 Owner 看见）。返回解除条数。"""
    count = 0
    for item in pending_notices(sdir):
        if item.get("hid") == hid and item.get("kind") not in keep:
            append_notice(sdir, {"type": "resolve", "id": item["id"], "t_ms": now_ms})
            count += 1
    return count


def heartbeat_path(sdir):
    return os.path.join(sdir, "heartbeat")


def read_heartbeat(sdir):
    """心跳（epoch 毫秒）；缺失或内容不合格返回 None。"""
    try:
        with open(heartbeat_path(sdir), encoding="utf-8") as stream:
            value = int(stream.read().strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def write_heartbeat(sdir, now_ms):
    """写心跳（daemon 用；S1 只给测试手写心跳文件用）。"""
    util.atomic_write_text(heartbeat_path(sdir), str(int(now_ms)))


def session_path(sdir, sid):
    safe = util.safe_id(sid)
    return None if safe is None else os.path.join(sdir, "sessions", safe + ".json")


def read_session(sdir, sid):
    path = session_path(sdir, sid)
    if path is None:
        return {}
    status, value = util.read_json(path)
    return value if status == "ok" and isinstance(value, dict) else {}


def update_session(sdir, sid, mutate):
    """每会话 flock 内读改写 sessions/<sid>.json（同 hook_nudge 的 guard 写法）；拿不到锁或会话号不安全返回 False。"""
    path = session_path(sdir, sid)
    if path is None:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path[:-len(".json")] + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        end = time.monotonic() + GUARD_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    return False
                time.sleep(0.01)
        status, value = util.read_json(path)
        record = value if status == "ok" and isinstance(value, dict) else {}
        mutate(record)
        util.atomic_write_json(path, record)
        return True
    finally:
        os.close(fd)


def spawn_ring(project_dir, kind, what, why, ask, recommend, if_no_reply):
    """钩子起分离子进程响铃（设计 :54 行）：不等待、不继承标准流；失败不影响钩子决定。"""
    args = [
        sys.executable, os.path.join(MVP_ROOT, "bin", "handoffd"), "ring",
        "--project", project_dir, "--kind", kind,
        "--what", what, "--why", why, "--ask", ask, "--recommend", str(recommend), "--if-no-reply", if_no_reply,
    ]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
