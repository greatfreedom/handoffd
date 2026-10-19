# 测试 H57–H59：handoffd S2a R2 整合账的 K4（单一执行者）与 B2/B3/B5/B6/B7；预期从整合账与规格写出，只看假世界里发生了什么。
#   H57  off 路径的语义：daemon.lock 单一执行者、pending→sending CAS、发信号前重读意图、B 忽略 SIGTERM / 信号失败 / 超时、
#        resolve 不吞未响 notice、B 尚未登记时 off、kill:marker 后 A 自死、收尾期 B 死只响一次
#   H58  CLI `off` 与 daemon 的并发交错：真实 run_daemon（持 daemon.lock）+ 真实 CLI 入口，调度由测试控制
#   H59  真实子进程 kill -9（不执行 finally）：notice 写盘→发声、baton:marker、offkill 认领后；父进程再做重启检查 K1
import json
import os
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import MVP_ROOT, require, run_cases  # noqa: E402
from swap_helpers import full_handoff_until_sealed, scenario, start_handoff  # noqa: E402
from test_swap_exhaustive import CrashPoint  # noqa: E402
from test_swap_k import collect, read_notices  # noqa: E402
from cdm import baton, daemon as daemon_module, sessions  # noqa: E402


def abandon_like_off(sc):
    """`off` 的前半（落意图、切 manual），不执行——用来把执行放到测试指定的运行体里。"""
    result, handoff = baton.abandon(sc.sdir, sc.clock.ms)
    require(result == "ok" and handoff, "abandon 失败：%r" % result)
    sessions.write_mode(sc.sdir, "manual", sc.clock.ms)
    return handoff


def ready(sc):
    a = sc.spawn("sA")
    dm = sc.daemon()
    b, hid = full_handoff_until_sealed(sc, a, dm)
    return a, b, hid, dm


def kinds(sc):
    return [n["kind"] for n in read_notices(sc)[0]]


# ---------------------------------------------------------------- H57

def t_lock():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        fd = daemon_module.acquire_daemon_lock(os.path.join(sc.sdir, "daemon.lock"))     # 有 daemon 在跑（持锁）
        try:
            code, out, err = sc.cli(["off"])
            require(code == 0 and sc.world.kills == [] and "交给" in out, "daemon 持锁：CLI 不执行，只落意图并交给 daemon：%r %r" % (sc.world.kills, out))
            require(sc.state().get("off_kill") and sc.state()["off_kill"][0]["state"] == "pending", "意图仍是 pending")
        finally:
            os.close(fd)
        sc.tick(dm)
        require([k["pid"] for k in sc.world.kills] == [b["pid"]], "daemon 的下一轮执行（恰好一次）：%r" % sc.world.kills)
        fd = daemon_module.acquire_daemon_lock(os.path.join(sc.sdir, "daemon.lock"), wait=0)
        require(fd is not None, "CLI 那一轮之后锁已放开")
        os.close(fd)
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        code, out, err = sc.cli(["off"])
        require(code == 0 and [k["pid"] for k in sc.world.kills] == [b["pid"]], "没有 daemon：CLI 先取得锁、当场执行：%r" % sc.world.kills)
        fd = daemon_module.acquire_daemon_lock(os.path.join(sc.sdir, "daemon.lock"), wait=0)
        require(fd is not None, "CLI 跑完后锁已放开")
        os.close(fd)
    return "K4：daemon 持锁 ⇒ CLI 只落意图、不执行，daemon 下一轮恰好一次；没有 daemon ⇒ CLI 先取锁再执行，之后放锁"


def t_cas():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {b["pid"]}     # SIGTERM 发出后 B 还要一会儿才退出（正常的异步退场）
        abandon_like_off(sc)
        snapshot = sc.state()              # 两个执行者都读到了 pending
        second = sc.daemon()
        second.now = sc.clock.ms
        hit = []

        def point(label):
            if label == "offkill:marker" and not hit:
                hit.append(label)
                second._run_off_kills(snapshot)       # 另一个执行者用它先前读到的快照继续

        sc.tick(sc.daemon(point_fn=point))
        require(hit and len(sc.world.kills) == 1, "两个执行者读到同一条 pending：只有 CAS 的赢家发信号（恰好 1 次）：%r" % sc.world.kills)
    return "pending→sending 用 CAS：两个执行者读到同一条 pending，信号只发 1 次"


