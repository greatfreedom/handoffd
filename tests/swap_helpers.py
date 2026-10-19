# handoffd S2a 换棒测试的公共工具：假世界（进程表 / 会话收件箱 / 信号 / 启动器）、可控时钟下的场景脚手架。
# 不起任何真实进程、不开窗、不向任何真实会话投递：msg / kill / launcher / 判活 / cwd 全部注入；
# CDM_CLAUDE_SESSIONS_DIR 恒指向假目录，万一漏注入 msg_fn 也只会读到假目录。
import contextlib
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import patched_env, require, temp_dir  # noqa: E402
from relay_helpers import Rings, call_relay, fixed_clock, ident_fn, read_baton, sdir_of, set_auto, tool_call  # noqa: E402
from cdm import baton, config, daemon as daemon_module, handoffd_cli, msg, procs, sessions  # noqa: E402
from cdm.notify import RecordingNotifier  # noqa: E402

# 命令行解析器每次现建要 2 ms（子命令多了之后明显），穷举 / 交错用例要调几万次：测试进程内缓存一份（解析器无状态）
import functools  # noqa: E402
handoffd_cli.build_parser = functools.lru_cache(maxsize=1)(handoffd_cli.build_parser)

KIT_TEXT = """# 交接包 kit_demo

## 事实
一些内容。

## 给新 session 的开场指令
你是第 2 棒主控。先读 kit，再按下面做：
1. 复核结论用 SendMessage 回给 A；
2. 完成后运行 handoffd sign in。

### 小节（更深一级，仍属开场指令）
细则。

## 其它
不属于开场指令。
"""
OPENING = """你是第 2 棒主控。先读 kit，再按下面做：
1. 复核结论用 SendMessage 回给 A；
2. 完成后运行 handoffd sign in。

### 小节（更深一级，仍属开场指令）
细则。"""


