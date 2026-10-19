# 测试 H62–H82：handoffd S3「装成能用」的离线部分（launchctl 全部是假的，不碰真实 launchd；通知走假 runner / 静音闸，不出声）。
# H62 on 拉起 daemon（plist 逐键、落 <S>/launchd/、不进 LaunchAgents、已装不重装）；H63 off 的收尾保护（有未收尾意图不 bootout）；H64 status 的 daemon 行；
# H65 daemon 抢锁（等锁、非 0 退出）；H66 旧 cdm 退役 + test-alert；H67 / H68 G7（bootout 失败、print 查询失败、替换失败）；
# H69 G9 的 sound / sounds 优先级与 test-alert 不轮换；H70 G9 的 make_voice_clips.sh（末行无换行、发布阶段回滚）；H71 G10 轮换持久化；H72 新话术与 README；H73 label 哈希；H74 daemon 在 manual 无未收尾意图时自退；H75 on 对「已加载没在跑」kickstart；H76 launch_config 开窗；H77 临时 Warp 配置清理；H78 off 文案；H79 兜底清扫过期的临时 Warp 配置；H80 临时配置的准确路径与 off 删除；H81 manual status 心跳；H82 on 对卡住的 daemon kickstart -k。
import contextlib
import hashlib
import io
import json
import os
import shutil
import plistlib
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import FIXTURE_VOICE, MVP_ROOT, patched_env, require, run_cases, temp_dir, write_exe  # noqa: E402
from swap_helpers import b_comes_online, scenario, start_handoff  # noqa: E402
from test_launcher import Fixture, fake_claude  # noqa: E402
from relay_helpers import fixed_clock, handoff_with_b, sdir_of, write_baton  # noqa: E402
from cdm import baton, config, daemon as daemon_module, handoffd_cli, notify, sessions, util  # noqa: E402
from cdm.notify import RecordingNotifier  # noqa: E402

def label_for(project, name="proj"):
    """launchd label = local.handoffd.<项目名>.<项目绝对路径 sha256 的前 8 位>（Owner 裁定）。"""
    return "local.handoffd.%s.%s" % (name, hashlib.sha256(project.encode("utf-8")).hexdigest()[:8])


FAKE_LAUNCHCTL = """#!/bin/sh
# 带状态的假 launchctl：状态 = $FAKE_LC_STATE/loaded 文件；调用记录追加到 $FAKE_LC_LOG。
# 故障注入：FAKE_LC_PRINT=<退出码>（print 一律这样失败）、FAKE_LC_BOOTOUT=fail|fail_but_gone、FAKE_LC_FAIL_IF_CONTAINS=<串>（plist 含该串时 bootstrap 失败）、FAKE_LC_RUNNING=no（print 报「state = not running」）、FAKE_LC_KICK=fail（kickstart 失败）。
printf '%s\\n' "$*" >> "$FAKE_LC_LOG"
case "$1" in
  print)
    if [ -n "$FAKE_LC_PRINT" ]; then echo "boom" >&2; exit "$FAKE_LC_PRINT"; fi
    if [ -e "$FAKE_LC_STATE/loaded" ]; then
      if [ "$FAKE_LC_RUNNING" = no ]; then echo "	state = not running"; else echo "	state = running"; fi
      exit 0
    fi
    echo "Could not find service" >&2; exit 113 ;;
  kickstart)
    if [ "$FAKE_LC_KICK" = fail ]; then echo "kickstart failed" >&2; exit 5; fi
    exit 0 ;;
  bootstrap)
    if [ -n "$FAKE_LC_FAIL_IF_CONTAINS" ] && grep -q -- "$FAKE_LC_FAIL_IF_CONTAINS" "$3"; then echo "bootstrap failed" >&2; exit 5; fi
    touch "$FAKE_LC_STATE/loaded"; exit 0 ;;
  bootout)
    if [ "$FAKE_LC_BOOTOUT" = fail ]; then echo "bootout failed" >&2; exit 5; fi
    rm -f "$FAKE_LC_STATE/loaded"
    if [ "$FAKE_LC_BOOTOUT" = fail_but_gone ]; then echo "bootout failed" >&2; exit 5; fi
    exit 0 ;;
esac
exit 0
"""


class FakeLaunchctl:
    def __init__(self, work):
        self.state = os.path.join(work, "lc_state")
        os.makedirs(self.state)
        self.log = os.path.join(work, "lc.log")
        self.path = os.path.join(work, "bin", "launchctl")
        write_exe(self.path, FAKE_LAUNCHCTL)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as stream:
            return stream.read().splitlines()

    def verbs(self, since=0):
        return [line.split()[0] for line in self.calls()[since:]]

    def loaded(self):
        return os.path.exists(os.path.join(self.state, "loaded"))


@contextlib.contextmanager
def fake_launchctl(work, **faults):
    """CDM_LAUNCHCTL_BIN 指向带状态的假 launchctl；faults 是额外的 FAKE_LC_* 环境变量（值 None = 不设）。"""
    fake = FakeLaunchctl(work)
    changes = {"CDM_LAUNCHCTL_BIN": fake.path, "FAKE_LC_LOG": fake.log, "FAKE_LC_STATE": fake.state, "FAKE_LC_PRINT": None, "FAKE_LC_BOOTOUT": None,
               "FAKE_LC_FAIL_IF_CONTAINS": None, "FAKE_LC_RUNNING": None, "FAKE_LC_KICK": None}
    changes.update(faults)
    with patched_env(**changes):
        yield fake


def make_project(work, name="proj"):
    project = os.path.join(work, name)
    os.makedirs(project)
    return project


