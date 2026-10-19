# 测试 F5：移植 poc/q6_interleaving_check.py 的 L1–L9 到新 lockcore（全部用真实进程与真实 guard，不桩判活）。
# S1 起 hook_lock 已删除：L1 的"钩子"改为 lockcore.lock_path + acquire 的等价运行器（同样只在 publish_lock 前后加停顿）。
import hashlib
import json
import os
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (MVP_ROOT, POC_DIR, git, make_fake_git, make_fake_ps, make_repo, patched_env, path_with,  # noqa: E402
                     process_pool, require, run_cases, temp_dir, write_exe)
from cdm import lockcore, procs  # noqa: E402

BASELINE = os.path.join(POC_DIR, "baseline_r3", "q6_single_main_lock.py")

# 新版 L1 的运行器：只在 publish_lock 前后加观察用的停顿，其余全部走真实 lockcore 代码；输出 acquire 的结果名
RUNNER = '''
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, %(root)r)
from cdm import lockcore, procs

pause = os.environ.get("L1_PAUSE")
barrier = os.environ["L1_BARRIER"]
real_publish = lockcore.publish_lock


def paused_publish(lock, record):
    if pause == "A_before_publish":
        open(os.path.join(barrier, "A_before_publish.reached"), "w").close()
        until = time.monotonic() + 5
        while not os.path.exists(os.path.join(barrier, "A_before_publish.release")):
            if time.monotonic() > until:
                raise RuntimeError("barrier watchdog")
            time.sleep(0.01)
    real_publish(lock, record)


lockcore.publish_lock = paused_publish
payload = json.load(sys.stdin)
deadline = procs.Deadline(5)
status, lock = lockcore.lock_path(os.environ["CLAUDE_PROJECT_DIR"], deadline)
record = {"session_id": payload["session_id"], "pid": int(os.environ["L1_PID"]), "lstart": os.environ["L1_LSTART"]}
state, _holder = lockcore.acquire(lock, record, deadline)
print(json.dumps({"state": state}))
''' % {"root": MVP_ROOT}

# 旧版基线补丁里的停顿适配器（与 PoC 一致）
BASELINE_ADAPTER = """
def test_barrier(name):
    wanted = os.environ.get("Q6_TEST_PAUSE")
    if wanted != name:
        return
    root = os.environ["Q6_TEST_BARRIER"]
    open(os.path.join(root, name + ".reached"), "w").close()
    until = time.monotonic() + 5
    while not os.path.exists(os.path.join(root, name + ".release")):
        if time.monotonic() > until:
            raise RuntimeError("test barrier watchdog")
        time.sleep(0.01)
"""


def file_digest(path):
    """文件的 sha256。"""
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def wait_for(path, label):
    """等文件出现，5 秒看门狗。"""
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if os.path.exists(path):
            return
        time.sleep(0.01)
    raise AssertionError("watchdog: did not reach " + label)


def prepare_baseline(destination):
    """把 R3 基线复制成带停顿点的测试副本（只读加载原文件，不修改它）。"""
    with open(BASELINE, encoding="utf-8") as stream:
        code = stream.read()
    code = code.replace("import json, os, re, subprocess, sys, time", "import json, os, re, subprocess, sys, time\n" + BASELINE_ADAPTER)
    code = code.replace(
        "fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)",
        "fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)\n        test_barrier('A_after_create')",
    )
    code = code.replace("os.replace(tmp, lock)", "test_barrier('B_before_replace')\n        os.replace(tmp, lock)")
    code = code.replace("MY_PID = my_claude_pid()", "MY_PID = int(os.environ['Q6_TEST_PID'])")
    code = code.replace("MY_LSTART = ps(MY_PID, \"lstart\") if MY_PID else None", "MY_LSTART = 'TEST-LSTART'")
    require("test_barrier('A_after_create')" in code and "test_barrier('B_before_replace')" in code and "Q6_TEST_PID" in code,
            "基线补丁没有打上，测试无效")
    with open(destination, "w", encoding="utf-8") as stream:
        stream.write(code)


