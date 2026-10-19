# 测试 F1–F4、R1、F6、F7：git 探测出错不退回本地锁、锁路径、ps 出错不判 dead、进程真死才判 dead。
# S1 起 hook_lock 已删除：原先经钩子走的断言改为直接走 lockcore（lock_path + acquire），语义与旧钩子放行/阻断一一对应。
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (git, load_old_q6, make_fake_git, make_fake_ps, make_repo, patched_env,  # noqa: E402
                     path_with, process_pool, require, run_cases, run_old_hook, temp_dir)
from cdm import lockcore, procs  # noqa: E402


def try_lock(proj, ident, seconds=5):
    """旧钩子的等价入口：lock_path + acquire。返回 ("granted"|"blocked", 细节)；锁位置探测失败、conflict、busy、error 都算 blocked。"""
    deadline = procs.Deadline(seconds)
    status, lock = lockcore.lock_path(proj, deadline)
    if status != "ok":
        return "blocked", "lock_path_error: " + lock
    state, _holder = lockcore.acquire(lock, dict(ident), deadline)
    return ("granted" if state in ("acquired", "held_by_me") else "blocked"), state


def case_f1():
    """F1：git 探测出错（退出 128 / 空输出 / 挂起）时，新版阻断，旧版两个会话都获准。"""
    details = []
    old = load_old_q6()
    with temp_dir() as work, process_pool(work) as pool:
        repo_new = os.path.join(work, "repo_new")
        repo_old = os.path.join(work, "repo_old")
        make_repo(repo_new)
        make_repo(repo_old)
        for repo in (repo_new, repo_old):
            os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        proc_a = pool.spawn_claude()
        proc_b = pool.spawn_claude()
        ident_a = pool.identity(proc_a, "session-A")
        ident_b = pool.identity(proc_b, "session-B")

        # 新版：A 用真实 git 取锁
        verdict, detail = try_lock(repo_new, ident_a)
        require(verdict == "granted", "新版 A 没拿到锁：%r %r" % (verdict, detail))
        shared_lock = os.path.join(repo_new, ".git", "claude-main-controller.lock")
        require(os.path.exists(shared_lock), "新版锁没放在 git 公共目录")
        # 旧版：A 用真实 git 取锁
        code, out = run_old_hook(old, "prompt", "session-A", repo_old, proc_a.pid)
        require(code == 0, "旧版 A 没拿到锁")
        details.append("旧版 事件顺序：A 用真实 git 取得共享锁 → 出故障后 B 请求锁")

        for mode in ("exit128", "empty", "hang"):
            bin_dir = os.path.join(work, "fakebin_" + mode)
            make_fake_git(bin_dir, mode)
            with patched_env(PATH=path_with(bin_dir)):
                new_result = try_lock(repo_new, ident_b, seconds=2.0)
                old_code, old_out = run_old_hook(old, "prompt", "session-B", repo_old, proc_b.pid)
            require(new_result[0] == "blocked", "新版 %s：B 没被阻断：%r" % (mode, new_result))
            require("lock_path_error" in new_result[1], "新版 %s：阻断原因应是锁位置探测失败：%r" % (mode, new_result))
            if mode == "hang":
                # 旧版 git 挂起会耗尽整个时间预算，随后取自身 lstart 也超时 → 身份无效而被阻断，属于碰巧安全，不构成区分力
                require(old_code == 2, "旧版 hang：预期因预算耗尽而阻断，实际退出码 %r" % old_code)
                details.append("旧版 hang：git 超时耗尽预算 → 取身份失败 → 碰巧阻断（此模式不区分新旧）；新版 B 被阻断")
                continue
            require(old_code == 0 and "additionalContext" in old_out, "旧版 %s：没有复现 B 获准（退出码 %r）" % (mode, old_code))
            details.append("旧版 %s：B 也获准（退出码 0，输出获准提示）；新版 B 被阻断" % mode)
        require(not os.path.exists(os.path.join(repo_new, ".claude", "main-controller.lock")), "新版偷偷建了本地锁")
        require(os.path.exists(os.path.join(repo_old, ".claude", "main-controller.lock")), "旧版没有落到本地锁（复现失败）")
        details.append("旧版 结果：A 持有 .git 里的共享锁，B 落到 <项目>/.claude/main-controller.lock，两个会话同时获准")
    return details


