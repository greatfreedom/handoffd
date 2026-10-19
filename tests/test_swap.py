# 测试 H38–H41、H45–H47：handoffd S2a「换棒」的场景验收（假世界 + 可控时钟；不开窗、不发信号、不向真实会话投递）。
#   H38 msg.py 真 unix socket（假会话目录）  H39 seal 抽取与 request / seal / sign / adopt / reply / off 的命令语义
#   H40 正常交接全程（OPEN 原文一次、双签、Stop 数组空门、commit、SIGTERM A、BATON、归档）
#   H41 设计:102 的 S2 场景变体（在途不换 / 单签不换 / A 签后崩 / 封存未签崩 / 未封存崩 / seal 先于 B 上线 / off / 76% 代发）
#   H45 J4 响铃完备（本片每种情形一条 notice + 一次响铃，:64 与 G5 只无声）  H46 G8、G10  H47 B 的角色说明与上线接线
# 穷举 / 崩溃点 / 交错见 test_swap_exhaustive.py（H42–H44）。
import json
import os
import socket
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import patched_env, require, run_cases, temp_dir  # noqa: E402
from swap_helpers import (KIT_TEXT, OPENING, b_comes_online, full_handoff_until_sealed, scenario, start_handoff)  # noqa: E402
from cdm import baton, daemon as daemon_module, msg, sessions, util  # noqa: E402


def case_h38():
    """H38：msg.py——真 unix socket（假会话目录）：线协议一行 JSON、OPEN 原文无前缀、其余 `[handoffd] TYPE`、失败路径返回 False（确定没发出）。"""
    details = []
    with temp_dir() as work:
        sock_path = os.path.join(work, "s.sock")
        require(len(sock_path) < 100, "socket 路径过长：%d" % len(sock_path))
        received = []
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(4)
        server.settimeout(5)

        def serve():
            for _ in range(2):
                try:
                    conn, _addr = server.accept()
                except OSError:
                    return
                data = b""
                conn.settimeout(3)
                try:
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                received.append(data)
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        sessions_dir = os.path.join(work, "sessions")
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "4242.json"), "w", encoding="utf-8") as stream:
            json.dump({"pid": 4242, "sessionId": "s", "cwd": "/proj/sub", "messagingSocketPath": sock_path}, stream)
        with patched_env(CDM_CLAUDE_SESSIONS_DIR=sessions_dir):
            require(msg.sessions_dir() == sessions_dir, "CDM_CLAUDE_SESSIONS_DIR 应生效")
            ok, detail = msg.send(4242, OPENING)
            require(ok and detail == "", "投递失败：%r" % ((ok, detail),))
            body = msg.format_message("OPEN", OPENING)
            require(body == OPENING and not body.startswith("[handoffd]"), "OPEN 无前缀")
            ok2, _ = msg.send(4242, msg.format_message("BATON", "你已是主控"))
            require(ok2, "BATON 投递失败")
            thread.join(5)
            require(len(received) == 2, "服务端应收到 2 行：%r" % received)
            first = json.loads(received[0].decode("utf-8"))
            second = json.loads(received[1].decode("utf-8"))
            require(first == {"type": "user", "message": {"role": "user", "content": OPENING}}, "OPEN 线格式 / 内容不对：%r" % first)
            require(received[0].endswith(b"\n") and received[0].count(b"\n") == 1, "一行一条")
            require(second["message"]["content"] == "[handoffd] BATON 你已是主控", "BATON 要带 [handoffd] 前缀：%r" % second)
            details.append("OPEN 原文（含换行 / 中文 / 小节）逐字到达、无前缀；BATON 带 `[handoffd] BATON`；一行一条")
            require(msg.session_cwd(4242) == "/proj/sub", "session_cwd 应读 session 文件里的 cwd")
            require(msg.session_cwd(1) is None and msg.session_cwd(True) is None and msg.session_cwd(999999) is None, "非法 / 缺失 pid ⇒ None")
            require(msg.send(999999, "x") == (False, "no_socket"), "没有会话文件：no_socket")
            require(msg.send(4242, "") == (False, "empty_content"), "空内容不发")
            with open(os.path.join(sessions_dir, "4343.json"), "w", encoding="utf-8") as stream:
                json.dump({"pid": 4343, "messagingSocketPath": os.path.join(work, "nobody.sock")}, stream)
            ok3, why = msg.send(4343, "x")
            require(ok3 is False and why, "连不上 socket：False + 原因：%r" % why)
            with open(os.path.join(sessions_dir, "4444.json"), "w", encoding="utf-8") as stream:
                stream.write("{bad")
            require(msg.send(4444, "x") == (False, "no_socket") and msg.session_cwd(4444) is None, "坏会话文件 ⇒ 失败，不抛")
            details.append("session_cwd 读 cwd；缺文件 / 坏文件 / 非法 pid / 空内容 / 连不上 socket ⇒ (False, 原因)，不抛异常")
        server.close()
    return details


