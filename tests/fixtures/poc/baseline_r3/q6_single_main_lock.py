#!/usr/bin/env python3
# Q6：同一项目只允许一个主控——SessionStart 抢锁/警告，UserPromptSubmit 对非持锁会话 block，SessionEnd 释放
# 用法（钩子命令）：q6_single_main_lock.py start|prompt|end ；逃生口：环境变量 MAIN_LOCK_OVERRIDE=1（例如主控自己在项目里跑 `claude -p` 时）
# R1 复核后修正：① 抢锁原子化（O_EXCL 建锁；接管失效锁用 mkdir 互斥 + 复查 + os.replace）；② 锁不放随 worktree 变化的 cwd
# R2 复核后修正：③ 持锁身份 = 会话号 + claude 进程 pid + 进程启动时间（同一会话号在两个终端同时恢复时，第二个进程不算持锁者）；
#   ④ 进程启动时间用 LC_ALL=C 取，避免不同 locale 的字符串比较失配；⑤ 只有持锁进程自己的 SessionEnd 才删锁；
#   ⑥ 接管互斥目录超过 60 秒视为残留并清理；⑦ git 仓库时锁放在 git 公共目录（主检出与所有 worktree 共用一把锁）
import json, os, re, subprocess, sys, time

mode = sys.argv[1]
inp = json.load(sys.stdin)
sid = inp.get("session_id")
proj = os.environ.get("CLAUDE_PROJECT_DIR") or inp.get("cwd") or os.getcwd()
logf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "q6_lock_log.jsonl")

def lock_path():
    r = subprocess.run(["git", "-C", proj, "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return os.path.join(r.stdout.strip(), "claude-main-controller.lock")     # 在 .git 里：不会被提交，所有 worktree 同一路径
    return os.path.join(proj, ".claude", "main-controller.lock")

lock = lock_path()
mutex = lock + ".takeover"

def log(**kw):
    with open(logf, "a") as f:
        f.write(json.dumps(dict(t=time.strftime("%H:%M:%S"), mode=mode, sid=sid, **kw), ensure_ascii=False) + "\n")

def emit(o): print(json.dumps(o, ensure_ascii=False))

def ps(pid, field):
    r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C", LANG="C"))
    return r.stdout.strip() if r.returncode == 0 else ""

def is_claude(comm):
    name = os.path.basename(comm.split()[0]) if comm.split() else ""
    return name.startswith("claude") or bool(re.fullmatch(r"\d+\.\d+\.\d+", name))   # 进程名可能显示为版本号（二进制文件名）

def my_claude_pid():
    pid = os.getppid()
    for _ in range(6):
        if not pid or pid <= 1: break
        if is_claude(ps(pid, "comm")):
            return pid
        pid = int(ps(pid, "ppid") or 0)
    return None

MY_PID = my_claude_pid()
MY_LSTART = ps(MY_PID, "lstart") if MY_PID else None

def live_sessions():
    try:
        out = subprocess.run(["claude", "agents", "--json", "--cwd", proj], capture_output=True, text=True, timeout=10).stdout
        return {s["sessionId"]: s for s in json.loads(out or "[]") if s.get("sessionId") and s.get("pid")}
    except Exception:
        return {}

def proc_alive(h):
    pid = (h or {}).get("pid")
    return bool(pid and is_claude(ps(pid, "comm")) and ps(pid, "lstart") == h.get("lstart"))

def is_me(h):
    return bool(h and h.get("session_id") == sid and h.get("pid") == MY_PID and h.get("lstart") == MY_LSTART)

def alive(h, live):
    """持锁进程还活着？有 pid 记录时以进程为准（同会话号的另一进程不算）；没有 pid 时退回按会话号查活会话列表。"""
    if not h: return False
    if h.get("pid"): return proc_alive(h)
    return h.get("session_id") in live

def read_lock():
    for _ in range(10):                      # 别的会话刚 O_EXCL 建锁、还没写完内容时会读到空文件：稍等重读，不当失效锁
        try:
            with open(lock) as f: return json.load(f)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            time.sleep(0.1)
    return None

def me_record():
    return {"session_id": sid, "pid": MY_PID, "lstart": MY_LSTART, "acquired": time.strftime("%Y-%m-%d %H:%M:%S")}

def try_create(rec):
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f: json.dump(rec, f, ensure_ascii=False)
    return True

def acquire(live):
    """返回 (状态, 持锁记录)：acquired / held_by_me / conflict / busy"""
    rec = me_record()
    if try_create(rec): return "acquired", rec
    h = read_lock()
    if is_me(h): return "held_by_me", h
    if alive(h, live): return "conflict", h
    try:
        if time.time() - os.stat(mutex).st_mtime > 60: os.rmdir(mutex)   # 接管进程被杀留下的残留互斥目录
    except FileNotFoundError:
        pass
    try:
        os.mkdir(mutex)                      # 接管互斥：同一时刻只有一个会话能接管失效锁
    except FileExistsError:
        return "busy", h
    try:
        h2 = read_lock()
        if h2 and h2 != h and alive(h2, live): return "conflict", h2   # 复查：别人刚抢到
        rec = dict(rec, took_over_stale=(h or {}).get("session_id"), took_over_pid=(h or {}).get("pid"))
        tmp = f"{lock}.{os.getpid()}.tmp"
        with open(tmp, "w") as f: json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, lock)
        return "acquired", rec
    finally:
        os.rmdir(mutex)

if os.environ.get("MAIN_LOCK_OVERRIDE") == "1":
    log(action="override"); sys.exit(0)

if mode == "end":
    h = read_lock()
    if is_me(h):
        os.remove(lock); log(action="released", pid=MY_PID)
    else:
        log(action="end_not_holder", pid=MY_PID, holder=(h or {}).get("session_id"), holder_pid=(h or {}).get("pid"))
    sys.exit(0)

live = live_sessions()
state, h = acquire(live)
if state in ("acquired", "held_by_me"):
    log(action=state, pid=MY_PID, lock=lock, stale_prev=h.get("took_over_stale"), stale_prev_pid=h.get("took_over_pid"))
    sys.exit(0)

hl = live.get((h or {}).get("session_id"), {})
same = " （与本会话同一会话号，但在另一个进程里运行）" if (h or {}).get("session_id") == sid else ""
msg = f"本项目已有主控在运行：{hl.get('name', '?')}（pid {(h or {}).get('pid')}，会话 {str((h or {}).get('session_id'))[:8]}，状态 {hl.get('status', '未登记')}）{same}。同一项目只允许一个主控。"
log(action=state, pid=MY_PID, holder=(h or {}).get("session_id"), holder_pid=(h or {}).get("pid"))
if mode == "start":
    emit({"systemMessage": "⛔ " + msg + " 本会话的提示将被拦截；确认旧会话已废弃请先关掉它，或以 MAIN_LOCK_OVERRIDE=1 启动。",
          "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "⛔ " + msg + " 你不是主控，不要做任何改动。"}})
else:
    emit({"decision": "block", "reason": "⛔ " + msg + " 本条提示已被拦截（未发送给模型）。"})
