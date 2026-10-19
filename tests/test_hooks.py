# 测试 H1–H13：handoffd S1 钩子（hook_relay + baton + policy + sessions）。
# 全部进程内调用、假 stdin、注入身份；判活用真实的假 claude 进程（ProcessPool），响铃用 Rings 记录器，不出声。
import json
import os
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (MVP_ROOT, make_fake_ps, make_repo, patched_env, path_with, process_pool, require,  # noqa: E402
                     run_cases, temp_dir)
from relay_helpers import (Rings, call_relay, handoff_with_b, holder_of, ident_fn, is_blocked,  # noqa: E402
                           is_denied, read_baton, sdir_of, set_auto, tool_call, tree_snapshot, write_baton)
from cdm import baton, ctx, hook_relay, sessions, util  # noqa: E402

READ_TOOLS = (("Read", {"file_path": "/x"}), ("Grep", {"pattern": "a"}), ("Glob", {"pattern": "*"}))
WRITE_TOOLS = (("Write", {"file_path": "/x", "content": "c"}), ("Edit", {"file_path": "/x", "old_string": "a", "new_string": "b"}),
               ("NotebookEdit", {"notebook_path": "/x.ipynb"}), ("MultiEdit", {"file_path": "/x", "edits": []}),
               ("Bash", {"command": "rm -rf x"}), ("Bash", {"command": "ls"}), ("Bash", {"command": "handoffd off"}),
               ("Bash", {"command": "handoffd status && rm x"}), ("Bash", {"command": "handoffd status > out.txt"}),
               # R3：旁观会话没有 Bash，连 handoffd status 也没有（状态由启动提示与拒绝文案给出）
               ("Bash", {"command": "handoffd status"}), ("Bash", {"command": "/x/bin/handoffd status"}),
               ("Bash", {"command": "handoffd --config /c/handoffd.json status"}),
               ("SendMessage", {"to": "a"}), ("mcp__srv__write", {}), ("Agent", {"prompt": "p"}))


def make_project(work, name="proj"):
    project = os.path.join(work, name)
    os.makedirs(project)
    return project