def t_cas_both_paused():
    """两个执行者都停在「认领之后、发信号之前」（真实线程 + 可控放行）：只有一个能拿到 owner、到达发信号前的切点，SIGTERM B 至多一次。
    专抓「删掉 pending CAS 与 owner 判断、只留 sending 判断」：那样第二个执行者会覆盖 owner 令牌、两个都发信号。"""
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {b["pid"]}     # B 不会因第一次信号就退出，两个执行者的重核都能通过
        abandon_like_off(sc)
        snapshot = sc.state()
        arrived, release = [], threading.Event()
        lock = threading.Lock()

        def point(label):
            if label == "offkill:marker":
                with lock:
                    arrived.append(threading.current_thread().name)
                release.wait(3)

        def run(name):
            dmx = sc.daemon(point_fn=point)
            dmx.now = sc.clock.ms
            dmx._run_off_kills(snapshot)

        threads = [threading.Thread(target=run, args=("exec-%d" % i,), name="exec-%d" % i) for i in range(2)]
        threads[0].start()
        deadline = 3.0
        end = time.monotonic() + deadline
        while not arrived and time.monotonic() < end:
            time.sleep(0.01)
        threads[1].start()
        time.sleep(0.6)                     # 给第二个执行者足够的时间走到同一个切点（若 CAS 失效它会走到）
        with lock:
            at_marker = list(arrived)
        release.set()
        for thread in threads:
            thread.join(10)
        require(len(at_marker) == 1, "只有一个认领者拿到 owner、走到发信号前的切点：%r" % at_marker)
        require(len(sc.world.kills) == 1, "SIGTERM B 至多一次：%r" % sc.world.kills)
    return "两个执行者都想认领：只有一个拿到 owner 并到达发信号前，SIGTERM B 恰好 1 次"


def t_reread():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {b["pid"]}
        abandon_like_off(sc)
        hit = []

        def point(label):
            if label == "offkill:marker" and not hit:
                hit.append(label)
                baton.transact(sc.sdir, lambda state: state.update(off_kill=[]))    # 意图记录在认领后、发信号前被别人清掉

        sc.tick(sc.daemon(point_fn=point))
        require(hit and sc.world.kills == [], "发出前重读意图记录本身：记录没了就不发信号：%r" % sc.world.kills)
    return "发信号前重读这条意图记录：被清掉 ⇒ 不发信号"


def t_unconfirmed():
    with scenario() as sc:                                    # B 忽略 SIGTERM
        a, b, hid, dm = ready(sc)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {b["pid"]}
        sc.cli(["off"])
        require(len(sc.world.kills) == 1 and sc.state()["off_kill"][0]["state"] == "sent", "信号发出后意图保留（sent）：%r" % sc.state().get("off_kill"))
        sc.tick(sc.daemon(), 60)
        require(kinds(sc) == [] and sc.state().get("off_kill"), "60 s：还在等，不响、不清")
        sc.tick(sc.daemon(), 61)
        require(kinds(sc) == ["off_kill_unconfirmed"] and sc.ring_count() == 1 and not sc.state().get("off_kill") and len(sc.world.kills) == 1,
                "超时（120 s）B 仍活：响铃一次、清意图、不重发：%r %r" % (kinds(sc), sc.world.kills))
    with scenario() as sc:                                    # 信号失败
        a, b, hid, dm = ready(sc)

        def failing(pid, sig):
            sc.world.kills.append({"pid": pid})
            raise PermissionError("denied")

        sc.world.kill_fn = failing
        sc.cli(["off"])
        sc.tick(sc.daemon(kill_fn=failing), 5)
        require(kinds(sc) == ["off_kill_unconfirmed"] and sc.ring_count() == 1 and not sc.state().get("off_kill"), "信号失败：响铃一次并结束意图：%r" % kinds(sc))
    with scenario() as sc:                                    # 正常：B 随后退出 ⇒ 意图完成，不响
        a, b, hid, dm = ready(sc)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {b["pid"]}
        sc.cli(["off"])
        sc.world.die(b)
        sc.tick(sc.daemon(), 5)
        require(not sc.state().get("off_kill") and kinds(sc) == [] and len(sc.world.kills) == 1, "B 退出后意图完成、无铃、信号 1 次")
    return "B2：信号发出后意图保留到 B 已死才清；超时仍活 / 信号失败 ⇒ 响铃一次；已发信号不重发"