def cli(argv, project, notifier=None):
    """进程内跑 handoffd 命令（Owner 终端：无会话、无祖先链）；返回 (退出码, 标准输出, 标准错误)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = handoffd_cli.main(argv, env={"CLAUDE_PROJECT_DIR": project}, chain_fn=lambda deadline: [], notifier=notifier)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def plist_file(sdir, project):
    return os.path.join(sdir, "launchd", label_for(project) + ".plist")


def read_plist(sdir, project):
    with open(plist_file(sdir, project), "rb") as stream:
        return plistlib.load(stream)


def write_config(work, project, name="cfg.json"):
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"project_dir": project}, stream)
    return path


@contextlib.contextmanager
def daemon_lock_held(sdir):
    """模拟「有 daemon 在跑」：持着真实的 daemon.lock，CLI 的非阻塞试取拿不到，收尾留给 daemon。"""
    os.makedirs(sdir, exist_ok=True)
    fd = daemon_module.acquire_daemon_lock(os.path.join(sdir, "daemon.lock"), wait=0)
    require(fd is not None, "测试里取 daemon.lock 失败")
    try:
        yield
    finally:
        os.close(fd)


def case_h62():
    """H62（§1.1）：`handoffd on` 拉起 daemon——plist 逐键断言、落 <S>/launchd/、绝不进 ~/Library/LaunchAgents（不开机自启）、bootstrap 参数、已装不重复装。"""
    with temp_dir() as work, fake_launchctl(work) as fake:
        home = os.path.join(work, "home")
        os.makedirs(home)
        project = make_project(work)
        sdir = sdir_of(project)
        with patched_env(HOME=home):
            code, out, err = cli(["on"], project)
            require(code == 0 and "daemon：已拉起" in out, "on 应拉起 daemon：%r %r %r" % (code, out, err))
            directory = os.path.join(sdir, "launchd")
            expected = {
                "Label": label_for(project),
                "ProgramArguments": ["/usr/bin/python3", os.path.join(MVP_ROOT, "bin", "handoffd"), "daemon"],
                "WorkingDirectory": project,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 30,
                "StandardOutPath": os.path.join(directory, "daemon.out.log"),
                "StandardErrorPath": os.path.join(directory, "daemon.err.log"),
                "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"},
            }
            plist = read_plist(sdir, project)
            for key, value in expected.items():
                require(plist.get(key) == value, "plist 键 %s 不对：%r != %r" % (key, plist.get(key), value))
            require(sorted(plist) == sorted(expected), "plist 多了或少了键：%r" % sorted(plist))
            require(plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"] is True, "崩溃被拉起（SuccessfulExit=false）")
            require(fake.calls() == ["print gui/%d/%s" % (os.getuid(), label_for(project)), "bootstrap gui/%d %s" % (os.getuid(), plist_file(sdir, project))], "launchctl 调用不对：%r" % fake.calls())
            require(not os.path.exists(os.path.join(home, "Library", "LaunchAgents")) and not any(
                name.endswith(".plist") for _base, _dirs, files in os.walk(home) for name in files), "plist 不得进 ~/Library/LaunchAgents（不开机自启）")
            code, out, err = cli(["on"], project)
            require(code == 0 and "不重复安装" in out and fake.verbs() == ["print", "bootstrap", "print"], "已装：不重复装：%r %r" % (out, fake.calls()))
    return ["plist 逐键断言；落 <S>/launchd/、HOME 下无任何 plist；bootstrap 参数；再敲 on 只 print、不再 bootstrap"]


def make_pending(kind, sdir):
    """造一种「off 之后还没收尾」的状态：off_kill 意图 / 换棒后的收尾 / 已写未响的 notice。"""
    if kind == "off_kill":
        util.atomic_write_json(baton.baton_path(sdir), {"version": 1, "holder": None, "handoff": None, "retire": [],
                                                         "off_kill": [{"hid": "h-9", "uuid": "u", "pid": 4242, "lstart": "x", "state": "sent", "ms": 1}]})
    elif kind == "committed":
        post = {"kill": {"state": "pending"}, "baton": {"state": "pending"}, "a_old": {"sid": "sA", "pid": 1, "lstart": "x"}}
        write_baton(sdir, handoff=handoff_with_b("u", phase="committed", post=post, committed_ms=1))
    else:
        sessions.append_notice(sdir, {"type": "notice", "id": "n-h-9-x", "t_ms": 1, "cat": "handoff", "hid": "h-9", "kind": "x", "ring": True,
                                      "status": "pending", "text": "t", "what": "w", "options": ["o"]})


def clear_pending(sdir):
    write_baton(sdir)
    try:
        os.unlink(sessions.notices_path(sdir))
    except FileNotFoundError:
        pass


def case_h63():
    """H63（§1.1，S2a 待裁 2）：`handoffd off`——没有未收尾的交接意图才 bootout 并删 plist；有（off_kill / 换棒后的收尾 / 未响 notice）则保留 daemon，
    status 显示「daemon 收尾中」；收完后再 off 才停。"""
    details = []
    with temp_dir() as work, fake_launchctl(work) as fake:
        project = make_project(work)
        sdir = sdir_of(project)
        require(cli(["on"], project)[0] == 0 and fake.loaded() and os.path.exists(plist_file(sdir, project)), "前提：on 之后 daemon 已加载")
        with daemon_lock_held(sdir):   # 「有 daemon 在跑」：CLI 当场收不了尾，意图原样留在登记册 / 通知记录里
            for kind in ("off_kill", "committed", "notice"):
                make_pending(kind, sdir)
                before = len(fake.calls())
                code, out, err = cli(["off"], project)
                require(code == 0 and "daemon：保留" in out and "bootout" not in fake.verbs(before) and fake.loaded() and os.path.exists(plist_file(sdir, project)),
                        "%s：有未收尾意图时 daemon 不能停：%r %r %r" % (kind, code, out, fake.calls()[before:]))
                code, out, err = cli(["status"], project)
                require("daemon：收尾中" in out, "%s：status 应显示「daemon 收尾中」：%r" % (kind, out))
                clear_pending(sdir)
                details.append("%s：off 保留 daemon、status 显示收尾中" % kind)
            before = len(fake.calls())
            code, out, err = cli(["off"], project)
            require(code == 0 and "daemon：已停止" in out and "bootout" in fake.verbs(before) and not fake.loaded() and not os.path.exists(plist_file(sdir, project)),
                    "收完后再 off：bootout 并删 plist：%r %r" % (out, fake.calls()[before:]))
            code, out, err = cli(["status"], project)
            require("daemon：未启用（manual）" in out and "不在 launchd 里" in out, "停掉后 status：%r" % out)
        details.append("无未收尾意图：off 才 bootout、删 plist")
    return details


def case_h64():
    """H64（§1.1）：status 显示 daemon 是否在 launchd 里（在 / 不在 / 查询失败＝不确定）与心跳年龄。"""
    with temp_dir() as work, fake_launchctl(work) as fake, fixed_clock() as clock:
        project = make_project(work)
        sdir = sdir_of(project)
        require(cli(["on"], project)[0] == 0, "前提：on")
        sessions.write_heartbeat(sdir, clock.ms - 3000)
        code, out, err = cli(["status"], project)
        require("daemon（launchd）：已在 launchd 里（%s）" % label_for(project) in out and "心跳 3 秒前（正常）" in out, "加载且心跳正常：%r" % out)
        os.unlink(os.path.join(fake.state, "loaded"))
        code, out, err = cli(["status"], project)
        require("daemon（launchd）：不在 launchd 里" in out, "没加载：%r" % out)
        with patched_env(FAKE_LC_PRINT="1"):
            code, out, err = cli(["status"], project)
        require("查询 launchd 失败，不确定" in out and "不在 launchd 里" not in out, "查询失败不得说成「不在」：%r" % out)
        sessions.write_heartbeat(sdir, clock.ms - 200_000)
        code, out, err = cli(["status"], project)
        require("已停" in out, "心跳已停：%r" % out)
    return ["status：launchd 在 / 不在 / 查询失败（不确定）三态；心跳正常 / 已停"]


def case_h65():
    """H65（§1.2，S2a R3 Opus P2-1）：常驻 daemon（launchd 拉起）遇到锁被占——先等；仍占着以非 0 退出（交给 KeepAlive 重拉，不能 0）；等的过程中锁释放则拿到锁正常跑。"""
    with temp_dir() as work:
        path = os.path.join(work, "daemon.lock")
        holder = daemon_module.acquire_daemon_lock(path, wait=0)
        require(holder is not None, "前提")
        ticks, out = [], []

        class Spy:
            def tick(self, now):
                ticks.append(now)

        class Stop(Exception):
            pass

        def stop(_seconds):
            raise Stop()

        start = time.monotonic()
        code = daemon_module.run_daemon(work, Spy(), 5, once=False, out=out.append, lock_wait=0.5, lock_interval=0.05)
        require(code != 0 and ticks == [] and time.monotonic() - start >= 0.4, "锁一直被占：等够时间、不 tick、非 0 退出：%r %r" % (code, ticks))
        threading.Timer(0.3, lambda: os.close(holder)).start()
        try:
            daemon_module.run_daemon(work, Spy(), 5, once=False, out=out.append, lock_wait=3.0, lock_interval=0.05, sleep_fn=stop, clock=lambda: 7)
        except Stop:
            pass
        require(ticks == [7], "等锁期间锁被释放：应拿到锁并跑一轮：%r" % ticks)
    return ["锁被占 ⇒ 等满后非 0 退出且不 tick；等锁期间释放 ⇒ 拿到锁正常 tick"]


def case_h66():
    """H66（§1.3）：旧 cdm 命令行退役（bin/cdm、cdm/cli.py 已删）；`handoffd test-alert <kind>` 接替（三类、用配置、坏配置报错、未知类别拒）。"""
    require(not os.path.exists(os.path.join(MVP_ROOT, "bin", "cdm")) and not os.path.exists(os.path.join(MVP_ROOT, "cdm", "cli.py")), "旧入口应已删除")
    with temp_dir() as work:
        project = make_project(work)
        for kind in notify.KINDS:
            rec = RecordingNotifier()
            code, out, err = cli(["test-alert", kind], project, notifier=rec)
            require(code == 0 and rec.count(kind) == 1 and len(rec.calls) == 1, "test-alert %s：应发一次 %s：%r" % (kind, kind, rec.calls))
        code, out, err = cli(["test-alert", "exit_reminder"], project)
        require(code == 2, "未知类别应被拒（退出 2）：%r" % code)
        cfg = write_config(work, project)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([os.path.join(MVP_ROOT, "bin", "handoffd"), "test-alert", "quiet", "--config", cfg], capture_output=True, text=True, env=env, timeout=60)
        require(result.returncode == 0, "真实入口 bin/handoffd test-alert：%r %s" % (result.returncode, result.stderr))
        with open(cfg, "w", encoding="utf-8") as stream:
            json.dump({"project_dir": project, "bogus": 1}, stream)
        result = subprocess.run([os.path.join(MVP_ROOT, "bin", "handoffd"), "test-alert", "quiet", "--config", cfg], capture_output=True, text=True, env=env, timeout=60)
        require(result.returncode == 2 and "配置错误" in result.stderr, "坏配置应报错退出 2：%r" % result.returncode)
    return ["bin/cdm 与 cdm/cli.py 不存在；test-alert 三类各发一次；未知类别拒；真实入口可用、坏配置退出 2"]


def case_h67():
    """H67（§1.4 G7，off 侧）：bootout 失败不漏任务（仍加载 ⇒ 报错、保留 plist；已不在 ⇒ 按成功）；print 查询失败按「不确定」报错（off 与 on 都不动东西）。"""
    details = []
    with temp_dir() as work, fake_launchctl(work) as fake:
        project = make_project(work)
        sdir = sdir_of(project)
        require(cli(["on"], project)[0] == 0, "前提：on")
        before = open(plist_file(sdir, project), "rb").read()
        with patched_env(FAKE_LC_BOOTOUT="fail"):
            code, out, err = cli(["off"], project)
        require(code == 1 and "任务仍在运行" in err and fake.loaded() and open(plist_file(sdir, project), "rb").read() == before, "bootout 失败且仍加载：退出 1、保留 plist：%r %r" % (code, err))
        details.append("bootout 失败且任务仍在：退出 1、plist 保留")
        count = len(fake.calls())
        with patched_env(FAKE_LC_PRINT="1"):
            code, out, err = cli(["off"], project)
        require(code == 1 and "不确定" in err and os.path.exists(plist_file(sdir, project)) and fake.verbs(count) == ["print"], "print 查询失败：按不确定报错、plist 保留、不 bootout：%r %r" % (code, err))
        count = len(fake.calls())
        with patched_env(FAKE_LC_PRINT="1"):
            code, out, err = cli(["on"], project)
        require(code == 1 and "不确定" in err and fake.verbs(count) == ["print"], "on 时 print 查询失败：按不确定报错、不 bootstrap：%r %r" % (code, fake.calls()[count:]))
        details.append("print 查询失败：off / on 都按不确定报错，不改任何东西")
        with patched_env(FAKE_LC_BOOTOUT="fail_but_gone"):
            code, out, err = cli(["off"], project)
        require(code == 0 and "已不在" in out and not os.path.exists(plist_file(sdir, project)), "bootout 报错但任务已不在：按成功、删 plist：%r %r" % (out, err))
        code, out, err = cli(["off"], project)
        require(code == 0 and "本来就不在" in out, "没加载（113）：按成功：%r" % out)
        details.append("bootout 报错但任务已不在 / 本来没加载：按成功、删 plist")
    return details


def case_h68():
    """H68（§1.4 G7，替换侧）：on 发现 plist 内容变了（带了新的 --config）——bootout 旧任务再 bootstrap 新的；替换失败后恢复旧 plist 并重新加载旧任务；
    旧任务 bootout 不掉则不动旧任务。"""
    details = []
    with temp_dir() as work, fake_launchctl(work) as fake:
        project = make_project(work)
        sdir = sdir_of(project)
        cfg_a = write_config(work, project, "a.json")
        cfg_b = write_config(work, project, "b.json")
        require(cli(["on"], project)[0] == 0, "前提：on")
        count = len(fake.calls())
        code, out, err = cli(["on", "--config", cfg_a], project)
        require(code == 0 and fake.verbs(count) == ["print", "bootout", "bootstrap"] and read_plist(sdir, project)["ProgramArguments"][-2:] == ["--config", cfg_a],
                "plist 变了：先 bootout 再 bootstrap 新的：%r %r" % (code, fake.calls()[count:]))
        details.append("plist 变了：print → bootout → bootstrap，新 plist 带 --config")
        good = open(plist_file(sdir, project), "rb").read()
        count = len(fake.calls())
        with patched_env(FAKE_LC_FAIL_IF_CONTAINS="b.json"):
            code, out, err = cli(["on", "--config", cfg_b], project)
        require(code == 1 and "已重新加载" in err and open(plist_file(sdir, project), "rb").read() == good and fake.loaded()
                and fake.verbs(count) == ["print", "bootout", "bootstrap", "bootstrap"], "替换失败：恢复旧 plist 并重新加载旧任务：%r %r %r" % (code, err, fake.calls()[count:]))
        details.append("替换失败：旧 plist 原样恢复、旧任务重新 bootstrap")
        count = len(fake.calls())
        with patched_env(FAKE_LC_BOOTOUT="fail"):
            code, out, err = cli(["on", "--config", cfg_b], project)
        require(code == 1 and "没有换新" in err and open(plist_file(sdir, project), "rb").read() == good and fake.loaded() and "bootstrap" not in fake.verbs(count),
                "旧任务 bootout 不掉：不动 plist、不 bootstrap：%r %r" % (code, fake.calls()[count:]))
        details.append("旧任务 bootout 不掉：plist 与旧任务都不动")
    return details


def case_h69():
    """H69（§1.5 G9）：`sound` / `sounds` 优先级——sounds 非空就忽略 sound，要用 sound 须同时写 `sounds: []`；`test-alert` 每次从第一个铃声开始（不接着上次轮换）。"""
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append(args)
        return Result()

    def tone_for(alerts_raw):
        cfg = config.build_config({"project_dir": "/tmp/x", "alerts": alerts_raw})
        calls.clear()
        notify.Notifier(cfg["alerts"], runner=runner).alert("problem", "x")
        return [args[1] for args in calls if os.path.basename(args[0]) == "afplay"][:1]   # 只看铃声

    ripples = "/System/Library/Sounds/Glass.aiff"   # 默认第一个铃声（系统自带）
    require(tone_for({"problem": {"sound": "/x/custom.m4a"}}) == [ripples], "只写 sound：默认 sounds 优先，sound 被忽略")
    require(tone_for({"problem": {"sound": "/x/custom.m4a", "sounds": []}}) == ["/x/custom.m4a"], "sounds: [] + sound：用 sound")
    require(tone_for({"problem": {"sounds": ["/x/one.m4a", "/x/two.m4a"], "sound": "/x/custom.m4a"}}) == ["/x/one.m4a"], "sounds 非空：用 sounds，忽略 sound")
    twinkle = "/System/Library/Sounds/Ping.aiff"
    original = notify.Notifier
    notify.Notifier = lambda alerts, **kw: original(alerts, runner=runner, **kw)
    try:
        with temp_dir() as work:
            project = make_project(work)
            calls.clear()
            for _ in range(3):
                require(cli(["test-alert", "problem"], project)[0] == 0, "test-alert 应成功")
            tones = [args[1] for args in calls if os.path.basename(args[0]) == "afplay" and args[1].endswith(".aiff")]   # 只看铃声
    finally:
        notify.Notifier = original
    require(tones == [ripples] * 3 and twinkle not in tones, "test-alert 连敲三次都从第一个铃声开始（不轮换）：%r" % tones)
    return ["只写 sound 被默认 sounds 盖住；sounds: [] + sound 才生效；sounds 非空忽略 sound；test-alert 三次都从第一个铃声开始"]


VOICE_SCRIPT = os.path.join(MVP_ROOT, "tools", "make_voice_clips.sh")
FAKE_TTS = """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in --text=*) T="${1#--text=}";; --write-media) shift; OUT="$1";; esac; shift; done
printf '%s' "$T" > "$OUT"
"""
FAKE_MV = """#!/bin/bash
for a in "$@"; do case "$a" in *"$FAKE_MV_FAIL"*) [ -n "$FAKE_MV_FAIL" ] && exit 1 ;; esac; done
exec /bin/mv "$@"
"""


def voice_tree(work):
    """临时 tools/ + assets/voice/（两个旧音色目录；phrases / voices 末行都没有换行）。"""
    voice = os.path.join(work, "assets", "voice")
    os.makedirs(os.path.join(work, "tools"))
    with open(VOICE_SCRIPT, encoding="utf-8") as stream:
        write_exe(os.path.join(work, "tools", "make_voice_clips.sh"), stream.read())
    for name in ("old1", "old2"):
        os.makedirs(os.path.join(voice, name))
        with open(os.path.join(voice, name, "01.mp3"), "w", encoding="utf-8") as stream:
            stream.write("旧 " + name)
    with open(os.path.join(voice, "phrases.txt"), "w", encoding="utf-8") as stream:
        stream.write("第一句\n第二句\n末句没有换行")
    with open(os.path.join(voice, "voices.txt"), "w", encoding="utf-8") as stream:
        stream.write("# 注释\nv1 +0% +0Hz\nv2 -5% +5Hz")
    write_exe(os.path.join(work, "fake_tts"), FAKE_TTS)
    write_exe(os.path.join(work, "fbin", "mv"), FAKE_MV)
    return voice


def run_voice(work, **env_extra):
    env = dict(os.environ, EDGE_TTS=os.path.join(work, "fake_tts"), PATH=os.path.join(work, "fbin") + os.pathsep + os.environ["PATH"])
    env.update(env_extra)
    return subprocess.run(["/bin/bash", os.path.join(work, "tools", "make_voice_clips.sh")], capture_output=True, text=True, env=env, timeout=60)


def voice_files(voice):
    found = {}
    for base, _dirs, files in os.walk(voice):
        for name in files:
            with open(os.path.join(base, name), encoding="utf-8") as stream:
                found[os.path.relpath(os.path.join(base, name), voice)] = stream.read()
    return found


def voice_dirs(voice):
    return sorted(os.path.relpath(os.path.join(base, name), voice) for base, dirs, _files in os.walk(voice) for name in dirs)


def case_h70():
    """H70（§1.5 G9）：make_voice_clips.sh——phrases / voices 末行没有换行也不丢；发布阶段中途失败整批回滚（换新目录失败、挪旧目录进备份失败两处），原有文件逐字节不变。"""
    details = []
    with temp_dir() as work:
        voice = voice_tree(work)
        result = run_voice(work)
        require(result.returncode == 0, "成功路径：%r %s" % (result.returncode, result.stderr))
        files = voice_files(voice)
        for name in ("v1", "v2"):
            require([files.get("%s/0%d.mp3" % (name, n)) for n in (1, 2, 3)] == ["第一句", "第二句", "末句没有换行"] and "%s/04.mp3" % name not in files,
                    "%s：末行没有换行的句子不能丢：%r" % (name, sorted(files)))
        require(any(path.startswith(".previous/") and path.endswith("old1/01.mp3") for path in files), "旧音色应在备份里")
        details.append("末行无换行（phrases / voices 都是）：3 句 × 2 音色齐全")
    for label, fail in (("换新目录失败（第二个音色）", ".staging/v2"), ("旧目录挪进备份失败", "old2")):
        with temp_dir() as work:
            voice = voice_tree(work)
            before_files, before_dirs = voice_files(voice), voice_dirs(voice)
            result = run_voice(work, FAKE_MV_FAIL=fail)
            require(result.returncode != 0 and "已回滚" in result.stderr, "%s：应非 0 退出并说已回滚：%r %s" % (label, result.returncode, result.stderr))
            require(voice_files(voice) == before_files and voice_dirs(voice) == before_dirs, "%s：回滚后应与原来完全一致：%r vs %r" % (label, voice_dirs(voice), before_dirs))
            details.append("%s：整批回滚，文件与目录都和原来一致（无 .staging / 空 .previous）" % label)
    return details


def case_h71():
    """H71（§1.6 G10）：铃声 / 音色轮换位置由 daemon 持久化到 daemon_state.json——daemon 重启（新 Daemon、新 Notifier）后接着轮，不是每次从头；坏文件按从头；不冲掉文件里别的键。"""
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append(args)
        return Result()

    with temp_dir() as work:
        project = make_project(work)
        sdir = sdir_of(project)
        cfg = config.default_config(project)
        # 轮换要多个音色才测得出来；发布版不带音频，所以在临时目录造 3 个音色（内容复制 tests/fixtures 里的占位片段）
        voice_dir = os.path.join(work, "voices")
        for name in ("voice-a", "voice-b", "voice-c"):
            shutil.copytree(FIXTURE_VOICE, os.path.join(voice_dir, name))
        alerts = dict(cfg["alerts"], problem=dict(cfg["alerts"]["problem"], voice_clips_dir=voice_dir))
        state_path = os.path.join(sdir, "daemon_state.json")
        util.atomic_write_json(state_path, {"last_claude_version": "9.9.9"})

        def ring_once():
            """模拟 daemon 的一次（重启后的）发声：新 Daemon + 新 Notifier。"""
            calls.clear()
            dm = daemon_module.Daemon(cfg, sdir, notify.Notifier(alerts, runner=runner))
            dm.alert("problem", "x")
            plays = [args[1] for args in calls if os.path.basename(args[0]) == "afplay"]
            return os.path.basename(plays[0]), os.path.basename(os.path.dirname(plays[1]))

        picks = [ring_once() for _ in range(4)]
        require([tone for tone, _voice in picks] == ["Glass.aiff", "Ping.aiff"] * 2, "铃声应跨重启交替：%r" % picks)
        voices = sorted(name for name in os.listdir(alerts["problem"]["voice_clips_dir"]) if os.path.isdir(os.path.join(alerts["problem"]["voice_clips_dir"], name)))
        require(len(voices) == 3 and [voice for _tone, voice in picks] == voices + voices[:1], "音色应跨重启按目录名轮换：%r" % picks)
        status, state = util.read_json(state_path)
        require(status == "ok" and state["rotation"]["tone_next"] == 4 and state["rotation"]["voice_next"] == 4 and state["last_claude_version"] == "9.9.9",
                "轮换位置落盘，且不冲掉别的键：%r" % state)
        util.atomic_write_json(state_path, {"rotation": {"tone_next": "坏", "voice_next": -3, "last_clip": 7}, "last_claude_version": "9.9.9"})
        require(ring_once()[0] == "Glass.aiff", "轮换记录坏了：按从头轮，不崩")
        with open(state_path, "w", encoding="utf-8") as stream:
            stream.write("{坏 json")
        require(ring_once()[0] == "Glass.aiff", "daemon_state.json 读不出：按从头轮，不崩")
    return ["新 Daemon 实例（重启）接着轮：铃声 R/T/R/T、音色按目录名轮换；daemon_state.json 里别的键保留；坏记录 / 坏文件按从头"]


def case_h72():
    """H72（§1.7、§1.8）：新话术 10 句、不含 exit / 退棒、都是「Claude 需要你看一眼、去提示里的窗口」；README ≤150 行且不再讲旧 cdm 流程，写全三个命令、记录位置、已知限制。"""
    with open(os.path.join(MVP_ROOT, "assets", "voice", "phrases.txt"), encoding="utf-8") as stream:
        lines = [line for line in stream.read().splitlines() if line.strip()]
    require(len(lines) == 10, "应是 10 句：%d" % len(lines))
    for line in lines:
        require("exit" not in line.lower() and "退棒" not in line and "Claude" in line and "窗口" in line and "提示" in line, "话术不合格：%r" % line)
    require(len(set(lines)) == 10, "10 句不得重复")
    with open(os.path.join(MVP_ROOT, "README.md"), encoding="utf-8") as stream:
        readme = stream.read()
    require(len(readme.splitlines()) <= 150, "README 应 ≤150 行：%d" % len(readme.splitlines()))
    for needle in ("handoffd on", "handoffd off", "handoffd status", "!handoffd", "baton.json", "events.jsonl", "notices.jsonl", "handoffs.jsonl", "HANDOFFD_SILENT"):
        require(needle in readme, "README 缺：%s" % needle)
    for stale in ("bin/cdm", "mark-ready", "install-launchd", "launch-anyway", "exit_reminder", "cdm.json"):
        require(stale not in readme, "README 不应再提旧流程：%s" % stale)
    with open(os.path.join(MVP_ROOT, "assets", "voice", "README.md"), encoding="utf-8") as stream:
        voice_readme = stream.read()
    require("bin/cdm" not in voice_readme and "exit_reminder" not in voice_readme, "voice README 不应再提旧入口 / 旧类别")
    return ["话术 10 句不含 exit / 退棒；README %d 行、不提旧命令、写全三个命令与记录位置" % len(readme.splitlines())]


def case_h73():
    """H73（Owner 裁定 1）：launchd label 带项目绝对路径的 8 位短哈希——同名的两个项目 label 不同、plist 文件名不同，且都 on 得上。"""
    with temp_dir() as work, fake_launchctl(work) as fake:
        first = make_project(work, "proj")
        os.makedirs(os.path.join(work, "other"))
        second = make_project(os.path.join(work, "other"), "proj")
        labels = []
        for project in (first, second):
            code, out, err = cli(["on"], project)
            label = label_for(project)
            require(code == 0 and os.path.exists(plist_file(sdir_of(project), project)) and read_plist(sdir_of(project), project)["Label"] == label, "on：%r %r" % (out, err))
            require(label.startswith("local.handoffd.proj.") and len(label.rsplit(".", 1)[1]) == 8, "label 形如 local.handoffd.<名>.<8 位哈希>：%r" % label)
            labels.append(label)
        require(labels[0] != labels[1], "同名项目 label 不同：%r" % labels)
        boots = [line for line in fake.calls() if line.startswith("bootstrap")]
        require(len(boots) == 2 and boots[0].endswith(labels[0] + ".plist") and boots[1].endswith(labels[1] + ".plist"), "各自 bootstrap 自己的 plist：%r" % boots)
    return ["同名项目（/…/proj 与 /…/other/proj）label 后缀哈希不同，各自 plist、各自 bootstrap"]


def case_h74():
    """H74（Owner 裁定 5）：daemon 在 manual 且没有未收尾的交接意图时自己以 0 退出（规格:14）；有未收尾意图 / auto 则继续；main 把这条规则接进常驻循环。"""
    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop()

    class Spy:
        def __init__(self):
            self.ticks = []

        def tick(self, now):
            self.ticks.append(now)

    def run(sdir, **kw):
        spy, out = Spy(), []
        try:
            code = daemon_module.run_daemon(sdir, spy, 5, once=False, out=out.append, sleep_fn=stop, clock=lambda: 7,
                                            idle_exit=lambda: daemon_module.idle_exit_due(sdir), **kw)
        except Stop:
            code = "alive"
        return code, spy.ticks

    with temp_dir() as work:
        sdir = os.path.join(work, "S")
        os.makedirs(sdir)
        require(run(sdir) == (0, [7]), "manual 且没有未收尾意图：跑完这一轮后以 0 退出：%r" % (run(sdir),))
        make_pending(sdir=sdir, kind="off_kill")
        require(run(sdir) == ("alive", [7]), "manual 但有未收尾意图（off_kill）：继续跑")
        clear_pending(sdir)
        make_pending(sdir=sdir, kind="notice")
        require(run(sdir) == ("alive", [7]), "manual 但有已写未响的 notice：继续跑")
        clear_pending(sdir)
        sessions.write_mode(sdir, "auto", 1)
        require(run(sdir) == ("alive", [7]), "auto：继续跑")
        sessions.write_mode(sdir, "manual", 2)
        require(run(sdir)[0] == 0, "回到 manual 且收完：退出")
        captured = {}
        original = daemon_module.run_daemon
        daemon_module.run_daemon = lambda *args, **kw: captured.update(kw) or 0
        try:
            cfg = config.default_config(os.path.join(work, "p"))
            daemon_module.main(cfg, sdir)
        finally:
            daemon_module.run_daemon = original
        require(callable(captured.get("idle_exit")) and captured["idle_exit"]() is True, "main 应把 idle_exit 规则传给常驻循环：%r" % captured)
    return ["manual 无未收尾意图 ⇒ 退出 0；有 off_kill / 未响 notice、auto ⇒ 继续；main 接了 idle_exit"]


def case_h75():
    """H75（Owner 裁定 5）：`on` 发现任务已加载但没在跑（print 报 state = not running），用 launchctl kickstart 拉起；在跑就不动；kickstart 失败退出 1。"""
    with temp_dir() as work, fake_launchctl(work) as fake:
        project = make_project(work)
        require(cli(["on"], project)[0] == 0, "前提：on")
        count = len(fake.calls())
        code, out, err = cli(["on"], project)
        require(code == 0 and fake.verbs(count) == ["print"], "在跑：只 print，不 kickstart：%r" % fake.calls()[count:])
        count = len(fake.calls())
        with patched_env(FAKE_LC_RUNNING="no"):
            code, out, err = cli(["on"], project)
        require(code == 0 and "kickstart" in out and fake.calls()[count:] == ["print gui/%d/%s" % (os.getuid(), label_for(project)),
                                                                              "kickstart gui/%d/%s" % (os.getuid(), label_for(project))],
                "没在跑：应 kickstart：%r %r" % (out, fake.calls()[count:]))
        count = len(fake.calls())
        with patched_env(FAKE_LC_RUNNING="no", FAKE_LC_KICK="fail"):
            code, out, err = cli(["on"], project)
        require(code == 1 and "kickstart 拉起失败" in err and "bootstrap" not in fake.verbs(count), "kickstart 失败：退出 1：%r %r" % (code, err))
    return ["在跑 ⇒ 只 print；没在跑 ⇒ kickstart gui/<uid>/<label>；kickstart 失败 ⇒ 退出 1"]


def case_h76():
    """H76（真机发现 a）：open_warp.sh --warp-open launch_config 写 Launch Configuration（1 window / 1 tab：title=<名>、cwd=项目绝对路径、exec 命令文件）并
    `open -g warp://launch/<名>`，不写标签页配置；tab_config 保持原样；非法取值预检失败不调 open；config 键 warp_open 默认 launch_config 并传给启动器。"""
    with temp_dir() as work:
        fx = Fixture(work)
        fake_claude(fx.target, "Claude Code 2.1.290")
        result = fx.launch(name="b-1", script_args=("--warp-open", "launch_config"))
        require(result.returncode == 0, "launch_config 开窗：%r %s" % (result.returncode, result.stderr))
        command = os.path.join(fx.commands, "b-1.command")
        path = os.path.join(fx.launches, "b-1.yaml")
        import shlex
        expected = ("---\nname: \"b-1\"\nwindows:\n  - tabs:\n      - title: \"b-1\"\n        layout:\n          cwd: %s\n          commands:\n            - exec: %s\n"
                    % (json.dumps(work), json.dumps("exec " + shlex.quote(command))))
        with open(path, encoding="utf-8") as stream:
            require(stream.read() == expected, "Launch Configuration 内容不对")
        require(os.path.isabs(work) and os.path.exists(command) and not os.path.exists(os.path.join(fx.tabs, "b-1.toml")), "命令文件在、没有写标签页配置")
        with open(fx.opened, encoding="utf-8") as stream:
            require(stream.read().splitlines() == ["-g warp://launch/b-1"], "open 参数应是 -g warp://launch/<名>")
        require(("CDM_LAUNCHER_LAUNCH=" + path) in result.stdout.splitlines(), "输出应含 CDM_LAUNCHER_LAUNCH")
        before = fx.open_calls()
        bad = fx.launch(name="b-2", script_args=("--warp-open", "bogus"))
        require(bad.returncode == 2 and fx.open_calls() == before, "非法取值：退出 2、不调 open")
        tab = fx.launch(name="b-3", script_args=("--warp-open", "tab_config"))
        require(tab.returncode == 0 and os.path.exists(os.path.join(fx.tabs, "b-3.toml")) and not os.path.exists(os.path.join(fx.launches, "b-3.yaml")), "tab_config 回退：写 .toml")
    require(config.default_config("/tmp/x")["warp_open"] == "launch_config", "config 默认 launch_config")
    try:
        config.build_config({"project_dir": "/tmp/x", "warp_open": "bogus"})
    except config.ConfigError:
        pass
    else:
        raise AssertionError("warp_open 非法取值应报配置错误")
    captured = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    original = daemon_module.subprocess.run
    daemon_module.subprocess.run = lambda args, **kw: captured.setdefault("args", args) and Done()
    try:
        for mode in ("launch_config", "tab_config"):
            captured.clear()
            cfg = config.build_config({"project_dir": "/tmp/x", "warp_open": mode})
            daemon_module.run_launcher(cfg, "/tmp/x/S", {"b": {"name": "b", "uuid": "u"}})
            args = captured["args"]
            require(args[args.index("--warp-open") + 1] == mode, "run_launcher 应把 warp_open=%s 传给启动器：%r" % (mode, args))
    finally:
        daemon_module.subprocess.run = original
    return ["launch_config：yaml 逐字 + open warp://launch/<名> + 不写 toml；tab_config 回退；非法取值 2；config 默认 launch_config 并传给启动器"]


def case_h77():
    """H77（真机发现 b）：开窗用的临时配置（.yaml / .toml，HOME 指到临时目录）B 上线后由 daemon 删除；B 一直没上线则 launch_confirm_timeout 到期后删；
    上线 / 到期之前不删；删除失败只记 events、不崩、不重复记。"""
    details = []

    def launcher_with_files(sc):
        def launcher(cfg, sdir, handoff):
            result = sc.world.launcher_fn(cfg, sdir, handoff)
            for path in daemon_module.warp_config_paths(handoff["b"]["name"]):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("temp")
            return result
        return launcher

    def paths(sc):
        return daemon_module.warp_config_paths(sc.handoff()["b"]["name"])

    env = {"CDM_WARP_TAB_DIR": None, "CDM_WARP_LAUNCH_DIR": None}
    with temp_dir() as home, patched_env(HOME=home, **env):
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon(launcher_fn=launcher_with_files(sc))
            hid, uuid = start_handoff(sc, a)
            sc.tick(dm)
            require(all(os.path.exists(path) for path in paths(sc)) and all(path.startswith(home) for path in paths(sc)), "开窗后、B 上线前：配置还在（HOME 下）")
            sc.tick(dm, 30)
            require(all(os.path.exists(path) for path in paths(sc)), "未上线、未到期：不删")
            b_comes_online(sc, uuid)
            sc.tick(dm)
            require(not any(os.path.exists(path) for path in paths(sc)) and len(sc.events("daemon:warp_config_removed")) == 2, "B 上线后：yaml 与 toml 都删：%r" % sc.events("daemon:warp_config_removed"))
            details.append("B 上线后删除 .yaml / .toml")
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon(launcher_fn=launcher_with_files(sc))
            start_handoff(sc, a)
            sc.tick(dm)
            sc.tick(dm, 100)
            require(all(os.path.exists(path) for path in paths(sc)), "119 秒内：不删")
            sc.tick(dm, 30)
            require(not any(os.path.exists(path) for path in paths(sc)), "launch_confirm_timeout（120 秒）到期后：删")
            details.append("B 一直没上线：120 秒到期后删除")
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon(launcher_fn=launcher_with_files(sc))
            hid, uuid = start_handoff(sc, a)
            sc.tick(dm)
            blocked = daemon_module.warp_config_paths(sc.handoff()["b"]["name"])[1]
            os.unlink(blocked)
            os.makedirs(blocked)
            with open(os.path.join(blocked, "keep"), "w", encoding="utf-8") as stream:
                stream.write("x")      # 非空目录：unlink 必失败
            b_comes_online(sc, uuid)
            for _ in range(3):
                sc.tick(dm)
            failed = sc.events("daemon:warp_config_remove_failed")
            require(len(failed) == 1 and failed[0]["path"] == blocked and os.path.isdir(blocked), "删除失败：只记一条 events（不重复）：%r" % failed)
            require(sc.handoff() is not None and not os.path.exists(daemon_module.warp_config_paths(sc.handoff()["b"]["name"])[0]), "失败不影响别的：toml 照删、交接照走")
            details.append("删除失败：只记 events 一次、不崩")
    return details


def case_h78():
    """H78（真机发现 c）：`off` 的文案按实际状态——没有未收尾意图时不说「收尾已交给正在运行的 daemon」、daemon 实际在跑时不说「daemon 未启动，无需停止」；
    有未收尾意图且 daemon 在跑才说交给 daemon，并说明它做完会自己退出。"""
    with temp_dir() as work, fake_launchctl(work) as fake:
        project = make_project(work)
        sdir = sdir_of(project)
        require(cli(["on"], project)[0] == 0 and fake.loaded(), "前提：on（daemon 在 launchd 里）")
        with daemon_lock_held(sdir):      # daemon 实际在跑（持着锁）
            code, out, err = cli(["off"], project)
            require(code == 0 and "已交给正在运行的 daemon" not in out and "daemon 未启动" not in out + err and "无需停止" not in out + err and "daemon：已停止" in out,
                    "没有未收尾意图：不说交给 daemon、不说未启动：%r %r" % (out, err))
            require(cli(["on"], project)[0] == 0, "再 on")
            make_pending("off_kill", sdir)
            code, out, err = cli(["off"], project)
            require(code == 0 and "已交给正在运行的 daemon" in out and "daemon：保留" in out and "自己退出" in out and "daemon 未启动" not in out + err,
                    "有未收尾意图且 daemon 在跑：说交给 daemon、保留、会自己退出：%r %r" % (out, err))
        clear_pending(sdir)
        code, out, err = cli(["off"], project)
        require("已交给正在运行的 daemon" not in out and "daemon 未启动" not in out + err, "没人持锁、没有未收尾意图：也不乱说：%r %r" % (out, err))
    return ["无未收尾意图：不说「已交给正在运行的 daemon」「daemon 未启动，无需停止」，说「已停止」；有未收尾意图 + daemon 在跑：说交给 daemon、保留、自己退出"]


def case_h79():
    """H79（Owner 裁定 1 补；发布前审查 P0 收紧）：daemon 每轮兜底清扫——只认登记册（<S>/warp_configs.json，开窗前登记）里的路径：登记过 + 带本项目开窗前缀
    （<prefix>-handoff-）+ mtime 超过 launch_confirm_timeout 的 .yaml / .toml 才删，登记过的新文件保留；用户自己的文件——包括同前缀但没登记过的——绝不碰
    （阴性对照：收紧前的「凭前缀 + 扩展名 + mtime」会把 proj-handoff-notes.yaml 删掉）；删除失败只记 events 一次。"""
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
        tab_dir, launch_dir = os.path.join(home, ".warp", "tab_configs"), os.path.join(home, ".warp", "launch_configurations")
        os.makedirs(tab_dir)
        os.makedirs(launch_dir)
        now = sc.clock.ms / 1000.0
        files = {}
        for label, directory, name, age in (("旧 yaml", launch_dir, "proj-handoff-aaa111.yaml", 500), ("旧 toml", tab_dir, "proj-handoff-aaa111.toml", 500),
                                            ("新 yaml", launch_dir, "proj-handoff-bbb222.yaml", 10), ("Owner 自己的 toml", tab_dir, "my_tab_config.toml", 5000),
                                            ("Owner 自己的 yaml", launch_dir, "proj-notes.yaml", 5000), ("别的项目", launch_dir, "other-handoff-ccc333.yaml", 5000),
                                            ("同前缀的用户 yaml（没登记）", launch_dir, "proj-handoff-notes.yaml", 5000), ("同前缀的用户 toml（没登记）", tab_dir, "proj-handoff-mine.toml", 5000)):
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("x")
            os.utime(path, (now - age, now - age))
            files[label] = path
        # 登记册：开窗前登记的就是这几个（含别的项目前缀的一个，登记册被改过也不该删它）；用户的同前缀文件没登记
        for label in ("旧 yaml", "旧 toml", "新 yaml", "别的项目"):
            daemon_module.register_warp_config(sc.sdir, files[label], sc.clock.ms)
        dm = sc.daemon()
        sc.tick(dm)
        gone = sorted(label for label, path in files.items() if not os.path.exists(path))
        require(gone == ["旧 toml", "旧 yaml"], "只该删登记过、带前缀且过期的（阳性对照）；同前缀但没登记的用户文件不删（阴性对照）：被删的 = %r" % gone)
        require(len(sc.events("daemon:warp_config_swept")) == 2, "每个删掉的记一条 events")
        registered = daemon_module.read_warp_registry(sc.sdir)
        require(files["旧 yaml"] not in registered and files["旧 toml"] not in registered and files["新 yaml"] in registered, "删掉的从登记册清掉，保留的仍在：%r" % registered)
        blocked = os.path.join(launch_dir, "proj-handoff-ddd444.yaml")
        os.makedirs(blocked)
        with open(os.path.join(blocked, "keep"), "w", encoding="utf-8") as stream:
            stream.write("x")
        os.utime(blocked, (now - 500, now - 500))
        daemon_module.register_warp_config(sc.sdir, blocked, sc.clock.ms)
        # 目录不是普通文件：不碰、不报错
        for _ in range(2):
            sc.tick(dm)
        require(os.path.isdir(blocked) and not sc.events("daemon:warp_config_remove_failed"), "非普通文件不碰")
        locked = os.path.join(launch_dir, "proj-handoff-eee555.toml")
        with open(locked, "w", encoding="utf-8") as stream:
            stream.write("x")
        os.utime(locked, (now - 500, now - 500))
        daemon_module.register_warp_config(sc.sdir, locked, sc.clock.ms)
        os.chmod(launch_dir, 0o555)      # 目录不可写：unlink 必失败
        try:
            for _ in range(3):
                sc.tick(dm)
            failed = sc.events("daemon:warp_config_remove_failed")
        finally:
            os.chmod(launch_dir, 0o755)
        require(os.path.exists(locked) and len(failed) == 1 and failed[0]["path"] == locked, "删除失败：只记一条、不崩：%r" % failed)
    return ["带前缀且过期：删（yaml / toml 各一）；新文件、Owner 自己的 my_tab_config.toml / proj-notes.yaml、别的项目的：不碰；删除失败只记一次"]


def files_launcher(sc):
    """假启动器：照常记一次启动，并像真的 open_warp.sh 那样在（HOME 下的）Warp 配置目录写出本次的临时配置（.yaml 与 .toml）。"""
    def launcher(cfg, sdir, handoff):
        result = sc.world.launcher_fn(cfg, sdir, handoff)
        for path in daemon_module.warp_config_paths(handoff["b"]["name"]):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("temp")
        return result
    return launcher


def case_h80():
    """H80（Sol P0-1 + Opus P1-2）：开窗时把本次临时配置的准确路径记进登记册；`off` 放弃交接时 CLI 当场删除（输出里不出现「pid None」）；
    CLI 删不掉的留在 off_kill 意图里，由 daemon 在结束该意图前删，删完才允许 idle 退出。"""
    import shutil
    details = []
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None):
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon(launcher_fn=files_launcher(sc))
            start_handoff(sc, a)
            sc.tick(dm)
            target = daemon_module.warp_config_path(sc.handoff()["b"]["name"], "launch_config")
            require(sc.handoff()["launch"]["config_paths"] == [target] and os.path.exists(target) and target.startswith(home), "登记册记下准确路径（HOME 下的 .yaml）：%r" % sc.handoff()["launch"])
            code, out, err = sc.cli(["off"], None)
            require(code == 0 and not os.path.exists(target) and "已删除开窗用的临时配置" in out and "pid None" not in out + err, "off 当场删除：%r %r" % (out, err))
            require(sc.state()["off_kill"][0]["config_paths"] == [], "已删的路径从意图里去掉")
            sc.tick(dm, 130)
            require(daemon_module.idle_exit_due(sc.sdir), "B 始终没出现、配置已删：意图结束，daemon 可 idle 退出：%r" % daemon_module.pending_closeout(sc.sdir))
            details.append("off 当场删除记着的 .yaml，意图正常结束")
        with scenario() as sc:
            a = sc.spawn("sA")
            dm = sc.daemon(launcher_fn=files_launcher(sc))
            start_handoff(sc, a)
            sc.tick(dm)
            target = daemon_module.warp_config_path(sc.handoff()["b"]["name"], "launch_config")
            os.unlink(target)
            os.makedirs(target)
            with open(os.path.join(target, "keep"), "w", encoding="utf-8") as stream:
                stream.write("x")       # 非空目录：CLI 的 unlink 必失败
            code, out, err = sc.cli(["off"], None)
            require(code == 0 and "没删掉" in err and sc.state()["off_kill"][0]["config_paths"] == [target], "CLI 删不掉：留在 off_kill 里：%r %r" % (err, sc.state()))
            require("pid None" not in out + err, "B 未登记时不打印 pid None")
            sc.tick(dm, 130)
            require(not daemon_module.idle_exit_due(sc.sdir) and sc.state()["off_kill"], "daemon 也删不掉：意图保留、不许 idle 退出")
            shutil.rmtree(target)
            sc.tick(dm)
            require(not sc.state()["off_kill"] and daemon_module.idle_exit_due(sc.sdir), "障碍清除后 daemon 完成并结束意图：%r" % sc.state())
            details.append("CLI 删不掉：daemon 保留意图、删完才结束并允许 idle 退出")
    return details


def case_h81():
    """H81（Sol P0-2）：daemon 每轮写心跳（CLI 当场跑的那一轮不写）；manual 下 `status` 也显示最后心跳年龄，标注「daemon 已停止」或「收尾中」，不写成「正常」。"""
    with temp_dir() as work, fake_launchctl(work), fixed_clock() as clock:
        project = make_project(work)
        sdir = sdir_of(project)
        require(cli(["on"], project)[0] == 0, "前提：on")

        class Spy:
            def tick(self, now):
                pass

        daemon_module.run_daemon(sdir, Spy(), 5, once=True, clock=lambda: clock.ms)
        require(sessions.read_heartbeat(sdir) == clock.ms, "daemon 一轮之后应写心跳")
        beat = clock.ms
        require(cli(["off"], project)[0] == 0 and sessions.read_heartbeat(sdir) == beat, "`off` 里 CLI 当场跑的那一轮不是 daemon，不写心跳")
        clock.advance(7)
        code, out, err = cli(["status"], project)
        require("daemon：最后一次心跳 7 秒前（daemon 已停止）" in out and "正常" not in out, "manual、已停：%r" % out)
        with daemon_lock_held(sdir):
            make_pending("off_kill", sdir)
            code, out, err = cli(["status"], project)
            require("daemon：最后一次心跳 7 秒前（收尾中）" in out and "daemon：收尾中" in out and "pid None" not in out, "manual、收尾中：%r" % out)
            clear_pending(sdir)
        os.unlink(sessions.heartbeat_path(sdir))
        code, out, err = cli(["status"], project)
        require("daemon：无心跳记录（daemon 已停止）" in out, "没有心跳文件：%r" % out)
    return ["run_daemon 每轮写心跳、CLI 一轮不写；manual status：最后心跳年龄 + 已停止 / 收尾中；无心跳文件也说明"]


def case_h82():
    """H82（Opus P1-1）：`on` 遇到任务已加载、在跑、但心跳超过 heartbeat_stale_seconds（卡住）⇒ `launchctl kickstart -k <target>` 重启；心跳新鲜或没有心跳文件不动；kickstart -k 失败退出 1。"""
    with temp_dir() as work, fake_launchctl(work) as fake, fixed_clock() as clock:
        project = make_project(work)
        sdir = sdir_of(project)
        target = "gui/%d/%s" % (os.getuid(), label_for(project))
        require(cli(["on"], project)[0] == 0, "前提：on")
        count = len(fake.calls())
        require(cli(["on"], project)[0] == 0 and fake.verbs(count) == ["print"], "没有心跳文件：不动")
        sessions.write_heartbeat(sdir, clock.ms - 3000)
        count = len(fake.calls())
        require(cli(["on"], project)[0] == 0 and fake.verbs(count) == ["print"], "心跳新鲜：不动")
        sessions.write_heartbeat(sdir, clock.ms - 121_000)
        count = len(fake.calls())
        code, out, err = cli(["on"], project)
        require(code == 0 and fake.calls()[count:] == ["print " + target, "kickstart -k " + target] and "卡住" in out, "心跳超过 120 秒：kickstart -k：%r %r" % (out, fake.calls()[count:]))
        count = len(fake.calls())
        with patched_env(FAKE_LC_KICK="fail"):
            code, out, err = cli(["on"], project)
        require(code == 1 and "kickstart -k 重启失败" in err, "kickstart -k 失败：退出 1：%r" % err)
    return ["心跳新鲜 / 没有心跳：不动；心跳 >120 秒：kickstart -k gui/<uid>/<label>；失败退出 1"]


def main():
    """运行 H62–H82。"""
    return run_cases([("H62", case_h62), ("H63", case_h63), ("H64", case_h64), ("H65", case_h65), ("H66", case_h66), ("H67", case_h67),
                      ("H68", case_h68), ("H69", case_h69), ("H70", case_h70), ("H71", case_h71), ("H72", case_h72), ("H73", case_h73), ("H74", case_h74), ("H75", case_h75), ("H76", case_h76),
                      ("H77", case_h77), ("H78", case_h78), ("H79", case_h79), ("H80", case_h80), ("H81", case_h81), ("H82", case_h82)])


if __name__ == "__main__":
    sys.exit(main())