def case_h39():
    """H39：seal 的开场指令抽取 + request / seal / sign / adopt / reply / off 的命令语义与拒绝（角色、阶段、前提）。"""
    details = []
    ex = baton.extract_opening
    require(ex(KIT_TEXT) == OPENING, "标准 kit 抽取（含更深一级小节，止于同级标题）：%r" % ex(KIT_TEXT))
    require(ex("# t\n## 给新 session 的开场指令\n\n\n只有一行  \n\n\n## x\ny") == "只有一行  ", "首尾空行去掉、行内尾空白保留")
    require(ex("## 给新 session 的开场指令\nA\n# 一级标题\nB") == "A", "遇更高级标题即止")
    require(ex("### 5. 给新 session 的开场指令（必读）\n内容\n## 下一节") == "内容" and ex("## 其它\nx") is None, "标题行含该短语即可；缺节 ⇒ None")
    require(ex("## 给新 session 的开场指令\n\n  \n## 下一节") is None, "节空 ⇒ None")
    require(ex("正文里提到 给新 session 的开场指令 但不是标题\n## 别的\nx") is None, "只认标题行")
    details.append("extract_opening：逐字抽取 / 同级止 / 高级标题止 / 缺节与节空 ⇒ None / 只认标题行")
    with scenario() as sc:
        a = sc.spawn("sA", name="a-name")
        x = sc.spawn("sX")
        dm = sc.daemon()
        kit = sc.write_kit()
        sc.hook("SessionStart", a, source="startup")
        # 前提：manual / 非会话 / 非 holder
        code, out, err = sc.cli(["request"], x)
        require(code == 3 and "只有持棒的主控" in err, "旁观者不能 request：%r %s" % (code, err))
        code, out, err = sc.cli(["request"], None)
        require(code == 3 and "会话" in err, "Owner 终端（非会话）不能 request：%r %s" % (code, err))
        code, out, err = sc.cli(["seal", "--kit", kit], a)
        require(code == 3 and "没有进行中的交接" in err, "没有交接时 seal 被拒：%r %s" % (code, err))
        code, out, err = sc.cli(["sign", "out"], a)
        require(code == 3 and "没有进行中的交接" in err, "没有交接时 sign 被拒")
        # request 成功
        code, out, err = sc.cli(["request", "--reason", "ctx65"], a)
        require(code == 0, "request 失败：%s" % err)
        handoff = sc.handoff()
        require(handoff["phase"] == "arming" and handoff["reason"] == "ctx65" and handoff["a"]["sid"] == "sA" and handoff["a"]["name"] == "a-name"
                and handoff["b"]["uuid"] in out and handoff["b"]["name"] in out and "handoffd seal" in out and "handoffd sign out" in out
                and handoff["b"]["pid"] is None and handoff["kit"] is None, "request 的 handoff / 输出不对：%r %s" % (handoff, out))
        code, out, err = sc.cli(["request"], a)
        require(code == 3 and "已有进行中的交接" in err, "重复 request 被拒")
        code, out, err = sc.cli(["request"], x)
        require(code == 3, "旁观者仍不能 request")
        details.append("request：holder 才能；Owner 终端 / 旁观 / 重复都拒；输出带 B 的会话名、sid 与后续步骤")
        # seal：非 holder / 缺节 / 坏路径
        require(sc.cli(["seal", "--kit", kit], x)[0] == 3, "旁观者不能 seal")
        bad = sc.write_kit("# 没有开场指令\n", "bad.md")
        code, out, err = sc.cli(["seal", "--kit", bad], a)
        require(code == 2 and "开场指令" in err and sc.handoff()["kit"] is None, "缺节 ⇒ 拒绝且不落盘：%r %s" % (code, err))
        require(sc.cli(["seal", "--kit", os.path.join(sc.work, "nope.md")], a)[0] == 2, "kit 读不了 ⇒ 2")
        # sign 前提
        code, out, err = sc.cli(["sign", "out"], a)
        require(code == 3 and "kit 还没封存" in err, "kit 未封存不能 sign out：%r %s" % (code, err))
        code, out, err = sc.cli(["seal", "--kit", kit], a)
        require(code == 0, "seal 失败：%s" % err)
        kit_state = sc.handoff()["kit"]
        require(kit_state["opening"] == OPENING and len(kit_state["sha"]) == 64 and os.path.exists(os.path.join(sc.sdir, kit_state["path"]))
                and open(os.path.join(sc.sdir, kit_state["path"]), encoding="utf-8").read() == KIT_TEXT, "seal 记录 / 存档不对：%r" % kit_state)
        details.append("seal：holder 才能；缺节 / 读不了被拒；存档到 kits/<id>.md，记 sha 与开场指令原文")
        require(sc.cli(["sign", "in"], a)[0] == 3, "A 不能 sign in")
        code, out, err = sc.cli(["sign", "out"], x)
        require(code == 3, "旁观者不能 sign out")
        b = b_comes_online(sc, handoff["b"]["uuid"])
        code, out, err = sc.cli(["sign", "out"], b)
        require(code == 3 and "只能由持棒的主控" in err, "B 不能 sign out：%r %s" % (code, err))
        code, out, err = sc.cli(["sign", "in"], x)
        require(code == 3, "第三会话不能 sign in")
        code, out, err = sc.cli(["sign", "in"], b)
        require(code == 0 and sc.handoff()["b_signed"]["pid"] == b["pid"] and sc.handoff()["b_signed"]["sid"] == handoff["b"]["uuid"], "B sign in 记三元组：%r" % err)
        require(sc.cli(["sign", "in"], b)[0] == 3, "重复签字被拒")
        code, out, err = sc.cli(["sign", "out"], a)
        require(code == 0 and sc.handoff()["a_signed"]["by"] == "A" and sc.handoff()["a_signed"]["pid"] == a["pid"], "A sign out 记三元组")
        code, out, err = sc.cli(["seal", "--kit", kit], a)
        require(code == 3 and "A 已签字" in err, "签字后不能再 seal：%r %s" % (code, err))
        details.append("sign：out 只 holder、in 只 B（三元组 + 会话号），记三元组；kit 未封存 / 重复 / 他人都拒；签字后不能再 seal")
        # adopt：A 活着 / 已签字 ⇒ 拒
        code, out, err = sc.cli(["adopt"], None)
        require(code == 3 and "已有签字" in err, "A 已签字不需要 adopt")
        # reply
        code, out, err = sc.cli(["reply", "task"], x)
        require(code == 3, "旁观者不能 reply")
        code, out, err = sc.cli(["reply", "owner"], a)
        require(code == 0 and sessions.read_session(sc.sdir, "sA")["last_reply"]["kind"] == "owner" and "daemon 会核对" in out, "reply 登记并提示 daemon 会核对")
        require(sc.cli(["reply", "bogus"], a)[0] == 2, "reply 种类闭域")
        details.append("adopt：A 已签字拒；reply：主控 / B 才能，登记并提示 daemon 会核对、种类闭域")
    # off 语义
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        code, out, err = sc.cli(["off"], None)
        require(code == 0 and sc.handoff() is None and sc.state()["holder"]["pid"] == a["pid"], "off：放弃交接、holder 不动：%r %s" % (code, err))
        require([k["pid"] for k in sc.world.kills] == [b["pid"]], "off：SIGTERM 未接棒的 B（三元组核对）：%r" % sc.world.kills)
        require(sessions.read_mode(sc.sdir)[0] == "manual", "off 回到 manual")
        lines = [json.loads(line) for line in open(os.path.join(sc.sdir, "handoffs.jsonl"), encoding="utf-8")]
        require(lines[-1]["phase"] == "abandoned" and lines[-1]["id"] == hid, "放弃的交接归档")
        details.append("off（换棒前）：handoff 归档为 abandoned、holder 不动、SIGTERM B（核三元组）、回 manual")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.world.kill_ignored = True
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        sc.tick(dm)
        require(sc.handoff()["phase"] == "committed" and sc.state()["holder"]["pid"] == b["pid"], "前置：已换棒")
        killed_before = len(sc.world.kills)
        code, out, err = sc.cli(["off"], None)
        require(code == 0 and "已经换棒" in out and sc.handoff() is not None and sc.handoff()["phase"] == "committed"
                and sc.state()["holder"]["pid"] == b["pid"] and len(sc.world.kills) == killed_before, "off（换棒后）：不放弃、不杀 B、handoff 保留：%r %s" % (out, err))
        details.append("off（换棒后）：不放弃、不杀已是主控的 B、handoff 原样保留")
    return details