def t_resolve():
    with scenario() as sc:
        a = sc.spawn("sA")
        hid, uuid = start_handoff(sc, a)
        sc.tick(sc.daemon())

        def point(label):
            if label == "notice:written":
                raise CrashPoint(label)

        try:
            sc.tick(sc.daemon(point_fn=point), 121)
        except CrashPoint:
            pass
        require(kinds(sc) == ["b_not_online"] and sc.ring_count() == 0, "前置：notice 已写、未发声")
        sc.cli(["off"])
        for _ in range(3):
            sc.tick(sc.daemon(), 5)
        require(sc.ring_count() == 1, "off 的 resolve 不吞未响的 notice：仍补响一次：%d" % sc.ring_count())
        sessions_records = [n for n in read_notices(sc)[0]]
        require(len(sessions_records) == 1, "仍只有 1 条 notice")
    return "B3：off（resolve）之后，未响的 notice 仍补响恰好一次"


def t_unregistered():
    with scenario() as sc:                                    # B 的窗口开出来了，但还没登记
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        box = {}
        sc.world.on_launch = lambda handoff: box.update(b=sc.spawn(uuid, name=handoff["b"]["name"]))   # 只有进程 + 会话文件，没有 SessionStart
        sc.tick(dm)
        b = box["b"]
        require(sc.handoff()["b"]["pid"] is None, "前置：B 未登记")
        sc.cli(["off"])
        require([k["pid"] for k in sc.world.kills] == [b["pid"]] and sc.world.proc_state(b) == "dead", "B 没登记时 off：按 uuid 找到并关掉：%r" % sc.world.kills)
    with scenario() as sc:                                    # 窗口晚到：先等，出现后再关
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        sc.tick(dm)
        sc.cli(["off"])
        require(sc.state()["off_kill"][0]["pid"] is None and sc.world.kills == [], "B 还没出现：意图等着")
        b = sc.spawn(uuid)
        sc.tick(sc.daemon(), 30)
        require([k["pid"] for k in sc.world.kills] == [b["pid"]], "B 出现后关掉：%r" % sc.world.kills)
    with scenario() as sc:                                    # 一直没出现：超时结束，不杀任何东西
        a = sc.spawn("sA")
        dm = sc.daemon()
        hid, uuid = start_handoff(sc, a)
        sc.tick(dm)
        sc.cli(["off"])
        sc.tick(sc.daemon(), 125)
        require(not sc.state().get("off_kill") and sc.world.kills == [], "120 s 内始终未出现 ⇒ 视为没开出来，意图结束，不发信号")
    return "B5：B 未登记时 off ⇒ 意图按 uuid 落，B 出现（会话文件 + cwd 在项目内）后关掉；一直不出现 ⇒ 120 s 后结束"


def t_a_dies_sending():
    with scenario() as sc:
        a = sc.spawn("sA")
        dm0 = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm0)
        sc.tick(dm0)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)

        def point(label):
            if label == "kill:marker":
                raise CrashPoint(label)

        try:
            sc.tick(sc.daemon(point_fn=point))
        except CrashPoint:
            pass
        sc.world.die(a)                       # 标记已落、信号没发；A 自己死了
        sc.tick(sc.daemon())
        sc.tick(sc.daemon())
        require("a_crash_signed" in kinds(sc) and sc.ring_count() == 0 and sc.handoff() is None, "B6：kill:marker 之后 A 自死 + 重启：补 quiet a_crash_signed：%r" % kinds(sc))
    return "B6：kill:marker 之后 A 自死 + 重启 ⇒ 补 quiet a_crash_signed"


