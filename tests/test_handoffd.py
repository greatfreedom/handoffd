# 测试 H14–H21：handoffd S1 的心跳、时间（换时区 / 时钟回拨）、轮次账、notify、on/off/status 命令、配置、状态目录与祖先链。
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (git, make_fake_git, make_fake_ps, make_repo, patched_env, path_with, process_pool, require, run_cases,  # noqa: E402
                     temp_dir)
from relay_helpers import (Rings, call_relay, fixed_clock, handoff_with_b, holder_of, ident_fn, read_baton, sdir_of,  # noqa: E402
                           set_auto, tool_call, tree_snapshot, write_baton)
from cdm import baton, config, handoffd_cli, lockcore, notify, procs, review, sessions, util  # noqa: E402
from cdm.notify import RecordingNotifier  # noqa: E402


def make_project(work, name="proj"):
    project = os.path.join(work, name)
    os.makedirs(project)
    return project


def cli(argv, project, sid=None, ident=None, notifier=None, kill_fn=None, cwd_fn=None):
    """进程内跑 handoffd 命令；返回 (退出码, 标准输出, 标准错误)。sid+ident 表示"在该会话里敲的"，否则是 Owner 终端。"""
    env = {"CLAUDE_PROJECT_DIR": project}
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    chain = (lambda deadline: [(ident["pid"], ident["lstart"])]) if ident else (lambda deadline: [])
    out, err = io.StringIO(), io.StringIO()
    kwargs = {"kill_fn": kill_fn} if kill_fn else {}
    if cwd_fn:
        kwargs["cwd_fn"] = cwd_fn
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = handoffd_cli.main(argv, env=env, chain_fn=chain, notifier=notifier, **kwargs)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def heartbeat_rings(rings):
    return [call for call in rings.calls if call["kind"] == "problem" and "心跳" in call["what"]]


def case_h14():
    """H14：daemon 心跳停（设计:64 行）——auto 下超 120 s 响铃一次、同段不重复、恢复后清标记可再响、文件缺失按 mode.since 起算、manual 不响。"""
    details = []
    with fixed_clock() as clock, temp_dir() as work, process_pool(work) as pool:
        me = ident_fn(pool.identity(pool.spawn_claude(), "sA"))

        def setup(name):
            project = make_project(work, name)
            return project, set_auto(project), Rings()

        def ping(project, rings, sid="sA"):
            code, out = call_relay("PreToolUse", project, sid, me, rings=rings, extra=tool_call("Read", file_path="/x"))
            require(code == 0 and out is None, "心跳检查不得阻断：%r" % out)

        project, sdir, rings = setup("a")
        sessions.write_heartbeat(sdir, clock.ms - 5000)
        ping(project, rings)
        require(rings.count() == 0, "心跳 5 秒前：不响")
        sessions.write_heartbeat(sdir, clock.ms - 120_000)
        ping(project, rings)
        require(rings.count() == 0, "心跳恰好 120 秒：不算停")
        sessions.write_heartbeat(sdir, clock.ms - 121_000)
        ping(project, rings)
        require(len(heartbeat_rings(rings)) == 1, "121 秒：应响铃")
        call = rings.calls[0]
        require(call["ask"] == "1) 重启 daemon（handoffd on） 2) 切回 manual（handoffd off）" and call["recommend"] == 1 and call["what"] and call["why"]
                and call["if_no_reply"], "四段 + 编号选项（规格:72）：%r" % call)
        notices = sessions.pending_notices(sdir)
        require(len(notices) == 1 and notices[0]["cat"] == "heartbeat", "应写 heartbeat notice")
        for _ in range(3):
            clock.advance(30)
            ping(project, rings)
        require(rings.count() == 1, "同一段停跳只响一次")
        details.append("5 s 不响、恰好 120 s 不响、121 s 响一次（四段 + 选项 1 重启 2 切回 manual）、同段不重复")
        sessions.write_heartbeat(sdir, clock.ms - 1000)
        ping(project, rings)
        require(not os.path.exists(sessions.run_path(sdir, "hb_stale")), "心跳恢复应清标记")
        clock.advance(200)
        ping(project, rings)
        require(rings.count() == 2, "恢复后再停：应再响（2 次）")
        details.append("心跳恢复清标记；再停再响")
        # 文件缺失：自 mode.since 起算 120 s
        project, sdir, rings = setup("b")
        ping(project, rings)
        require(rings.count() == 0, "mode 刚开 10 秒、无心跳文件：不响")
        clock.advance(100)
        ping(project, rings)
        require(rings.count() == 0, "110 秒：不响")
        clock.advance(15)
        ping(project, rings)
        require(rings.count() == 1, "无心跳文件、自 mode.since 起 125 秒：按停处理响铃")
        details.append("heartbeat 文件缺失：自 mode.since 起算，110 s 不响、125 s 响")
        # manual 不响、不写
        project = make_project(work, "m")
        before = tree_snapshot(project)
        rings = Rings()
        ping(project, rings)
        require(rings.count() == 0 and tree_snapshot(project) == before, "manual 下没有心跳检查")
        details.append("manual：不检查、不响、不写")
    return details