def run_full(sc, a, dm, a_stop=True):
    """正常交接到 commit 前一刻：request → 开窗 → B 上线 → seal → OPEN → 双签。返回 (B 身份记录, handoff id)。"""
    b, hid = full_handoff_until_sealed(sc, a, dm)
    sc.tick(dm)
    sc.cli(["sign", "out"], a)
    sc.cli(["sign", "in"], b)
    return b, hid


def case_h40():
    """H40：正常交接全程（设计:102）：request → B 上线 → seal → OPEN 一次（content 逐字 = 开场指令、无前缀）→ 双签 → A 签后 Stop 数组空 → commit → SIGTERM A → BATON → 归档。"""
    details = []
    with scenario() as sc:
        a = sc.spawn("sA", name="a-name")
        x = sc.spawn("sX")
        dm = sc.daemon()
        sc.hook("SessionStart", a, source="startup")
        hid, uuid = start_handoff(sc, a)
        require(sc.world.launches == [] and sc.state()["holder"]["pid"] == a["pid"], "前置：A 是 holder，还没开窗")
        holder = {}
        sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        b = holder["b"]
        launch = sc.handoff()["launch"]
        require(sc.world.launches == [hid] and launch["state"] == "opened" and launch["exit"] == 0 and launch["version"] == "2.1.290", "开窗恰好一次并记结果：%r" % launch)
        require(sc.handoff()["b"]["pid"] == b["pid"] and sc.handoff()["b"]["online_ms"], "B 上线已登记（b 槽 = B 进程）")
        sc.tick(dm)
        require(sc.world.messages == [], "kit 未封存：不得投 OPEN（J3）")
        require(sc.cli(["seal", "--kit", sc.write_kit()], a)[0] == 0, "seal")
        sc.tick(dm)
        opens = sc.world.messages_to(b)
        require(opens == [OPENING] and not opens[0].startswith("[handoffd]"), "OPEN 恰好一次、content 逐字 = 开场指令、无前缀：%r" % opens)
        for _ in range(3):
            sc.tick(dm)
        require(len(sc.world.messages) == 1, "OPEN 至多一次：%r" % sc.world.messages)
        require(sc.handoff()["open"]["state"] == "sent", "open.state=sent")
        details.append("开窗 1 次；kit 未封存不发 OPEN；seal 后 OPEN 恰好 1 次、逐字、无前缀")
        # 单签 / 在途
        sc.cli(["sign", "out"], a)
        for _ in range(3):
            sc.tick(dm)
        require(sc.handoff()["phase"] == "arming", "只有 A 签字：不换棒")
        sc.cli(["sign", "in"], b)
        sc.stop(a, tasks=["bg-1"])
        sc.tick(dm)
        require(sc.handoff()["phase"] == "arming", "A 的 Stop 带在途任务数组非空：不换棒")
        sc.stop(a, crons=["cron-1"])
        sc.tick(dm)
        require(sc.handoff()["phase"] == "arming", "A 的 Stop 带 crons 非空：不换棒")
        details.append("单签不换；A 的 Stop 数组非空（tasks / crons）不换")
        sc.stop(a)
        require(sc.state()["holder"]["pid"] == a["pid"], "daemon 没 tick 前仍是 A")
        sc.world.kill_ignored = False
        sc.tick(dm)
        state = sc.state()
        require(state["holder"]["pid"] == b["pid"] and state["holder"]["sid"] == uuid, "换棒：holder = B：%r" % state["holder"])
        require(sc.count_commits() == 1 and state["retire"][-1]["pid"] == a["pid"], "commit 恰好一次、A 进 retire：%r" % state["retire"])
        require(len(sc.world.kills) == 1 and sc.world.kills[0]["pid"] == a["pid"] and sc.world.kills[0]["sig"] == 15, "SIGTERM A 恰好一次：%r" % sc.world.kills)
        require(sc.world.kills[0]["holder"]["pid"] == b["pid"] and sc.world.kills[0]["phase"] == "committed", "先写 baton（holder=B）后 kill：发信号那一刻登记册已是 B / committed：%r" % sc.world.kills[0])
        batons = [c for pid, c in sc.world.messages if pid == b["pid"] and "BATON" in c]
        require(len(batons) == 1 and batons[0].startswith("[handoffd] BATON"), "BATON 恰好一次给 B：%r" % sc.world.messages)
        require(sc.handoff() is None, "收尾：handoff 清空")
        done = [json.loads(line) for line in open(os.path.join(sc.sdir, "handoffs.jsonl"), encoding="utf-8")]
        require(done[-1]["phase"] == "completed" and done[-1]["id"] == hid and done[-1]["post"]["kill"]["state"] == "sent", "归档为 completed：%r" % done[-1])
        for _ in range(4):
            sc.tick(dm)
        require(sc.count_commits() == 1 and len(sc.world.kills) == 1 and len(sc.world.messages) == 2 and len(sc.world.launches) == 1, "之后任何 tick 都不再 commit / kill / 投递 / 开窗")
        details.append("A 签后 Stop 数组空 → commit 恰好 1 次（holder=B）→ SIGTERM A 恰好 1 次（在 baton 写入之后）→ BATON 恰好 1 次 → 归档；之后 4 轮无任何动作")
        require(sc.pre(b) is None, "B 现在是 holder：写放行")
        require(sc.pre(x) is not None, "第三会话仍旁观：写被拒")
        require(sc.ring_count() == 0 and sc.notices() == [], "正常交接 0 响铃 0 notice（规格:45）：%r" % sc.notices())
        require(not any(sc.notifier.guard_held_during_alert), "发声时 baton guard 空闲（G10）")
        details.append("换棒后：B 写放行、第三会话被拒；正常交接全程 0 响铃")
    return details


