# handoffd 守护程序（设计:22、§4、§5.3；S2a 范围＝换棒部分，S2b 的看护在 cdm/watch.py）：每轮读 <S>/baton.json，推进「请求 → 开 B → OPEN → 双签 → commit → SIGTERM A → BATON → 归档」，
# 并按设计 §4 的 :56、:57、:64、:47④⑤ 行发通知。不调用模型。
# 纪律（不变量 J1–J5，见 claudedocs/review/handoffd_S2a_transitions_20261001.md）：
#   J1 holder 槽只经 baton.claim 的 CAS 与 baton.commit 改变，commit 同一 handoff 至多一次；
#   J2① 每个外部动作（开窗 / OPEN / BATON / SIGTERM）都先把「将要做」落盘（launching / sending），重启后据此不重做，只补做没有标记的；
#       ② 外部动作的目标进程（pid, lstart）必须等于登记册对应槽位的三元组且其 cwd 在本项目目录下，否则不做、记 events；
#   J3 OPEN 的 content = seal 时抽出的开场指令原文、无前缀，只在 B 上线且 kit 已封存后发；
#   J4 本片响铃情形各恰好一条 notice + 一次响铃（notice id = 交接 id + 情形键，重启后仍去重）；:64 只进记录。
#   K1 每个落盘的意图必有结局：已完成，或已有一条「已响」的 notice 指明卡在哪（notice 与 rung 分开落盘，每轮补响「有 notice 无 rung」的）；
#      交接收尾（SIGTERM A / BATON / 归档）与 off 留下的「SIGTERM 未接棒 B」不受 mode 影响（manual 下也由 tick 做完）。
#   K2 换棒输入按进程身份：A 的 Stop 按 holder 槽进程自己的单调 Stop 序号判（baton.stop_after_sign）。
#   K3 每个外部动作在发出前一刻重读登记册、重核目标三元组 + cwd，投递用重核时读到的 socket（_recheck）。
import errno
import fcntl
import os
import re
import signal
import subprocess
import time
import traceback

from cdm import agents as agents_module, baton, msg, notify, procs, sessions, util
from cdm.watch import WatchMixin

MVP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_RETRY_SECONDS = 3.0
LOCK_RETRY_INTERVAL = 0.2
DAEMON_LOCK_WAIT_SECONDS = 30.0  # 常驻 daemon（launchd 拉起）遇到锁被占（例如 handoffd off 正持锁跑一轮）：等这么久，仍没拿到就以非 0 退出，交给 KeepAlive 重拉（S2a R3 Opus P2-1）
LAUNCHER_TIMEOUT_SECONDS = 45    # 设计:22：120→45 s，保证单轮 < 2 分钟心跳门槛
OLD_A_ALIVE_SECONDS = 60         # 设计:57 行 ⑤：换棒后旧 A 60 s 仍活（规格无数值，设计:57 写 60 s）
NOTICE_CAT = "handoff"           # 机器层「交接卡住」类（规格:65）
STICKY_KINDS = ("baton_ambiguous", "baton_failed", "b_died_after_commit", "off_kill_refused", "off_kill_unconfirmed")   # 归档 / 放弃时不自动解除


MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+-]+(\[[0-9a-z]+\])?\Z")   # S4a 自定：状态栏 model.id 的合法形态（可带 [1m] 后缀；用 \Z 而非 $，否则末尾换行会混过去）
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")        # 状态栏 effort.level 的五档（statusline.md:191）


def _drop_flag(args, name):
    """从额外参数里去掉 --<name> 的两种写法（`--name=值` 一个 token；`--name 值` 两个 token，值一并去掉）。"""
    result, skip = [], False
    for arg in args:
        if skip:
            skip = False
            if not arg.startswith("-"):
                continue
        if arg == "--" + name:
            skip = True
        elif not arg.startswith("--" + name + "="):
            result.append(arg)
    return result


def inherit_plan(cfg, sdir, handoff):
    """S4a：B 的 --model / --effort 跟 A（发起交接的持棒者）当前值一致。读 A 的状态栏快照 <S>/ctx/<A 会话号>.json（auto 下 ctx.tap 落盘）：
    model.id 合法 ⇒ 继承；effort.level ∈ 五档 ⇒ 继承（模型不支持档位时快照里没有，就只继承模型）；继承了哪项，就去掉 claude_extra_args 里同名参数。
    快照缺失 / 读不出 / 字段非法 ⇒ 该项不继承，按 claude_extra_args 原样。不继承 fast_mode / 权限模式。
    返回 {"model", "effort", "extra"（给启动器的 --extra-arg 列表，不含 --session-id）, "src", "note"}。"""
    extra = list(cfg["claude_extra_args"])
    plan = {"model": None, "effort": None, "extra": extra, "src": "config", "note": ""}
    sid = util.safe_id(((handoff.get("a") or {}).get("sid")))
    if sid is None:
        plan["note"] = "交接记录里没有可用的 A 会话号"
        return plan
    status, snap = util.read_json(os.path.join(sdir, "ctx", sid + ".json"))
    if status != "ok" or not isinstance(snap, dict):
        plan["note"] = "A 的状态栏快照%s" % ("不存在" if status == "missing" else "读不出")
        return plan
    model_obj, effort_obj = snap.get("model"), snap.get("effort")
    model = model_obj.get("id") if isinstance(model_obj, dict) else None
    effort = effort_obj.get("level") if isinstance(effort_obj, dict) else None
    notes = []
    if isinstance(model, str) and MODEL_ID.match(model):
        plan["model"] = model
        extra = _drop_flag(extra, "model")
        extra.append("--model=" + model)
    else:
        notes.append("model.id 缺失或非法")
    if isinstance(effort, str) and effort in EFFORT_LEVELS:
        plan["effort"] = effort
        extra = _drop_flag(extra, "effort")
        extra.append("--effort=" + effort)
    else:
        notes.append("effort.level 缺失或非法")
    plan["extra"] = extra
    plan["src"] = "snapshot" if (plan["model"] or plan["effort"]) else "config"
    plan["note"] = "；".join(notes)
    return plan


def run_launcher(cfg, sdir, handoff):
    """调用开窗启动器 open_warp.sh，返回 (退出码, 输出末尾, claude 版本)；超时会抛 TimeoutExpired（调用方按「结果不确定」处理）。
    B 的会话号用预分配 --session-id（RT:T5、T8）；不带 --prompt-file：B 先空闲，开场指令在 seal 后由 daemon 投递。"""
    script = os.path.join(MVP_ROOT, "bin", "open_warp.sh")
    b = handoff["b"]
    args = [
        "/bin/bash", script,
        "--name", b["name"],
        "--dir", cfg["project_dir"],
        "--command-dir", os.path.join(sdir, "launch"),
        "--min-version", cfg["min_claude_version"],
        "--required-env", " ".join(cfg["required_env"]),
        "--claude-bin", cfg["claude_bin"],
        "--extra-arg", "--session-id=" + b["uuid"],
        "--warp-open", cfg["warp_open"],
    ]
    plan = handoff.get("s4a_plan") or inherit_plan(cfg, sdir, handoff)   # daemon._launch 已算好并落盘时直接用，保证「记下的」就是「用的」
    for extra in plan["extra"]:
        args.extend(["--extra-arg", extra])
    result = subprocess.run(args, capture_output=True, text=True, timeout=LAUNCHER_TIMEOUT_SECONDS)
    output = (result.stdout or "") + (result.stderr or "")
    version = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("CDM_LAUNCHER_VERSION="):
            version = line.split("=", 1)[1].strip()
    return result.returncode, output[-500:], version