def case_f2():
    """F2：非 git 目录用本地锁；主检出与 worktree 共用同一把锁。"""
    with temp_dir() as work:
        plain = os.path.join(work, "plain")
        os.makedirs(plain)
        status, path = lockcore.lock_path(plain, procs.Deadline(5))
        require((status, path) == ("ok", os.path.join(plain, ".claude", "main-controller.lock")), "非 git 目录路径不对：%r" % (path,))
        main_repo = os.path.join(work, "main")
        make_repo(main_repo)
        worktree = os.path.join(work, "wt")
        git("worktree", "add", "-q", worktree, "-b", "wt-branch", cwd=main_repo)
        require(os.path.isfile(os.path.join(worktree, ".git")), "worktree 的 .git 应是文件")
        expected = os.path.join(main_repo, ".git", "claude-main-controller.lock")
        sub = os.path.join(main_repo, "subdir")
        os.makedirs(sub)
        for proj in (main_repo, worktree, sub):
            status, path = lockcore.lock_path(proj, procs.Deadline(5))
            require((status, path) == ("ok", expected), "%s 的锁路径不对：%r" % (proj, path))
        with patched_env(GIT_DIR=os.path.join(work, "nonexistent-gitdir")):
            status, reason = lockcore.lock_path(plain, procs.Deadline(5))
        require(status == "error", "GIT_DIR 存在但无效时应报错，实际 %r" % ((status, reason),))
    return ["非 git 走本地锁；主检出/子目录/worktree 共用 .git 公共目录里的锁；GIT_DIR 无效时报错"]


def case_f3():
    """F3：持锁者是真活进程，ps 出错（退出 1 / 空输出 / 挂起）时新版判 unknown 并阻断，旧版误判 dead 并接管。"""
    details = []
    old = load_old_q6()
    with temp_dir() as work, process_pool(work) as pool:
        holder = pool.spawn_claude()
        challenger = pool.spawn_claude()
        holder_id = pool.identity(holder, "holder")
        challenger_id = pool.identity(challenger, "challenger")
        require(procs.proc_state(holder_id, procs.Deadline(5)) == "live", "正常 ps 下持锁者应为 live")
        bin_dir = os.path.join(work, "fakebin")
        make_fake_ps(bin_dir)
        for mode in ("exit1", "empty", "hang"):
            proj_new = os.path.join(work, "new_" + mode)
            proj_old = os.path.join(work, "old_" + mode)
            for proj in (proj_new, proj_old):
                os.makedirs(os.path.join(proj, ".claude"))
            verdict, detail = try_lock(proj_new, holder_id)
            require(verdict == "granted", "新版持锁者没拿到锁：%r" % (detail,))
            code, out = run_old_hook(old, "prompt", "holder", proj_old, holder.pid)
            require(code == 0, "旧版持锁者没拿到锁")
            with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID=str(holder.pid), FAKE_PS_MODE=mode):
                state = procs.proc_state(holder_id, procs.Deadline(1.0))
                new_verdict = try_lock(proj_new, challenger_id, seconds=2.0)
                old_code, old_out = run_old_hook(old, "prompt", "challenger", proj_old, challenger.pid)
                old_state = old.proc_state(holder_id, old.Deadline(1.0))
            require(state == "unknown", "新版 %s：proc_state 应为 unknown，实际 %s" % (mode, state))
            require(new_verdict[0] == "blocked", "新版 %s：挑战者没被阻断：%r" % (mode, new_verdict))
            lock_file = os.path.join(proj_new, ".claude", "main-controller.lock")
            with open(lock_file, encoding="utf-8") as stream:
                require(json.load(stream)["session_id"] == "holder", "新版 %s：锁被改动" % mode)
            with open(os.path.join(proj_old, ".claude", "main-controller.lock"), encoding="utf-8") as stream:
                after_old = json.load(stream)
            if mode == "hang":
                # 旧版对"超时"本来就判 unknown（只有"ps 退出非零/空输出"才被误判 dead），所以挂起不构成区分力
                require(old_state == "unknown" and old_code == 2 and after_old["session_id"] == "holder",
                        "旧版 hang：预期 unknown 且阻断，实际 %s / %r" % (old_state, old_code))
                details.append("旧版 hang：ps 超时 → 旧版也判 unknown 并阻断（与新版一致，此模式不区分新旧）")
                continue
            require(old_state == "dead", "旧版 %s：proc_state 应复现 dead，实际 %s" % (mode, old_state))
            require(old_code == 0, "旧版 %s：挑战者没有复现接管（退出码 %r）" % (mode, old_code))
            require(after_old["session_id"] == "challenger" and after_old.get("took_over_stale") == "holder", "旧版 %s：锁没有被接管" % mode)
            details.append("旧版 %s：holder 仍存活，但 ps 出错 → 判 dead → challenger 接管（took_over_stale=holder）；新版判 unknown，challenger 被阻断" % mode)
    return details