def case_h41():
    """H41：设计:102 的 S2 场景变体——seal 先于 B 上线 / A 有在途 / A 签后崩 / 封存未签崩 / 未封存崩 / 76% 代发 / 重启不重复开窗 / 登记册读不出。"""
    details = []
    # seal 先于 B 上线：OPEN 等 B 上线
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        require(sc.cli(["seal", "--kit", sc.write_kit()], a)[0] == 0, "B 未上线也能 seal")
        sc.tick(dm)
        for _ in range(3):
            sc.tick(dm)
        require(sc.world.messages == [], "B 没上线：不投 OPEN")
        b = b_comes_online(sc, uuid)
        sc.tick(dm)
        require(sc.world.messages == [(b["pid"], OPENING)], "B 上线后才投、恰好一次")
        details.append("seal 先于 B 上线：B 上线前不投 OPEN，上线后投 1 次")
    # A 签后崩：B 照常接棒、无铃、:47② 不触发
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.world.die(a)
        sc.tick(dm)
        sc.tick(dm)
        require(sc.handoff()["phase"] == "arming" and sc.ring_count() == 0, "A 签后崩、B 未签：等 B（无铃）：rings=%d" % sc.ring_count())
        sc.cli(["sign", "in"], b)
        sc.tick(dm)
        require(sc.state()["holder"]["pid"] == b["pid"] and sc.world.kills == [] and sc.count_commits() == 1, "A 已死：B 签后 commit，不发 SIGTERM（跳过）")
        require(sc.ring_count() == 0 and sc.notice_kinds() == ["a_crash_signed"] and sc.notices()[0]["ring"] is False, "A 签后崩只进记录（quiet notice），不响铃：%r" % sc.notices())
        require(sc.notifier.count("quiet") == 1, "无声通知 1 次")
        require(len([m for m in sc.world.messages if "BATON" in m[1]]) == 1 and sc.handoff() is None, "BATON 照发，交接完成")
        details.append("A 签后崩：B 照常接棒（跳过 Stop 核对与 SIGTERM）、只进 quiet notice、0 响铃（:47② 未触发）")
    # 封存未签崩：adopt
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.world.die(a)
        for _ in range(3):
            sc.tick(dm)
        require(sc.notice_kinds() == ["a_crash_sealed"] and sc.ring_count() == 1 and sc.notifier.count("problem") == 1, "封存未签崩：恰好 1 条 notice + 1 次响铃：%r" % sc.notice_kinds())
        text = sc.notices()[0]["text"]
        require("handoffd adopt" in text and "handoffd off" in text and "1)" in text and "2)" in text and "（推荐）" in text, "选项：1 adopt 2 off：%s" % text)
        sc.cli(["sign", "in"], b)
        for _ in range(3):
            sc.tick(dm)
        require(sc.handoff()["phase"] == "arming", "没 adopt：不换棒")
        code, out, err = sc.cli(["adopt"], None)
        require(code == 0 and sc.handoff()["a_signed"]["by"] == "owner", "adopt：%r %s" % (code, err))
        sc.tick(dm)
        require(sc.state()["holder"]["pid"] == b["pid"] and sc.world.kills == [] and sc.count_commits() == 1, "adopt + B 已签：commit，A 已死不发 SIGTERM")
        require(sc.ring_count() == 1, "adopt 后不再多响")
        details.append("A 已封存 kit 未签字就崩：1 notice + 1 铃（选项 adopt / off）；adopt 后 B 签字即换棒")
    # 未封存崩（:56）：不再开窗
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        sc.world.die(a)
        for _ in range(4):
            sc.tick(dm)
        require(sc.world.launches == [] and sc.notice_kinds() == ["a_crash_unsealed"] and sc.ring_count() == 1, "A 在开窗前崩：不开窗；1 notice + 1 铃：%r" % sc.notice_kinds())
        text = sc.notices()[0]["text"]
        require("handoffd off" in text and "kit 已写就读 kit" in text, "选项：放弃交接 + 读 kit / 上一棒记录：%s" % text)
        details.append("交接前 / 中 A 崩（kit 未封存）：不开窗；1 notice + 1 铃（放弃交接，读 kit 或上一棒记录）")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        holder = {}
        sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
        sc.tick(dm)
        sc.world.die(a)
        for _ in range(3):
            sc.tick(dm)
        require(sc.notice_kinds() == ["a_crash_unsealed"] and len(sc.world.launches) == 1 and sc.world.messages == [], "B 已上线但 kit 未封存时 A 崩：仍是 :56，不投 OPEN")
        details.append("B 已上线、kit 未封存时 A 崩：仍是 :56，不投 OPEN、不再动")
    # 76% 代发
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        sc.hook("SessionStart", a, source="startup")
        sessions.update_session(sc.sdir, "sA", lambda record: record.update(request_pending={"reason": "ctx76", "ms": sc.clock.ms, "pct": 80}))
        sc.tick(dm)
        handoff = sc.handoff()
        require(handoff and handoff["reason"] == "ctx76" and handoff["a"]["sid"] == "sA" and handoff["phase"] == "arming", "76% 待发起标记 → daemon 建交接")
        require(not sessions.read_session(sc.sdir, "sA").get("request_pending"), "标记已消费")
        sc.world.die(a)
        sessions.update_session(sc.sdir, "sA", lambda record: record.update(request_pending={"reason": "ctx76"}))
        hid = handoff["id"]
        sc.tick(dm)
        require(sc.handoff()["id"] == hid, "已有交接：不重复建")
        details.append("76% 钩子落 request_pending → daemon 建 ctx76 交接并消费标记；已有交接不重复建")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        sc.hook("SessionStart", a, source="startup")
        sc.world.die(a)
        sessions.update_session(sc.sdir, "sA", lambda record: record.update(request_pending={"reason": "ctx76"}))
        sc.tick(dm)
        require(sc.handoff() is None, "holder 已死：不据待发起标记建交接（holder_not_live）")
        details.append("holder 已死：不据 request_pending 建交接")
    # 登记册读不出：不动作、不抛
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        sc.hook("SessionStart", a, source="startup")
        with open(baton.baton_path(sc.sdir), "w", encoding="utf-8") as stream:
            stream.write("{bad")
        sc.tick(dm)
        require(sc.world.launches == [] and sc.world.kills == [] and sc.world.messages == [] and sc.events("daemon:baton_unreadable"), "登记册读不出：不动作，记 events")
        details.append("baton.json 读不出：不开窗 / 不发信号 / 不投递，记 events")
    return details