def warp_config_paths(name):
    """开窗用的临时 Warp 配置文件（tab_config 的 .toml、launch_config 的 .yaml）；目录与 open_warp.sh 同一套环境变量 / HOME 规则。"""
    home = os.path.expanduser("~")
    tab_dir = os.environ.get("CDM_WARP_TAB_DIR") or os.path.join(home, ".warp", "tab_configs")
    launch_dir = os.environ.get("CDM_WARP_LAUNCH_DIR") or os.path.join(home, ".warp", "launch_configurations")
    return [os.path.join(tab_dir, name + ".toml"), os.path.join(launch_dir, name + ".yaml")]


def warp_config_path(name, mode):
    """本次开窗写的那一个临时配置文件的准确路径（launch_config → .yaml，tab_config → .toml）。"""
    tab_path, launch_path = warp_config_paths(name)
    return launch_path if mode == "launch_config" else tab_path


def describe_off_kill(item):
    """off_kill 意图的一句话（B 还没登记就别写 pid None）。"""
    text = "关掉未接棒的 B（pid %s）" % item["pid"] if item.get("pid") else "关掉未接棒的 B（窗口已开、B 还没登记）"
    return text + ("；清理开窗用的临时配置" if item.get("config_paths") else "")


def warp_config_dirs():
    return [os.path.dirname(path) for path in warp_config_paths("x")]


def warp_registry_path(sdir):
    """handoffd 自己开窗写过的临时 Warp 配置的登记册（状态目录里）：兜底清扫只认这里登记过的路径，绝不凭文件名去 ~/.warp 里猜。"""
    return os.path.join(sdir, "warp_configs.json")


def load_warp_registry(sdir):
    """返回 (状态, {路径: 登记时的 epoch 毫秒})：状态 missing（没有登记册）/ ok / corrupt（坏 JSON、结构不对，或任何一个条目不合法）。
    corrupt 时一个条目都不采信（宁可少删，不误删）：整个登记册视为损坏，调用方不得据此删任何文件。"""
    status, data = util.read_json(warp_registry_path(sdir))
    if status == "missing":
        return "missing", {}
    if status != "ok" or not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return "corrupt", {}
    paths = data["paths"]
    if not all(isinstance(path, str) and isinstance(ms, int) and not isinstance(ms, bool) for path, ms in paths.items()):
        return "corrupt", {}
    return "ok", dict(paths)


def read_warp_registry(sdir):
    """{路径: 登记时的 epoch 毫秒}；没有登记册 / 登记册损坏都当空。"""
    return load_warp_registry(sdir)[1]


def register_warp_config(sdir, path, now_ms):
    """开窗前登记这次要写的临时配置路径（daemon 是唯一写者）。"""
    paths = read_warp_registry(sdir)
    paths[path] = now_ms
    util.atomic_write_json(warp_registry_path(sdir), {"paths": paths})


def pending_closeout(sdir):
    """还没收尾的交接意图（换棒后的收尾 / off 留下的 off_kill / 已写未响的 notice）；返回描述列表，空 = 都收完了。登记册读不出按「有」（不敢停 daemon）。
    `handoffd off` 用它决定停不停 daemon，daemon 自己用它决定在 manual 下退不退（规格:14）。"""
    status, state = baton.read(sdir)
    if status == "error":
        return ["登记册 baton.json 读不出，不能确认收尾是否做完"]
    items = []
    handoff = state.get("handoff")
    if handoff:
        items.append("交接 %s 换棒后的收尾（关旧 A / BATON / 归档）" % handoff.get("id"))
    for item in state.get("off_kill") or []:
        items.append(describe_off_kill(item))
    unrung = sessions.unrung_notices(sdir)
    if unrung:
        items.append("%d 条通知还没响铃" % len(unrung))
    return items


def idle_exit_due(sdir):
    """规格:14「没有项目在用时 daemon 自动停」：mode 不是 auto 且没有未收尾的意图 ⇒ daemon 该自己以 0 退出（KeepAlive 对 0 退出不拉）。"""
    return sessions.read_mode(sdir)[0] != "auto" and not pending_closeout(sdir)