def additional(output):
    return (output or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def write_ctx(sdir, sid, pct, size=1000):
    path = os.path.join(sdir, "ctx", sid + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.atomic_write_json(path, {"session_id": sid, "context_window": {"context_window_size": size, "used_percentage": pct}})


def case_h1():
    """H1：manual 每个钩子事件立即放行、零写入（含状态栏 tap 路径、高上下文下不提醒）；同样事件在 auto 下才有落盘（对照）。"""
    details = []
    events = (
        ("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "hi"}),
        ("PreToolUse", tool_call("Write", file_path="/x/a", content="c")), ("PreToolUse", tool_call("Bash", command="rm -rf x")),
        ("PreToolUse", tool_call("Agent", prompt="p")), ("PostToolUse", tool_call("Bash", command="ls")),
        ("PostToolUseFailure", tool_call("Bash", command="ls")),
        ("Stop", {"background_tasks": [{"type": "shell"}], "session_crons": 0}),
        ("StopFailure", {"error": "authentication_failed"}), ("StopFailure", {"error": "rate_limit"}),
        ("SessionEnd", {"reason": "other"}), ("Notification", {"notification_type": "idle_prompt"}),
        ("Notification", {"notification_type": "quota_auto_resume_fired"}), ("SubagentStop", {}),
    )
    with patched_env(CTX_WINDOW=None), temp_dir() as work, process_pool(work) as pool:
        me = pool.identity(pool.spawn_claude(), "s1")
        snapshot_json = json.dumps({"session_id": "s1", "context_window": {"context_window_size": 1000, "used_percentage": 80}})
        for label in ("没有任何状态目录", "mode=manual 且有 80% 快照"):
            project = make_project(work, "p-" + str(len(details)))
            sdir = sdir_of(project)
            if label != "没有任何状态目录":
                sessions.write_mode(sdir, "manual", 1)
                write_ctx(sdir, "s1", 80)
            before = tree_snapshot(project)
            rings = Rings()
            for event, extra in events:
                code, out = call_relay(event, project, "s1", ident_fn(me), rings=rings, extra=extra)
                require(code == 0 and out is None, "manual 下 %s 应立即放行且无输出：%r %r" % (event, code, out))
            require(rings.count() == 0, "manual 下这些事件都不应响铃：%r" % rings.calls)
            require(ctx.tap(project, snapshot_json) is True, "tap 应正常返回")
            after = tree_snapshot(project)
            require(after == before, "manual 下有写入：%r" % sorted(set(after.items()) ^ set(before.items())))
            details.append("%s：%d 个事件 + tap 全部放行，目录树逐字节不变" % (label, len(events)))
        # git 项目：<S> 在 git 公共目录下，manual 也不能建出来
        repo = os.path.join(work, "repo")
        make_repo(repo)
        for event, extra in events:
            code, out = call_relay(event, repo, "s1", ident_fn(me), extra=extra)
            require(code == 0 and out is None, "git 项目 manual 下 %s 应放行" % event)
        require(ctx.tap(repo, snapshot_json) is True, "git 项目 tap 应正常返回")
        require(not os.path.exists(os.path.join(repo, ".git", "handoffd")), "git 项目 manual 下不应建出 <S>")
        details.append("git 项目（<S>=.git/handoffd）：manual 下 <S> 不存在")
        # 对照：同样的事件在 auto 下确有落盘，证明上面的"无写入"不是测试失灵
        project = make_project(work, "p-auto")
        sdir = set_auto(project)
        before = tree_snapshot(project)
        for event, extra in events:
            call_relay(event, project, "s1", ident_fn(me), rings=Rings(), extra=extra)
        require(ctx.tap(project, snapshot_json) is True and tree_snapshot(project) != before
                and os.path.exists(os.path.join(sdir, "events.jsonl")) and os.path.exists(os.path.join(sdir, "ctx", "s1.json")),
                "对照失败：auto 下应有 events.jsonl 与 ctx 快照落盘")
        details.append("对照：auto 下同样事件写出 events.jsonl 与 ctx/s1.json")
    return details


def case_h2():
    """H2：manual 的等输入响铃——四类信号响铃、同次等待只响一次、非等待类事件清标记后可再响；唯一写入是 run/waiting.<sid>。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        me = ident_fn(pool.identity(pool.spawn_claude(), "w1"))
        rings = Rings()
        before = tree_snapshot(project)

        def ev(event, sid="w1", **extra):
            code, out = call_relay(event, project, sid, me, rings=rings, extra=extra)
            require(code == 0 and out is None, "%s 应放行：%r" % (event, out))

        def expect(total, why):
            require(rings.count("waiting") == total and rings.count() == total, "%s：应共 %d 次，实际 %r" % (why, total, rings.calls))

        ev("PreToolUse", **tool_call("Bash", command="ls"))
        expect(0, "普通工具不响")
        ev("PermissionRequest", tool_name="Bash")
        expect(1, "权限框马上响")
        ev("Notification", notification_type="permission_prompt")
        expect(1, "同次等待 6 秒后的 Notification 不再响")
        new_files = sorted(set(tree_snapshot(project)) - set(before))
        require(new_files == [os.path.join(".claude"), os.path.join(".claude", "handoffd"), os.path.join(".claude", "handoffd", "run"),
                              os.path.join(".claude", "handoffd", "run", "waiting.w1")], "manual 只应新增 run/waiting.w1：%r" % new_files)
        ev("Notification", notification_type="idle_prompt")
        ev("Notification", notification_type="permission_prompt")
        expect(1, "Notification 不是「非等待类」事件，不清标记")
        call = rings.calls[0]
        require(call["kind"] == "waiting" and "w1" in call["what"] and call["ask"] == "1) 去该窗口处理" and call["recommend"] == 1
                and call["why"] and call["if_no_reply"], "四段文案不全：%r" % call)
        ev("PreToolUse", **tool_call("Read", file_path="/x"))
        ev("PermissionRequest", tool_name="Write")
        expect(2, "PreToolUse 清标记后再请求权限要再响")
        ev("PostToolUseFailure", **tool_call("Write", file_path="/x"))
        ev("Elicitation", mcp_server_name="srv")
        expect(3, "PostToolUseFailure 清标记后 MCP 索要输入要响")
        ev("Stop")
        ev("Notification", notification_type="elicitation_url_dialog")
        expect(4, "Stop 清标记后 elicitation_url_dialog 要响")
        ev("UserPromptSubmit")
        ev("PreToolUse", **tool_call("AskUserQuestion", questions=[]))
        expect(5, "AskUserQuestion 马上响")
        ev("PreToolUse", **tool_call("AskUserQuestion", questions=[]))
        ev("Notification", notification_type="elicitation_dialog")
        expect(5, "AskUserQuestion 自己不清标记")
        ev("PostToolUse", **tool_call("AskUserQuestion"))
        ev("Notification", notification_type="quota_auto_resume_stale")
        expect(6, "PostToolUse 清标记；额度重置后等你按 Enter 要响（设计 §9）")
        ev("StopFailure", error="authentication_failed")
        ev("PermissionRequest", tool_name="Bash")
        expect(7, "StopFailure 清标记")
        ev("PermissionRequest", sid="w2", tool_name="Bash")
        expect(8, "另一会话的等待独立")
        files = sorted(path for path, digest in tree_snapshot(project).items() if digest != "<dir>")
        require(all(os.path.basename(path).startswith("waiting.") for path in files), "manual 下除标记外不应有别的文件：%r" % files)
    return ["权限框 / elicitation / AskUserQuestion / stale 各响一次；去重 + 6 种非等待类事件清标记；manual 只写 run/waiting.<sid>"]


OWNER_ONLY_DENY = (
    "handoffd on", "handoffd adopt", "/x/bin/handoffd on", "cd /p && handoffd on", "env A=1 handoffd adopt",
    "bash -c 'handoffd on'", "python3 /x/bin/handoffd on", "handoffd --config /c.json on", "handoffd status; handoffd on",
    "echo $(handoffd on)", "sudo handoffd adopt",
    # F6 原文兜底：宁可多拦文字，也不让命令替换 / eval / 直接调 handoffd_cli.py 绕过
    'eval "handoffd on"', "python3 /x/cdm/handoffd_cli.py on", 'echo "handoffd on"', "grep 'handoffd on' README.md", "git commit -m 'docs: handoffd on'",
)
OWNER_ONLY_ALLOW = (
    "handoffd status", "handoffd off", "handoffd onward", "ls", "cat handoffd_adopt.md",
    'handoffd notify --why "tests on main" --what x',
)


def case_h3():
    """H3：Bash 里命令位置上的 `handoffd on|adopt` 在 manual 与 auto 下都被拦（设计 §9 §7-1）；同名文字、别的子命令不误拦。"""
    with temp_dir() as work, process_pool(work) as pool:
        for mode in ("manual", "auto"):
            project = make_project(work, "p-" + mode)
            rec = pool.identity(pool.spawn_claude(), "h3-" + mode)
            if mode == "auto":
                sdir = set_auto(project)
                sessions.write_heartbeat(sdir, util.now_ms())
                code, _out = call_relay("SessionStart", project, rec["session_id"], ident_fn(rec), extra={"source": "startup"})
                require(read_baton(sdir)["holder"]["sid"] == rec["session_id"], "auto 下应先认领成 holder")
            for command in OWNER_ONLY_DENY:
                code, out = call_relay("PreToolUse", project, rec["session_id"], ident_fn(rec), extra=tool_call("Bash", command=command))
                require(code == 0 and is_denied(out) and "Owner" in out["hookSpecificOutput"]["permissionDecisionReason"],
                        "%s 下应拦 %r：%r" % (mode, command, out))
            for command in OWNER_ONLY_ALLOW:
                code, out = call_relay("PreToolUse", project, rec["session_id"], ident_fn(rec), extra=tool_call("Bash", command=command))
                require(code == 0 and out is None, "%s 下不应拦 %r：%r" % (mode, command, out))
            code, out = call_relay("PreToolUse", project, rec["session_id"], ident_fn(rec),
                                   extra=tool_call("Write", file_path="/x", content="handoffd on"))
            require(out is None, "文件内容里的文字不应拦")
    return ["manual / auto：%d 种写法被拦（含 bash -c、env、python3、包装词、命令替换、eval、handoffd_cli.py）；%d 种不误拦" % (len(OWNER_ONLY_DENY), len(OWNER_ONLY_ALLOW))]


def case_h4():
    """H4：钩子任何异常一律放行（规格:79）：输入坏 / 内部抛错 / 登记册损坏 / 状态目录不可写 / 响铃失败；并跑一次真实子进程。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        me = ident_fn(pool.identity(pool.spawn_claude(), "e1"))
        for label, raw in (("非 JSON", "not json"), ("数组", "[]"), ("null", "null"), ("空输入", ""), ("深层嵌套", "[" * 100000)):
            code, out = call_relay("PreToolUse", project, "e1", me, raw=raw)
            require(code == 0 and out is None, "输入 %s 应放行：%r %r" % (label, code, out))
        code, out = call_relay("UserPromptSubmit", project, "e1", me, extra={"cwd": 7}, env={"CLAUDE_PROJECT_DIR": None})
        require(code == 0 and out is None, "cwd 类型错应放行：%r" % (out,))
        details.append("坏输入（非 JSON / 数组 / null / 空 / 深层嵌套 / cwd=7）全部放行")
        # 内部抛错：各事件下 handle 抛任意异常都放行
        original = hook_relay.handle
        try:
            for exc in (RuntimeError("boom"), TypeError("x"), OSError("disk"), KeyError("k"), RecursionError()):
                def boom(*args, **kwargs):
                    raise exc
                hook_relay.handle = boom
                for event in ("UserPromptSubmit", "PreToolUse", "SessionStart", "PermissionRequest"):
                    code, out = call_relay(event, project, "e1", me, extra=tool_call("Write", file_path="/x"))
                    require(code == 0 and out is None, "%s 内部抛 %r 应放行：%r" % (event, exc, out))
        finally:
            hook_relay.handle = original
        details.append("handle 内部抛 5 类异常 × 4 个事件：退出码 0、无阻断输出")
        # auto：登记册损坏
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        with open(baton.baton_path(sdir), "w", encoding="utf-8") as stream:
            stream.write("{broken")
        for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {}),
                             ("PreToolUse", tool_call("Write", file_path="/x", content="c")),
                             ("PostToolUse", tool_call("Bash", command="ls"))):
            code, out = call_relay(event, project, "e2", me, extra=extra)
            require(code == 0 and not is_blocked(out) and not is_denied(out), "登记册损坏时 %s 应放行：%r" % (event, out))
        require(open(baton.baton_path(sdir), encoding="utf-8").read() == "{broken", "登记册损坏时不得覆盖（不认领）")
        details.append("auto + 登记册损坏：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse 放行，且不覆盖、不认领")
        # auto：状态目录不可写
        os.unlink(baton.baton_path(sdir))
        os.chmod(sdir, 0o500)
        try:
            code, out = call_relay("UserPromptSubmit", project, "e3", me)
            require(code == 0 and not is_blocked(out), "状态目录不可写时应放行：%r" % (out,))
            code, out = call_relay("PreToolUse", project, "e3", me, extra=tool_call("Write", file_path="/x", content="c"))
            require(code == 0 and not is_denied(out), "状态目录不可写时 Write 应放行：%r" % (out,))
        finally:
            os.chmod(sdir, 0o700)
        details.append("auto + 状态目录只读：放行")
        # 响铃失败：放行，且标记回滚（下一次等待仍能响）
        manual_project = make_project(work, "pm")

        def failing_ring(*args):
            raise OSError("no sound")

        code, out = call_relay("PermissionRequest", manual_project, "e4", me, rings=failing_ring, extra={"tool_name": "Bash"})
        require(code == 0 and out is None, "响铃失败应放行")
        rings = Rings()
        call_relay("PermissionRequest", manual_project, "e4", me, rings=rings, extra={"tool_name": "Bash"})
        require(rings.count() == 1, "响铃失败不能留着去重标记把下一次等待吞掉")
        details.append("响铃函数抛错：放行，去重标记回滚，下一次等待照常响")
        # 真实子进程
        hook = os.path.join(MVP_ROOT, "cdm", "hook_relay.py")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project, PYTHONDONTWRITEBYTECODE="1")
        for stdin_text in ("garbage", json.dumps({"session_id": "x", "tool_name": "Write", "tool_input": {"file_path": "/x"}})):
            result = subprocess.run([sys.executable, hook, "PreToolUse"], input=stdin_text, capture_output=True, text=True, env=env, timeout=30)
            require(result.returncode == 0 and "permissionDecision" not in result.stdout and "block" not in result.stdout,
                    "真实子进程应退出 0 且不阻断：%r %r" % (result.returncode, result.stdout))
        details.append("真实子进程（坏输入 / 无法取得本进程身份）：退出码 0、无阻断")
    return details