def case_f4():
    """F4：持锁 pid 真的不存在时，两版都判 dead、允许接管（ps 正常与 ps 出错两种）。"""
    old = load_old_q6()
    with temp_dir() as work, process_pool(work) as pool:
        holder = pool.spawn_claude()
        challenger = pool.spawn_claude()
        holder_id = pool.identity(holder, "holder")
        challenger_id = pool.identity(challenger, "challenger")
        bin_dir = os.path.join(work, "fakebin")
        make_fake_ps(bin_dir)
        proj_new = os.path.join(work, "new")
        os.makedirs(os.path.join(proj_new, ".claude"))
        verdict, _detail = try_lock(proj_new, holder_id)
        require(verdict == "granted", "持锁者没拿到锁")
        pool.kill(holder)
        require(procs.pid_exists(holder.pid) is False, "持锁进程应已不存在")
        for label, env in (("ps 正常", {}), ("ps 出错", {"PATH": path_with(bin_dir), "FAKE_PS_PID": str(holder.pid), "FAKE_PS_MODE": "exit1"})):
            with patched_env(**env):
                new_state = procs.proc_state(holder_id, procs.Deadline(2))
                old_state = old.proc_state(holder_id, old.Deadline(2))
            require(new_state == "dead", "新版（%s）应判 dead，实际 %s" % (label, new_state))
            require(old_state == "dead", "旧版（%s）应判 dead，实际 %s" % (label, old_state))
        with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID=str(holder.pid), FAKE_PS_MODE="exit1"):
            verdict, detail = try_lock(proj_new, challenger_id)
        require(verdict == "granted", "持锁进程已死时挑战者应能接管：%r %r" % (verdict, detail))
        with open(os.path.join(proj_new, ".claude", "main-controller.lock"), encoding="utf-8") as stream:
            require(json.load(stream).get("took_over_stale") == "holder", "锁记录里没有接管标记")
    return ["持锁 pid 已不存在：新旧两版在 ps 正常和 ps 出错时都判 dead，新版挑战者成功接管"]