def spawn_hook(script_args, env, sid):
    """启动一个钩子子进程并写入输入。"""
    process = subprocess.Popen(script_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    process.stdin.write(json.dumps({"session_id": sid, "cwd": "/unused"}))
    process.stdin.close()
    process.stdin = None
    return process


def finish(process):
    """等钩子结束，返回 (退出码, 标准输出, 标准错误)。"""
    out, err = process.communicate(timeout=10)
    return process.returncode, out.strip(), err.strip()


def l1_old(work):
    """L1 旧版：R3 基线必须实际双放行。"""
    scripts = os.path.join(work, "old_scripts")
    os.makedirs(os.path.join(scripts, "out"))
    script = os.path.join(scripts, "old.py")
    prepare_baseline(script)
    fake_bin = os.path.join(work, "old_bin")
    write_exe(os.path.join(fake_bin, "ps"), "#!/usr/bin/env python3\nimport sys\nf=' '.join(sys.argv)\nprint('TEST-LSTART' if 'lstart=' in f else 'claude')\n")
    write_exe(os.path.join(fake_bin, "claude"), "#!/bin/sh\necho '[]'\n")    # 基线会调 claude agents，用替身保证不跑真实 claude
    project = os.path.join(work, "old-project")
    os.makedirs(os.path.join(project, ".claude"))
    barrier = os.path.join(work, "old-barriers")
    os.makedirs(barrier)

    def env_for(pid, pause=None):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project, Q6_TEST_BARRIER=barrier, Q6_TEST_PID=str(pid), PATH=path_with(fake_bin))
        if pause:
            env["Q6_TEST_PAUSE"] = pause
        return env

    events = []
    proc_a = spawn_hook([sys.executable, script, "prompt"], env_for(4101, "A_after_create"), "A")
    wait_for(os.path.join(barrier, "A_after_create.reached"), "old A after O_EXCL")
    require(proc_a.poll() is None, "旧版 A 在停顿点前就退出了")
    events.append("A：O_EXCL 建出空的正式锁文件")
    proc_b = spawn_hook([sys.executable, script, "prompt"], env_for(4102, "B_before_replace"), "B")
    wait_for(os.path.join(barrier, "B_before_replace.reached"), "old B before replace")
    events.append("B：两次读到空锁、准备好接管替换")
    open(os.path.join(barrier, "A_after_create.release"), "w").close()
    code, out, err = finish(proc_a)
    require(code == 0 and not out and not err, "旧版 A 没有放行：%r %r %r" % (code, out, err))
    events.append("A：提示被放行（模拟的 claude 父进程仍活着）")
    open(os.path.join(barrier, "B_before_replace.release"), "w").close()
    code, out, err = finish(proc_b)
    require(code == 0 and not out and not err, "旧版 B 没有放行：%r %r %r" % (code, out, err))
    events.append("B：替换完成，提示也被放行（双放行）")
    return events


def l1_new(work, pool):
    """L1 新版：guard 让 B 先 busy，A 发布后 B 再试变 conflict。"""
    runner = os.path.join(work, "runner.py")
    with open(runner, "w", encoding="utf-8") as stream:
        stream.write(RUNNER)
    project = os.path.join(work, "new-project")
    os.makedirs(os.path.join(project, ".claude"))
    barrier = os.path.join(work, "new-barriers")
    os.makedirs(barrier)
    ident_a = pool.identity(pool.spawn_claude(), "A")
    ident_b = pool.identity(pool.spawn_claude(), "B")

    def env_for(ident, pause=None):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project, L1_BARRIER=barrier, L1_PID=str(ident["pid"]), L1_LSTART=ident["lstart"],
                   PYTHONDONTWRITEBYTECODE="1")
        if pause:
            env["L1_PAUSE"] = pause
        return env

    events = []
    proc_a = spawn_hook([sys.executable, runner], env_for(ident_a, "A_before_publish"), "A")
    wait_for(os.path.join(barrier, "A_before_publish.reached"), "new A before publish")
    require(proc_a.poll() is None, "新版 A 在停顿点前就退出了")
    events.append("A：持有固定 guard，尚未发布记录")
    code, out, err = finish(spawn_hook([sys.executable, runner], env_for(ident_b), "B"))
    require(code == 0 and json.loads(out)["state"] == "busy" and not err, "新版 B 没有因 guard busy 被挡：%r %r %r" % (code, out, err))
    events.append("B：guard busy，不授权")
    open(os.path.join(barrier, "A_before_publish.release"), "w").close()
    code, out, err = finish(proc_a)
    require(code == 0 and json.loads(out)["state"] == "acquired" and not err, "新版 A 没有取得锁：%r %r %r" % (code, out, err))
    events.append("A：原子发布记录，取得锁")
    code, out, err = finish(spawn_hook([sys.executable, runner], env_for(ident_b), "B"))
    require(code == 0 and json.loads(out)["state"] == "conflict" and not err, "新版 B 重试没有因 conflict 被挡：%r %r %r" % (code, out, err))
    events.append("B：重读到存活的持锁者 A，conflict")
    return events


def case_f5():
    """F5：L1–L9。"""
    before = file_digest(BASELINE)
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        old_events = l1_old(work)
        new_events = l1_new(work, pool)
        details.append("L1 基线复现：R3 原版双放行")
        details.extend("  旧 " + event for event in old_events)
        details.append("L1 新版 PASS：guard 阻止双放行")
        details.extend("  新 " + event for event in new_events)
        details.extend(l2_to_l9(work, pool))
    require(before == file_digest(BASELINE), "基线文件在测试期间被改动")
    return details