def case_h5():
    """H5：auto 首会话认领 holder；第二会话旁观，写工具被拒、`handoffd status` 放行；取不到本进程身份的会话放行（规格:79、:82）。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        rings = Rings()
        code, out = call_relay("SessionStart", project, "sA", ident_fn(a), rings=rings, extra={"source": "startup"})
        text = additional(out)
        require("auto 模式" in text and "README" in text and "主控" in text, "SessionStart 应注入 auto 模式提示：%r" % text)
        holder = read_baton(sdir)["holder"]
        require(holder["sid"] == "sA" and holder["pid"] == a["pid"] and holder["lstart"] == a["lstart"], "首会话应认领：%r" % holder)
        code, out = call_relay("SessionStart", project, "sB", ident_fn(b), rings=rings, extra={"source": "startup"})
        require("旁观" in additional(out), "第二会话应被告知旁观：%r" % out)
        code, out = call_relay("UserPromptSubmit", project, "sB", ident_fn(b), rings=rings)
        require(code == 0 and out is None, "旁观会话的提示词不被阻断")
        require(read_baton(sdir)["holder"] == holder, "第二会话不得改 holder")
        for tool, tool_input in WRITE_TOOLS:
            code, out = call_relay("PreToolUse", project, "sB", ident_fn(b), rings=rings, extra=tool_call(tool, **tool_input))
            require(is_denied(out), "旁观会话的 %s %r 应被拒：%r" % (tool, tool_input, out))
            code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call(tool, **tool_input))
            require(not is_denied(out), "holder 的 %s %r 应放行：%r" % (tool, tool_input, out))
        for tool, tool_input in READ_TOOLS:
            for sid, rec in (("sB", b), ("sA", a)):
                code, out = call_relay("PreToolUse", project, sid, ident_fn(rec), rings=rings, extra=tool_call(tool, **tool_input))
                require(out is None, "%s 的 %s %r 应放行：%r" % (sid, tool, tool_input, out))
        require(read_baton(sdir)["holder"] == holder, "整个过程 holder 不变")
        # holder 用会话号就能认（取不到进程身份时也放行）；陌生会话取不到身份 ⇒ 按规格:79 放行
        code, out = call_relay("PreToolUse", project, "sA", None, extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None, "holder 取不到进程身份时按会话号识别")
        code, out = call_relay("PreToolUse", project, "sC", None, extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None, "取不到本进程身份的陌生会话按规格:79 放行（S1 自定）")
        code, out = call_relay("SessionStart", project, "sC", None, extra={"source": "startup"})
        require(out is None and read_baton(sdir)["holder"] == holder, "取不到身份的会话不认领、无输出")
        events = open(os.path.join(sdir, "events.jsonl"), encoding="utf-8").read()
        require('"role": "spectator"' in events and "deny:Write" in events, "events.jsonl 应记旁观与拒绝")
    return ["首会话认领 holder；第二会话旁观：%d 个写 / 越权工具（含 Bash 与 handoffd status）全拒、%d 个只读放行；holder 全放行；holder 不变" % (len(WRITE_TOOLS), len(READ_TOOLS))]


def case_h6():
    """H6：holder 死 ∧ handoff==null ⇒ 新会话认领；holder 死 ∧ handoff≠null ∧ 非 B ⇒ UserPromptSubmit block 并提示 handoffd off；B 上线；判活不确定不认领。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        # (1) handoff == null：认领
        project = make_project(work, "p1")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "sA")
        c = pool.identity(pool.spawn_claude(), "sC")
        write_baton(sdir, holder=holder_of(a))
        code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
        require(out is None and read_baton(sdir)["holder"]["sid"] == "sA", "holder 还活着时新会话不得认领")
        pool.kill(a_proc)
        code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
        state = read_baton(sdir)
        require(out is None and state["holder"]["sid"] == "sC" and state["holder"]["pid"] == c["pid"], "holder 死且无交接：新会话应认领：%r" % state)
        require(state["retire"] and state["retire"][0]["sid"] == "sA", "旧 holder 应进 retire：%r" % state["retire"])
        code, out = call_relay("PreToolUse", project, "sC", ident_fn(c), extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None, "认领后可写")
        details.append("holder 死 ∧ handoff==null：新会话认领，旧 holder 进 retire")
        # (2) handoff≠null：block
        project = make_project(work, "p2")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "sA")
        c = pool.identity(pool.spawn_claude(), "sC")
        b = pool.identity(pool.spawn_claude(), "b-uuid-1")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-1"))
        pool.kill(a_proc)
        before = open(baton.baton_path(sdir), "rb").read()
        code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
        require(is_blocked(out) and "handoffd off" in out["reason"] and "放弃交接" in out["reason"], "应 block 并提示先放弃交接：%r" % out)
        code, out = call_relay("SessionStart", project, "sC", ident_fn(c), extra={"source": "startup"})
        require("handoffd off" in additional(out), "SessionStart 也应提示")
        code, out = call_relay("PreToolUse", project, "sC", ident_fn(c), extra=tool_call("Write", file_path="/x", content="c"))
        require(is_denied(out), "blocked 会话不得写")
        require(open(baton.baton_path(sdir), "rb").read() == before, "blocked 不得认领、不得改登记册")
        details.append("holder 死 ∧ handoff≠null ∧ 非 B：UserPromptSubmit block（提示 handoffd off）、不认领、不能写")
        # (3) B 上线
        code, out = call_relay("UserPromptSubmit", project, "b-uuid-1", ident_fn(b))
        require(out is None, "B 的提示词不被阻断：%r" % out)
        state = read_baton(sdir)
        require(state["handoff"]["b"]["pid"] == b["pid"] and state["handoff"]["b"]["lstart"] == b["lstart"]
                and state["handoff"]["b"]["online_ms"] and state["holder"]["sid"] == "sA", "B 应登记上线且不动 holder：%r" % state)
        code, out = call_relay("PreToolUse", project, "b-uuid-1", ident_fn(b), extra=tool_call("Write", file_path="/x", content="c"))
        require(is_denied(out), "交接期间 B 不能写")
        code, out = call_relay("PreToolUse", project, "b-uuid-1", ident_fn(b), extra=tool_call("Read", file_path="/x"))
        require(out is None, "B 可读")
        details.append("B（会话号=handoff.b.uuid）上线登记；holder 不变；B 只读")
        # (4) holder 活 + handoff≠null：非 B 是旁观，不 block
        project = make_project(work, "p4")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        c = pool.identity(pool.spawn_claude(), "sC")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-9"))
        code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
        require(out is None, "holder 活着时非 B 只是旁观，不 block：%r" % out)
        code, out = call_relay("PreToolUse", project, "sC", ident_fn(c), extra=tool_call("Write", file_path="/x", content="c"))
        require(is_denied(out), "旁观不能写")
        details.append("holder 活 ∧ handoff≠null：非 B 旁观，不 block")
        # (5) 判活不确定（ps 出错）⇒ 不认领、不授权写
        project = make_project(work, "p5")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "sA")
        c = pool.identity(pool.spawn_claude(), "sC")
        write_baton(sdir, holder=holder_of(a))
        bin_dir = os.path.join(work, "fakeps")
        make_fake_ps(bin_dir)
        with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID=str(a["pid"]), FAKE_PS_MODE="exit1"):
            code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
            code2, out2 = call_relay("PreToolUse", project, "sC", ident_fn(c), extra=tool_call("Write", file_path="/x", content="c"))
        require(read_baton(sdir)["holder"]["sid"] == "sA" and is_denied(out2), "判活不确定：不认领、不授权写：%r %r" % (read_baton(sdir)["holder"], out2))
        details.append("判活不确定（ps 出错）：不认领、写被拒")
    return details