def t_b_dies_tail():
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.world.kill_ignored, sc.world.ignore_pids = True, {a["pid"]}      # A 不退出：收尾期拉长
        sc.world.msg_fail = True                                                # BATON 投不出去
        sc.stop(a)
        sc.tick(dm)
        sc.world.die(b)
        for _ in range(10):
            sc.tick(dm)
        require(kinds(sc).count("b_died_after_commit") == 1 and "baton_failed" not in kinds(sc), "B7：收尾期 B 死只发 b_died_after_commit：%r" % kinds(sc))
        require(sc.ring_count() == 1, "只响一次：%d" % sc.ring_count())
    return "B7：收尾期 B 死 ⇒ 只发 b_died_after_commit，不再另发 baton_failed"


def case_h57():
    """H57：off 路径与 B2/B3/B5/B6/B7 的语义（K4 单一执行者、CAS、发出前重读意图、B 忽略 SIGTERM / 信号失败 / 超时、resolve 不吞未响 notice、B 未登记时 off、A 自死、收尾期 B 死）。"""
    return collect([("K4 daemon.lock", t_lock), ("K4 CAS", t_cas), ("K4 双认领者同时在场", t_cas_both_paused), ("K4 重读意图", t_reread), ("B2", t_unconfirmed), ("B3", t_resolve),
                    ("B5", t_unregistered), ("B6", t_a_dies_sending), ("B7", t_b_dies_tail)])


# ---------------------------------------------------------------- H58

def c_delegate_when_daemon_runs():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        gate, ticked = threading.Event(), threading.Event()
        stop = []

        def sleep_fn(_):
            ticked.set()
            gate.wait(10)
            gate.clear()
            if stop:
                raise SystemExit

        thread = threading.Thread(target=lambda: daemon_module.run_daemon(sc.sdir, dm, 5, once=False, sleep_fn=sleep_fn, clock=lambda: sc.clock.ms))
        thread.start()
        try:
            require(ticked.wait(10), "daemon 第一轮没跑完")
            ticked.clear()
            code, out, err = sc.cli(["off"])               # 真实 CLI 入口；daemon 持着真实 daemon.lock
            require(code == 0 and sc.world.kills == [] and "交给" in out, "CLI 不执行：%r %r" % (sc.world.kills, out))
            gate.set()
            require(ticked.wait(10), "daemon 第二轮没跑完")
            require([k["pid"] for k in sc.world.kills] == [b["pid"]], "daemon 的下一轮执行恰好一次：%r" % sc.world.kills)
        finally:
            stop.append(1)
            gate.set()
            thread.join(10)
    return "真实 run_daemon 持锁运行：CLI off 只落意图；daemon 下一轮执行，信号恰好 1 次"


def c_paused_executor():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        abandon_like_off(sc)
        paused, release = threading.Event(), threading.Event()

        def point(label):
            if label == "offkill:marker":
                paused.set()
                release.wait(10)

        runner = sc.daemon(point_fn=point)
        thread = threading.Thread(target=lambda: daemon_module.run_daemon(sc.sdir, runner, 5, once=True, clock=lambda: sc.clock.ms))
        thread.start()
        try:
            require(paused.wait(10), "daemon 没走到认领后的切点")
            code, out, err = sc.cli(["off"])                 # daemon 已认领、发信号前被暂停；CLI 入口此刻进来
            require(code == 0 and sc.world.kills == [] and "交给" in out, "CLI 拿不到锁：不执行、不发信号：%r" % sc.world.kills)
        finally:
            release.set()
            thread.join(10)
        require(len(sc.world.kills) == 1 and not thread.is_alive(), "总共只有 daemon 发的 1 次信号：%r" % sc.world.kills)
    return "daemon 认领后暂停时 CLI off 进来：CLI 拿不到锁、不执行；总共 1 次信号"