def record_of(pool, child, sid):
    """真实进程的身份记录。"""
    ident = pool.identity(child, sid)
    ident["acquired"] = "test"
    return ident


def l2_to_l9(work, pool):
    """L2–L9：真实 guard 文件、真实进程；故障只经 PATH 前置的假 ps 注入。"""
    events = []
    deadline = procs.Deadline(20)
    lock = os.path.join(work, "extra", "extra.lock")
    os.makedirs(os.path.dirname(lock))

    # L2：B 接管已死的持锁者后发布；C 随后读到 B 存活而 conflict
    dead_child = pool.spawn_claude()
    dead_rec = record_of(pool, dead_child, "dead")
    pool.kill(dead_child)
    lockcore.publish_lock(lock, dead_rec)
    rec_b = record_of(pool, pool.spawn_claude(), "B")
    rec_c = record_of(pool, pool.spawn_claude(), "C")
    state, _ = lockcore.acquire(lock, rec_b, deadline)
    require(state == "acquired", "L2 B 没有接管：%s" % state)
    state, _ = lockcore.acquire(lock, rec_c, deadline)
    require(state == "conflict", "L2 C 没有 conflict：%s" % state)
    events.append("L2 PASS：B 接管已死持锁者并发布；C 重读到存活的 B 后 conflict")

    # L3：SessionEnd 持有 guard 时 C 不能发布；迟到的旧 end 不会删掉 C 的记录
    rec_a = record_of(pool, pool.spawn_claude(), "A")
    lockcore.publish_lock(lock, rec_a)
    fd = lockcore.guard_lock(lock)
    require(fd is not None, "L3 guard 准备失败")
    state, _ = lockcore.acquire(lock, rec_c, deadline)
    require(state == "busy", "L3 C 越过了 SessionEnd 的 guard：%s" % state)
    lockcore.release_guard(fd)
    require(lockcore.release(lock, rec_a) == "released", "L3 A 的 end 没有释放")
    state, _ = lockcore.acquire(lock, rec_c, deadline)
    require(state == "acquired", "L3 A 结束后 C 没有拿到锁：%s" % state)
    require(lockcore.release(lock, rec_a) == "not_holder", "L3 旧 end 删掉了 C 的记录")
    events.append("L3 PASS：迟到的 end 持有 guard 时接管被挡；之后旧 end 不能删 C 的锁")

    # L4：旧 mtime 不能绕过被持有的 flock；inode 保持固定
    fd = lockcore.guard_lock(lock)
    guard_file = lock + ".guard"
    inode = os.stat(guard_file).st_ino
    os.utime(guard_file, (time.time() - 120, time.time() - 120))
    rec_d = record_of(pool, pool.spawn_claude(), "D")
    state, _ = lockcore.acquire(lock, rec_d, deadline)
    require(state == "busy" and os.stat(guard_file).st_ino == inode, "L4 旧 guard 被偷走")
    lockcore.release_guard(fd)
    events.append("L4 PASS：变旧的 guard 仍然 busy，inode 未变")

    # L5：发布前没有正式记录；已发布的存活持锁者 conflict；持锁进程真死后才可接管
    os.unlink(lock)
    child_e = pool.spawn_claude()
    rec_e = record_of(pool, child_e, "E")
    rec_f = record_of(pool, pool.spawn_claude(), "F")
    state, _ = lockcore.acquire(lock, rec_e, deadline)
    require(state == "acquired", "L5 E 没有发布：%s" % state)
    state, _ = lockcore.acquire(lock, rec_f, deadline)
    require(state == "conflict", "L5 存活的父进程被接管：%s" % state)
    pool.kill(child_e)
    state, _ = lockcore.acquire(lock, rec_f, deadline)
    require(state == "acquired", "L5 持锁进程已死却没有接管：%s" % state)
    events.append("L5 PASS：发布前不存在；存活持锁者 conflict；进程真死后接管")

    # L6：坏记录和"不确定"状态都不授权发布
    with open(lock, "w", encoding="utf-8") as stream:
        stream.write("{broken")
    rec_g = record_of(pool, pool.spawn_claude(), "G")
    state, _ = lockcore.acquire(lock, rec_g, deadline)
    require(state == "error", "L6 坏记录被接受：%s" % state)
    child_h = pool.spawn_claude()
    rec_h = record_of(pool, child_h, "H")
    lockcore.publish_lock(lock, rec_h)
    bin_dir = os.path.join(work, "l6bin")
    make_fake_ps(bin_dir)
    with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID=str(child_h.pid), FAKE_PS_MODE="exit1"):
        state, _ = lockcore.acquire(lock, rec_g, procs.Deadline(5))
    require(state == "error", "L6 ps 出错时仍接受了发布：%s" % state)
    events.append("L6 PASS：坏记录与 ps 出错（unknown）都不覆盖锁")

    # L7：guard 被占用时 SessionEnd 立即得到 busy，不等待
    fd = lockcore.guard_lock(lock)
    started = time.monotonic()
    require(lockcore.release(lock, rec_h) == "busy", "L7 end 等待或拿到了 guard")
    require(time.monotonic() - started < 0.2, "L7 end 超出预算")
    lockcore.release_guard(fd)
    events.append("L7 PASS：guard 被占时 SessionEnd 立即返回 busy")
    events.append(l7_hang_variants(work, pool))

    # L8：主检出与 worktree 的锁路径相同，因此互相 conflict
    main_repo = os.path.join(work, "l8main")
    make_repo(main_repo)
    worktree = os.path.join(work, "l8wt")
    git("worktree", "add", "-q", worktree, "-b", "l8", cwd=main_repo)
    status_main, path_main = lockcore.lock_path(main_repo, deadline)
    status_wt, path_wt = lockcore.lock_path(worktree, deadline)
    require(status_main == status_wt == "ok" and path_main == path_wt, "L8 主检出与 worktree 锁路径不同")
    rec_main = record_of(pool, pool.spawn_claude(), "main")
    rec_wt = record_of(pool, pool.spawn_claude(), "worktree")
    state, _ = lockcore.acquire(path_main, rec_main, deadline)
    require(state == "acquired", "L8 主检出没拿到锁")
    state, _ = lockcore.acquire(path_wt, rec_wt, deadline)
    require(state == "conflict", "L8 worktree 没有与主检出冲突：%s" % state)
    events.append("L8 PASS：主检出与 worktree 共用锁并 conflict")

    # L9：同 pid + lstart、不同会话号是同进程上一生命周期，可接管；不同的存活 pid 仍 conflict
    os.unlink(path_main)
    child_1 = pool.spawn_claude()
    old_sid = record_of(pool, child_1, "old-sid")
    new_sid = dict(old_sid, session_id="new-sid")
    lockcore.publish_lock(path_main, old_sid)
    state, held = lockcore.acquire(path_main, new_sid, deadline)
    require(state == "acquired" and held["took_over_stale"] == "old-sid", "L9 同进程新生命周期没有接管：%s" % state)
    other = record_of(pool, pool.spawn_claude(), "other-pid")
    lockcore.publish_lock(path_main, other)
    state, _ = lockcore.acquire(path_main, dict(old_sid, session_id="newer"), deadline)
    require(state == "conflict", "L9 不同的存活 pid 没有 conflict：%s" % state)
    events.append("L9 PASS：同 pid+lstart 换会话号可接管；不同存活 pid 仍 conflict")
    return events