def case_h7():
    """H7：/clear、/resume 同 pid 换号不算新会话（RT:T1b/T1c）；崩溃后同会话号换进程只在旧进程确已死时才更新。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "s-1")
        b = pool.identity(pool.spawn_claude(), "sB")
        code, out = call_relay("SessionStart", project, "s-1", ident_fn(a), extra={"source": "startup"})
        require(read_baton(sdir)["holder"]["sid"] == "s-1", "先认领")
        code, out = call_relay("SessionStart", project, "s-2", ident_fn(a), extra={"source": "clear"})
        state = read_baton(sdir)
        require(state["holder"]["sid"] == "s-2" and state["holder"]["pid"] == a["pid"] and not state["retire"], "/clear 同 pid 换号应只更新会话号：%r" % state)
        require("主控" in additional(out) and "旁观" not in additional(out), "换号后仍是主控：%r" % out)
        code, out = call_relay("PreToolUse", project, "s-2", ident_fn(a), extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None, "换号后可写")
        code, out = call_relay("SessionStart", project, "s-1", ident_fn(a), extra={"source": "resume"})
        require(read_baton(sdir)["holder"]["sid"] == "s-1", "/resume 回原会话号")
        code, out = call_relay("PreToolUse", project, "s-3", ident_fn(a), extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None and read_baton(sdir)["holder"]["sid"] == "s-1", "同进程未登记的新会话号也按 holder 放行，且不改登记册")
        code, out = call_relay("SessionStart", project, "s-2", ident_fn(b), extra={"source": "clear"})
        require("旁观" in additional(out) and read_baton(sdir)["holder"]["sid"] == "s-1", "别的进程用同名新会话号仍是旁观")
        details.append("/clear（s-1→s-2）/resume（回 s-1）同 pid 只换号；同进程未登记会话号放行；别的进程仍旁观")
        # 崩溃后 claude --resume 回来：同会话号、新进程
        a_proc.kill()
        a_proc.wait(timeout=5)
        a2 = pool.identity(pool.spawn_claude(), "s-1")
        code, out = call_relay("SessionStart", project, "s-1", ident_fn(a2), extra={"source": "resume"})
        holder = read_baton(sdir)["holder"]
        require(holder["sid"] == "s-1" and holder["pid"] == a2["pid"] and holder["lstart"] == a2["lstart"], "旧进程已死：同会话号新进程应更新 pid：%r" % holder)
        a3 = pool.identity(pool.spawn_claude(), "s-1")
        call_relay("SessionStart", project, "s-1", ident_fn(a3), extra={"source": "resume"})
        require(read_baton(sdir)["holder"]["pid"] == a2["pid"], "旧进程仍活着：不得被同会话号的另一进程替换")
        details.append("崩溃后同会话号换进程：旧进程已死才更新登记册 pid；旧进程仍活则不动")
    return details


def case_h8():
    """H8：没有旁路变量——旁观会话带 MAIN_LOCK_OVERRIDE=1 写入仍被拒；holder 的子进程（祖先链含 holder claude 进程）写入放行。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        x = pool.identity(pool.spawn_claude(), "sub-1")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        write = tool_call("Write", file_path="/x", content="c")
        code, out = call_relay("PreToolUse", project, "sB", ident_fn(b), extra=write, env={"MAIN_LOCK_OVERRIDE": "1"})
        require(is_denied(out), "旁观会话带旁路变量写入应被拒：%r" % out)
        code, out = call_relay("PreToolUse", project, "sB", ident_fn(b), extra=write, env={"MAIN_LOCK_OVERRIDE": "1", "CLAUDE_PROJECT_DIR": project})
        require(is_denied(out), "旁路变量无论怎么设都不生效")
        details.append("旁观会话 + MAIN_LOCK_OVERRIDE=1：写入仍被拒")
        chain_ok = lambda deadline: [(x["pid"], x["lstart"]), (a["pid"], a["lstart"])]  # noqa: E731
        code, out = call_relay("PreToolUse", project, "sub-1", ident_fn(x), chain=chain_ok, extra=write)
        require(out is None, "祖先链含 holder 的子任务（如 claude -p）应放行：%r" % out)
        code, out = call_relay("SessionStart", project, "sub-1", ident_fn(x), chain=chain_ok, extra={"source": "startup"})
        require(out is None and read_baton(sdir)["holder"]["sid"] == "sA", "子任务不认领、不改 holder")
        chain_wrong_lstart = lambda deadline: [(x["pid"], x["lstart"]), (a["pid"], "Mon Jan  1 00:00:00 2001")]  # noqa: E731
        code, out = call_relay("PreToolUse", project, "sub-1", ident_fn(x), chain=chain_wrong_lstart, extra=write)
        require(is_denied(out), "祖先链里 pid 同但 lstart 不同（pid 复用）不能算 holder 子任务：%r" % out)
        chain_other = lambda deadline: [(x["pid"], x["lstart"]), (b["pid"], b["lstart"])]  # noqa: E731
        code, out = call_relay("PreToolUse", project, "sub-1", ident_fn(x), chain=chain_other, extra=write)
        require(is_denied(out), "祖先链是别的会话的进程不算：%r" % out)
        details.append("holder 子任务（祖先链含 holder pid+lstart）放行；lstart 不符 / 祖先是旁观者 → 拒绝；子任务不认领")
        source = open(os.path.join(MVP_ROOT, "cdm", "hook_relay.py"), encoding="utf-8").read()
        require(not os.path.exists(os.path.join(MVP_ROOT, "cdm", "hook_lock.py")) and "environ.get(\"MAIN_LOCK" not in source,
                "旧 hook_lock 应已删、新钩子不读旁路变量")
    return details