def case_h45():
    """H45（J4）：本片响铃情形逐条触发——各恰好 1 条 notice + 1 次响铃（problem），:64 与 G5 只无声（quiet）；时间门槛 120 s / 60 s 精确。"""
    details = []
    # ④a B 120 s 未上线
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        sc.tick(dm)                                   # 开窗，started_ms = 此刻
        started = sc.handoff()["launch"]["started_ms"]
        sc.clock.ms = started + 119_000
        dm.tick(sc.clock.ms)
        require(sc.ring_count() == 0, "119 s：不响")
        sc.clock.ms = started + 120_000
        dm.tick(sc.clock.ms)
        require(sc.ring_count() == 1 and sc.notice_kinds() == ["b_not_online"], "120 s：响 1 次：%r" % sc.notice_kinds())
        for _ in range(5):
            sc.tick(dm, 30)
        require(sc.ring_count() == 1 and len(sc.notices()) == 1, "之后不重复")
        require("handoffd off" in sc.notices()[0]["text"] and "去看新窗口" in sc.notices()[0]["text"], "选项：去看窗口 / 放弃交接")
        details.append("④ B 120 s 未上线：119 s 不响、120 s 响 1 次、之后不重复")
    # ④b B 上线后死
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        require(sc.ring_count() == 0, "B 活着：不响")
        sc.world.die(b)
        for _ in range(4):
            sc.tick(dm)
        require(sc.ring_count() == 1 and sc.notice_kinds() == ["b_died"], "B 上线后死：1 notice + 1 铃：%r" % sc.notice_kinds())
        details.append("④ B 上线后死：1 notice + 1 铃")
    # ④c 预检连败 3 次
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.world.launch_script = [(2, "claude 版本太低", None)] * 6
        sc.tick(dm)
        calls_after_first = len(sc.world.launches)
        sc.tick(dm, 10)
        require(len(sc.world.launches) == calls_after_first, "next_attempt 前不重试")
        sc.tick(dm, 61)
        require(len(sc.world.launches) == 2 and sc.ring_count() == 0, "第 2 次失败：不响")
        sc.tick(dm, 61)
        require(len(sc.world.launches) == 3 and sc.ring_count() == 1 and sc.notice_kinds() == ["preflight_failed"], "第 3 次失败：响 1 次：%r" % sc.notice_kinds())
        sc.tick(dm, 61)
        sc.tick(dm, 61)
        require(len(sc.world.launches) == 5 and sc.ring_count() == 1, "之后继续重试但不重复响铃")
        require("预检" in sc.notices()[0]["text"] and "claude 版本太低" in sc.notices()[0]["text"], "文案带预检输出")
        details.append("④ 预检连败 3 次：前 2 次不响、第 3 次响 1 次，之后继续重试不再响")
    # ④d 启动器非 0 / 异常：不重试
    for label, entry in (("退出码 3", (3, "open 失败", "2.1.290")), ("异常", ("raise", "", None))):
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon()
            start_handoff(sc, a)
            sc.world.launch_script = [entry]
            sc.tick(dm)
            for _ in range(5):
                sc.tick(dm, 90)
            require(len(sc.world.launches) == 1 and sc.handoff()["launch"]["state"] == "uncertain", "%s：不自动重开" % label)
            require(sc.notice_kinds() == ["launcher_failed"] and sc.ring_count() == 1, "%s：1 notice + 1 铃：%r" % (label, sc.notice_kinds()))
    details.append("④ 启动器非 0 / 异常：结果不确定 ⇒ 永不自动重开；1 notice + 1 铃")
    # ⑤ 换棒后旧 A 60 s 仍活
    for label, setup in (("A 不理 SIGTERM", lambda sc, a: setattr(sc.world, "kill_ignored", True)),
                         ("A 的 cwd 不在项目下（目标核对不过，信号没发）", lambda sc, a: sc.world.table[a["pid"]].update(cwd="/somewhere/else"))):
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon()
            b, hid = full_handoff_until_sealed(sc, a, dm)
            sc.tick(dm)
            sc.cli(["sign", "out"], a)
            sc.cli(["sign", "in"], b)
            sc.stop(a)
            setup(sc, a)
            sc.tick(dm)
            require(sc.handoff()["phase"] == "committed" and sc.state()["holder"]["pid"] == b["pid"], "%s：前置：已换棒" % label)
            base = sc.handoff()["post"]["kill"]["ms"] or sc.handoff()["committed_ms"]
            sc.clock.ms = base + 59_000
            dm.tick(sc.clock.ms)
            require(sc.ring_count() == 0, "%s：59 s 不响" % label)
            sc.clock.ms = base + 60_000
            dm.tick(sc.clock.ms)
            require(sc.ring_count() == 1 and sc.notice_kinds() == ["old_a_alive"], "%s：60 s 响 1 次：%r" % (label, sc.notice_kinds()))
            for _ in range(3):
                sc.tick(dm, 30)
            require(sc.ring_count() == 1 and len(sc.notices()) == 1, "%s：不重复" % label)
            if "cwd" in label:
                archived = [json.loads(line) for line in open(os.path.join(sc.sdir, "handoffs.jsonl"), encoding="utf-8")][-1]
                require(sc.world.kills == [] and archived["post"]["kill"]["state"] == "skipped" and archived["post"]["kill"]["reason"] == "cwd_outside",
                        "cwd 核对不过：不发信号、记 skipped:cwd_outside：%r %r" % (sc.world.kills, archived["post"]["kill"]))
            require(sc.handoff() is None, "%s：响过「仍活」铃后收尾（旧 A 此后只是多开着）" % label)
            sc.world.die(a)
            sc.tick(dm)
    details.append("⑤ 换棒后旧 A 60 s 仍活（不理 SIGTERM / 核对不过没发信号）：59 s 不响、60 s 响 1 次、之后不重复、响铃后收尾")
    # G5：claude 版本变了 → 无声通知；首次不发
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.tick(dm)
        require(sc.notices() == [] and json.load(open(os.path.join(sc.sdir, "daemon_state.json")))["last_claude_version"] == "2.1.290", "首次记录版本，不发通知")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        util.atomic_write_json(os.path.join(sc.sdir, "daemon_state.json"), {"last_claude_version": "2.1.280"})
        start_handoff(sc, a)
        sc.tick(dm)
        require(sc.notice_kinds() == ["claude_version"] and sc.notices()[0]["ring"] is False and sc.ring_count() == 0 and sc.notifier.count("quiet") == 1,
                "版本变了：quiet notice、无铃：%r" % sc.notices())
        require(json.load(open(os.path.join(sc.sdir, "daemon_state.json")))["last_claude_version"] == "2.1.290", "记下新版本")
        details.append("G5：记 claude --version，与上次不同发无声通知（首次不发、不响铃）")
    # 重启后不重复：notice 去重靠登记的 notice（新 Daemon 对象）
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        sc.world.die(a)
        sc.tick(dm)
        dm2 = sc.daemon()
        sc.tick(dm2)
        sc.tick(dm2)
        require(sc.ring_count() == 1, "daemon 重启后同一情形不重复响铃：%d" % sc.ring_count())
        details.append("重启后同一情形不重复响铃（notice id 去重）")
        require(not any(sc.notifier.guard_held_during_alert), "所有发声时 baton guard 都空闲")
    return details