class World:
    """假世界：进程表（pid → lstart / cwd / 存活）、假收件箱、假信号、假启动器。"""

    def __init__(self, project, sessions_dir):
        self.project = project
        self.sessions_dir = sessions_dir
        self.table = {}
        self.unknown = set()
        self.messages = []          # (pid, content)
        self.kills = []             # {"pid", "sig", "holder", "phase", "lstart"}
        self.sent_lstart = []       # 与 messages 一一对应
        self.launches = []          # 启动器被调用时的 handoff id
        self.launch_script = []     # 依次返回的 (code, tail, version)；用完后默认 (0, "", "2.1.290")
        self.msg_fail = False
        self.agents = None          # `claude agents --json` 的假结果（S2b：prompt>stop 的在途核对 / reply task 核对）；None = 查询失败（本轮不问）
        self.kill_ignored = False   # SIGTERM 发出但进程不退出
        self.ignore_pids = None     # 非空时 kill_ignored 只对这些 pid 生效
        self.on_launch = None       # 启动器成功返回后的回调（模拟 Warp 窗口里的 B 起来）
        self.sdir = sdir_of(project)
        self.next_pid = 51000
        os.makedirs(sessions_dir, exist_ok=True)

    def spawn(self, sid, cwd=None, name=None, pid=None):
        pid = pid or self.next_pid
        self.next_pid = max(self.next_pid, pid) + 1
        lstart = "Thu Oct  1 12:%02d:%02d 2026" % (pid // 60 % 60, pid % 60)
        self.table[pid] = {"lstart": lstart, "cwd": cwd or self.project, "alive": True}
        with open(os.path.join(self.sessions_dir, "%d.json" % pid), "w", encoding="utf-8") as stream:
            json.dump({"pid": pid, "sessionId": sid, "cwd": cwd or self.project, "name": name or ("sess-" + sid[:4]),
                       "messagingSocketPath": "/nonexistent/%d.sock" % pid}, stream)
        return {"session_id": sid, "pid": pid, "lstart": lstart}

    def die(self, rec):
        self.table[rec["pid"]]["alive"] = False

    def proc_state(self, record):
        pid = record["pid"]
        if pid in self.unknown:
            return "unknown"
        entry = self.table.get(pid)
        if entry is None or not entry["alive"] or entry["lstart"] != record["lstart"]:
            return "dead"
        return "live"

    def reuse_pid(self, rec, cwd=None):
        """pid 复用：同一 pid 现在是另一个进程（lstart 不同）。"""
        self.table[rec["pid"]] = {"lstart": "Fri Oct  2 00:00:00 2026", "cwd": cwd or self.project, "alive": True}

    def lstart_fn(self, pid):
        entry = self.table.get(pid)
        return entry["lstart"] if entry and entry["alive"] else None

    def cwd_fn(self, pid):
        entry = self.table.get(pid)
        return entry["cwd"] if entry else None

    def msg_fn(self, pid, content, info=None):
        if self.msg_fail:
            return False, "ConnectionRefusedError"
        self.messages.append((pid, content))
        entry = self.table.get(pid)
        self.sent_lstart.append(entry["lstart"] if entry else None)   # 投递那一刻该 pid 上的进程 lstart（K3：用来发现发给了被复用的无辜进程）
        return True, ""

    def kill_fn(self, pid, sig):
        status, state = baton.read(self.sdir)
        entry = self.table.get(pid)
        self.kills.append({"pid": pid, "sig": sig, "holder": (state or {}).get("holder"),
                           "phase": ((state or {}).get("handoff") or {}).get("phase"), "lstart": entry["lstart"] if entry else None})
        ignored = self.kill_ignored and (self.ignore_pids is None or pid in self.ignore_pids)
        if not ignored and pid in self.table:
            self.table[pid]["alive"] = False

    def launcher_fn(self, cfg, sdir, handoff):
        self.launches.append(handoff["id"])
        code, tail, version = self.launch_script.pop(0) if self.launch_script else (0, "", "2.1.290")
        if code == "raise":
            raise OSError("launcher boom")
        if code == 0 and self.on_launch:
            self.on_launch(handoff)
        return code, tail, version

    def messages_to(self, rec):
        return [content for pid, content in self.messages if pid == rec["pid"]]

    @contextlib.contextmanager
    def patch_procs(self):
        original = procs.proc_state
        procs.proc_state = lambda record, deadline=None: self.proc_state(record)
        try:
            yield
        finally:
            procs.proc_state = original


class GuardProbeNotifier(RecordingNotifier):
    """通知器替身：每次被调用时探一下 baton guard 是否空闲（G10：不得在 guard 内发声）。"""

    def __init__(self, sdir):
        super().__init__()
        self.sdir = sdir
        self.guard_held_during_alert = []

    def alert(self, kind, message, voice_text=None):
        with baton.guarded(self.sdir, wait=0.05) as acquired:
            self.guard_held_during_alert.append(not acquired)
        super().alert(kind, message, voice_text)


RING_KINDS = ("waiting", "problem")


class Scenario:
    """一个项目 + 假世界 + 可控时钟下的换棒场景脚手架。"""

    def __init__(self, work, clock, name="proj", cfg_overrides=None):
        self.work = work
        self.clock = clock
        self.project = os.path.join(work, name)
        os.makedirs(self.project)
        self.sdir = set_auto(self.project, since_ms=clock.ms - 10_000)
        self.sessions_dir = os.path.join(work, "claude-sessions")
        self.world = World(self.project, self.sessions_dir)
        raw = {"project_dir": self.project}
        raw.update(cfg_overrides or {})
        self.cfg = config.build_config(raw)
        self.notifier = GuardProbeNotifier(self.sdir)
        self.rings = Rings()
        self.env_patch = None

    # ---- 参与者 ----
    def spawn(self, sid, **kw):
        return self.world.spawn(sid, **kw)

    def daemon(self, **over):
        kwargs = dict(launcher_fn=self.world.launcher_fn, msg_fn=self.world.msg_fn, kill_fn=self.world.kill_fn,
                      proc_state_fn=self.world.proc_state, cwd_fn=self.world.cwd_fn, agents_fn=lambda: self.world.agents)
        if daemon_module.Daemon.__init__.__code__.co_varnames.count("lstart_fn"):
            kwargs["lstart_fn"] = self.world.lstart_fn
        kwargs.update(over)
        return daemon_module.Daemon(self.cfg, self.sdir, self.notifier, **kwargs)

    def tick(self, dm, seconds=1):
        self.clock.advance(seconds)
        dm.tick(self.clock.ms)

    # ---- 钩子 / 命令 ----
    def hook(self, event, rec, **extra):
        return call_relay(event, self.project, rec["session_id"], ident_fn(rec), rings=self.rings, extra=extra)

    def pre(self, rec, tool="Write", **tool_input):
        tool_input = tool_input or {"file_path": "/x", "content": "c"}
        return call_relay("PreToolUse", self.project, rec["session_id"], ident_fn(rec), rings=self.rings, extra=tool_call(tool, **tool_input))[1]

    def stop(self, rec, tasks=(), crons=()):
        """A 的一次主线程 Stop（先让时钟走 1 秒：Stop 要「晚于签字」才算，设计:5）。"""
        self.clock.advance(1)
        return self.hook("Stop", rec, background_tasks=list(tasks), session_crons=list(crons))

    def cli(self, argv, rec=None):
        """进程内跑 handoffd 命令；rec=None 表示 Owner 终端。返回 (退出码, 标准输出, 标准错误)。"""
        import io
        env = {"CLAUDE_PROJECT_DIR": self.project}
        if rec:
            env["CLAUDE_CODE_SESSION_ID"] = rec["session_id"]
        chain = (lambda deadline: [(rec["pid"], rec["lstart"])]) if rec else (lambda deadline: [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                extra = {"lstart_fn": self.world.lstart_fn} if "lstart_fn" in handoffd_cli.main.__code__.co_varnames else {}
                code = handoffd_cli.main(argv, env=env, chain_fn=chain, notifier=self.notifier, kill_fn=self.world.kill_fn,
                                         now_fn=lambda: self.clock.ms, **extra)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def write_kit(self, text=KIT_TEXT, name="kit.md"):
        path = os.path.join(self.work, name)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)
        return path

    # ---- 读 ----
    def state(self):
        return read_baton(self.sdir)

    def handoff(self):
        return self.state()["handoff"]

    def notices(self):
        out = []
        try:
            with open(sessions.notices_path(self.sdir), encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    if item.get("type") == "notice":
                        out.append(item)
        except OSError:
            pass
        return out

    def notice_kinds(self):
        return [item["kind"] for item in self.notices()]

    def ring_count(self):
        return sum(self.notifier.count(kind) for kind in RING_KINDS)

    def events(self, name=None):
        out = []
        try:
            with open(os.path.join(self.sdir, "events.jsonl"), encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    if name is None or item.get("event") == name:
                        out.append(item)
        except OSError:
            pass
        return out

    def count_commits(self):
        return sum(1 for item in self.state().get("retire", []) if item.get("reason") == "handoff_commit")


@contextlib.contextmanager
def scenario(start_ms=1_790_000_000_000, **kw):
    """临时目录 + 可控时钟 + 假进程表 + 假 procs.proc_state（钩子 / 命令共用）。"""
    with temp_dir() as work, fixed_clock(start_ms) as clock:
        sc = Scenario(work, clock, **kw)
        original_send = msg.send
        msg.send = lambda pid, content, *args, **kwargs: sc.world.msg_fn(pid, content)   # 命令行 / 默认路径万一走到 msg.send，也只落到假世界
        try:
            with sc.world.patch_procs(), patched_env(CDM_CLAUDE_SESSIONS_DIR=sc.sessions_dir):
                yield sc
        finally:
            msg.send = original_send


def start_handoff(sc, a, b_sid_hint=None):
    """A 认领 holder → request → 返回 (handoff 的 id, B 的 uuid)。"""
    sc.hook("SessionStart", a, source="startup")
    code, out, err = sc.cli(["request", "--reason", "goal"], a)
    require(code == 0, "request 失败：%r %s" % (code, err))
    handoff = sc.handoff()
    return handoff["id"], handoff["b"]["uuid"]


def b_comes_online(sc, uuid, cwd=None):
    """模拟 B 的 Warp 窗口里 claude 起来：新进程 + SessionStart 钩子。返回 B 的身份记录。"""
    b = sc.spawn(uuid, cwd=cwd, name=sc.handoff()["b"]["name"])
    sc.hook("SessionStart", b, source="startup")
    return b


def full_handoff_until_sealed(sc, a, dm):
    """request → tick（开窗）→ B 上线 → A seal。返回 (B 身份记录, handoff id)。"""
    hid, uuid = start_handoff(sc, a)
    holder = {}
    sc.world.on_launch = lambda handoff: holder.update(b=b_comes_online(sc, uuid))
    sc.tick(dm)
    b = holder["b"]
    kit = sc.write_kit()
    code, out, err = sc.cli(["seal", "--kit", kit], a)
    require(code == 0, "seal 失败：%r %s" % (code, err))
    return b, hid