def case_h9():
    """H9：baton.json 读改写在同一 guard 内；并发认领只有一个成功；guard 被占时不认领（放行）；旁观 / 命令类事件不改 holder。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        sdir = sdir_of(make_project(work, "conc"))
        recs = [pool.identity(pool.spawn_claude(), "c%d" % index) for index in range(2)]
        results = []

        def worker(rec):
            results.append(baton.claim(sdir, rec["session_id"], lambda: (rec["pid"], rec["lstart"]), lambda: [], util.now_ms()))

        threads = [threading.Thread(target=worker, args=(recs[index % 2],)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        claimed = [item for item in results if item[0] == "claimed"]
        require(len(results) == 8 and len(claimed) == 1, "8 个并发认领应恰好 1 个成功：%r" % results)
        others = [item for item in results if item[0] != "claimed"]
        require(all(item in (("denied", "spectator"), ("ok", "holder")) for item in others), "其余应是旁观或已是 holder：%r" % others)
        holder = read_baton(sdir)["holder"]
        require(holder["sid"] in ("c0", "c1"), "登记册 holder 应是其中之一：%r" % holder)
        leftovers = [name for name in os.listdir(sdir) if name.startswith(".tmp.")]
        require(not leftovers, "原子替换不应留下临时文件：%r" % leftovers)
        details.append("8 线程 × 2 身份并发认领：恰好 1 个 claimed，其余旁观；无临时文件残留")
        # guard 被占：不认领、放行，释放后可认领
        project = make_project(work, "busy")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        c = pool.identity(pool.spawn_claude(), "sC")
        with baton.guarded(sdir) as acquired:
            require(acquired, "测试自己应拿到 guard")
            started = time.monotonic()
            code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
            waited = time.monotonic() - started
            require(out is None and not os.path.exists(baton.baton_path(sdir)), "guard 被占时不得写登记册、不阻断：%r" % out)
            require(waited < 4.0, "等 guard 必须有界：%.1f 秒" % waited)
        code, out = call_relay("UserPromptSubmit", project, "sC", ident_fn(c))
        require(read_baton(sdir)["holder"]["sid"] == "sC", "guard 释放后应能认领")
        details.append("guard 被占：有界等待后放行、登记册未被写；释放后认领成功")
        # 旁观 / 命令类事件不改 holder
        project = make_project(work, "ro")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        before = open(baton.baton_path(sdir), "rb").read()
        for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {}), ("PreToolUse", tool_call("Write", file_path="/x")),
                             ("PostToolUse", tool_call("SendMessage")), ("Stop", {}), ("StopFailure", {"error": "unknown"}),
                             ("SessionEnd", {"reason": "other"}), ("PermissionRequest", {"tool_name": "Bash"})):
            call_relay(event, project, "sB", ident_fn(b), extra=extra)
        require(open(baton.baton_path(sdir), "rb").read() == before, "旁观会话的任何事件都不得改登记册")
        details.append("旁观会话的 8 类事件：登记册逐字节不变")
    return details


def case_h10():
    """H10：65 / 76 两档各提醒一次；76 档写「待发起」标记并注入「只准收尾」；≥65% 拦新长任务集合；只对 holder 生效。"""
    details = []
    with patched_env(CTX_WINDOW=None), temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        post = tool_call("Bash", command="ls")
        write_ctx(sdir, "sA", 60)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=post)
        require(out is None, "60% 不提醒")
        write_ctx(sdir, "sA", 66)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=post)
        require("NUDGE-" in additional(out) and "第一档" in additional(out), "65 档应提醒：%r" % out)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=post)
        require(out is None, "65 档只提醒一次")
        require(not sessions.read_session(sdir, "sA").get("request_pending"), "65 档不写待发起标记")
        write_ctx(sdir, "sA", 77)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=post)
        require("此后只准收尾" in additional(out), "76 档应注入「此后只准收尾」：%r" % out)
        pending = sessions.read_session(sdir, "sA").get("request_pending")
        require(pending and pending["reason"] == "ctx76", "76 档应写待发起标记：%r" % pending)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=post)
        require(out is None, "76 档只提醒一次")
        details.append("66% 提醒 1 次（无标记）；77% 提醒 1 次并写 request_pending(ctx76)；再调用不重复")
        # 直接到 80：只发高档
        j = pool.identity(pool.spawn_claude(), "sJ")
        project_j = make_project(work, "pj")
        sdir_j = set_auto(project_j)
        sessions.write_heartbeat(sdir_j, util.now_ms())
        call_relay("SessionStart", project_j, "sJ", ident_fn(j), extra={"source": "startup"})
        write_ctx(sdir_j, "sJ", 80)
        code, out = call_relay("PostToolUse", project_j, "sJ", ident_fn(j), extra=post)
        require("此后只准收尾" in additional(out) and sessions.read_session(sdir_j, "sJ").get("request_pending"), "首采样 80% 只发高档并写标记")
        # 拦新长任务
        long_tasks = (("Agent", {"prompt": "p"}), ("Workflow", {"name": "w"}), ("Monitor", {"command": "tail"}), ("CronCreate", {"schedule": "*"}),
                      ("ScheduleWakeup", {"delay": 1}), ("Bash", {"command": "sleep 9", "run_in_background": True}))
        fine = (("Bash", {"command": "ls"}), ("Bash", {"command": "ls", "run_in_background": False}), ("Edit", {"file_path": "/x"}),
                ("Write", {"file_path": "/x", "content": "c"}), ("Read", {"file_path": "/x"}))
        for tool, tool_input in long_tasks:
            code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=tool_call(tool, **tool_input))
            require(is_denied(out) and "只准收尾" in out["hookSpecificOutput"]["permissionDecisionReason"], "≥65%% 应拦 %s：%r" % (tool, out))
        for tool, tool_input in fine:
            code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=tool_call(tool, **tool_input))
            require(out is None, "≥65%% 不应拦 %s %r：%r" % (tool, tool_input, out))
        write_ctx(sdir, "sA", 64.9)
        for tool, tool_input in long_tasks:
            code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=tool_call(tool, **tool_input))
            require(out is None, "<65%% 不拦 %s" % tool)
        os.unlink(os.path.join(sdir, "ctx", "sA.json"))
        code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=tool_call("Agent", prompt="p"))
        require(out is None, "取不到上下文百分比不拦")
        details.append("≥65%%：拦 %d 种新长任务、放行 %d 种常规工具；<65%% 与无读数不拦" % (len(long_tasks), len(fine)))
        # 只对 holder 提醒
        write_ctx(sdir, "sB", 80)
        call_relay("SessionStart", project, "sB", ident_fn(b), extra={"source": "startup"})
        code, out = call_relay("PostToolUse", project, "sB", ident_fn(b), extra=post)
        require(out is None and not sessions.read_session(sdir, "sB").get("request_pending"), "旁观会话不提醒、不写标记")
        write_ctx(sdir, "sA", 80)
        code, out = call_relay("PostToolUse", project, "sA", ident_fn(a), extra=dict(post, agent_id="child"))
        require(out is None, "子代理的 PostToolUse 不提醒")
        details.append("旁观会话与子代理不被提醒")
    return details


def case_h11():
    """H11：auto 的等输入响铃去重——批准→工具失败→再请求权限必须再响；各清标记事件；notice 记录与解除。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        rings = Rings()

        def ev(event, **extra):
            code, out = call_relay(event, project, "sA", ident_fn(a), rings=rings, extra=extra)
            require(code == 0 and not is_blocked(out) and not is_denied(out), "%s 应放行：%r" % (event, out))

        ev("PermissionRequest", tool_name="Bash")
        require(rings.count("waiting") == 1, "权限框响铃")
        pending = sessions.pending_notices(sdir)
        require(len(pending) == 1 and pending[0]["cat"] == "waiting" and "1) 去该窗口处理" in pending[0]["text"]
                and pending[0]["options"] == ["去该窗口处理"], "auto 应写 pending notice（四段+编号）：%r" % pending)
        ev("PostToolUseFailure", **tool_call("Bash", command="false"))
        require(sessions.pending_notices(sdir) == [], "非等待类事件应把 notice 标为已解除")
        ev("PermissionRequest", tool_name="Bash")
        require(rings.count("waiting") == 2, "批准→工具失败→再请求权限：必须再响（2 次）")
        for clearing, extra in (("PostToolUse", tool_call("Bash", command="ls")), ("PreToolUse", tool_call("Read", file_path="/x")),
                                ("UserPromptSubmit", {}), ("Stop", {}), ("StopFailure", {"error": "overloaded"}),
                                ("PostToolUseFailure", tool_call("Bash", command="x"))):
            before = rings.count("waiting")
            ev(clearing, **extra)
            ev("PermissionRequest", tool_name="Bash")
            require(rings.count("waiting") == before + 1, "%s 清标记后应能再响" % clearing)
            ev("PermissionRequest", tool_name="Bash")
            require(rings.count("waiting") == before + 1, "%s 之后同次等待重复信号不再响" % clearing)
        before = rings.count("waiting")
        for keeping, extra in (("Notification", {"notification_type": "idle_prompt"}), ("SubagentStop", {}), ("SessionEnd", {"reason": "clear"})):
            ev(keeping, **extra)
            ev("PermissionRequest", tool_name="Bash")
        require(rings.count("waiting") == before, "Notification / SubagentStop / SessionEnd 不清标记")
        ev("PreToolUse", **tool_call("Read", file_path="/x"))
        ev("PreToolUse", **tool_call("AskUserQuestion", questions=[]))
        ev("Notification", notification_type="elicitation_dialog")
        require(rings.count("waiting") == before + 1, "AskUserQuestion 响一次；同次等待的 Notification 不重复")
        ev("Notification", notification_type="quota_auto_resume_stale")
        require(rings.count("waiting") == before + 1, "标记未清：stale 也属同次等待")
        require(all(call["kind"] == "waiting" for call in rings.calls), "等输入一律 waiting 类")
    return ["auto：批准→失败→再请求必须再响；PostToolUse / PreToolUse / UserPromptSubmit / Stop / StopFailure / PostToolUseFailure 各清标记；Notification / SubagentStop / SessionEnd 不清；notice 写入与解除"]