def case_h46():
    """H46（G8、G10）：daemon.lock 只把 EWOULDBLOCK 当已有实例（其它错误抛出）；单实例重试避开瞬时占用；发声延迟到 guard 之外（删保护即红）。"""
    details = []
    import errno
    import fcntl
    with temp_dir() as work:
        path = os.path.join(work, "daemon.lock")

        def failing(code):
            def flock(fd, op):
                raise OSError(code, os.strerror(code))
            return flock

        for code in (errno.EBADF, errno.EIO, errno.ENOLCK, errno.EINVAL):
            try:
                daemon_module.acquire_daemon_lock(path, wait=0.2, interval=0.01, flock_fn=failing(code))
            except OSError as exc:
                require(exc.errno == code, "错误应原样抛出")
            else:
                require(False, "errno %d 不是「已有实例」：必须抛出，不得返回 None" % code)
        require(daemon_module.acquire_daemon_lock(path, wait=0.1, interval=0.01, flock_fn=failing(errno.EWOULDBLOCK)) is None, "EWOULDBLOCK ⇒ 已有实例 ⇒ None")
        require(daemon_module.acquire_daemon_lock(path, wait=0.1, interval=0.01, flock_fn=failing(errno.EAGAIN)) is None, "EAGAIN 同")
        details.append("G8：EBADF / EIO / ENOLCK / EINVAL 抛出；EWOULDBLOCK / EAGAIN 才算已有实例（None）")
        # 单实例重试：他人瞬时占用 0.5 s
        holder_fd = os.open(path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)

        def release():
            time.sleep(0.5)
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

        threading.Thread(target=release, daemon=True).start()
        start = time.monotonic()
        fd = daemon_module.acquire_daemon_lock(path, wait=3.0, interval=0.05)
        require(fd is not None and time.monotonic() - start >= 0.4, "瞬时占用时应重试到拿到锁（G10：重试保护）：%r" % fd)
        os.close(fd)
        details.append("G10：他人瞬时占用 0.5 s ⇒ 重试后拿到锁（删重试即红）")
        # 两个实例：第二个立即 0 退出、不 tick
        fd1 = daemon_module.acquire_daemon_lock(path)
        ticks = []

        class Spy:
            def tick(self, now):
                ticks.append(now)

        out = []
        code = daemon_module.run_daemon(work, Spy(), 5, once=True, out=out.append)
        require(code == 0 and ticks == [] and out == ["守护程序已在运行"], "第二个实例：0 退出、不 tick、不动作：%r %r" % (code, out))
        os.close(fd1)
        code = daemon_module.run_daemon(work, Spy(), 5, once=True, out=out.append, clock=lambda: 7)
        require(code == 0 and ticks == [7], "锁释放后能跑一轮")
        details.append("两个实例：第二个 0 退出且不 tick；锁释放后可跑")
    # 命令行入口：handoffd daemon --once（无交接：只跑一轮，不做任何外部动作）
    with scenario() as sc:
        a = sc.spawn("sA")
        sc.hook("SessionStart", a, source="startup")
        before = open(baton.baton_path(sc.sdir), "rb").read()
        code, out, err = sc.cli(["daemon", "--once"], None)
        require(code == 0 and open(baton.baton_path(sc.sdir), "rb").read() == before, "handoffd daemon --once：无交接时只跑一轮、不改登记册：%r %s" % (code, err))
        require(os.path.exists(os.path.join(sc.sdir, "daemon.lock")), "daemon.lock 已创建")
        details.append("handoffd daemon --once（launchd plist 用的入口）：无交接时只跑一轮，登记册不变")
    # 发声延迟到 guard 之外
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.world.launch_script = [(2, "x", None)] * 3
        sc.tick(dm)
        sc.tick(dm, 61)
        sc.tick(dm, 61)
        require(sc.notifier.count("problem") == 1 and sc.notifier.guard_held_during_alert == [False], "预检连败的铃在 record_launch 的 guard 内被决定、出 guard 后才发（发声时 guard 空闲）：%r" % sc.notifier.guard_held_during_alert)
        details.append("G10：guard 内决定的响铃出 guard 后才发（发声时 baton guard 可取）；删延迟即红")
    return details