def l7_hang_variants(work, pool):
    """L7 变体（T4）：git / ps 挂起时，锁位置探测与身份探测都在 1.5 秒内返回，且锁内容不变。"""
    repo = os.path.join(work, "l7repo")
    make_repo(repo)
    holder = record_of(pool, pool.spawn_claude(), "holder")
    status, lock = lockcore.lock_path(repo, procs.Deadline(5))
    require(status == "ok", "L7 变体：取不到锁位置")
    state, _held = lockcore.acquire(lock, holder, procs.Deadline(5))
    require(state == "acquired", "L7 变体：持锁者没拿到锁")
    with open(lock, encoding="utf-8") as stream:
        before = stream.read()
    hang_git = os.path.join(work, "l7git")
    make_fake_git(hang_git, "hang")
    hang_ps = os.path.join(work, "l7ps")
    make_fake_ps(hang_ps)
    scenarios = (
        ("git 挂起", {"PATH": path_with(hang_git)}, lambda deadline: lockcore.lock_path(repo, deadline)[0] == "error"),
        ("ps 挂起", {"PATH": path_with(hang_ps), "FAKE_PS_PID": "ANY", "FAKE_PS_MODE": "hang"},
         lambda deadline: lockcore.my_identity(deadline)[0] is None),
    )
    for label, env, probe in scenarios:
        started = time.monotonic()
        with patched_env(**env):
            ok = probe(procs.Deadline(1.0))
        elapsed = time.monotonic() - started
        with open(lock, encoding="utf-8") as stream:
            after = stream.read()
        require(ok and elapsed < 1.5, "L7 变体（%s）：应在 1.5 秒内按失败返回，实际 %r / %.2f 秒" % (label, ok, elapsed))
        require(after == before, "L7 变体（%s）：锁内容被改动" % label)
    return "L7 变体 PASS：git 挂起、ps 挂起时锁位置 / 身份探测在 1.5 秒内按失败返回，锁内容不变"


def main():
    """运行 F5。"""
    return run_cases([("F5", case_f5)])


if __name__ == "__main__":
    sys.exit(main())