def case_h15():
    """H15（G1 · 换时区）：时间一律 epoch 毫秒 UTC——换时区不改 now_ms，也不改超时判断（同一份心跳在任何时区下判停一致）。"""
    original = os.environ.get("TZ")
    zones = ("America/New_York", "Asia/Tokyo", "Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC")
    details = []
    try:
        for zone in zones:
            os.environ["TZ"] = zone
            time.tzset()
            skew = abs(util.now_ms() - time.time() * 1000)
            require(skew < 5000, "TZ=%s：now_ms 应是 epoch 毫秒 UTC，与 time.time 偏差 %.0f ms" % (zone, skew))
        details.append("5 个时区下 now_ms 与 epoch 偏差 < 5 s")
        with temp_dir() as work, process_pool(work) as pool:
            me = ident_fn(pool.identity(pool.spawn_claude(), "sA"))
            for writer_zone, reader_zone in (("America/New_York", "Asia/Tokyo"), ("Pacific/Kiritimati", "Pacific/Pago_Pago"), ("Asia/Tokyo", "America/New_York")):
                project = make_project(work, "p-%s-%s" % (writer_zone[-5:], reader_zone[-5:]))
                sdir = sdir_of(project)
                os.environ["TZ"] = writer_zone
                time.tzset()
                sessions.write_mode(sdir, "auto", util.now_ms() - 10_000)
                sessions.write_heartbeat(sdir, util.now_ms() - 130_000)
                os.environ["TZ"] = reader_zone
                time.tzset()
                rings = Rings()
                call_relay("PreToolUse", project, "sA", me, rings=rings, extra=tool_call("Read", file_path="/x"))
                require(len(heartbeat_rings(rings)) == 1, "%s 写、%s 读：130 s 的心跳必须判停并响铃一次：%r" % (writer_zone, reader_zone, rings.calls))
                project2 = make_project(work, "f-%s-%s" % (writer_zone[-5:], reader_zone[-5:]))
                sdir2 = sdir_of(project2)
                os.environ["TZ"] = writer_zone
                time.tzset()
                sessions.write_mode(sdir2, "auto", util.now_ms() - 10_000)
                sessions.write_heartbeat(sdir2, util.now_ms() - 5_000)
                os.environ["TZ"] = reader_zone
                time.tzset()
                rings2 = Rings()
                call_relay("PreToolUse", project2, "sA", me, rings=rings2, extra=tool_call("Read", file_path="/x"))
                require(rings2.count() == 0, "%s 写、%s 读：5 s 的心跳不应判停：%r" % (writer_zone, reader_zone, rings2.calls))
        details.append("跨时区写 / 读：130 s 的心跳一律判停（响 1 次），5 s 的心跳一律不停")
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
    return details


def case_h16():
    """H16（G1 · 时钟回拨）：年龄为负或跳变 > 1 h ⇒ 计时从当前时刻重算、不响铃、不永久失效（设计:7）。"""
    details = []
    with fixed_clock() as clock, temp_dir() as work, process_pool(work) as pool:
        me = ident_fn(pool.identity(pool.spawn_claude(), "sA"))

        def ping(project, rings):
            call_relay("PreToolUse", project, "sA", me, rings=rings, extra=tool_call("Read", file_path="/x"))

        def fresh(name):
            project = make_project(work, name)
            sdir = set_auto(project)
            sessions.write_heartbeat(sdir, clock.ms - 5000)
            return project, sdir, Rings()

        start = clock.ms
        for label, jump_ms in (("回拨 5 小时", -5 * 3600_000), ("回拨 30 秒", -30_000), ("向前跳 2 小时", 2 * 3600_000)):
            clock.ms = start
            project, sdir, rings = fresh("p%d" % jump_ms)
            clock.ms = start + jump_ms
            ping(project, rings)
            require(rings.count() == 0, "%s：时钟跳变当下不得响铃：%r" % (label, rings.calls))
            # 之后 daemon 始终没有新心跳：最迟在重算起点 / 原心跳起 120 秒后响一次，且不会永久失效
            rung_after = None
            for step in range(1, 61):
                clock.advance(10)
                ping(project, rings)
                if rings.count() and rung_after is None:
                    rung_after = step * 10
            require(len(heartbeat_rings(rings)) == 1 and rung_after and rung_after >= 60,
                    "%s：daemon 仍没心跳，应在之后某刻响铃且只响一次（实际 %r 秒后，%d 次）" % (label, rung_after, rings.count()))
            details.append("%s：当下不响；%d 秒后才响一次" % (label, rung_after))
        # 回拨后 daemon 用新时钟重新写心跳 ⇒ 一切正常，不响
        clock.ms = start
        project, sdir, rings = fresh("recover")
        clock.ms = start - 3 * 3600_000
        ping(project, rings)
        sessions.write_heartbeat(sdir, clock.ms - 1000)
        clock.advance(200)
        sessions.write_heartbeat(sdir, clock.ms - 1000)
        ping(project, rings)
        require(rings.count() == 0, "回拨后 daemon 正常写心跳：不响")
        details.append("回拨后 daemon 按新时钟写心跳：不响")
    return details