def case_h47():
    """H47：B 的角色说明（自己是接棒者、A 的会话名与 sid、等开场指令、结论回 A、sign in、等 BATON）与上线接线（SessionStart → baton.b_online）。"""
    details = []
    with scenario() as sc:
        a = sc.spawn("sA-session-id", name="alpha-name")
        sc.hook("SessionStart", a, source="startup")
        code, out, err = sc.cli(["request"], a)
        handoff = sc.handoff()
        uuid = handoff["b"]["uuid"]
        calls = []
        original = baton.b_online
        baton.b_online = lambda *args, **kwargs: calls.append(args) or original(*args, **kwargs)
        try:
            b = sc.spawn(uuid)
            code, output = sc.hook("SessionStart", b, source="startup")
        finally:
            baton.b_online = original
        text = output["hookSpecificOutput"]["additionalContext"]
        for needle in ("接手方（B）", "alpha-name", "sA-session-id", "开场指令", "SendMessage", "handoffd sign in", "BATON"):
            require(needle in text, "B 的 SessionStart 注入缺 %r：%s" % (needle, text))
        require("不能 off" in text, "B 被告知不能 off")
        require(len(calls) == 1 and calls[0][1] == uuid, "SessionStart 调用了 baton.b_online：%r" % calls)
        require(sc.handoff()["b"]["pid"] == b["pid"] and sc.handoff()["b"]["online_ms"], "b 槽 = B 进程、online_ms 已记")
        # 非 B 的会话不触发登记
        x = sc.spawn("other")
        before = json.dumps(sc.handoff()["b"], sort_keys=True)
        sc.hook("SessionStart", x, source="startup")
        require(json.dumps(sc.handoff()["b"], sort_keys=True) == before, "别的会话的 SessionStart 不改 b 槽")
        details.append("B 的 SessionStart 注入含：接手方、A 的会话名与 sid、开场指令、SendMessage、sign in、BATON、不能 off；b_online 被调用且 b 槽登记")
    return details