def inside_project(project_dir, cwd):
    """cwd（realpath）是否等于或位于项目目录之下。"""
    root = os.path.realpath(project_dir)
    path = os.path.realpath(cwd)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Daemon(WatchMixin):
    """守护程序主体；启动器、投递、信号、判活、cwd 读取、通知器、时钟都可注入。S2b 的看护逻辑在 cdm/watch.py（WatchMixin）。"""

    def __init__(self, cfg, sdir, notifier, launcher_fn=None, msg_fn=None, kill_fn=None, proc_state_fn=None, cwd_fn=None,
                 point_fn=None, session_fn=None, lstart_fn=None, find_session_fn=None, agents_fn=None):
        self.cfg = cfg
        self.sdir = sdir
        self.project_dir = cfg["project_dir"]
        self.notifier = notifier
        self.launcher_fn = launcher_fn or run_launcher
        self.msg_fn = msg_fn       # 注入时调用 msg_fn(pid, content)；缺省走 msg.send 并绑定重核时读到的会话文件（K3）
        self.kill_fn = kill_fn or os.kill
        self.proc_state_fn = proc_state_fn or baton.default_proc_state
        self.cwd_fn = cwd_fn
        self.session_fn = session_fn or msg.session_info
        self.lstart_fn = lstart_fn or self._ps_lstart
        self.find_session_fn = find_session_fn or msg.find_session
        self.agents_fn = agents_fn or (lambda: agents_module.list_agents(cfg["claude_bin"], cfg["project_dir"]))   # S2b：reply task 核对用（测试注入）
        self._wstate = None        # S2b：daemon_state.json["watch"] 的本轮副本
        self._unreadable = None    # S2b：本轮是否有读不出的登记册 / 复核账（连 3 轮响铃，:47③）
        self.point = point_fn or (lambda label: None)   # 崩溃点钩子（测试在「标记已落盘、动作未做 / 已做、结果未记」处注入重启）
        self.off_results = []      # 本次 tick 里「off 留下的 SIGTERM B」的结局（供 handoffd off 打印）
        self.now = 0
        self._defer = False
        self._pending = []
        self._last_logged = {}

    # ---- 记录与通知 ----

    def log(self, event, **fields):
        """事件流水（events.jsonl，尽力而为）。"""
        sessions.append_event(self.sdir, dict({"t_ms": self.now, "sid": None, "role": "daemon", "event": "daemon:" + event}, **fields))

    def log_change(self, key, event, **fields):
        """等待类事件（每 5 秒都可能重复成立）：同一 key 的取值没变就不重复记。"""
        marker = repr(sorted(fields.items()))
        if self._last_logged.get(key) != marker:
            self._last_logged[key] = marker
            self.log(event, **fields)

    def alert(self, kind, text, voice_text=None, notice_id=None):
        """发声的唯一出口。在 baton guard 内（self._defer）只入队，出 guard 后再发（G10：通知器可能耗时数十秒，不得占着 guard 把钩子挡成 busy）。"""
        if self._defer:
            self._pending.append((kind, text, voice_text, notice_id))
        else:
            self._alert_now(kind, text, voice_text, notice_id)

    def _alert_now(self, kind, text, voice_text, notice_id=None):
        """真发声；发声成功之后才落 rung（K1：「已响」单独落盘）。通知器抛错 ⇒ 不落 rung，下一轮补响。"""
        self._load_rotation()
        try:
            self.notifier.alert(kind, text, voice_text)
        except Exception as exc:
            self._save_rotation()
            self.log("notify_error", kind=kind, error=repr(exc))
            return
        self._save_rotation()
        self.point("notice:acted")
        if notice_id:
            sessions.append_rung(self.sdir, notice_id, self.now)
            self.point("notice:rung")

    def _state_path(self):
        return os.path.join(self.sdir, "daemon_state.json")

    def _load_rotation(self):
        """G10：铃声 / 音色「下一次轮到谁」跟着 daemon_state.json 走（设计 §3），daemon 重启后接着轮，不是每次从头。读不出 / 结构不对按从头。"""
        if not hasattr(self.notifier, "rotation"):
            return
        status, value = util.read_json(self._state_path())
        saved = value.get("rotation") if status == "ok" and isinstance(value, dict) else None
        rotation = {}
        if isinstance(saved, dict):
            for key in ("tone_next", "voice_next"):
                if isinstance(saved.get(key), int) and not isinstance(saved.get(key), bool) and saved[key] >= 0:
                    rotation[key] = saved[key]
            if isinstance(saved.get("last_clip"), str):
                rotation["last_clip"] = saved["last_clip"]
        self.notifier.rotation = rotation

    def _save_rotation(self):
        """发声之后把轮换位置写回（与 _claude_version 同一个文件：读整个、只改 rotation 键、原子写）；写失败只记 events，不影响发声。"""
        if not hasattr(self.notifier, "rotation"):
            return
        status, value = util.read_json(self._state_path())
        state = value if status == "ok" and isinstance(value, dict) else {}
        state["rotation"] = dict(self.notifier.rotation)
        try:
            util.atomic_write_json(self._state_path(), state)
        except OSError as exc:
            self.log("rotation_save_failed", error=repr(exc))

    def _deferring(self, fn):
        """在 fn 期间把发声入队，fn 返回（guard 已释放）后统一发出。"""
        self._defer = True
        try:
            return fn()
        finally:
            self._defer = False
            pending, self._pending = self._pending, []
            for kind, text, voice_text, notice_id in pending:
                self._alert_now(kind, text, voice_text, notice_id)

    def _replay_rings(self):
        """K1：每轮开头对「notice 已写、rung 没有」的补响一次（notice 写盘与发声之间 / 发声与 rung 之间重启过）。宁可重复一次，不可零次。"""
        for item in sessions.unrung_notices(self.sdir):
            self._alert_now("problem" if item.get("ring") else "quiet", item.get("text", ""), item.get("what"), item["id"])

    def notice(self, hid, key, fields, ring, options, sid=None):
        """每个（交接, 情形键）恰好一条 notice：id 确定、存在即去重（重启后仍成立）；ring=True 响铃（problem），False 只进记录 + 无声横幅（quiet）。
        写盘失败 ⇒ 不发声、返回 False（条件仍成立，下一轮再写，不会「没记录却响了」）；发声成功后另落 rung。返回是否新写了一条。"""
        notice_id = "n-%s-%s" % (hid, key)
        if sessions.notice_exists(self.sdir, notice_id):
            return False
        text = notify.format_notice(**fields)
        if not sessions.append_notice(self.sdir, {
                "type": "notice", "id": notice_id, "t_ms": self.now, "sid": sid, "cat": NOTICE_CAT, "kind": key, "hid": hid,
                "ring": ring, "status": "pending" if ring else "info", "text": text, "what": fields["what"], "options": options}):
            self.log("notice_write_failed", hid=hid, key=key)
            return False
        self.point("notice:written")
        self.alert("problem" if ring else "quiet", text, fields["what"], notice_id)
        return True

    # ---- 取数 ----

    def _fresh(self):
        """重读登记册；返回 state（读不出 ⇒ None 并记 events；缺失 = 空登记册）。"""
        status, state = baton.read(self.sdir)
        if status == "error":
            self._unreadable = "登记册"
            self.log("baton_unreadable")
            return None
        return state

    @staticmethod
    def _ps_lstart(pid):
        status, lstart = procs.ps_field(pid, "lstart", procs.Deadline(2.0))
        return lstart if status == "ok" else None

    def _proc(self, slot):
        return self.proc_state_fn(slot)

    def _session(self, sid):
        return sessions.read_session(self.sdir, sid)

    def _info(self, pid):
        """pid 的会话文件内容（cwd、socket）：注入的 cwd_fn 只给 cwd；缺省读 ~/.claude/sessions/<pid>.json。"""
        if self.cwd_fn is not None and self.session_fn is msg.session_info:
            cwd = self.cwd_fn(pid)
            return {"cwd": cwd} if cwd else None
        return self.session_fn(pid)

    def _target_ok(self, slot):
        """J2②：外部动作的目标进程 == 登记册槽位的三元组（判活按 (pid, lstart) 比对）且 cwd 在本项目目录下。
        返回 (ok, 原因, 会话文件内容)——投递要用这里读到的 socket（K3）。"""
        state = self._proc(slot)
        if state != "live":
            return False, "not_live" if state == "dead" else "unknown", None
        info = self._info(slot["pid"])
        cwd = info.get("cwd") if isinstance(info, dict) else None
        if not cwd:
            return False, "cwd_unknown", None
        if not inside_project(self.project_dir, cwd):
            return False, "cwd_outside", None
        return True, "", info

    def _recheck(self, hid, phase, getter, target):
        """K3：动作发出前一刻重读登记册（仍是这次交接、phase 没变、槽位三元组仍 == 落标记时的目标）并重核目标进程（三元组 + cwd）。
        返回 (槽位, 会话文件内容, 原因)；原因非空 ⇒ 不得动作。"""
        state = self._fresh()
        handoff = state["handoff"] if state else None
        if handoff is None or handoff.get("id") != hid or handoff.get("phase") != phase:
            return None, None, "handoff_gone"
        slot = getter(state, handoff)
        if not slot or baton.triple(slot) != tuple(target):
            return None, None, "slot_changed"
        ok, why, info = self._target_ok(slot)
        return (slot, info, "") if ok else (None, None, why)

    def _send(self, pid, content, info):
        if self.msg_fn is not None:
            return self.msg_fn(pid, content)
        return msg.send(pid, content, info=info)

    # ---- 主循环 ----

    def tick(self, now_ms):
        """执行一轮；任何异常只记 events，不让进程退出。S2b：连续 3 轮异常（或登记册 / 复核账读不出）响铃一次（:47⑥③，见 watch._tick_health）。"""
        self.now = now_ms
        self._unreadable = None
        failed = None
        try:
            self._tick()
        except Exception as exc:
            failed = "tick 抛出未预料的异常 %r" % (exc,)
            self.log("tick_error", error=repr(exc), trace=traceback.format_exc()[-800:])
        self._tick_health(failed)

    def _tick(self):
        self.off_results = []
        self._replay_rings()                       # K1：不受 mode 影响
        self._sweep_warp_configs()
        state = self._fresh()
        if state is None:
            return
        self._run_off_kills(state)                 # K1：off 留下的「SIGTERM 未接棒 B」，manual 下也做完
        handoff = state["handoff"]
        if sessions.read_mode(self.sdir)[0] != "auto":
            # A3：off 之后已换棒的交接，收尾（SIGTERM 旧 A、BATON、归档）照样做完——此时没有任何别的动作
            if handoff is not None and handoff.get("phase") == "committed":
                self._post_commit(handoff)
            return
        if handoff is None:
            self._request_pending(state)
        elif handoff.get("phase") == "committed":
            self._post_commit(handoff)
        else:
            self._pre_commit(handoff)
        self._watch()                              # S2b 看护（cdm/watch.py）：auto 才做

    # ---- off 留下的「SIGTERM 未接棒 B」（K1；baton.abandon 与清 handoff 同一次写入落盘）----

    def _run_off_kills(self, state):
        """K4：只有持 daemon.lock 的进程（daemon，或拿到锁的 CLI 那一轮）执行。意图 pending → sending 用 guard 内 CAS，发信号前重读这条记录本身；
        信号发出后意图保留到确认 B 已死才清（B2）：超时（launch_confirm_timeout_seconds）仍活 ⇒ 响铃；信号失败 ⇒ 响铃；已发信号不重发。"""
        timeout_ms = int(self.cfg["launch_confirm_timeout_seconds"] * 1000)
        for item in list(state.get("off_kill") or []):
            hid = item.get("hid")
            if not item.get("pid"):
                # B5：B 还没登记（窗口刚开出来 / 开到一半）：按预分配的 uuid 等它的会话文件出现，再取三元组
                found = self.find_session_fn(item.get("uuid"))
                lstart = self.lstart_fn(found[0]) if found else None
                cwd = (found[1].get("cwd") if found else None)
                if not found or not lstart or not cwd or not inside_project(self.project_dir, cwd):
                    if self.now - int(item.get("ms") or 0) >= timeout_ms:
                        self._off_done(hid, item)      # 一直没出现：视为没有开出来，意图结束
                        self.log("off_kill_unopened", hid=hid)
                        self.off_results.append(("unopened", None))
                    else:
                        self.log_change("off_kill_wait:" + str(hid), "off_kill_wait", hid=hid, uuid=item.get("uuid"))
                    continue
                if not baton.off_kill_bind(self.sdir, hid, found[0], lstart):
                    continue
                item = dict(item, pid=found[0], lstart=lstart)
            record = {"sid": item.get("uuid"), "pid": item.get("pid"), "lstart": item.get("lstart")}
            current = self._proc(record)
            if item.get("state") in ("sent", "sending"):
                if current == "dead":
                    self._off_done(hid, item)           # 确认 B 已死：意图完成
                    self.off_results.append(("gone", record["pid"]))
                elif item.get("state") == "sending":
                    # 认领了、没记「已发」就重启：不重发（至多一次）；B 仍活 ⇒ 响铃
                    self._off_notice(hid, "off_kill_unconfirmed", record, "不确定 off 的 SIGTERM 有没有发出去（B 仍在运行）")
                    self._off_done(hid, item)
                    self.off_results.append(("unconfirmed", record["pid"]))
                elif self.now - int(item.get("ms") or 0) >= timeout_ms:
                    self._off_notice(hid, "off_kill_unconfirmed", record, "SIGTERM 发出后 B 仍没有退出")
                    self._off_done(hid, item)
                    self.off_results.append(("unconfirmed", record["pid"]))
                continue
            if current == "dead":
                self._off_done(hid, item)
                self.off_results.append(("gone", record["pid"]))
                continue
            if current != "live":
                self.log_change("off_kill_wait:" + str(hid), "off_kill_wait", hid=hid, pid=record["pid"])
                continue
            ok, why, _info = self._target_ok(record)
            if not ok:
                self._off_refused(hid, record, why, item)
                continue
            token = os.urandom(6).hex()
            if not baton.off_kill_claim(self.sdir, hid, token, self.now):
                continue                                           # 别的执行者已认领（K4：正常不会发生，CAS 兜底）
            self.point("offkill:marker")
            if not baton.off_kill_owned(self.sdir, hid, token):    # 发出前重读这条意图记录本身
                self.log("off_kill_intent_changed", hid=hid)
                continue
            ok, why, _info = self._target_ok(record)       # K3：发信号前一刻重核进程
            if not ok:
                self._off_refused(hid, record, why, item)
                continue
            try:
                self.kill_fn(record["pid"], signal.SIGTERM)
            except OSError as exc:
                self.log("off_kill_error", hid=hid, pid=record["pid"], error=repr(exc))
                self._off_notice(hid, "off_kill_unconfirmed", record, "向 B 发 SIGTERM 失败（%s）" % type(exc).__name__)
                self._off_done(hid, item)
                self.off_results.append(("error", record["pid"]))
                continue
            self.log("off_kill", hid=hid, pid=record["pid"])
            self.point("offkill:acted")
            baton.off_kill_sent(self.sdir, hid, token, self.now)
            self.point("offkill:recorded")
            self.off_results.append(("killed", record["pid"]))

    def _off_refused(self, hid, record, why, item):
        self.log("off:kill_b_refused", hid=hid, pid=record["pid"], reason=why)
        self.off_results.append(("refused:" + why, record["pid"]))
        self._off_notice(hid, "off_kill_refused", record, "没有向 B 发 SIGTERM（%s）" % why)
        self._off_done(hid, item)

    def _off_notice(self, hid, key, record, what):
        self.notice(hid, key, dict(
            what="Owner off 之后，未接棒的 B（pid %s）没有被关掉：%s（交接 %s）。" % (record["pid"], what, hid),
            why="为避免误杀别的进程，机器只在「三元组 == 登记册 ∧ cwd 在本项目下」时发信号。",
            ask="1) 自己关掉 B 的窗口", recommend=1, if_no_reply="B 会一直开着，但它不是主控、不能改文件。"),
            True, ["自己关掉 B 的窗口"])

    def _same_handoff(self, hid, phase):
        """重读并确认仍是这一次交接、且 phase 未变；否则 None（本轮不再继续）。"""
        state = self._fresh()
        handoff = state["handoff"] if state else None
        if handoff is None or handoff.get("id") != hid or handoff.get("phase") != phase:
            return None
        return handoff

    # ---- 76% 代发 request（设计:93 行：钩子落 request_pending，daemon 建交接）----

    def _request_pending(self, state):
        holder = state["holder"]
        if not holder:
            return
        pending = self._session(holder["sid"]).get("request_pending")
        if not pending:
            return
        hid, name, b_uuid = baton.new_ids(self.now, self.cfg["session_name_prefix"])
        result, _value = baton.request_for_holder(self.sdir, holder["sid"], self.now, str(pending.get("reason") or "ctx76"),
                                                  hid, b_uuid, name, self.proc_state_fn)
        if result == "ok":
            sessions.update_session(self.sdir, holder["sid"], lambda record: record.pop("request_pending", None))
            self.log("request", hid=hid, reason=pending.get("reason"))

    # ---- 换棒前 ----

    def _cleanup_warp_config(self, handoff):
        """开窗用的临时 Warp 配置（.toml / .yaml）不留在 ~/.warp 里：B 上线后，或 launch_confirm_timeout 到期后删除；删除失败只记 events（同一路径不重复记）。"""
        b, launch = handoff.get("b") or {}, handoff.get("launch") or {}
        name = b.get("name")
        if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9._-]+$", name) or name in (".", ".."):
            return
        started = launch.get("started_ms")
        if not b.get("online_ms") and not (isinstance(started, int) and self.now - started >= int(self.cfg["launch_confirm_timeout_seconds"] * 1000)):
            return
        self._rm_configs(list(dict.fromkeys(warp_config_paths(name) + list(launch.get("config_paths") or []))), handoff.get("id"))

    def _rm_configs(self, paths, hid):
        """删临时配置文件；返回没删掉的路径（缺失按已删）。失败只记 events（同一路径不重复记）。"""
        left = []
        for path in paths:
            if not os.path.lexists(path):
                continue
            try:
                os.unlink(path)
            except OSError as exc:
                self.log_change("warp_cfg_rm:" + path, "warp_config_remove_failed", hid=hid, path=path, error=repr(exc))
                left.append(path)
                continue
            self.log("warp_config_removed", hid=hid, path=path)
        return left

    def _off_done(self, hid, item):
        """结束一条 off_kill 意图：先删它记着的临时配置，删完才清（删不掉就保留意图，继续拦着 daemon 的 idle 退出）。"""
        if self._rm_configs(list(item.get("config_paths") or []), hid):
            return False
        return baton.off_kill_clear(self.sdir, hid)

    def _sweep_warp_configs(self):
        """兜底（被 off 放弃、B 一直没上线的交接没有 handoff 可评估）：只清扫「登记册里登记过的」临时配置（read_warp_registry，开窗前由 _launch 登记）；
        登记过 + 在 ~/.warp 的两个配置目录里 + 文件名带 <session_name_prefix>-handoff- 前缀 + .yaml / .toml + 是普通文件 + mtime 超过 launch_confirm_timeout_seconds 才删。
        不列目录、不凭文件名去猜：用户自己的同前缀文件（登记册里没有）一律不碰；没有登记册（从没开过窗）就什么都不扫；manual 下整个清扫不做；登记册有任何损坏就一个都不删。
        文件已经不在的登记项顺手清掉。删除失败只记 events（同一路径不重复记）。"""
        if sessions.read_mode(self.sdir)[0] != "auto":
            return      # 先判 mode：manual 下完全不碰 ~/.warp（off 留下的临时配置由 off_kill 意图删，下次 auto 开窗后本清扫再兜底）
        status, registry = load_warp_registry(self.sdir)
        if status == "corrupt":
            self.log_change("warp_registry_corrupt", "warp_registry_corrupt", path=warp_registry_path(self.sdir))
            return      # 登记册任何一处损坏：本轮不删任何文件，只记 events
        if not registry:
            return
        prefix = self.cfg["session_name_prefix"] + "-handoff-"
        limit = float(self.cfg["launch_confirm_timeout_seconds"])
        directories = warp_config_dirs()
        remaining = dict(registry)
        for path in registry:
            name = os.path.basename(path)
            if os.path.dirname(path) not in directories or not name.startswith(prefix) or not name.endswith((".yaml", ".toml")):
                continue    # 登记册被改成别的位置 / 别的项目的前缀：不认
            if not os.path.lexists(path):
                del remaining[path]
                continue
            try:
                if not os.path.isfile(path) or self.now / 1000.0 - os.path.getmtime(path) <= limit:
                    continue
                os.unlink(path)
            except OSError as exc:
                self.log_change("warp_cfg_sweep:" + path, "warp_config_remove_failed", path=path, error=repr(exc))
                continue
            del remaining[path]
            self.log("warp_config_swept", path=path)
        if remaining != registry:
            try:
                util.atomic_write_json(warp_registry_path(self.sdir), {"paths": remaining})
            except OSError as exc:
                self.log_change("warp_registry_write", "warp_registry_failed", error=repr(exc))

    def _pre_commit(self, handoff):
        hid = handoff["id"]
        self._cleanup_warp_config(handoff)
        state = self._fresh()
        holder = state["holder"] if state else None
        holder_state = self._proc(holder) if holder else "none"
        self._crash_notices(handoff, holder_state)
        a_signed = handoff.get("a_signed")
        if holder_state == "dead" and not a_signed:
            return  # :56 / :57：A 没了又没签字，不再往前推（不开窗、不 commit），等 Owner 处理
        if holder_state == "live" or (holder_state == "dead" and a_signed):
            self._launch(handoff)
        handoff = self._same_handoff(hid, "arming")
        if handoff is None:
            return
        self._watch_b(handoff)
        self._open_ambiguous(handoff)
        self._open(handoff)
        handoff = self._same_handoff(hid, "arming")
        if handoff is None:
            return
        self._commit(handoff)

    def _crash_notices(self, handoff, holder_state):
        """设计 §4 :56 / :57 / :64：holder 确认已死（proc_state==dead）∧ 交接未换棒。"""
        if holder_state != "dead":
            return
        hid = handoff["id"]
        sid = (handoff.get("a") or {}).get("sid")
        if handoff.get("a_signed"):
            if (handoff["a_signed"] or {}).get("by") == "owner":
                return   # A9：Owner 代签（adopt）不是「A 签字之后崩溃」
            self.notice(hid, "a_crash_signed", dict(
                what="主控 A 在签字之后崩溃了，B 照常接棒（交接 %s）。" % hid,
                why="A 已签字，换棒的条件不依赖 A 继续运行；这条只是事后记录。",
                ask="1) 无需处理", recommend=1, if_no_reply="B 签字后 daemon 会照常换棒。"), False, ["无需处理"], sid)
        elif handoff.get("kit") and handoff["b"].get("online_ms"):
            self.notice(hid, "a_crash_sealed", dict(
                what="主控 A 已封存 kit 但没来得及签字就崩溃了（交接 %s）。" % hid,
                why="换棒要 A、B 双签；A 没了，机器不能替你判断 kit 是否完整。",
                ask="1) 接手（handoffd adopt）：由 B 复核 kit 后接棒 2) 放弃交接（handoffd off）", recommend=1,
                if_no_reply="交接停在这里，不会自己继续；新开的窗口会被拦下。"), True,
                ["接手（handoffd adopt）", "放弃交接（handoffd off）"], sid)
        else:
            self.notice(hid, "a_crash_unsealed", dict(
                what="主控 A 在交接中途崩溃了（B 还没上线或 kit 还没封存，交接 %s）。" % hid,
                why="交接要 A 写完 kit、B 复核后才能换棒；A 没了，机器没法继续。",
                ask="1) 放弃交接（handoffd off），重开会话：kit 已写就读 kit，没写就读上一棒的交接记录", recommend=1,
                if_no_reply="交接停在这里，不会自己继续；新开的窗口会被拦下，提示先确认放弃交接。"), True,
                ["放弃交接（handoffd off），重开会话"], sid)

    def _launch(self, handoff):
        """开 B：先落盘 launching（J2①），再调启动器，最后记结果。重启后 launching 不再重开（只会因 B 120 s 不上线响铃）。"""
        hid = handoff["id"]
        launch = handoff.get("launch") or {}
        if launch.get("state") not in (None, "retry"):
            return
        plan = inherit_plan(self.cfg, self.sdir, handoff)
        result, _attempt = baton.mark_launching(self.sdir, hid, self.now, [warp_config_path(handoff["b"]["name"], self.cfg["warp_open"])],
                                                inherit=plan)
        if result != "ok":
            return
        try:
            register_warp_config(self.sdir, warp_config_path(handoff["b"]["name"], self.cfg["warp_open"]), self.now)   # 兜底清扫只认登记过的路径
        except OSError as exc:
            self.log("warp_registry_failed", hid=hid, error=repr(exc))   # 登记失败只意味着兜底清扫以后不会碰这个文件；开窗照常
        self.point("launch:marker")
        self.log("launch_inherit", hid=hid, src=plan["src"], model=plan["model"], effort=plan["effort"], note=plan["note"],
                 extra_args=plan["extra"])
        try:
            code, tail, version = self.launcher_fn(self.cfg, self.sdir, dict(handoff, s4a_plan=plan))
        except Exception as exc:
            code, tail, version = None, "启动器异常：%r" % (exc,), None
        self.point("launch:acted")

        def notify_inside_guard(key, value):
            """record_launch 在 guard 内回调：只入队（alert 在 _deferring 内不发声），出 guard 后发。"""
            if key == "preflight_failed":
                self.notice(hid, "preflight_failed", dict(
                    what="开 B 的预检连续失败 %s 次（还没开窗，交接 %s）：%s" % (value, hid, (tail or "").strip()[-160:]),
                    why="预检没过（claude 版本 / 环境变量 / Warp 配置），启动器保证没有开窗，机器会每隔一会儿重试。",
                    ask="1) 去看预检输出，修好后机器会自动重试 2) 放弃交接（handoffd off）", recommend=1,
                    if_no_reply="机器继续重试，但同一个问题不会自己消失；交接一直停着。"), True,
                    ["去看预检输出", "放弃交接（handoffd off）"])
            else:
                self.notice(hid, "launcher_failed", dict(
                    what="开 B 的启动器异常或返回非 0（退出码 %r，交接 %s）：%s" % (value, hid, (tail or "").strip()[-160:]),
                    why="不能确定 Warp 窗口是否已经弹出；为了不重复开窗，机器不会自动重试。",
                    ask="1) 去看有没有新窗口，有就在其中继续 2) 放弃交接（handoffd off）", recommend=1,
                    if_no_reply="机器不会再开窗；交接一直停着。"), True,
                    ["去看有没有新窗口", "放弃交接（handoffd off）"])

        self._deferring(lambda: baton.record_launch(self.sdir, hid, self.now, code, version, self.cfg["preflight_retry_seconds"],
                                                    notify_inside_guard))
        self.point("launch:recorded")
        self.log("launch", hid=hid, code=code, version=version)
        if version:
            self._claude_version(hid, version)

    def _claude_version(self, hid, version):
        """G5：记 claude --version；与上次不同发无声通知（首次不发）。"""
        path = os.path.join(self.sdir, "daemon_state.json")
        status, value = util.read_json(path)
        state = value if status == "ok" and isinstance(value, dict) else {}
        previous = state.get("last_claude_version")
        if previous != version:
            state["last_claude_version"] = version
            util.atomic_write_json(path, state)
            if previous:
                self.notice(hid, "claude_version", dict(
                    what="claude 版本变了：%s → %s（开 B 时记录）。" % (previous, version),
                    why="新版本可能改变钩子 / 会话行为，仅供参考。", ask="1) 无需处理", recommend=1,
                    if_no_reply="没有影响。"), False, ["无需处理"])

    def _watch_b(self, handoff):
        """设计 §4 :47④：B 120 s 未上线 / B 上线后死。（预检连败 3 次、启动器非 0 在 record_launch 的回调里发。）"""
        hid = handoff["id"]
        launch, b = handoff.get("launch") or {}, handoff["b"]
        if not b.get("online_ms"):
            started = launch.get("started_ms")
            if (launch.get("state") in ("launching", "opened") and isinstance(started, int)
                    and self.now - started >= int(self.cfg["launch_confirm_timeout_seconds"] * 1000)):
                self.notice(hid, "b_not_online", dict(
                    what="开了 B 的窗口，但 %d 秒内 B 没有上线登记（交接 %s）。" % (self.cfg["launch_confirm_timeout_seconds"], hid),
                    why="B 没起来（窗口没弹、claude 起不来或钩子没跑），交接没法继续。",
                    ask="1) 去看新窗口 2) 放弃交接（handoffd off）", recommend=1,
                    if_no_reply="交接一直停在等 B 上线。"), True, ["去看新窗口", "放弃交接（handoffd off）"])
            return
        if b.get("pid") and self._proc(b) == "dead":
            self.notice(hid, "b_died", dict(
                what="B 上线之后又退出了（交接 %s）。" % hid,
                why="接手方没了，换棒没法继续。",
                ask="1) 去看 B 的窗口 2) 放弃交接（handoffd off）", recommend=2,
                if_no_reply="交接一直停着；A 仍是主控。"), True, ["去看 B 的窗口", "放弃交接（handoffd off）"])

    def _open_ambiguous(self, handoff):
        """S2a 自定（待裁）：OPEN 的标记写了「sending」却在一轮开头仍是 sending ⇒ daemon 在「标记已落、结果未记」之间重启过：不重发（至多一次，J2①），
        也不知道 B 是否收到；这是唯一一处「动作丢了但没人知道」的窗口，响一次铃让 Owner 去 B 窗口看。"""
        hid = handoff["id"]
        if (handoff.get("open") or {}).get("state") != "sending":
            return
        self.notice(hid, "open_ambiguous", dict(
            what="daemon 在给 B 投开场指令的途中重启了，不确定 B 有没有收到（交接 %s）。" % hid,
            why="为了不重复投递，机器不会重发；B 如果没收到，会一直空闲等着。",
            ask="1) 去 B 的窗口看：没收到开场指令就放弃交接（handoffd off）重来；收到了就不用管 2) 放弃交接（handoffd off）", recommend=1,
            if_no_reply="B 没收到的话交接会一直停着。"), True, ["去 B 的窗口看", "放弃交接（handoffd off）"])

    def _open(self, handoff):
        """B 已上线且 kit 已封存时投 OPEN（content = 开场指令原文，J3）：目标核对（J2②）→ 先落盘 sending → 发出前重核（K3）→ 发 → 记结果。至多一次。"""
        hid = handoff["id"]
        b = handoff["b"]
        if not handoff.get("kit") or not b.get("online_ms") or (handoff.get("open") or {}).get("state") is not None:
            return
        ok, why, _info = self._target_ok(b)
        if not ok:
            self.log_change("open_target:" + hid, "open_target_refused", hid=hid, reason=why)
            if why in ("cwd_unknown", "cwd_outside"):
                self._open_refused(hid, b, why)
            return
        target = (b["pid"], b["lstart"])
        result, content = baton.mark_delivery(self.sdir, hid, "open", self.now, target)
        if result != "ok":
            return
        self.point("open:marker")
        slot, info, why = self._recheck(hid, "arming", lambda state, h: h["b"], target)
        if slot is None:
            baton.finish_delivery(self.sdir, hid, "open", self.now, False)    # 确定没发：退回可重试
            self.log("open_recheck_refused", hid=hid, reason=why)
            return
        delivered, detail = self._send(slot["pid"], content, info)
        self.point("open:acted")
        baton.finish_delivery(self.sdir, hid, "open", self.now, delivered)
        self.point("open:recorded")
        self.log("open", hid=hid, delivered=delivered, detail=detail)

    def _open_refused(self, hid, b, why):
        self.notice(hid, "open_refused", dict(
            what="B（pid %s）在线，但机器查不到它的会话文件 / 它的工作目录不在本项目下（%s），没有投开场指令（交接 %s）。" % (b["pid"], why, hid),
            why="为避免投错会话，机器只向「三元组 == 登记册 ∧ cwd 在本项目下」的进程投递。",
            ask="1) 去 B 的窗口看 2) 放弃交接（handoffd off）", recommend=1, if_no_reply="交接一直停着。"),
            True, ["去 B 的窗口看", "放弃交接（handoffd off）"])

    def _commit(self, handoff):
        hid = handoff["id"]
        if not handoff.get("a_signed") or not handoff.get("b_signed"):
            return
        self.point("commit:before")
        result, value = baton.commit(self.sdir, self.now, self.proc_state_fn)
        if result != "committed":
            if result == "wait":
                self.log_change("commit_wait:" + hid, "commit_wait", hid=hid, reason=value)
            return
        self.point("commit:after")
        self.log("commit", hid=hid, a_state=value["a_state"])
        committed = self._same_handoff(hid, "committed")
        if committed is not None:
            self._post_commit(committed)

    # ---- 换棒后 ----

    def _post_commit(self, handoff):
        hid = handoff["id"]
        self._cleanup_warp_config(handoff)
        self._watch_tail(handoff)
        if handoff["post"]["kill"]["state"] == "pending":
            self._kill_a(handoff)
        handoff = self._same_handoff(hid, "committed")
        if handoff is None:
            return
        if handoff["post"]["baton"]["state"] == "pending":
            self._send_baton(handoff)
        handoff = self._same_handoff(hid, "committed")
        if handoff is None:
            return
        self._watch_tail(handoff)
        self._watch_old_a(handoff)
        self._complete(handoff)

    def _watch_tail(self, handoff):
        """K1：换棒收尾期的「卡住」都要有铃——BATON 发到一半重启（sending）/ 放弃（failed）、新主控 B 在收尾期死了。"""
        hid = handoff["id"]
        baton_state = handoff["post"]["baton"]["state"]
        state = self._fresh()
        holder = state["holder"] if state else None
        b_dead = bool(holder and holder.get("sid") == (handoff.get("b") or {}).get("uuid") and self._proc(holder) == "dead")
        if baton_state == "sending":
            self.notice(hid, "baton_ambiguous", dict(
                what="daemon 在给新主控 B 投 BATON 的途中重启了，不确定 B 有没有收到（交接 %s）。" % hid,
                why="为了不重复投递，机器不会重发；B 按提示在收到 BATON 前一直空闲等着。",
                ask="1) 去 B 的窗口，告诉它「你已是主控，请按 kit 继续」", recommend=1, if_no_reply="B 没收到的话会一直空闲，接力停在这里。"),
                True, ["去 B 的窗口告诉它继续"], (handoff.get("a") or {}).get("sid"))
        elif baton_state == "failed" and not b_dead:     # B 已死时只发 b_died_after_commit（它的建议才对）
            self.notice(hid, "baton_failed", dict(
                what="换棒完成了，但给新主控 B 的 BATON 没能送达（交接 %s）。" % hid,
                why="B 不可达或核对不过（连败 %d 次或目标已不是登记册里的那个进程），机器放弃投递；B 在收到 BATON 前一直空闲等着。" % baton.DELIVERY_RETRY_LIMIT,
                ask="1) 去 B 的窗口，告诉它「你已是主控，请按 kit 继续」", recommend=1, if_no_reply="B 会一直空闲，接力停在这里。"),
                True, ["去 B 的窗口告诉它继续"], (handoff.get("a") or {}).get("sid"))
        if b_dead:
            self.notice(hid, "b_died_after_commit", dict(
                what="换棒完成后，新主控 B 退出了（交接 %s）。" % hid,
                why="B 在收尾期没了；旧主控 A 已被（或正被）关闭，项目现在没有主控。",
                ask="1) 重开会话：kit 已存档在状态目录 kits/ 下，交接记录在 handoffs.jsonl", recommend=1,
                if_no_reply="项目没有主控；下一个提交提示词的会话会成为新主控。"),
                True, ["重开会话，读 kit 与交接记录"], holder.get("sid"))
        kill = handoff["post"]["kill"]
        if kill["state"] == "sending" and self._proc(handoff["post"]["a_old"]) == "dead" and (handoff.get("a_signed") or {}).get("by") != "owner":
            # B6：SIGTERM 标记已落、A 随后死了（不知是我们的信号还是它自己）：补一条与 :64 同键的无声记录
            self._a_crash_quiet(handoff)

    def _kill_a(self, handoff):
        """commit 后 SIGTERM A：目标 = post.a_old（commit 时的 holder 槽三元组）。已死 / 核对不过 ⇒ 记 skipped 不发信号；判活不确定 ⇒ 下一轮再看。
        先落 sending，发出前一刻重核（K3），核对没过 ⇒ 记 skipped。"""
        hid = handoff["id"]
        a_old = handoff["post"]["a_old"]
        ok, why, _info = self._target_ok(a_old)
        if not ok:
            if why in ("unknown",):
                self.log_change("kill_wait:" + hid, "kill_wait", hid=hid, reason=why)
                return
            self._kill_skipped(handoff, why)
            return
        target = (a_old["pid"], a_old["lstart"])
        result, _kill = baton.mark_kill(self.sdir, hid, self.now, "sending", target=target)
        if result != "ok":
            return
        self.point("kill:marker")
        slot, _info, why = self._recheck(hid, "committed", lambda state, h: h["post"]["a_old"], target)
        if slot is None:
            self._kill_skipped(handoff, why)
            return
        try:
            self.kill_fn(slot["pid"], signal.SIGTERM)
            self.log("kill", hid=hid, pid=slot["pid"])
        except OSError as exc:
            self.log("kill_error", hid=hid, pid=slot["pid"], error=repr(exc))
        self.point("kill:acted")
        baton.mark_kill(self.sdir, hid, self.now, "sent")
        self.point("kill:recorded")

    def _kill_skipped(self, handoff, why):
        hid = handoff["id"]
        baton.mark_kill(self.sdir, hid, self.now, "skipped", reason=why)
        self.log("kill_skipped", hid=hid, reason=why)
        if why == "not_live" and (handoff.get("a_signed") or {}).get("by") != "owner":
            self._a_crash_quiet(handoff)   # A 在 SIGTERM 之前自己死了：补一条与 :64 同键的无声记录（归档会吞掉它，A4）

    def _a_crash_quiet(self, handoff):
        hid = handoff["id"]
        self.notice(hid, "a_crash_signed", dict(
            what="主控 A 在签字之后崩溃了，B 照常接棒（交接 %s）。" % hid,
            why="A 已签字，换棒的条件不依赖 A 继续运行；这条只是事后记录。",
            ask="1) 无需处理", recommend=1, if_no_reply="B 已是主控。"), False, ["无需处理"], (handoff.get("a") or {}).get("sid"))

    def _send_baton(self, handoff):
        """commit 后给 B（此时的 holder 槽）投 BATON：目标核对（J2②）→ 先落盘 sending → 发出前重核（K3）→ 发 → 记结果。至多一次；目标已死 / 核对不过 ⇒ 放弃（failed，有铃）。"""
        hid = handoff["id"]
        state = self._fresh()
        holder = state["holder"] if state else None
        if not holder:
            return
        ok, why, _info = self._target_ok(holder)
        if not ok and why == "unknown":
            self.log_change("baton_wait:" + hid, "baton_wait", hid=hid, reason=why)
            return
        target = (holder["pid"], holder["lstart"])
        result, _ = baton.mark_delivery(self.sdir, hid, "baton", self.now, target)
        if result != "ok":
            return
        if not ok:
            baton.finish_delivery(self.sdir, hid, "baton", self.now, False, give_up=True)
            self.log("baton_refused", hid=hid, reason=why)
            return
        self.point("baton:marker")
        slot, info, why = self._recheck(hid, "committed", lambda state, h: state["holder"], target)
        if slot is None:
            baton.finish_delivery(self.sdir, hid, "baton", self.now, False, give_up=True)
            self.log("baton_recheck_refused", hid=hid, reason=why)
            return
        a = handoff["post"]["a_old"]
        text = msg.format_message("BATON", "你已是本项目的主控（holder）：旧主控 A（会话 %s）已交棒并会被关闭。请按 kit 继续原目标。" % (a.get("sid") or "?")[:8])
        delivered, detail = self._send(slot["pid"], text, info)
        self.point("baton:acted")
        baton.finish_delivery(self.sdir, hid, "baton", self.now, delivered)
        self.point("baton:recorded")
        self.log("baton", hid=hid, delivered=delivered, detail=detail)

    def _watch_old_a(self, handoff):
        """设计 §4 :47⑤：换棒后旧 A 60 s 仍活（kill 已发 / 目标核对没过没发 / 发到一半崩了）⇒ 响铃一次。"""
        hid = handoff["id"]
        kill = handoff["post"]["kill"]
        if kill["state"] == "pending" or (kill["state"] == "skipped" and kill.get("reason") == "not_live"):
            return
        base = kill["ms"] if isinstance(kill.get("ms"), int) else handoff.get("committed_ms")
        if not isinstance(base, int) or self.now - base < OLD_A_ALIVE_SECONDS * 1000:
            return
        if self._proc(handoff["post"]["a_old"]) != "live":
            return
        self.notice(hid, "old_a_alive", dict(
            what="换棒已完成，但旧主控 A 在 %d 秒后仍在运行（交接 %s）。" % (OLD_A_ALIVE_SECONDS, hid),
            why="机器已按规格发 SIGTERM（或因目标核对不过没有发）；旧主控已不再是主控，只是还没退出。",
            ask="1) 去旧主控窗口手动关掉", recommend=1, if_no_reply="B 已是主控，旧窗口只是多开着，不影响接力。"),
            True, ["去旧主控窗口手动关掉"], (handoff.get("a") or {}).get("sid"))

    def _complete(self, handoff):
        """收尾：kill 与 BATON 都有结果，且旧 A 已不活（或已就「仍活」响过铃）⇒ 归档 handoff、清空（此后与无交接时规则一致）。
        「卡住了」类 notice（STICKY_KINDS）不随归档解除，留到 Owner 看见。"""
        hid = handoff["id"]
        post = handoff["post"]
        if post["kill"]["state"] == "pending" or post["baton"]["state"] in ("pending",):
            return
        if self._proc(post["a_old"]) == "live" and not sessions.notice_exists(self.sdir, "n-%s-old_a_alive" % hid):
            return
        self.point("complete:before")
        baton.complete(self.sdir, hid, self.now)
        sessions.resolve_notices_for(self.sdir, hid, self.now, keep=STICKY_KINDS)
        self.point("complete:after")
        self.log("complete", hid=hid)