def case_h12():
    """H12：B 的工具面——只读 + handoffd 白名单（sign in / status / notify / reply；S2a 收窄）+ SendMessage + 配置里逐字一致的实测命令；SendMessage 计数。"""
    details = []
    verify = "cd /repo && ./run_tests.sh --fast 2>&1 | tail -5"
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        os.makedirs(os.path.join(project, ".claude"))
        with open(os.path.join(project, ".claude", "handoffd.json"), "w", encoding="utf-8") as stream:
            json.dump({"project_dir": project, "b_verify_commands": [verify]}, stream)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "b-uuid-1")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-1"))
        call_relay("SessionStart", project, "b-uuid-1", ident_fn(b), extra={"source": "startup"})

        def pre(tool, **tool_input):
            return call_relay("PreToolUse", project, "b-uuid-1", ident_fn(b), extra=tool_call(tool, **tool_input))[1]

        for tool, tool_input in (("Bash", {"command": verify}), ("Bash", {"command": "handoffd status"}), ("Bash", {"command": "handoffd sign in"}),
                                 ("Bash", {"command": "handoffd reply task"}), ("Bash", {"command": "handoffd --config /c.json status"}),
                                 ("Read", {"file_path": "/x"}), ("Grep", {"pattern": "a"}), ("SendMessage", {"to": "sA"})):
            require(pre(tool, **tool_input) is None, "B 应放行 %s %r" % (tool, tool_input))
        for tool, tool_input in (("Bash", {"command": verify + " "}), ("Bash", {"command": verify.replace("--fast", "--slow")}),
                                 ("Bash", {"command": verify.replace("cd /repo", "cd /repo2")}), ("Bash", {"command": verify + " && rm -rf x"}),
                                 ("Bash", {"command": "handoffd status && rm x"}), ("Bash", {"command": "handoffd on"}),
                                 ("Bash", {"command": "handoffd adopt"}), ("Bash", {"command": "ls"}), ("Write", {"file_path": "/x", "content": "c"}),
                                 # S2a：B 的 handoffd 子命令收成白名单 sign in / status / notify / reply（S1 R3 Opus P2-3）
                                 ("Bash", {"command": "handoffd off"}), ("Bash", {"command": "handoffd request --reason goal"}),
                                 ("Bash", {"command": "handoffd seal --kit /k.md"}), ("Bash", {"command": "handoffd sign out"}),
                                 ("Bash", {"command": "handoffd sign"}), ("Bash", {"command": "handoffd sign in extra"}),
                                 ("Bash", {"command": "handoffd review-round --product x --round 1 --p0 0"}), ("Bash", {"command": "handoffd daemon"}),
                                 ("Agent", {"prompt": "p"})):
            require(is_denied(pre(tool, **tool_input)), "B 应拒绝 %s %r" % (tool, tool_input))
        details.append("B 放行：逐字实测命令 / handoffd sign in、status、reply / 只读 / SendMessage；拒绝：改一字符、加空格、拼 &&、on / adopt、off / request / seal / sign out / review-round / daemon、普通 Bash、写、Agent")
        for index in range(3):
            call_relay("PostToolUse", project, "b-uuid-1", ident_fn(b), extra=tool_call("SendMessage", to="sA"))
        require(read_baton(sdir)["handoff"]["qa_count"] == 3, "B 的 SendMessage 应计数：%r" % read_baton(sdir)["handoff"])
        call_relay("PostToolUse", project, "sA", ident_fn(a), extra=tool_call("SendMessage", to="b"))
        call_relay("PostToolUse", project, "b-uuid-1", ident_fn(b), extra=tool_call("Read", file_path="/x"))
        require(read_baton(sdir)["handoff"]["qa_count"] == 3, "A 的 SendMessage 与 B 的其它工具不计数")
        details.append("qa_count：B 的 SendMessage 计 3 次；A 的不计")
        # 配置坏 ⇒ 白名单退回空（不放行任何 Bash）
        with open(os.path.join(project, ".claude", "handoffd.json"), "w", encoding="utf-8") as stream:
            stream.write("{bad")
        require(is_denied(pre("Bash", command=verify)), "配置不合法时白名单为空")
        details.append("handoffd.json 不合法：白名单退回空")
    return details


def case_h13():
    """H13：auto 会话状态记录——Stop 两数组、StopFailure 归类与响铃、quota 相关通知、SessionEnd；SessionStart 注入文案；manual 无注入。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        rings = Rings()
        call_relay("SessionStart", project, "sA", ident_fn(a), rings=rings, extra={"source": "startup"})

        def ev(event, **extra):
            return call_relay(event, project, "sA", ident_fn(a), rings=rings, extra=extra)

        ev("Stop", background_tasks=[{"type": "shell"}, {"type": "subagent"}], session_crons=1)
        record = sessions.read_session(sdir, "sA")
        require(record["bg_tasks"] == 2 and record["bg_crons"] == 1 and record["last_stop_ms"], "Stop 应记两数组：%r" % record)
        ev("Stop")
        record = sessions.read_session(sdir, "sA")
        require(record["bg_tasks"] is None and record["bg_crons"] is None, "两数组缺席记为未知（None），不当作空：%r" % record)
        ev("Stop", background_tasks=[], session_crons=0)
        require(sessions.read_session(sdir, "sA")["bg_tasks"] == 0, "空数组记 0")
        details.append("Stop：数组长度 / 缺席=None / 空=0")
        for error in ("rate_limit", "overloaded", "server_error", "max_output_tokens"):
            ev("StopFailure", error=error)
        require(rings.count() == 0, "撞额度与临时 API 错误永不响铃：%r" % rings.calls)
        record = sessions.read_session(sdir, "sA")
        require(record["last_error"]["type"] == "max_output_tokens", "应记最近一次错误")
        ev("StopFailure", error="rate_limit")
        require(sessions.read_session(sdir, "sA")["quota_wait_ms"], "rate_limit 记 quota_wait")
        ev("Notification", notification_type="quota_auto_resume_fired")
        require(sessions.read_session(sdir, "sA")["quota_wait_ms"] is None, "fired 清 quota_wait")
        ev("StopFailure", error="rate_limit")
        ev("Notification", notification_type="quota_auto_resume_disabled")
        record = sessions.read_session(sdir, "sA")
        require(record["quota_wait_ms"] is None and record["quota_disabled_ms"], "disabled 记录并清 quota_wait")
        require(rings.count() == 0, "disabled 也不响铃（规格:39、:40）")
        details.append("rate_limit / overloaded / server_error / max_output_tokens 永不响铃；quota_wait 记与清；disabled 记录")
        human = ("authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error", "invalid_request", "model_not_found",
                 "cloud_credential_error", "unknown", "some_new_error")
        for index, error in enumerate(human, start=1):
            ev("StopFailure", error=error)
            require(rings.count("problem") == index, "%s 应响铃一次" % error)
        call = rings.calls[-1]
        require(call["kind"] == "problem" and call["ask"] == "1) 去该窗口处理" and call["what"] and call["why"] and call["if_no_reply"], "四段不全：%r" % call)
        require(len([item for item in sessions.pending_notices(sdir) if item["cat"] == "api_error"]) == len(human), "每次都写 notice")
        details.append("需人处理的 %d 类错误（含未知新错误串）各响铃一次并写 notice" % len(human))
        ev("SessionEnd", reason="clear")
        require(not sessions.read_session(sdir, "sA").get("ended_ms"), "clear 的 SessionEnd 忽略")
        ev("SessionEnd", reason="other")
        require(sessions.read_session(sdir, "sA")["end_reason"] == "other", "其它 SessionEnd 标正常退出")
        events = [json.loads(line) for line in open(os.path.join(sdir, "events.jsonl"), encoding="utf-8")]
        require(all({"t_ms", "sid", "event"} <= set(item) for item in events) and any(item["event"] == "StopFailure" for item in events), "events.jsonl 字段")
        details.append("SessionEnd：clear/resume 忽略，其它记 end_reason；events.jsonl 有 t_ms/sid/event")
        manual_project = make_project(work, "pm")
        code, out = call_relay("SessionStart", manual_project, "sA", ident_fn(a), extra={"source": "startup"})
        require(out is None, "manual 下 SessionStart 不注入")
    return details


def write_probe(project, sid, rec, tool="Write"):
    """不经 call_relay（它改进程环境，线程里不安全）：直接调 hook_relay.main，返回该 PreToolUse 是否被拒。调用方需已设好 CLAUDE_PROJECT_DIR。"""
    import io
    payload = dict({"hook_event_name": "PreToolUse", "session_id": sid, "cwd": project}, **tool_call(tool, file_path="/x", content="c"))
    out = io.StringIO()
    hook_relay.main(["PreToolUse"], stdin=io.StringIO(json.dumps(payload)), stdout=out, identity=ident_fn(rec),
                    chain=lambda deadline: [(rec["pid"], rec["lstart"])], ring_fn=Rings())
    return is_denied(json.loads(out.getvalue()) if out.getvalue().strip() else None)


def run_threads(jobs):
    results = []

    def worker(job):
        results.append(job())

    threads = [threading.Thread(target=worker, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def case_h22():
    """H22（F1）：会话号相同但进程不是登记的那个——登记进程活着 ⇒ 旁观；交接中登记进程已死 ⇒ block；无交接已死 ⇒ 重新挂靠。"""
    details = []
    write = tool_call("Write", file_path="/x", content="c")
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a_proc = pool.spawn_claude()
        a = pool.identity(a_proc, "S")
        a2 = pool.identity(pool.spawn_claude(), "S")
        call_relay("SessionStart", project, "S", ident_fn(a), extra={"source": "startup"})
        before = open(baton.baton_path(sdir), "rb").read()
        code, out = call_relay("SessionStart", project, "S", ident_fn(a2), extra={"source": "resume"})
        require("旁观" in additional(out) and "你是本项目的主控" not in additional(out), "第二个同会话号进程应被告知旁观：%r" % out)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(a2), extra=write)
        require(is_denied(out), "同会话号的第二个存活进程写入应被拒（P0-1）：%r" % out)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(a), extra=write)
        require(out is None, "登记的那个进程照常可写")
        code, out = call_relay("UserPromptSubmit", project, "S", ident_fn(a2))
        require(out is None and open(baton.baton_path(sdir), "rb").read() == before, "旁观不被 block、不改登记册")
        details.append("同 sid 第二存活进程：旁观、写被拒；原进程照常；登记册不变")
        # 判活不确定：同样不授权
        bin_dir = os.path.join(work, "fakeps")
        make_fake_ps(bin_dir)
        with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID=str(a["pid"]), FAKE_PS_MODE="exit1"):
            code, out = call_relay("PreToolUse", project, "S", ident_fn(a2), extra=write)
        require(is_denied(out), "登记进程判活不确定：同 sid 的另一进程也不授权")
        code, out = call_relay("PreToolUse", project, "S", None, extra=write)
        require(out is None, "取不到进程身份时按会话号认（规格:79）")
        # 交接中登记进程死了：同 sid 新窗口先拦下
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-1"))
        pool.kill(a_proc)
        before = open(baton.baton_path(sdir), "rb").read()
        code, out = call_relay("UserPromptSubmit", project, "S", ident_fn(a2))
        require(is_blocked(out) and "handoffd off" in out["reason"], "交接中 A 死后 --resume 同 sid 应 block（规格:44）：%r" % out)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(a2), extra=write)
        require(is_denied(out) and open(baton.baton_path(sdir), "rb").read() == before, "blocked：写被拒、不认领")
        details.append("交接中 A 死后同 sid 新窗口：UserPromptSubmit block（handoffd off）、写被拒、登记册不变")
        # 无交接：重新挂靠
        write_baton(sdir, holder=holder_of(a), handoff=None)
        code, out = call_relay("UserPromptSubmit", project, "S", ident_fn(a2))
        holder = read_baton(sdir)["holder"]
        require(out is None and holder["pid"] == a2["pid"] and holder["sid"] == "S", "无交接：同 sid 新进程重新挂靠：%r" % holder)
        code, out = call_relay("PreToolUse", project, "S", ident_fn(a2), extra=write)
        require(out is None, "挂靠后可写")
        details.append("无交接：同 sid 新进程重新挂靠（规格:110）")
    return details


def case_h23():
    """H23（F2）：没人持棒时放行写工具前必须先认领——两个未认领会话并发写只有一个放行；认领没完成 ⇒ 写被拒、只读放行。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        # a) `on` 之后两个未认领会话
        project = make_project(work, "a")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        x = pool.identity(pool.spawn_claude(), "sX")
        y = pool.identity(pool.spawn_claude(), "sY")
        code, out = call_relay("PreToolUse", project, "sX", ident_fn(x), extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None and read_baton(sdir)["holder"]["sid"] == "sX", "先写的 X 认领并放行")
        code, out = call_relay("PreToolUse", project, "sY", ident_fn(y), extra=tool_call("Write", file_path="/x", content="c"))
        require(is_denied(out), "后写的 Y 是旁观，写被拒：%r" % out)
        details.append("on 之后未认领的 X、Y 先后写：X 认领放行，Y 被拒")
        # 并发
        project = make_project(work, "b")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        with patched_env(CLAUDE_PROJECT_DIR=project):
            results = run_threads([(lambda sid=sid, rec=rec: (sid, write_probe(project, sid, rec))) for sid, rec in (("sX", x), ("sY", y)) * 4])
        allowed = {sid for sid, denied in results if not denied}
        require(len(results) == 8 and len(allowed) == 1 and read_baton(sdir)["holder"]["sid"] in allowed,
                "8 个并发写只能有一个会话放行，且就是登记的 holder：%r" % results)
        details.append("未认领的 X、Y 各 4 个并发写：只有一个会话放行，且就是登记册里的 holder")
        # b) holder 死、无交接：两个旁观并发写
        project = make_project(work, "c")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        h_proc = pool.spawn_claude()
        h = pool.identity(h_proc, "sH")
        s1 = pool.identity(pool.spawn_claude(), "s1")
        s2 = pool.identity(pool.spawn_claude(), "s2")
        call_relay("SessionStart", project, "sH", ident_fn(h), extra={"source": "startup"})
        for sid, rec in (("s1", s1), ("s2", s2)):
            code, out = call_relay("PreToolUse", project, sid, ident_fn(rec), extra=tool_call("Write", file_path="/x", content="c"))
            require(is_denied(out), "holder 活着时旁观被拒")
        pool.kill(h_proc)
        with patched_env(CLAUDE_PROJECT_DIR=project):
            results = run_threads([(lambda sid=sid, rec=rec: (sid, write_probe(project, sid, rec))) for sid, rec in (("s1", s1), ("s2", s2)) * 4])
        allowed = {sid for sid, denied in results if not denied}
        require(len(allowed) == 1 and read_baton(sdir)["holder"]["sid"] in allowed, "holder 死后两个旁观并发写只能有一个放行：%r" % results)
        require(len(read_baton(sdir)["retire"]) == 1, "旧 holder 进 retire")
        details.append("holder 死（无交接）后两个旁观各 4 个并发写：只有一个放行并成为 holder")
        # c) 认领没完成：guard 忙 / 无身份 ⇒ 写拒、读放行
        project = make_project(work, "d")
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        with baton.guarded(sdir):
            code, out = call_relay("PreToolUse", project, "sX", ident_fn(x), extra=tool_call("Write", file_path="/x", content="c"))
            require(is_denied(out), "guard 被占、认领没完成：写被拒")
            code, out = call_relay("PreToolUse", project, "sX", ident_fn(x), extra=tool_call("Read", file_path="/x"))
            require(out is None, "认领没完成：只读放行")
        code, out = call_relay("PreToolUse", project, "sZ", None, extra=tool_call("Write", file_path="/x", content="c"))
        require(out is None and not os.path.exists(baton.baton_path(sdir)),
                "无人持棒且取不到进程身份：按规格:79 fail-open 放行，但不认领（R3 起与「认领没完成拒写」分开：身份取不到是 fail_open 分支）")
        code, out = call_relay("PreToolUse", project, "sZ", None, extra=tool_call("Read", file_path="/x"))
        require(out is None, "只读放行")
        details.append("认领没完成（guard 忙）：写被拒、只读放行；无身份：fail_open 放行且不认领")
    return details