def case_h50():
    """H50（规格:29 B 复核有异议 → A 改 kit）：A 签字（a_signed）之前允许重新 seal——更新 kit 存档与 sha、记 events；OPEN 已发则不重发，
    新开场指令与已发的不同时 seal 输出提示 A 通知 B 重读 kit；a_signed 之后仍冻结。"""
    details = []
    kit2 = KIT_TEXT.replace("你是第 2 棒主控。", "你是第 2 棒主控（改版）。")
    kit3 = kit2.replace("## 事实\n一些内容。", "## 事实\n只改了事实段，开场指令与 B 已收到的一致。")
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        require(sc.cli(["seal", "--kit", sc.write_kit(KIT_TEXT, "k1.md")], a)[0] == 0, "首次 seal")
        sha1 = sc.handoff()["kit"]["sha"]
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit(kit2, "k2.md")], a)
        require(code == 0 and "重新封存" in out and "开场指令变了" not in out, "OPEN 前重新 seal：允许，不报 stale：%r %s" % (out, err))
        kit = sc.handoff()["kit"]
        require(kit["sha"] != sha1 and "（改版）" in kit["opening"], "kit 记录更新")
        require(open(os.path.join(sc.sdir, kit["path"]), encoding="utf-8").read() == kit2, "kit 存档更新")
        b = b_comes_online(sc, uuid)
        sc.tick(dm)
        require(sc.world.messages_to(b) == [kit["opening"]] and "（改版）" in sc.world.messages_to(b)[0], "OPEN 取发送时最新的开场指令")
        details.append("OPEN 前重新 seal：更新存档与 sha，OPEN 用最新开场指令")
        # OPEN 已发之后重新 seal：开场指令变了 ⇒ 不重发 + 提示
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit(KIT_TEXT, "k3.md")], a)
        require(code == 0 and "开场指令变了" in out and "SendMessage" in out and "不会重发" in out, "已发 OPEN 且指令变了：提示 A 用 SendMessage 通知 B：%r %s" % (out, err))
        for _ in range(3):
            sc.tick(dm)
        require(len(sc.world.messages_to(b)) == 1 and sc.handoff()["open"]["state"] == "sent", "OPEN 不重发（至多一次）：%r" % sc.world.messages)
        require(open(os.path.join(sc.sdir, sc.handoff()["kit"]["path"]), encoding="utf-8").read() == KIT_TEXT and sc.handoff()["kit"]["opening"] == OPENING, "kit 存档与记录已更新到最新版")
        seals = sc.events("seal")
        require(len(seals) == 3 and [e["resealed"] for e in seals] == [False, True, True] and [e["stale_open"] for e in seals] == [False, False, True], "events 记每次 seal：%r" % seals)
        # 开场指令与 B 已收到的一致（只改了别的段）：不提示
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit(kit3, "k4.md")], a)
        require(code == 0 and "开场指令变了" not in out and "重新封存" in out, "指令没变：不提示：%r" % out)
        details.append("OPEN 已发后重新 seal：不重发；指令变了 ⇒ 输出提示 A 用 SendMessage 告诉 B，指令没变 ⇒ 不提示；每次 seal 记 events")
        # a_signed 之后冻结
        sc.cli(["sign", "out"], a)
        frozen_sha = sc.handoff()["kit"]["sha"]
        code, out, err = sc.cli(["seal", "--kit", sc.write_kit(kit2, "k5.md")], a)
        require(code == 3 and "冻结" in err and sc.handoff()["kit"]["sha"] == frozen_sha, "A 签字后冻结：%r %s" % (code, err))
        require(open(os.path.join(sc.sdir, sc.handoff()["kit"]["path"]), encoding="utf-8").read() == kit3, "冻结后存档不变")
        details.append("a_signed 之后 seal 被拒（冻结），存档与 sha 不变")
    return details


def case_h49():
    """H49（J1 的结构核对）：baton.json 只在 baton.py 里写；holder 槽的赋值只出现在 _cas_holder / claim（同进程换号）/ commit 三个函数内；
    transact 在 fn 动了 holder 而没声明 may_change_holder 时写盘前即抛、登记册不变。"""
    import re
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cdm")
    sources = {name: open(os.path.join(root, name), encoding="utf-8").read() for name in sorted(os.listdir(root)) if name.endswith(".py")}
    for name, text in sources.items():
        if name != "baton.py":
            require("baton_path" not in text and not re.search(r"""["']baton\.json""", text), "%s 不得直接读写 baton.json（只经 baton 模块）" % name)
    lines = sources["baton.py"].split("\n")
    owners = []
    current = None
    for number, line in enumerate(lines, start=1):
        match = re.match(r"^def (\w+)\(", line)
        if match:
            current = match.group(1)
        if re.search(r"""\[["']holder["']\]\s*=[^=]""", line):
            owners.append((current, number))
    # commit 的赋值写在 commit() 内嵌的 fn 里：向上数到最近的顶层 def 是 commit
    require([name for name, _ in owners] == ["_cas_holder", "claim", "commit"], "holder 槽赋值只应出现在 _cas_holder / claim / commit：%r" % owners)
    with scenario() as sc:
        a = sc.spawn("sA")
        sc.hook("SessionStart", a, source="startup")
        before = open(baton.baton_path(sc.sdir), "rb").read()
        try:
            baton.transact(sc.sdir, lambda state: state.__setitem__("holder", {"sid": "evil", "pid": 2, "lstart": "x"}))
        except AssertionError:
            pass
        else:
            require(False, "transact 里动 holder 而没声明 may_change_holder 应抛 AssertionError")
        require(open(baton.baton_path(sc.sdir), "rb").read() == before, "断言失败时不得写盘")
    return ["baton.json 只在 baton.py 里读写；holder 赋值只在 _cas_holder / claim / commit（%r）；transact 守卫：未声明就改 holder ⇒ 抛、不落盘" % (owners,)]


def main():
    """运行 H38–H41、H45–H47、H49、H50。"""
    return run_cases([("H38", case_h38), ("H39", case_h39), ("H40", case_h40), ("H41", case_h41), ("H45", case_h45), ("H46", case_h46), ("H47", case_h47),
                      ("H49", case_h49), ("H50", case_h50)])


if __name__ == "__main__":
    sys.exit(main())