def c_cli_runs_without_daemon():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        code, out, err = sc.cli(["off"])
        require(code == 0 and len(sc.world.kills) == 1, "没有 daemon：CLI 取锁执行 1 次")
        code, out, err = sc.cli(["off"])
        require(code == 0 and len(sc.world.kills) == 1, "重复 off 幂等：不再发信号")
    return "没有 daemon：CLI 取锁执行；重复 off 不再发信号"


def case_h58():
    """H58：CLI `off` 与 daemon 并发——真实 run_daemon（持真实 daemon.lock）+ 真实 CLI 入口，调度由测试控制；信号总数恒为 1。"""
    return collect([("daemon 在跑", c_delegate_when_daemon_runs), ("daemon 暂停在认领后", c_paused_executor), ("无 daemon", c_cli_runs_without_daemon)])


# ---------------------------------------------------------------- H59

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")


def run_child(sc, label, logfile):
    """把假世界写成文件，起一个真实子进程跑一轮 daemon；子进程在切点上 SIGKILL 自己（不执行 finally）。返回 (退出码, 日志行列表)。"""
    world = os.path.join(sc.work, "world.json")
    with open(world, "w", encoding="utf-8") as stream:
        json.dump({str(pid): entry for pid, entry in sc.world.table.items()}, stream)
    params = os.path.join(sc.work, "child.json")
    with open(params, "w", encoding="utf-8") as stream:
        json.dump({"mvp": MVP_ROOT, "project": sc.project, "sdir": sc.sdir, "world": world, "log": logfile, "label": label, "now": sc.clock.ms}, stream)
    result = subprocess.run([sys.executable, CHILD, params], capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    lines = [json.loads(line) for line in open(logfile, encoding="utf-8")] if os.path.exists(logfile) else []
    return result.returncode, lines, result.stderr


def real_notice():
    with scenario() as sc:
        a = sc.spawn("sA")
        start_handoff(sc, a)
        sc.world.die(a)                                        # A 在交接中途崩了：daemon 要写 a_crash_unsealed 并响铃
        code, lines, err = run_child(sc, "notice:written", os.path.join(sc.work, "child.log"))
        require(code == -9, "子进程应死于 SIGKILL：%r %s" % (code, err[-200:]))
        require([l["kind"] for l in lines] == ["point"], "notice 已写、发声前被杀：子进程没有发过任何声：%r" % lines)
        require(kinds(sc) == ["a_crash_unsealed"] and sc.ring_count() == 0, "前置：notice 已落盘、未响")
        sc.tick(sc.daemon())
        sc.tick(sc.daemon())
        require(sc.ring_count() == 1 and kinds(sc) == ["a_crash_unsealed"], "重启后补响恰好一次：%d" % sc.ring_count())
    return "真实 kill -9（notice 写盘后、发声前）：重启补响恰好 1 次"


def real_baton():
    with scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        b, hid = full_handoff_until_sealed(sc, a, dm)
        sc.tick(dm)
        sc.cli(["sign", "out"], a)
        sc.cli(["sign", "in"], b)
        sc.stop(a)
        code, lines, err = run_child(sc, "baton:marker", os.path.join(sc.work, "child.log"))
        require(code == -9, "子进程应死于 SIGKILL：%r %s" % (code, err[-200:]))
        child_kills = [l for l in lines if l["kind"] == "kill"]
        require(len(child_kills) == 1 and not [l for l in lines if l["kind"] == "msg" and "BATON" in l["content"]], "子进程：已 commit、已 SIGTERM A、BATON 标记已落但没发：%r" % lines)
        sc.world.die(a)
        sc.tick(sc.daemon())
        sc.tick(sc.daemon())
        require("baton_ambiguous" in kinds(sc) and sc.ring_count() >= 1 and sc.handoff() is None, "重启：BATON 不确定 ⇒ 响铃、收尾：%r" % kinds(sc))
        require(not [1 for pid, c in sc.world.messages if "BATON" in c], "BATON 至多一次（这里 0 次）")
    return "真实 kill -9（baton:marker）：重启后响 baton_ambiguous 并收尾，BATON 不重发"


def real_offkill():
    with scenario() as sc:
        a, b, hid, dm = ready(sc)
        abandon_like_off(sc)
        code, lines, err = run_child(sc, "offkill:marker", os.path.join(sc.work, "child.log"))
        require(code == -9, "子进程应死于 SIGKILL：%r %s" % (code, err[-200:]))
        require(not [l for l in lines if l["kind"] == "kill"], "子进程认领后、发信号前被杀")
        require(sc.state()["off_kill"][0]["state"] == "sending", "意图停在 sending")
        sc.tick(sc.daemon())
        require(kinds(sc) == ["off_kill_unconfirmed"] and sc.ring_count() == 1 and not sc.state().get("off_kill") and sc.world.kills == [],
                "重启：不重发、B 仍活 ⇒ 响铃并结束意图：%r %r" % (kinds(sc), sc.world.kills))
    return "真实 kill -9（off 认领后）：重启不重发信号，B 仍活 ⇒ 响 off_kill_unconfirmed"


def case_h59():
    """H59：三个切点用真实子进程 SIGKILL（不执行 finally / 通知队列冲刷）验证 K1：notice 写盘→发声、baton:marker、off_kill 认领后。"""
    return collect([("notice", real_notice), ("baton:marker", real_baton), ("off_kill sending", real_offkill)])


SILENT_CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silent_child.py")


def run_silent_child(silent):
    """起子进程，用真实的 Notifier 走 `off` 与 `notify` 两条路径；osascript / afplay / say 都换成只记录调用的假程序。返回 (调用记录行, 子进程输出)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        work = os.path.realpath(work)
        log = os.path.join(work, "calls.log")
        fakes = {}
        for name in ("osascript", "afplay", "say"):
            path = os.path.join(work, "fake_" + name)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\necho \"%s $*\" >> \"%s\"\nexit 0\n" % (name, log))
            os.chmod(path, 0o755)
            fakes["fake_" + name] = path
        project = os.path.join(work, "proj")
        os.makedirs(project)
        params = os.path.join(work, "child.json")
        with open(params, "w", encoding="utf-8") as stream:
            json.dump(dict(fakes, mvp=MVP_ROOT, project=project, sdir=os.path.join(project, ".claude", "handoffd")), stream)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CDM_CLAUDE_SESSIONS_DIR=os.path.join(work, "no-sessions"))
        env.pop("HANDOFFD_SILENT", None)
        if silent:
            env["HANDOFFD_SILENT"] = "1"
        result = subprocess.run([sys.executable, SILENT_CHILD, params], capture_output=True, text=True, timeout=60, env=env)
        require(result.returncode == 0 and '"off": 0' in result.stdout, "子进程应正常结束：%r %s" % (result.returncode, result.stderr[-300:]))
        lines = open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
        return lines, result.stdout


def case_h61():
    """H61（静音闸）：设了 HANDOFFD_SILENT=1 时，真实的 Notifier（`handoffd off` 走到的 run_daemon_once、`handoffd notify`）一次也不调
    osascript / afplay / say——这里 notify 模块里的三个程序路径被换成只记录调用的假程序，绝不调用真实的。对照：不设该变量，同样的子进程会调用这些假程序。"""
    quiet_calls, _out = run_silent_child(True)
    require(quiet_calls == [], "HANDOFFD_SILENT=1：一次都不应调用 osascript / afplay / say：%r" % quiet_calls)
    loud_calls, _out = run_silent_child(False)
    names = {line.split()[0] for line in loud_calls}
    require({"osascript", "afplay"} <= names, "对照（不设静音闸）：同一子进程会调用（假的）osascript / afplay，说明用例真的走到了发声路径：%r" % loud_calls)
    return ["HANDOFFD_SILENT=1：`off` 与 `notify` 走真实 Notifier，假 osascript / afplay / say 零调用；对照：不设该变量则调用 %d 次（假程序）" % len(loud_calls)]


def main():
    """运行 H57–H59、H61。"""
    return run_cases([("H57", case_h57), ("H58", case_h58), ("H59", case_h59), ("H61", case_h61)])


if __name__ == "__main__":
    sys.exit(main())