def acquire_daemon_lock(path, wait=LOCK_RETRY_SECONDS, interval=LOCK_RETRY_INTERVAL, flock_fn=fcntl.flock):
    """取 daemon.lock：只有 EWOULDBLOCK / EAGAIN 才算「已有实例」（在 wait 秒内重试，避开 status 的瞬时试取，仍失败返回 None）；
    其余 OSError（EBADF、EIO、ENOLCK……）是真故障，原样抛出，不当作已有实例而静默退出（G8）。"""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    end = time.monotonic() + wait
    while True:
        try:
            flock_fn(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise
            if time.monotonic() >= end:
                os.close(fd)
                return None
            time.sleep(interval)


def _beat(sdir, now_ms):
    try:
        sessions.write_heartbeat(sdir, now_ms)
    except OSError:
        pass


def run_daemon(sdir, daemon, poll_seconds, once=False, sleep_fn=time.sleep, clock=util.now_ms, out=print, lock_wait=None, lock_interval=LOCK_RETRY_INTERVAL,
               idle_exit=None):
    """取得 daemon.lock 后循环 tick。锁被占：--once（前台 / 测试）等 3 s、仍占则 0 退出且不做任何动作；常驻（launchd 拉起）等 DAEMON_LOCK_WAIT_SECONDS、
    仍占则以非 0 退出，让 KeepAlive 重拉——不能 0 退出，否则 launchd 当作正常结束不再拉起，daemon 就永久缺席。
    idle_exit（常驻时由 main 传入）：每轮 tick 之后返回 True ⇒ 以 0 退出（manual 且没有未收尾意图，规格:14）。"""
    os.makedirs(sdir, exist_ok=True)
    if lock_wait is None:
        lock_wait = LOCK_RETRY_SECONDS if once else DAEMON_LOCK_WAIT_SECONDS
    fd = acquire_daemon_lock(os.path.join(sdir, "daemon.lock"), wait=lock_wait, interval=lock_interval)
    if fd is None:
        if once:
            out("守护程序已在运行")
            return 0
        out("守护程序：daemon.lock 被别的进程占着，等了 %d 秒仍没拿到；以非 0 退出，交给 launchd 重拉" % lock_wait)
        return 1
    try:
        _beat(sdir, clock())                       # 心跳（设计 §3：每轮写）：钩子 / status / `on` 都靠它判 daemon 是不是活着
        while True:
            daemon.tick(clock())
            _beat(sdir, clock())
            if once:
                return 0
            if idle_exit is not None and idle_exit():
                out("守护程序：manual 且没有未收尾的交接意图，自己退出（handoffd on 会再拉起）")
                return 0
            sleep_fn(poll_seconds)
    finally:
        os.close(fd)


def main(cfg, sdir, once=False):
    """真实运行入口（handoffd daemon，launchd plist 与前台共用）。"""
    def log_fn(**fields):
        sessions.append_event(sdir, dict({"t_ms": util.now_ms(), "sid": None, "role": "daemon"}, **fields))

    notifier = notify.Notifier(cfg["alerts"], log_fn=log_fn)
    return run_daemon(sdir, Daemon(cfg, sdir, notifier), cfg["poll_seconds"], once=once, idle_exit=lambda: idle_exit_due(sdir))