def case_h24():
    """H24（F3 / R3）：旁观 / blocked 没有 Bash；B 的 Bash 按「字面」匹配——含 shell 元字符或展开的一律拒（Sol 两条探针），命令词只认裸 handoffd 或安装绝对路径，逐字白名单不受此限。"""
    details = []
    meta = (
        'handoffd status "$(touch probe.txt)"', 'env -S "touch probe2.txt" handoffd status', "handoffd status; touch x", "handoffd status && id",
        "handoffd status | cat", "handoffd status > out.txt", "handoffd status < in", "handoffd status `id`", "handoffd status $HOME",
        "handoffd status *", "handoffd status ~", "handoffd status {a,b}", "handoffd status\ntouch x", "handoffd status\\", "handoffd status (x)",
        "handoffd status 'unterminated", "handoffd status !x", "X=1 handoffd status", "touch x handoffd status", "handoffd status extra",
    )
    verify = "cd /repo && ./run_tests.sh --fast 2>&1 | tail -5"
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        os.makedirs(os.path.join(project, ".claude"))
        with open(os.path.join(project, ".claude", "handoffd.json"), "w", encoding="utf-8") as stream:
            json.dump({"project_dir": project, "b_verify_commands": [verify]}, stream)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b_spec = pool.identity(pool.spawn_claude(), "sB")
        b = pool.identity(pool.spawn_claude(), "b-uuid-1")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})

        def bash(sid, rec, command):
            return call_relay("PreToolUse", project, sid, ident_fn(rec), extra=tool_call("Bash", command=command))[1]

        for command in meta:
            require(is_denied(bash("sB", b_spec, command)), "旁观会话应拒 %r" % command)
        for command in ("handoffd status", "/x/bin/handoffd status", "handoffd --config /c/handoffd.json status", "ls", "./handoffd status"):
            denied = bash("sB", b_spec, command)
            require(is_denied(denied) and "当前状态" in denied["hookSpecificOutput"]["permissionDecisionReason"],
                    "R3：旁观会话没有 Bash（含 handoffd status），拒绝文案带状态：%r" % (denied,))
        details.append("旁观：%d 种带元字符 / 展开的命令与 handoffd status 全拒（旁观没有 Bash，拒绝文案带状态）" % len(meta))
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-1"))
        for command in meta:
            if command == "handoffd status extra":
                continue  # B 的 handoffd 调用本来就可带普通参数
            require(is_denied(bash("b-uuid-1", b, command)), "B 应拒 %r" % command)
        require(bash("b-uuid-1", b, verify) is None, "B 的逐字白名单（含 && | 2>&1）照旧放行")
        from cdm import policy
        require(bash("b-uuid-1", b, "%s status" % policy.INSTALLED_HANDOFFD) is None and bash("b-uuid-1", b, "handoffd status") is None,
                "B 调 handoffd：裸名或安装绝对路径放行")
        for fake in ("./handoffd status", "/x/bin/handoffd status", "bin/handoffd status", "../handoffd status"):
            require(is_denied(bash("b-uuid-1", b, fake)), "B 不认同名的别处文件 %r" % fake)
        require(bash("b-uuid-1", b, 'handoffd notify --cat rule --what x --why y --ask "1) 去看 2) 放弃" --recommend 1 --if-no-reply z') is None,
                "B 的 handoffd 调用可带引号内的文字（含括号）")
        require(is_denied(bash("b-uuid-1", b, 'handoffd notify --what "$(id)"')), "引号里的 $( ) 仍是展开，拒绝")
        details.append("B：带元字符的拒；逐字白名单放行；handoffd notify 带引号文字放行、引号内 $( ) 拒")
        # blocked 与旁观同矩阵
        a_proc = pool.spawn_claude()
        dead = pool.identity(a_proc, "sD")
        pool.kill(a_proc)
        c = pool.identity(pool.spawn_claude(), "sC")
        write_baton(sdir, holder=holder_of(dead), handoff=handoff_with_b("b-uuid-9"))
        require(is_denied(bash("sC", c, 'handoffd status "$(touch probe.txt)"')) and is_denied(bash("sC", c, "handoffd status")), "blocked 同旁观矩阵（无 Bash）")
        details.append("blocked：同旁观矩阵")
    return details