def write_lock(path, text):
    """直接写锁文件内容。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)


def case_r1():
    """R1：用真实 lockcore.inspect 读真实锁文件：缺失/坏 JSON/溢出 pid/深层嵌套/存活/已死/git 出错。"""
    with temp_dir() as work, process_pool(work) as pool:
        proj = os.path.join(work, "plain")
        os.makedirs(proj)
        lock = os.path.join(proj, ".claude", "main-controller.lock")
        info = lockcore.inspect(proj)
        require(info["lock_status"] == "ok" and info["record_status"] == "missing" and info["holder_state"] == "none", "缺失应为 none：%r" % info)
        write_lock(lock, "{broken")
        info = lockcore.inspect(proj)
        require(info["record_status"] == "error" and info["holder_state"] == "unknown", "坏 JSON 应为 unknown：%r" % info)
        child = pool.spawn_claude()
        ident = pool.identity(child, "s1")
        write_lock(lock, json.dumps(dict(ident, pid=2 ** 31)))
        info = lockcore.inspect(proj)
        require(info["holder_state"] == "unknown", "pid 溢出应为 unknown：%r" % info)
        write_lock(lock, "[" * 100000 + "]" * 100000)
        info = lockcore.inspect(proj)
        require(info["holder_state"] == "unknown", "深层嵌套应为 unknown：%r" % info)
        write_lock(lock, json.dumps(ident))
        info = lockcore.inspect(proj)
        require(info["holder_state"] == "live" and info["holder"]["pid"] == child.pid, "存活进程应为 live：%r" % info)
        pool.kill(child)
        info = lockcore.inspect(proj)
        require(info["holder_state"] == "dead", "已结束进程应为 dead：%r" % info)
        before = os.stat(lock).st_mtime_ns
        lockcore.inspect(proj)
        require(os.stat(lock).st_mtime_ns == before and not os.path.exists(lock + ".guard"), "inspect 必须只读（不建 guard、不改锁）")
        repo = os.path.join(work, "repo")
        make_repo(repo)
        make_fake_git(os.path.join(work, "fakegit"), "exit128")
        with patched_env(PATH=path_with(os.path.join(work, "fakegit"))):
            info = lockcore.inspect(repo)
        require(info["lock_status"] == "error" and info["holder_state"] == "unknown", "git 出错应 lock_status=error：%r" % info)
    return ["真实 inspect：缺失 none / 坏 JSON、pid 溢出、深层嵌套 unknown 且不抛异常 / 存活 live / 已死 dead / git 出错 error；只读"]


def case_f6():
    """F6：持锁身份在 TZ=America/New_York 下取得，挑战者在 TZ=Asia/Tokyo 下判活 → live（真实进程、真实 ps）。"""
    with temp_dir() as work, process_pool(work) as pool:
        child = pool.spawn_claude()
        challenger = pool.spawn_claude()
        with patched_env(TZ="America/New_York"):
            holder = pool.identity(child, "holder")
        with patched_env(TZ="Asia/Tokyo"):
            state = procs.proc_state(holder, procs.Deadline(5))
            challenger_id = pool.identity(challenger, "challenger")
            proj = os.path.join(work, "proj")
            os.makedirs(proj)
            lock = os.path.join(proj, ".claude", "main-controller.lock")
            os.makedirs(os.path.dirname(lock))
            lockcore.publish_lock(lock, holder)
            status, _holder = lockcore.acquire(lock, challenger_id, procs.Deadline(5))
        require(state == "live", "不同时区下存活持锁者应判 live，实际 %s" % state)
        require(status == "conflict", "不同时区的挑战者应 conflict，实际 %s" % status)
    return ["TZ=America/New_York 取得的身份在 TZ=Asia/Tokyo 下仍判 live，挑战者 conflict"]


def case_f7():
    """F7：异常输入不能变成放行：锁记录 pid 溢出、深层嵌套、pid 类型错 → 挑战者拿不到锁。
    （原 F7 还含钩子的 cwd 类型错误 / 输入坏 → prompt 阻断；hook_lock 已删，新钩子按规格:79 一律放行，见 H4。）"""
    with temp_dir() as work, process_pool(work) as pool:
        proj = os.path.join(work, "proj")
        os.makedirs(proj)
        lock = os.path.join(proj, ".claude", "main-controller.lock")
        challenger = pool.identity(pool.spawn_claude(), "B")
        holder = pool.identity(pool.spawn_claude(), "A")
        for label, text in (("pid=2**31", json.dumps(dict(holder, pid=2 ** 31))),
                            ("深层嵌套", "[" * 100000 + "]" * 100000),
                            ("pid 为字符串", json.dumps(dict(holder, pid="1234")))):
            write_lock(lock, text)
            verdict, detail = try_lock(proj, challenger)
            require(verdict == "blocked" and detail == "error", "锁记录 %s 时挑战者应得 error 而被阻断：%r %r" % (label, verdict, detail))
    return ["锁记录 pid 溢出、深层嵌套、pid 类型错 → 挑战者 acquire 返回 error（不授权）"]


def main():
    """运行 F1–F4、R1、F6、F7。"""
    return run_cases([("F1", case_f1), ("F2", case_f2), ("F3", case_f3), ("F4", case_f4), ("R1", case_r1),
                      ("F6", case_f6), ("F7", case_f7)])


if __name__ == "__main__":
    sys.exit(main())