def case_h17():
    """H17：复核轮次账只增不减；N 小于账上拒绝；S1 只登记不叫停；命令行的持棒者校验。"""
    details = []
    with fixed_clock() as clock, temp_dir() as work, process_pool(work) as pool:
        sdir = sdir_of(make_project(work, "r"))
        entry = review.register(sdir, "P1", 1, 2, clock.ms)
        require(entry["round"] == 1 and entry["p0"] == 2, "登记第 1 轮")
        review.register(sdir, "P1", 2, 0, clock.ms)
        for bad in (1,):
            try:
                review.register(sdir, "P1", bad, 0, clock.ms)
            except review.Refused as exc:
                require("只增不减" in str(exc), "拒绝原因应说只增不减：%s" % exc)
            else:
                require(False, "N 小于账上必须拒绝")
        require(review.register(sdir, "P1", 2, 1, clock.ms)["p0"] == 1, "同一轮次重复登记只更新 P0（S1 自定）")
        review.register(sdir, "P2", 1, 0, clock.ms)
        _status, ledger = review.read_ledger(sdir)
        require(ledger["P1"]["round"] == 2 and ledger["P2"]["round"] == 1, "产物之间独立：%r" % ledger)
        for label, args in (("轮次 0", ("P1", 0, 0)), ("轮次负", ("P1", -1, 0)), ("轮次字符串", ("P1", "2", 0)), ("轮次布尔", ("P1", True, 0)),
                            ("P0 负", ("P1", 3, -1)), ("产物含空格", ("a b", 1, 0)), ("产物空", ("", 1, 0))):
            try:
                review.register(sdir, *args, clock.ms)
            except review.Refused:
                pass
            else:
                require(False, "%s 应被拒" % label)
        require(review.read_ledger(sdir)[1]["P1"]["round"] == 2, "被拒的登记不得改账")
        review.register(sdir, "P3", 3, 4, clock.ms)
        require(not os.path.exists(os.path.join(sdir, "notices.jsonl")), "S1 只登记：round≥3 ∧ p0>0 的自动叫停在 S2，这里不写 notice")
        details.append("只增不减（2→1 拒）、同轮更新 P0、产物独立、7 种坏参数被拒且不改账、round=3 p0=4 只登记不叫停")
        # 并发：乱序登记 1..8，终值 8，且账上的轮次从不下降
        sdir2 = sdir_of(make_project(work, "conc"))
        outcomes = []

        def worker(number):
            try:
                review.register(sdir2, "C", number, 0, clock.ms)
                outcomes.append("ok")
            except review.Refused:
                outcomes.append("refused")

        threads = [threading.Thread(target=worker, args=(n,)) for n in (5, 1, 8, 3, 7, 2, 6, 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        require(review.read_ledger(sdir2)[1]["C"]["round"] == 8 and len(outcomes) == 8, "并发登记终值应为 8：%r" % outcomes)
        details.append("8 线程乱序并发：终值 8")
        # 账损坏：拒绝且不覆盖
        with open(review.review_path(sdir2), "w", encoding="utf-8") as stream:
            stream.write("{broken")
        try:
            review.register(sdir2, "C", 9, 0, clock.ms)
        except review.Refused:
            pass
        else:
            require(False, "账读不出必须拒绝")
        require(open(review.review_path(sdir2), encoding="utf-8").read() == "{broken", "账损坏时不得覆盖")
        details.append("账损坏：拒绝且不覆盖")
        # 命令行
        project = make_project(work, "cli")
        sdir3 = set_auto(project)  # manual 下不登记（F7，见 H28），这里测 auto
        a = pool.identity(pool.spawn_claude(), "sA")
        b = pool.identity(pool.spawn_claude(), "sB")
        code, out, err = cli(["review-round", "--product", "X", "--round", "2", "--p0", "1"], project)
        require(code == 0 and "第 2 轮" in out, "Owner 终端登记：%r %r" % (code, err))
        code, out, err = cli(["review-round", "--product", "X", "--round", "1", "--p0", "0"], project)
        require(code == 3 and "只增不减" in err, "回退应退出 3：%r %r" % (code, err))
        code, out, err = cli(["review-round", "--product", "X", "--round", "x", "--p0", "0"], project)
        require(code == 2, "非整数参数由 argparse 拒绝（退出 2）：%r" % (code,))
        write_baton(sdir3, holder=holder_of(a))
        code, out, err = cli(["review-round", "--product", "X", "--round", "3", "--p0", "0"], project, sid="sB", ident=b)
        require(code == 3 and "持棒" in err, "auto 下旁观会话不能登记：%r %r" % (code, err))
        code, out, err = cli(["review-round", "--product", "X", "--round", "3", "--p0", "0"], project, sid="sA", ident=a)
        require(code == 0, "auto 下 holder 可登记：%r %r" % (code, err))
        details.append("命令行：Owner 终端可登记；回退退出 3；auto 下旁观拒绝、holder 通过")
    return details


def case_h18():
    """H18：format_notice 四段 + 编号选项；notify：auto 写 notices.jsonl 并响铃、manual 只响铃不写；ring 按参数发声；spawn_ring 起分离子进程。"""
    details = []
    text = notify.format_notice("A 崩了", "B 没上线", "1) 放弃交接 2) 去两个窗口看", 1, "交接一直停着")
    labels = ["发生了什么：", "为什么停：", "要你做什么：", "不回复会怎样："]
    positions = [text.index(label) for label in labels]
    require(positions == sorted(positions), "四段顺序不对：%r" % text)
    require("  1) 放弃交接（推荐）" in text and "  2) 去两个窗口看\n" in text, "选项应带编号且只标推荐项：%r" % text)
    for label, args in (("无编号", ("a", "b", "去看", 1, "c")), ("编号不从 1", ("a", "b", "2) x 3) y", 2, "c")), ("编号不连续", ("a", "b", "1) x 3) y", 1, "c")),
                        ("推荐不在选项里", ("a", "b", "1) x 2) y", 3, "c")), ("空段", ("", "b", "1) x", 1, "c")), ("推荐布尔", ("a", "b", "1) x", True, "c"))):
        try:
            notify.format_notice(*args)
        except ValueError:
            pass
        else:
            require(False, "format_notice 应拒绝：%s" % label)
    require(notify.parse_options(["甲", "乙"]) == [(1, "甲"), (2, "乙")] and notify.parse_options("1. a 2. b") == [(1, "a"), (2, "b")]
            and notify.parse_options("1) 等 2.5 分钟 2) z")[0] == (1, "等 2.5 分钟"), "选项解析")
    details.append("四段顺序、编号、推荐标记；6 种不合格入参被拒；选项解析含列表 / 1. 写法 / 文字里的小数")
    with temp_dir() as work:
        project = make_project(work)
        sdir = set_auto(project)
        rec = RecordingNotifier()
        argv = ["notify", "--cat", "incident", "--what", "出事了", "--why", "测试红了", "--ask", "1) 去看 2) 回滚", "--recommend", "1",
                "--if-no-reply", "一直等"]
        code, out, err = cli(argv, project, notifier=rec)
        require(code == 0 and len(rec.calls) == 1 and rec.calls[0][0] == "problem", "auto：应响铃一次（problem 类）：%r %r" % (code, rec.calls))
        require(all(label in rec.calls[0][1] for label in labels) and rec.calls[0][2] == "出事了", "响铃文案应是四段，语音文本=发生了什么")
        lines = [json.loads(line) for line in open(os.path.join(sdir, "notices.jsonl"), encoding="utf-8")]
        require(len(lines) == 1 and lines[0]["cat"] == "incident" and lines[0]["ring"] and lines[0]["options"] == ["去看", "回滚"]
                and lines[0]["text"] == rec.calls[0][1], "auto：应写 notices.jsonl：%r" % lines)
        code, out, err = cli(["status"], project)
        require("待处理的铃：1 条" in out and "incident" in out, "status 应列出待处理的铃：%r" % out)
        bad_ask = list(argv)
        bad_ask[argv.index("--ask") + 1] = "去看"
        rec2 = RecordingNotifier()
        code, out, err = cli(bad_ask, project, notifier=rec2)
        require(code == 2 and rec2.calls == [] and len(open(os.path.join(sdir, "notices.jsonl")).readlines()) == 1, "选项无编号：拒绝、不响、不写：%r" % (code,))
        code, out, err = cli(["notify", "--cat", "bogus"] + argv[3:], project, notifier=rec2)
        require(code == 2 and rec2.calls == [], "未知类别由 argparse 拒绝")
        code, out, err = cli(argv[:argv.index("--recommend") + 1] + ["9"] + argv[argv.index("--recommend") + 2:], project, notifier=rec2)
        require(code == 2 and rec2.calls == [], "推荐项不在选项里：拒绝")
        details.append("auto：响铃 + 写 notice（status 可见）；无编号 / 未知类别 / 推荐越界：拒绝、不响、不写")
        project_m = make_project(work, "manual")
        before = tree_snapshot(project_m)
        rec3 = RecordingNotifier()
        code, out, err = cli(argv, project_m, notifier=rec3)
        require(code == 0 and len(rec3.calls) == 1 and tree_snapshot(project_m) == before, "manual：响铃但不写任何文件：%r" % (code,))
        details.append("manual：响铃、零写入")
        # ring：按参数发声，不读记录
        rec4 = RecordingNotifier()
        code, out, err = cli(["ring", "--project", project_m, "--kind", "waiting", "--what", "w", "--why", "y", "--ask", "1) 去看", "--recommend", "1",
                              "--if-no-reply", "n"], project_m, notifier=rec4)
        require(code == 0 and rec4.calls[0][0] == "waiting" and "发生了什么：w" in rec4.calls[0][1] and rec4.calls[0][2] == "w" and tree_snapshot(project_m) == before,
                "ring：按参数发声且零写入：%r" % rec4.calls)
        code, out, err = cli(["ring", "--project", project_m, "--kind", "nope", "--what", "w", "--why", "y", "--ask", "1) a", "--recommend", "1", "--if-no-reply", "n"], project_m)
        require(code == 2, "ring 未知 kind 拒绝")
        details.append("ring：按参数发声、零写入、未知 kind 拒绝")
        # notify.ring 走真实 Notifier 链路（runner 替身，不出声）
        calls = []

        class Done:
            returncode = 0

        def runner(args, **kwargs):
            calls.append(args)
            return Done()

        notify.ring(config.default_config(project)["alerts"], "waiting", "msg", "语音", runner=runner)
        require(any(args[0] == notify.OSASCRIPT for args in calls) and any(args[0] == notify.AFPLAY and args[1] == "/System/Library/Sounds/Glass.aiff" for args in calls),
                "ring 应发横幅与 waiting 铃声：%r" % calls)
        # spawn_ring：分离子进程
        captured = {}
        original = subprocess.Popen

        def fake_popen(args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs

        subprocess.Popen = fake_popen
        try:
            sessions.spawn_ring("/p", "waiting", "w", "y", "1) 去看", 1, "n")
        finally:
            subprocess.Popen = original
        require(captured["args"][:2] == [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "handoffd")]
                and "ring" in captured["args"] and "--kind" in captured["args"] and captured["kwargs"]["start_new_session"] is True
                and captured["kwargs"]["stdout"] == subprocess.DEVNULL, "spawn_ring 应起分离的 handoffd ring：%r" % captured)
        details.append("notify.ring 走横幅 + waiting 铃声；spawn_ring 起分离（start_new_session）的 handoffd ring 子进程")
    return details


def case_h19():
    """H19：on / off / status——on 做 mode=auto + 认领并拉起 daemon（假 launchctl；详见 test_s3.py）；off 放弃交接、核身份后才 SIGTERM B、回 manual；status 只读。"""
    details = []
    with temp_dir() as work, process_pool(work) as pool:
        project = make_project(work)
        sdir = sdir_of(project)
        before = tree_snapshot(project)
        code, out, err = cli(["status"], project)
        require(code == 0 and "模式：manual" in out and tree_snapshot(project) == before, "manual 下 status：只读、不建目录：%r" % out)
        code, out, err = cli(["off"], project)
        require(code == 0 and tree_snapshot(project) == before, "从未开启过：off 不写任何东西")
        details.append("从未开启：status / off 只读")
        with fixed_clock() as clock:
            opened = clock.ms
            code, out, err = cli(["on"], project)
            require(code == 0 and "模式：auto" in out and "daemon：已拉起" in out and "当前无主控" in out, "Owner 终端 on：%r" % out)
            require(sessions.read_mode(sdir) == ("auto", opened) and not os.path.exists(baton.baton_path(sdir)), "终端 on：mode=auto、不认领")
            clock.advance(50)
            code, out, err = cli(["on"], project)
            require(sessions.read_mode(sdir)[1] == opened, "重复 on 不改 since")
            a = pool.identity(pool.spawn_claude(), "sA")
            b = pool.identity(pool.spawn_claude(), "sB")
            code, out, err = cli(["on"], project, sid="sA", ident=a)
            require("本会话（sA）" in out and read_baton(sdir)["holder"]["sid"] == "sA", "在会话里 on：该会话=holder：%r" % out)
            code, out, err = cli(["on"], project, sid="sB", ident=b)
            require("旁观" in out and read_baton(sdir)["holder"]["sid"] == "sA", "别的会话再 on：旁观，不抢：%r" % out)
            details.append("on：终端=vacant、会话内=holder、第二会话不抢；on 报告 daemon 已拉起；重复 on 不重置 since")
            snapshot = tree_snapshot(project)
            code, out, err = cli(["status"], project)
            require(code == 0 and "模式：auto" in out and "存活" in out and "无心跳" in out and "交接：无" in out and tree_snapshot(project) == snapshot,
                    "auto status：%r" % out)
            sessions.write_heartbeat(sdir, clock.ms - 3000)
            snapshot = tree_snapshot(project)
            code, out, err = cli(["status"], project)
            require("心跳 3 秒前（正常）" in out and tree_snapshot(project) == snapshot, "心跳正常：%r" % out)
            sessions.write_heartbeat(sdir, clock.ms - 200_000)
            code, out, err = cli(["status"], project)
            require("已停" in out, "心跳已停：%r" % out)
            details.append("status：模式 / 心跳（无 / 正常 / 已停）/ 持棒者存活 / 交接；全程只读")
            # off：放弃交接并请 B 退出
            a_proc = pool.spawn_claude()
            a2 = pool.identity(a_proc, "sA2")
            b_proc = pool.spawn_claude()
            b_rec = pool.identity(b_proc, "b-uuid-1")
            handoff = handoff_with_b("b-uuid-1", phase="committing")
            handoff["b"].update(pid=b_rec["pid"], lstart=b_rec["lstart"], online_ms=1)
            write_baton(sdir, holder=holder_of(a2), handoff=handoff)
            code, out, err = cli(["status"], project)
            require("交接：h-1 阶段 committing" in out, "status 应显示交接：%r" % out)
            # S2a：off 的 SIGTERM 与 daemon 同一套 J2② ——拿不到会话文件 / cwd ⇒ 不发信号、记 events（交接仍放弃）
            fake_sessions = os.path.join(work, "no-sessions")
            os.makedirs(fake_sessions)
            with patched_env(CDM_CLAUDE_SESSIONS_DIR=fake_sessions):
                code, out, err = cli(["off"], project)
            require(code == 0 and "已放弃进行中的交接 h-1" in out and "没有向 B" in err and b_proc.poll() is None, "没有会话文件 / cwd：不发信号：%r %r" % (out, err))
            refused = [json.loads(line) for line in open(os.path.join(sdir, "events.jsonl"), encoding="utf-8") if "off:kill_b_refused" in line]
            require(refused and refused[-1]["reason"] == "cwd_unknown" and refused[-1]["pid"] == b_rec["pid"], "记 events：off:kill_b_refused cwd_unknown：%r" % refused)
            require(read_baton(sdir)["handoff"] is None, "放弃交接本身照常完成")
            details.append("off：B 的会话文件 / cwd 取不到 ⇒ 不发 SIGTERM、记 events（交接照样放弃）")
            sessions.write_mode(sdir, "auto", clock.ms)
            write_baton(sdir, holder=holder_of(a2), handoff=handoff)
            code, out, err = cli(["off"], project, cwd_fn=lambda pid: os.path.dirname(project))
            require(code == 0 and "没有向 B" in err and b_proc.poll() is None, "cwd 在项目外（含项目的上一级）：不发信号：%r %r" % (out, err))
            sessions.write_mode(sdir, "auto", clock.ms)
            write_baton(sdir, holder=holder_of(a2), handoff=handoff)
            code, out, err = cli(["off"], project, cwd_fn=lambda pid: os.path.join(project, "sub"))
            require(code == 0 and "已放弃进行中的交接 h-1" in out and "模式：manual" in out, "off 输出：%r %r" % (out, err))
            try:
                b_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                require(False, "未接棒的 B 应被 SIGTERM")
            require(b_proc.returncode == -15, "B 应死于 SIGTERM：%r" % b_proc.returncode)
            state = read_baton(sdir)
            require(state["handoff"] is None and state["holder"]["sid"] == "sA2", "off 清 handoff、不动 holder：%r" % state)
            record = [json.loads(line) for line in open(os.path.join(sdir, "handoffs.jsonl"), encoding="utf-8")][-1]
            require(record["phase"] == "abandoned" and record["abandoned_by"] == "off" and record["id"] == "h-1", "handoffs.jsonl 应记 abandoned：%r" % record)
            require(sessions.read_mode(sdir)[0] == "manual", "off 后 mode=manual")
            code, out, err = cli(["off"], project)
            require(code == 0 and "没有进行中的交接" in out, "off 幂等")
            details.append("off：handoff→abandoned（handoffs.jsonl）、mode→manual、B 被 SIGTERM、holder 不动；重复 off 幂等")
            # 核身份：B 的 lstart 对不上（pid 复用）⇒ 不杀
            innocent = pool.spawn_claude()
            handoff = handoff_with_b("b-uuid-2")
            handoff["b"].update(pid=innocent.pid, lstart="Mon Jan  1 00:00:00 2001", online_ms=1)
            sessions.write_mode(sdir, "auto", clock.ms)
            write_baton(sdir, holder=holder_of(a2), handoff=handoff)
            code, out, err = cli(["off"], project)
            require(code == 0 and innocent.poll() is None and "已不在运行" in out, "身份对不上不得动别的进程：%r" % out)
            # B 已死
            dead = pool.spawn_claude()
            dead_rec = pool.identity(dead, "b-uuid-3")
            pool.kill(dead)
            handoff = handoff_with_b("b-uuid-3")
            handoff["b"].update(pid=dead_rec["pid"], lstart=dead_rec["lstart"], online_ms=1)
            write_baton(sdir, holder=holder_of(a2), handoff=handoff)
            code, out, err = cli(["off"], project)
            require(code == 0 and "已不在运行" in out, "B 已死：%r" % out)
            # 登记册读不出：off 不改模式
            sessions.write_mode(sdir, "auto", clock.ms)
            with open(baton.baton_path(sdir), "w", encoding="utf-8") as stream:
                stream.write("{bad")
            code, out, err = cli(["off"], project)
            require(code == 1 and sessions.read_mode(sdir)[0] == "auto", "登记册读不出：off 失败且不改 mode")
            code, out, err = cli(["status"], project)
            require(code == 0 and "读不出" in out, "status 容忍登记册损坏")
            details.append("off：B 的 pid+lstart 对不上不杀；B 已死只说明；登记册损坏时失败且 mode 不变")
    return details


def case_h20():
    """H20：配置——新键与默认值（65/76、30/5 分钟、120 秒、qa_limit、退避、b_verify_commands）、校验、handoffd.json 读取与退回默认。"""
    with temp_dir() as work:
        cfg = config.default_config(work)
        require(cfg["nudge_thresholds"] == [65.0, 76.0] and cfg["idle_ask_minutes"] == 30 and cfg["ask_reply_minutes"] == 5
                and cfg["heartbeat_stale_seconds"] == 120 and cfg["qa_limit"] == 12 and cfg["api_backoff_minutes"] == [3, 60]
                and cfg["b_verify_commands"] == [] and cfg["state_dir_set"] is False, "默认值：%r" % {k: cfg[k] for k in ("nudge_thresholds", "qa_limit")})
        require(config.build_config({"project_dir": work, "state_dir": os.path.join(work, "s")})["state_dir_set"] is True, "显式 state_dir 应标记 set")
        for label, raw in (("qa_limit 0", {"qa_limit": 0}), ("qa_limit 布尔", {"qa_limit": True}), ("退避起点大于封顶", {"api_backoff_minutes": [60, 3]}),
                           ("退避长度 3", {"api_backoff_minutes": [1, 2, 3]}), ("白名单含数字", {"b_verify_commands": [1]}),
                           ("白名单含空串", {"b_verify_commands": [""]}), ("心跳负数", {"heartbeat_stale_seconds": -1}),
                           ("询问 0", {"idle_ask_minutes": 0}), ("未知键", {"bogus": 1}),
                           ("已删除的 opening_prompt_template（S2a：B 首条 = kit 开场指令原样）", {"opening_prompt_template": "x"})):
            try:
                config.build_config(dict(raw, project_dir=work))
            except config.ConfigError:
                pass
            else:
                require(False, "应拒绝：%s" % label)
        os.makedirs(os.path.join(work, ".claude"))
        path = os.path.join(work, ".claude", "handoffd.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"project_dir": work, "b_verify_commands": ["echo ok"], "qa_limit": 5}, stream)
        cfg, error = config.load_handoffd(work)
        require(error is None and cfg["b_verify_commands"] == ["echo ok"] and cfg["qa_limit"] == 5, "应读 .claude/handoffd.json：%r" % error)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("{bad")
        cfg, error = config.load_handoffd(work)
        require(error and cfg["b_verify_commands"] == [] and cfg["nudge_thresholds"] == [65.0, 76.0], "坏配置退回默认并返回错误说明")
        os.unlink(path)
        cfg, error = config.load_handoffd(work)
        require(error is None and cfg["qa_limit"] == 12, "缺失退回默认")
        # 命令行自动找配置：从子目录向上
        sub = os.path.join(work, "a", "b")
        os.makedirs(sub)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"project_dir": work, "qa_limit": 7}, stream)
        found = handoffd_cli.find_project_config(type("A", (), {"config": None})(), {"CLAUDE_PROJECT_DIR": sub})
        require(found["qa_limit"] == 7 and found["project_dir"] == work, "命令行应从子目录向上找到 .claude/handoffd.json：%r" % (found["project_dir"],))
    return ["默认值 65/76、30/5 分钟、120 秒、qa 12、退避 [3,60]；9 种坏值被拒；handoffd.json 读取 / 坏文件退回默认 / 命令行向上找配置"]


def case_h21():
    """H21：状态目录 <S> 的取法（非 git / git / worktree / git 出错）与祖先链取法（真实嵌套进程）。"""
    details = []
    with temp_dir() as work:
        plain = make_project(work, "plain")
        require(lockcore.state_dir(plain, procs.Deadline(5)) == ("ok", os.path.join(plain, ".claude", "handoffd")), "非 git：<项目>/.claude/handoffd")
        repo = os.path.join(work, "repo")
        make_repo(repo)
        expected = os.path.join(repo, ".git", "handoffd")
        worktree = os.path.join(work, "wt")
        git("worktree", "add", "-q", worktree, "-b", "wt-branch", cwd=repo)
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        for proj in (repo, worktree, sub):
            require(lockcore.state_dir(proj, procs.Deadline(5)) == ("ok", expected), "%s 应共用 git 公共目录下的 handoffd" % proj)
        for mode in ("exit128", "empty", "hang"):
            bin_dir = os.path.join(work, "fg-" + mode)
            make_fake_git(bin_dir, mode)
            with patched_env(PATH=path_with(bin_dir)):
                status, reason = lockcore.state_dir(repo, procs.Deadline(1.0))
            require(status == "error", "git %s：必须 error，不退回本地目录：%r" % (mode, (status, reason)))
        with patched_env(GIT_DIR=os.path.join(work, "nope")):
            require(lockcore.state_dir(plain, procs.Deadline(5))[0] == "error", "GIT_DIR 无效：error")
        details.append("非 git / 主检出 / 子目录 / worktree 取法；git 退出 128 / 空输出 / 挂起 / GIT_DIR 无效都 error")
        # 祖先链：真实嵌套进程  outer(claude) → inner(claude) → python
        outer_dir, inner_dir = os.path.join(work, "outer"), os.path.join(work, "inner")
        os.makedirs(outer_dir)
        os.makedirs(inner_dir)
        os.symlink("/bin/bash", os.path.join(outer_dir, "claude"))
        os.symlink("/bin/bash", os.path.join(inner_dir, "claude"))
        mvp = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = ("import json, os, sys; sys.path.insert(0, %r); sys.dont_write_bytecode = True\n"
                  "from cdm import lockcore, procs\n"
                  "print(json.dumps({'parent': os.getppid(), 'chain': lockcore.claude_chain(procs.Deadline(10))}))\n") % mvp
        script_path = os.path.join(work, "chain.py")
        with open(script_path, "w", encoding="utf-8") as stream:
            stream.write(script)
        inner_cmd = "%s -c '%s %s; true'; true" % (os.path.join(inner_dir, "claude"), sys.executable, script_path)
        outer = subprocess.Popen([os.path.join(outer_dir, "claude"), "-c", inner_cmd], stdout=subprocess.PIPE, text=True)
        try:
            stdout, _ = outer.communicate(timeout=30)
        finally:
            if outer.poll() is None:
                outer.kill()
        data = json.loads(stdout.strip().splitlines()[-1])
        chain = data["chain"]
        require(len(chain) >= 2 and chain[0][0] == data["parent"] and chain[1][0] == outer.pid, "链应由近及远 [inner, outer, …]：%r（outer=%d）" % (chain, outer.pid))
        status, lstart = procs.ps_field(outer.pid, "lstart", procs.Deadline(5))
        details.append("真实嵌套进程：claude_chain 返回 [inner, outer, …] 且 pid 对得上（lstart 来自 ps：%s）" % ("可取" if status == "ok" else "outer 已退出"))
        bin_dir = os.path.join(work, "fakeps")
        make_fake_ps(bin_dir)
        with patched_env(PATH=path_with(bin_dir), FAKE_PS_PID="ANY", FAKE_PS_MODE="exit1"):
            require(lockcore.claude_chain(procs.Deadline(2.0)) == [] and lockcore.my_identity(procs.Deadline(2.0))[0] is None, "ps 全出错：链为空、身份取不到")
        details.append("ps 出错：链为空，不抛异常")
    return details


def case_h28():
    """H28（F7）：manual 下 review-round 不写盘（规格:85），只说明并退出 0；auto 下才登记。"""
    with temp_dir() as work:
        project = make_project(work)
        before = tree_snapshot(project)
        code, out, err = cli(["review-round", "--product", "X", "--round", "1", "--p0", "0"], project)
        require(code == 0 and "manual 模式不登记" in out and tree_snapshot(project) == before, "从未开启：不建目录、不写账：%r %r" % (code, out))
        sdir = sdir_of(project)
        sessions.write_mode(sdir, "manual", 1)
        before = tree_snapshot(project)
        code, out, err = cli(["review-round", "--product", "X", "--round", "1", "--p0", "0"], project)
        require(code == 0 and "manual 模式不登记" in out and tree_snapshot(project) == before and not os.path.exists(review.review_path(sdir)),
                "mode=manual：不写 review.json：%r" % out)
        code, out, err = cli(["review-round", "--product", "X", "--round", "0", "--p0", "0"], project)
        require(code == 0 and tree_snapshot(project) == before, "manual 下连参数校验也不写")
        set_auto(project)
        code, out, err = cli(["review-round", "--product", "X", "--round", "1", "--p0", "0"], project)
        require(code == 0 and "已登记" in out and review.read_ledger(sdir)[1]["X"]["round"] == 1, "auto 下登记")
    return ["manual：不建目录 / 不写 review.json，退出 0 并说明；auto 才登记"]



def case_h107():
    """H107（误报回归，2026-10-06 真实项目实测）：hook 先取 now、daemon 随后写心跳 ⇒ 年龄为几毫秒的负数。
    这不是时钟回拨：不得沿用旧的 hb_rebase 起点去算「停跳 N 秒」并响铃；心跳正常时旧 hb_rebase 必须清掉。"""
    details = []
    with fixed_clock() as clock, temp_dir() as work, process_pool(work) as pool:
        me = ident_fn(pool.identity(pool.spawn_claude(), "sA"))
        project = make_project(work, "race")
        sdir = set_auto(project)
        rings = Rings()

        def ping():
            call_relay("PreToolUse", project, "sA", me, rings=rings, extra=tool_call("Read", file_path="/x"))

        # 30 分钟前留下过一次 hb_rebase（当时也是同样的竞态）；此后心跳一直正常
        rebase_path = sessions.run_path(sdir, "hb_rebase")
        os.makedirs(os.path.dirname(rebase_path), exist_ok=True)
        util.atomic_write_text(rebase_path, str(clock.ms - 1800_000))
        for offset in (5, 50, 900):          # daemon 在 hook 取 now 之后几毫秒写心跳
            sessions.write_heartbeat(sdir, clock.ms + offset)
            ping()
            require(rings.count() == 0, "心跳比 hook 的 now 晚 %d ms：不得响铃：%r" % (offset, rings.calls))
            clock.advance(2)
        details.append("心跳晚于 hook now 5 / 50 / 900 ms + 旧 hb_rebase 30 min：不响")
        sessions.write_heartbeat(sdir, clock.ms - 1000)
        ping()
        require(not os.path.exists(rebase_path), "心跳正常时应清掉旧 hb_rebase")
        details.append("心跳正常一次即清掉 hb_rebase")
    return details


def main():
    """运行 H14–H21、H28、H107。"""
    return run_cases([("H14", case_h14), ("H15", case_h15), ("H16", case_h16), ("H17", case_h17), ("H18", case_h18), ("H19", case_h19),
                      ("H20", case_h20), ("H21", case_h21), ("H28", case_h28), ("H107", case_h107)])


if __name__ == "__main__":
    sys.exit(main())