def case_h25():
    """H25（F4）：去重标记只在响铃成功启动后才落——一次响铃失败不会让该段停跳 / 等待永不再响。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        a = pool.identity(pool.spawn_claude(), "sA")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        sessions.write_heartbeat(sdir, util.now_ms() - 200_000)

        def failing(*args):
            raise OSError("spawn failed")

        code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), rings=failing, extra=tool_call("Read", file_path="/x"))
        require(code == 0 and out is None, "响铃失败仍放行")
        require(not os.path.exists(sessions.run_path(sdir, "hb_stale")) and sessions.pending_notices(sdir) == [], "心跳铃失败：不落标记、不写 notice")
        rings = Rings()
        call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call("Read", file_path="/x"))
        require(len(heartbeat_calls(rings)) == 1 and os.path.exists(sessions.run_path(sdir, "hb_stale")) and len(sessions.pending_notices(sdir)) == 1,
                "下一个事件重试：响一次、落标记、写 notice")
        call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call("Read", file_path="/x"))
        require(rings.count() == 1, "成功之后同段不再响")
        # 等输入
        code, out = call_relay("PermissionRequest", project, "sA", ident_fn(a), rings=failing, extra={"tool_name": "Bash"})
        require(code == 0 and not os.path.exists(sessions.wait_marker(sdir, "sA")), "等输入铃失败：不落标记")
        require(len([n for n in sessions.pending_notices(sdir) if n["cat"] == "waiting"]) == 0, "等输入铃失败：不写 notice")
        rings = Rings()
        call_relay("Notification", project, "sA", ident_fn(a), rings=rings, extra={"notification_type": "permission_prompt"})
        require(rings.count("waiting") == 1 and os.path.exists(sessions.wait_marker(sdir, "sA")), "下一个信号重试并落标记")
    return ["心跳铃 / 等输入铃：启动失败不落标记不写 notice，下个事件重试；成功后才去重"]


def heartbeat_calls(rings):
    return [call for call in rings.calls if call["kind"] == "problem" and "心跳" in call["what"]]


def case_h26():
    """H26（F5）：心跳恢复即解除——pending 心跳 notice 被标 resolved，再停再产生新 notice。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        a = pool.identity(pool.spawn_claude(), "sA")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        rings = Rings()
        sessions.write_heartbeat(sdir, util.now_ms() - 200_000)
        call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call("Read", file_path="/x"))
        pending = sessions.pending_notices(sdir)
        require(len(pending) == 1 and pending[0]["cat"] == "heartbeat", "停跳：pending heartbeat notice")
        first = pending[0]["id"]
        sessions.write_heartbeat(sdir, util.now_ms() - 1000)
        call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call("Read", file_path="/x"))
        require(sessions.pending_notices(sdir) == [] and not os.path.exists(sessions.run_path(sdir, "hb_stale")), "心跳恢复：notice 已解除、标记已清")
        sessions.write_heartbeat(sdir, util.now_ms() - 200_000)
        call_relay("PreToolUse", project, "sA", ident_fn(a), rings=rings, extra=tool_call("Read", file_path="/x"))
        pending = sessions.pending_notices(sdir)
        require(len(pending) == 1 and pending[0]["id"] != first and rings.count() == 2, "再停：新 notice、再响")
    return ["心跳恢复解除 pending notice；再停产生新 notice"]


def case_h27():
    """H27（F6）：`handoffd on|adopt` 的原文兜底——命令替换 / eval / 直接调 handoffd_cli.py 也拦；子命令位置之外的 on 不拦。"""
    from cdm import policy
    deny_cases = ('result="$(handoffd on)"', 'echo "$(handoffd on)"', "$(handoffd on)", 'eval "handoffd on"', "python3 /x/cdm/handoffd_cli.py on",
                  "python3 /x/cdm/handoffd_cli.py adopt", "handoffd --config /c on", "sh -c 'handoffd adopt'", "handoffd on")
    allow_cases = ('handoffd notify --why "tests on main" --what x', "handoffd onward", "cat handoffd_adopt.md", "handoffd status", "handoffd off")
    for command in deny_cases:
        require(policy.invokes_owner_only(command), "应拦 %r" % command)
    for command in allow_cases:
        require(not policy.invokes_owner_only(command), "不应拦 %r" % command)
    with temp_dir() as work, process_pool(work) as pool:
        a = pool.identity(pool.spawn_claude(), "sA")
        for mode in ("manual", "auto"):
            project = make_project(work, mode)
            if mode == "auto":
                sdir = set_auto(project)
                sessions.write_heartbeat(sdir, util.now_ms())
            for command in deny_cases[:5]:
                code, out = call_relay("PreToolUse", project, "sA", ident_fn(a), extra=tool_call("Bash", command=command))
                require(is_denied(out), "%s 下钩子应拦 %r" % (mode, command))
    return ["原文兜底拦 %d 种写法（含 Sol 的 $( ) 两例、eval、handoffd_cli.py）；%d 种子命令位置之外的不误拦；manual / auto 钩子都拦" % (len(deny_cases), len(allow_cases))]


def case_h29():
    """H29（F8）：旁观 / blocked / B 放行 TodoWrite；Skill、Task*、Agent、EnterPlanMode 仍拒绝。"""
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = set_auto(project)
        sessions.write_heartbeat(sdir, util.now_ms())
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        call_relay("SessionStart", project, "sA", ident_fn(a), extra={"source": "startup"})
        for tool in ("TodoWrite",):
            code, out = call_relay("PreToolUse", project, "sB", ident_fn(b), extra=tool_call(tool, todos=[]))
            require(out is None, "旁观应放行 %s" % tool)
        for tool in ("Skill", "Task", "TaskCreate", "TaskUpdate", "Agent", "EnterPlanMode"):
            code, out = call_relay("PreToolUse", project, "sB", ident_fn(b), extra=tool_call(tool))
            require(is_denied(out), "旁观应拒 %s" % tool)
        bu = pool.identity(pool.spawn_claude(), "b-uuid-1")
        write_baton(sdir, holder=holder_of(a), handoff=handoff_with_b("b-uuid-1"))
        code, out = call_relay("PreToolUse", project, "b-uuid-1", ident_fn(bu), extra=tool_call("TodoWrite", todos=[]))
        require(out is None, "B 应放行 TodoWrite")
        code, out = call_relay("PreToolUse", project, "b-uuid-1", ident_fn(bu), extra=tool_call("Skill"))
        require(is_denied(out), "B 应拒 Skill")
    return ["旁观 / B 放行 TodoWrite；Skill / Task / TaskCreate / TaskUpdate / Agent / EnterPlanMode 仍拒"]



def main():
    """运行 H1–H13、H22–H27、H29。"""
    return run_cases([(name, globals()["case_" + name.lower()]) for name in ("H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10",
                                                                              "H11", "H12", "H13", "H22", "H23", "H24", "H25", "H26", "H27", "H29")])


if __name__ == "__main__":
    sys.exit(main())
