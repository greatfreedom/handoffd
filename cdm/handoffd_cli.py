# handoffd 命令行（设计 §5.1）：S1：on / off / status / review-round / notify，外加钩子内部用的 ring；
# S2a：request / seal / sign out|in / adopt / reply（只登记）/ daemon；S3：on 拉起 / off 停 daemon（cdm/launchd.py）、test-alert（旧 cdm 命令行退役后并入）。
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdm import baton, config, launchd, lockcore, msg, notify, procs, review, sessions, util  # noqa: E402
from cdm import daemon as daemon_module  # noqa: E402

EXIT_USAGE = 2
EXIT_REFUSED = 3
CATEGORIES = ("authorize", "decide", "incident", "stuck", "mismatch", "rule")
REASONS = ("goal", "ctx65", "ctx76")
REPLY_KINDS = ("task", "peer", "quota", "owner")
REPLY_ETA_MIN, REPLY_ETA_MAX = 15, 180     # reply task 可带的预计时长范围（分钟，含端点）
REPLY_ETA_FORMAT = re.compile(r"([0-9]+)m")   # 只用 fullmatch：`$` 会放过结尾换行（R3 P1）


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="handoffd.json 的路径（缺省：从当前目录向上找 .claude/handoffd.json）")
    parser = argparse.ArgumentParser(prog="handoffd", description="主控自动接力开关")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("on", parents=[common], help="当前项目开自动接力（仅限 Owner 自己的终端），并拉起 daemon")
    sub.add_parser("off", parents=[common], help="取消进行中的交接、回到 manual；没有未收尾的事就停 daemon")
    sub.add_parser("status", parents=[common], help="看模式、daemon（launchd / 心跳）、持棒者、待处理的铃")
    ta = sub.add_parser("test-alert", parents=[common], help="按真实配置发一次提醒试听（每次都从第一个铃声、第一个音色开始，不接着上次轮换）")
    ta.add_argument("kind", choices=notify.KINDS)
    rr = sub.add_parser("review-round", parents=[common], help="登记复核轮次（只增不减）")
    rr.add_argument("--product", required=True)
    rr.add_argument("--round", required=True, type=int, dest="round_number")
    rr.add_argument("--p0", required=True, type=int)
    nt = sub.add_parser("notify", parents=[common], help="四段通知 + 编号选项，并响铃（规格:50）")
    nt.add_argument("--cat", required=True, choices=CATEGORIES)
    nt.add_argument("--what", required=True)
    nt.add_argument("--why", required=True)
    nt.add_argument("--ask", required=True, help='带编号的选项，例如 "1) 去该窗口处理 2) 放弃交接"')
    nt.add_argument("--recommend", required=True, type=int)
    nt.add_argument("--if-no-reply", required=True, dest="if_no_reply")
    rq = sub.add_parser("request", parents=[common], help="（主控）申请交接：开 B 的窗口")
    rq.add_argument("--reason", choices=REASONS, default="goal")
    sl = sub.add_parser("seal", parents=[common], help="（主控）封存 kit，抽出「给新 session 的开场指令」交 daemon 投给 B")
    sl.add_argument("--kit", required=True)
    sg = sub.add_parser("sign", parents=[common], help="签字：A 用 out，B 用 in；双签后 daemon 核对并换棒")
    sg.add_argument("which", choices=("out", "in"))
    sub.add_parser("adopt", parents=[common], help="（仅限 Owner 终端）A 已封存 kit 未签字就崩溃时，补记 A 的签字")
    rp = sub.add_parser("reply", parents=[common], help="答 daemon 的进展询问（登记后由 daemon 核对）")
    rp.add_argument("kind", choices=REPLY_KINDS)
    rp.add_argument("eta", nargs="?", default=None, help="（仅 task）预计还要多久，例如 60m；%d–%d 分钟，期间 daemon 不再问" % (REPLY_ETA_MIN, REPLY_ETA_MAX))
    dm = sub.add_parser("daemon", parents=[common], help="（内部）运行 daemon 主循环")
    dm.add_argument("--once", action="store_true")
    ring = sub.add_parser("ring", help="（内部，钩子用）按参数直接发声，不读通知记录")
    ring.add_argument("--project", required=True)
    ring.add_argument("--kind", required=True, choices=notify.KINDS)
    ring.add_argument("--what", required=True)
    ring.add_argument("--why", required=True)
    ring.add_argument("--ask", required=True)
    ring.add_argument("--recommend", required=True, type=int)
    ring.add_argument("--if-no-reply", required=True, dest="if_no_reply")
    return parser


def find_project_config(args, env):
    """按 --config 或从当前目录向上找 .claude/handoffd.json；都没有就用全默认配置。"""
    if getattr(args, "config", None):
        return config.load_config(args.config)
    start = os.path.realpath(env.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    current = start
    while True:
        candidate = os.path.join(current, ".claude", "handoffd.json")
        if os.path.isfile(candidate):
            return config.load_config(candidate, current)
        parent = os.path.dirname(current)
        if parent == current:
            return config.default_config(start)
        current = parent


def resolve_sdir(cfg):
    """返回 (<S>, None) 或 (None, 原因)。"""
    if cfg["state_dir_set"]:
        return cfg["state_dir"], None
    status, path = lockcore.state_dir(cfg["project_dir"], procs.Deadline(3.0))
    return (path, None) if status == "ok" else (None, path)


class Caller:
    """命令是从哪个会话里敲的：CLAUDE_CODE_SESSION_ID（RT:T1a）+ 祖先链里最近的 claude 进程；Owner 终端里两者皆无。"""

    def __init__(self, env, chain_fn):
        sid = env.get("CLAUDE_CODE_SESSION_ID")
        self.sid = util.safe_id(sid) if isinstance(sid, str) else None
        try:
            self.chain = list(chain_fn(procs.Deadline(2.0)) or [])
        except Exception:
            self.chain = []
        self.in_session = bool(self.sid and self.chain)

    def identity(self):
        return self.chain[0] if self.chain else (None, "")

    def chain_fn(self):
        return self.chain


def fmt_ms(ms):
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def cmd_on(cfg, sdir, caller, now, args=None, **_):
    mode, since = sessions.read_mode(sdir)
    if mode != "auto":
        sessions.write_mode(sdir, "auto", now)
        print("模式：auto（已开启）")
    else:
        print("模式：auto（本来就开着，自 %s）" % fmt_ms(since))
    if caller.in_session:
        result, role = baton.claim(sdir, caller.sid, caller.identity, caller.chain_fn, now)
        if result in ("claimed", "rolled") or role == "holder":
            print("主控：本会话（%s）" % caller.sid[:8])
        elif role in ("spectator", "blocked"):
            print("主控：本项目已有别的主控会话在运行，本会话是旁观（在主控窗口敲 on 才算数）")
        else:
            print("主控：认领没有完成（%s）；下一条提示词会再试" % result)
    else:
        print("主控：当前无主控；本项目下一个提交提示词的会话将成为主控")
    # 模式已是 auto：daemon 没拉起来也不回滚（auto 下钩子会在心跳停 120 s 后响铃提醒，设计 :60/:72）；报错退出 1，再敲一次 on 重试
    return launchd.start(cfg, sdir, getattr(args, "config", None), now)


def run_daemon_once(cfg, sdir, now, notifier, kill_fn, cwd_fn, lstart_fn=None):
    """K4 单一执行者：落盘意图（SIGTERM 未接棒 B、已换棒交接的收尾）只由持有 daemon.lock 的进程执行。
    CLI 非阻塞试取该锁：取到（没有 daemon 在跑）⇒ 本进程就是执行者，跑一轮 daemon 逻辑再放锁；取不到 ⇒ 有 daemon（或别的执行者）在跑，
    意图已在登记册里，留给它。返回 Daemon，或 None（留给 daemon）。"""
    fd = daemon_module.acquire_daemon_lock(os.path.join(sdir, "daemon.lock"), wait=0)
    if fd is None:
        return None
    try:
        dm = daemon_module.Daemon(cfg, sdir, notifier or notify.Notifier(cfg["alerts"]), kill_fn=kill_fn, cwd_fn=cwd_fn, lstart_fn=lstart_fn)
        dm.tick(now)
        return dm
    finally:
        os.close(fd)


def cmd_off(cfg, sdir, caller, now, kill_fn=os.kill, cwd_fn=None, notifier=None, lstart_fn=None, **_):
    if not os.path.isdir(sdir):
        print("模式：manual（本来就是；没有进行中的交接）")
        return 0
    result, handoff = baton.abandon(sdir, now)
    if result not in ("ok", "committed"):
        print("放弃交接没有完成（登记册%s），模式未改动，请稍后重试" % ("正被占用" if result == "busy" else "读不出"), file=sys.stderr)
        return 1
    mode, _since = sessions.read_mode(sdir)
    if mode != "manual":
        sessions.write_mode(sdir, "manual", now)
    if handoff and result == "ok":
        sessions.resolve_notices_for(sdir, handoff.get("id"), now)
        print("已放弃进行中的交接 %s（阶段 %s）" % (handoff.get("id"), handoff.get("phase")))
    elif result == "committed":
        print("交接 %s 已经换棒（B 已是主控），不能放弃；已切回 manual。" % handoff.get("id"))
    else:
        print("没有进行中的交接")
    if result == "ok" and handoff:
        remove_launch_configs(sdir, handoff)
    # 收尾（SIGTERM 未接棒 B、已换棒交接的收尾）只由持 daemon.lock 的进程执行（K4）：abandon 已把意图落进登记册；
    # 没有 daemon 在跑 ⇒ 本命令先取得锁、当场跑一轮；有 daemon 在跑 ⇒ 留给它的下一轮
    dm = run_daemon_once(cfg, sdir, now, notifier, kill_fn, cwd_fn, lstart_fn)
    if dm is None and pending_closeout(sdir):   # 有 daemon 在跑 ∧ 确有没收尾的意图才这么说；没有就不说（按实际状态输出）
        print("收尾（关掉未接棒的 B / 换棒后的收尾）已交给正在运行的 daemon：它持有 daemon.lock，会在下一轮做完；handoffd status 可查。")
    for outcome, pid in (dm.off_results if dm else []):
        if outcome == "killed":
            print("已请未接棒的 B（pid %d）退出（B 退出后意图才算完成；超时仍在运行会响铃）" % pid)
        elif outcome == "gone":
            print("B（pid %d）已不在运行" % pid)
        elif outcome == "unopened":
            print("B 的窗口一直没有出现，视为没有开出来")
        elif outcome.startswith("refused:"):
            why = outcome.split(":", 1)[1]
            print("没有向 B（pid %d）发信号：%s；请自己关掉它的窗口" % (pid, "查不到它的会话文件 / 工作目录" if why == "cwd_unknown" else "它的工作目录不在本项目下" if why == "cwd_outside" else why), file=sys.stderr)
        else:
            print("B（pid %s）的关闭结果不确定，请自己确认" % pid, file=sys.stderr)
    status, state = baton.read(sdir)
    if result == "committed":
        left = (state or {}).get("handoff") if status != "error" else None
        if left:
            print("换棒后的收尾还没全部做完（%s）；daemon 在任何 mode 下都会继续做完，handoffd status 可查。" % describe_progress(left))
        else:
            print("换棒后的收尾已做完（旧 A 已关闭 / BATON 已发 / 归档）。")
    print("模式：manual")
    pending = pending_closeout(sdir)
    if pending:
        # 有未收尾的交接意图：daemon 不停，让它收完（它在任何 mode 下都会继续做完，K1）；收完它在 manual 下自己退出（daemon.idle_exit_due）
        if launchd.query(cfg) == "loaded":
            print("daemon：保留，还有没收尾的事：%s。它做完后会自己退出（manual 下没有未收尾的事就退）；handoffd status 可查进度。" % "；".join(pending))
        else:
            print("daemon：不在 launchd 里，没人替你做完这些收尾：%s。再敲一次 handoffd off 会当场再跑一轮收尾。" % "；".join(pending), file=sys.stderr)
        return 0
    return launchd.stop(cfg, sdir)


pending_closeout = daemon_module.pending_closeout


def remove_launch_configs(sdir, handoff):
    """off 放弃交接时当场删本次开窗记下的临时 Warp 配置（准确路径）；删不掉的留在 off_kill 意图里，由 daemon 在结束该意图前删。"""
    paths = list((handoff.get("launch") or {}).get("config_paths") or [])
    done = []
    for path in paths:
        try:
            if os.path.lexists(path):
                os.unlink(path)
                print("已删除开窗用的临时配置：%s" % path)
            done.append(path)
        except OSError as exc:
            print("开窗用的临时配置没删掉（%s）：%s；留给 daemon 删。" % (type(exc).__name__, path), file=sys.stderr)
    if done:
        baton.off_kill_drop_paths(sdir, handoff.get("id"), done)


def describe_progress(handoff):
    """status 里的进展一行：B 窗口 / 上线 / kit / 签字 / 换棒后的收尾。"""
    launch, b = handoff.get("launch") or {}, handoff.get("b") or {}
    parts = ["B 会话名 %s" % (b.get("name") or "?"),
             "开窗 %s" % ({None: "未开", "launching": "进行中", "opened": "已开", "retry": "预检失败待重试", "uncertain": "结果不确定"}.get(launch.get("state"), "?")),
             "B %s" % ("已上线" if b.get("online_ms") else "未上线"),
             "kit %s" % ("已封存" if handoff.get("kit") else "未封存"),
             "OPEN %s" % ({None: "未发", "sending": "发送中", "sent": "已发"}.get((handoff.get("open") or {}).get("state"), "?")),
             "A %s" % ("已签字" if handoff.get("a_signed") else "未签字"),
             "B %s" % ("已签字" if handoff.get("b_signed") else "未签字")]
    post = handoff.get("post")
    if post:
        parts.append("换棒后：关旧 A %s，BATON %s" % (post["kill"]["state"], post["baton"]["state"]))
    return "；".join(parts)


def print_b_model(handoff):
    """S4a 自定：交接段多一行 B 实际用的模型 / 档位（开窗时从 A 的状态栏快照继承；没继承的项按 claude_extra_args）。还没开过窗就不显示。"""
    launch = handoff.get("launch") or {}
    if launch.get("b_inherit") is not None:
        print("  B 模型：%s / 档位：%s" % (launch.get("b_model") or "未指定（按配置）", launch.get("b_effort") or "未指定（按配置）"))


def print_heartbeat(cfg, sdir, now, manual_note=None):
    """daemon 心跳年龄一行（无心跳 = daemon 没在运行，或刚启动还没写）。manual_note 非空 = manual 下：只说「最后一次心跳」并标注状态。"""
    heartbeat = sessions.read_heartbeat(sdir)
    limit = int(cfg["heartbeat_stale_seconds"])
    if manual_note:
        print("daemon：无心跳记录（%s）" % manual_note if heartbeat is None else "daemon：最后一次心跳 %d 秒前（%s）" % ((now - heartbeat) // 1000, manual_note))
        return
    if heartbeat is None:
        print("daemon：无心跳（daemon 没在运行，或刚启动还没写心跳）")
    else:
        age = (now - heartbeat) // 1000
        print("daemon：心跳 %d 秒前%s" % (age, "（已停，超过 %d 秒）" % limit if age > limit else "（正常）"))


def cmd_status(cfg, sdir, caller, now, **_):
    mode, since = sessions.read_mode(sdir) if os.path.isdir(sdir) else ("manual", 0)
    print("项目：%s" % cfg["project_dir"])
    print("状态目录：%s" % sdir)
    print("模式：%s%s" % (mode, "（自 %s）" % fmt_ms(since) if mode == "auto" else "（默认）"))
    if mode != "auto":
        closing = pending_closeout(sdir) if os.path.isdir(sdir) else []
        if closing:   # off 之后 daemon 为收尾保留着（S2a 待裁 2）
            print("daemon：收尾中（%s）" % "；".join(closing))
        else:
            print("daemon：未启用（manual）")
        if os.path.isdir(sdir):
            print_heartbeat(cfg, sdir, now, "收尾中" if closing else "daemon 已停止")   # 规格:15：status 显示心跳年龄；manual 下标明是最后一次的，不能写成「正常」
            print("daemon（launchd）：%s" % launchd.describe(cfg))
        print("主控：manual 下不登记")
        residual, pending_kills = None, []
        if os.path.isdir(sdir):
            status, state = baton.read(sdir)
            if status == "ok":
                residual, pending_kills = state["handoff"], state.get("off_kill") or []
        if residual:   # A3：off 之后仍有已换棒、没收尾完的交接——如实显示，不说「无」
            print("交接：%s 阶段 %s（off 前已换棒，收尾未完成）" % (residual.get("id"), residual.get("phase")))
            print("  " + describe_progress(residual))
            print_b_model(residual)
        else:
            print("交接：无")
        for item in pending_kills:
            print("未完成的收尾：off 之后还没做完：%s" % daemon_module.describe_off_kill(item))
        pending = sessions.pending_notices(sdir) if os.path.isdir(sdir) else []
        print("待处理的铃：%d 条" % len(pending))
        for item in pending[-3:]:
            print("  - %s [%s] %s" % (item.get("id"), item.get("cat"), str(item.get("text", "")).split("\n")[0]))
        return 0
    print_heartbeat(cfg, sdir, now)
    print("daemon（launchd）：%s" % launchd.describe(cfg))
    status, state = baton.read(sdir)
    if status == "error":
        print("主控：登记册 baton.json 读不出")
        print("交接：未知")
    else:
        holder = state["holder"]
        if holder:
            holder_state = procs.proc_state({"session_id": holder["sid"], "pid": holder["pid"], "lstart": holder["lstart"]},
                                            procs.Deadline(2.0))
            label = {"live": "存活", "dead": "已退出", "unknown": "状态不确定"}[holder_state]
            print("主控：会话 %s pid %d（%s）" % (holder["sid"][:8], holder["pid"], label))
        else:
            print("主控：无")
        handoff = state["handoff"]
        if handoff:
            print("交接：%s 阶段 %s，B=%s" % (handoff.get("id"), handoff.get("phase"), handoff["b"]["uuid"][:8]))
            print("  " + describe_progress(handoff))
            print_b_model(handoff)
        else:
            print("交接：无")
    pending = sessions.pending_notices(sdir)
    print("待处理的铃：%d 条" % len(pending))
    for item in pending[-3:]:
        print("  - %s [%s] %s" % (item.get("id"), item.get("cat"), str(item.get("text", "")).split("\n")[0]))
    return 0


def cmd_review_round(cfg, sdir, caller, now, args=None, **_):
    mode, _since = sessions.read_mode(sdir) if os.path.isdir(sdir) else ("manual", 0)
    if mode != "auto":
        # F7：manual 不记录（规格:75、:85）——不写盘、不建目录，只说明；退出 0
        print("manual 模式不登记轮次账（auto 才登记，规格:85）：%s 第 %d 轮、P0=%d 未记录" % (args.product, args.round_number, args.p0))
        return 0
    if caller.in_session:
        status, state = baton.read(sdir)
        if status == "error":
            print("拒绝：登记册 baton.json 读不出，不能确认你是主控", file=sys.stderr)
            return EXIT_REFUSED
        role, _info = baton.classify(state, caller.sid, caller.identity, caller.chain_fn)
        if role not in ("holder", "holder_sub"):
            print("拒绝：只有持棒的主控能登记复核轮次（你的角色：%s）" % role, file=sys.stderr)
            return EXIT_REFUSED
    try:
        entry = review.register(sdir, args.product, args.round_number, args.p0, now)
    except review.Refused as exc:
        print("拒绝：%s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    print("已登记 %s：第 %d 轮，P0=%d" % (args.product, entry["round"], entry["p0"]))
    return 0


def cmd_notify(cfg, sdir, caller, now, args=None, notifier=None, **_):
    try:
        text = notify.format_notice(args.what, args.why, args.ask, args.recommend, args.if_no_reply)
    except ValueError as exc:
        print("拒绝：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    mode, _since = sessions.read_mode(sdir) if os.path.isdir(sdir) else ("manual", 0)
    notice_id = sessions.new_notice_id(now)
    if mode == "auto":
        sessions.append_notice(sdir, {
            "type": "notice", "id": notice_id, "t_ms": now, "sid": caller.sid, "cat": args.cat, "ring": True,
            "status": "pending", "text": text, "options": [label for _n, label in notify.parse_options(args.ask)],
        })
    (notifier or notify.Notifier(cfg["alerts"])).alert("problem", text, args.what)
    print("已响铃%s：%s" % ("并记录" if mode == "auto" else "（manual 不记录）", notice_id))
    return 0


def cmd_test_alert(cfg, args, notifier=None):
    """按真实配置发一次提醒试听。每次都是新建的通知器：从第一个铃声、第一个音色开始，不接着上次轮换（轮换位置只由 daemon 持久化，G9）。"""
    (notifier or notify.Notifier(cfg["alerts"])).alert(args.kind, "这是一条 %s 类提醒的试听。" % args.kind)
    return 0


def cmd_ring(args, notifier=None):
    cfg, _error = config.load_handoffd(args.project)
    try:
        text = notify.format_notice(args.what, args.why, args.ask, args.recommend, args.if_no_reply)
    except ValueError as exc:
        print("拒绝：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    (notifier or notify.Notifier(cfg["alerts"])).alert(args.kind, text, args.what)
    return 0


def auto_and_in_session(sdir, caller, what):
    """request / seal / sign 的共同前提：模式是 auto、命令是从会话里敲的。返回退出码（None = 通过）。"""
    mode, _since = sessions.read_mode(sdir) if os.path.isdir(sdir) else ("manual", 0)
    if mode != "auto":
        print("拒绝：manual 模式没有自动交接（Owner 先 handoffd on）", file=sys.stderr)
        return EXIT_REFUSED
    if not caller.in_session:
        print("拒绝：%s 只能在会话里敲（需要会话身份）" % what, file=sys.stderr)
        return EXIT_REFUSED
    return None


def refuse(text):
    print("拒绝：" + text, file=sys.stderr)
    return EXIT_REFUSED


def cmd_request(cfg, sdir, caller, now, args=None, **_):
    code = auto_and_in_session(sdir, caller, "request")
    if code is not None:
        return code
    hid, name, b_uuid = baton.new_ids(now, cfg["session_name_prefix"])
    a_name = (msg.session_info(caller.identity()[0]) or {}).get("name")
    result, value = baton.request(sdir, caller.sid, caller.identity, caller.chain_fn, now, args.reason, hid, b_uuid, name, a_name)
    if result == "exists":
        return refuse("已有进行中的交接 %s（阶段 %s）" % (value.get("id"), value.get("phase")))
    if result == "not_holder":
        return refuse("只有持棒的主控能申请交接（你的角色：%s）" % value)
    if result != "ok":
        return refuse("申请没有完成（%s）" % result)
    print("已申请交接 %s（原因 %s）。接手方 B：会话名 %s，sid %s。" % (value["id"], args.reason, name, b_uuid))
    print("接下来（A）：")
    print("  1) 等 daemon 开出 B 的 Warp 窗口并上线（handoffd status 里「B 已上线」）；")
    print("  2) 写 kit（须含一节「## 给新 session 的开场指令」），运行 handoffd seal --kit <kit 路径> 封存；")
    print("  3) 等在途任务跑完，结果用 SendMessage 发给 B（会话名 %s）；" % name)
    print("  4) B 复核后会把结论 SendMessage 回你；有异议你们来回讨论，超过上限才会叫 Owner；")
    print("  5) 没有异议就运行 handoffd sign out，然后结束这一轮（不要再开新任务）；B 也签字后 daemon 核对并换棒，随后关闭本窗口。")
    return 0


def cmd_seal(cfg, sdir, caller, now, args=None, **_):
    code = auto_and_in_session(sdir, caller, "seal")
    if code is not None:
        return code
    try:
        with open(args.kit, "rb") as stream:
            raw = stream.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print("拒绝：读不了 kit：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    opening = baton.extract_opening(text)
    if opening is None:
        print("拒绝：kit 里缺「%s」一节（标题行含这几个字，节内要有内容）" % baton.OPENING_HEADING, file=sys.stderr)
        return EXIT_USAGE
    sha = hashlib.sha256(raw).hexdigest()
    result, value = baton.seal(sdir, caller.sid, caller.identity, caller.chain_fn, now, sha, opening, text)
    if result == "no_handoff":
        return refuse("没有进行中的交接（先 handoffd request）")
    if result == "not_holder":
        return refuse("只有持棒的主控能封存 kit（你的角色：%s）" % value)
    if result in ("signed", "phase"):
        return refuse("kit 已不能再改（%s）" % {"signed": "A 已签字，签字后冻结", "phase": "交接已换棒"}[result])
    if result != "ok":
        return refuse("封存没有完成（%s）" % result)
    sessions.append_event(sdir, {"t_ms": now, "sid": caller.sid, "role": "holder", "event": "seal", "sha": sha,
                                 "resealed": value["resealed"], "stale_open": value["stale_open"]})
    if value.get("b_signed_cleared"):
        print("注意：B 已经签过字，但它复核的是旧 kit——B 的签字已作废，请通知 B 重新读 kit 并重新 sign in（handoffd sign in）。")
    if value["stale_open"]:
        print("已重新封存 kit（sha256 %s…）。开场指令变了，B 已收到旧版（daemon 不会重发）：请用 SendMessage 告诉 B 重读 kit。" % sha[:12])
    elif value["resealed"]:
        print("已重新封存 kit（sha256 %s…），开场指令 %d 个字符；A 签字之前都可以再改。" % (sha[:12], len(opening)))
    else:
        print("已封存 kit（sha256 %s…），开场指令 %d 个字符；B 上线后 daemon 会把它原样投给 B。" % (sha[:12], len(opening)))
    return 0


def cmd_sign(cfg, sdir, caller, now, args=None, **_):
    code = auto_and_in_session(sdir, caller, "sign")
    if code is not None:
        return code
    result, value = baton.sign(sdir, args.which, caller.sid, caller.identity, caller.chain_fn, now)
    reasons = {"no_handoff": "没有进行中的交接", "phase": "交接已经换棒", "no_kit": "kit 还没封存（A 先 handoffd seal）",
               "stop_unreadable": "读不出本进程的 Stop 记录（文件读取出错），不能确定签字时的基线；登记册没有改动，请稍后重新 handoffd sign out",
               "b_not_online": "B 还没上线登记", "already": "你已经签过字了"}
    if result == "not_allowed":
        return refuse("sign %s 只能由%s签（你的角色：%s）" % (args.which, "持棒的主控 A" if args.which == "out" else "接手方 B", value))
    if result in reasons:
        return refuse(reasons[result])
    if result != "ok":
        return refuse("签字没有完成（%s）" % result)
    if args.which == "out":
        print("A 已签字。接下来：结束这一轮（不要再开新任务）；B 也签字、且你这一轮的 Stop 事件显示没有在途任务后，daemon 会换棒并关闭本窗口。")
    else:
        print("B 已签字。接下来：等 daemon 核对并换棒——换棒后会有一条「[handoffd] BATON」消息投给你，收到前不要自行行动；"
              "5 分钟还没收到就运行 handoffd status，看「主控」是不是你的会话。")
    return 0


def cmd_adopt(cfg, sdir, caller, now, **_):
    mode, _since = sessions.read_mode(sdir) if os.path.isdir(sdir) else ("manual", 0)
    if mode != "auto":
        return refuse("manual 模式没有自动交接")
    result, value = baton.adopt(sdir, now)
    reasons = {"no_handoff": "没有进行中的交接", "phase": "交接已经换棒", "already": "A 已有签字（不需要 adopt）",
               "no_holder": "登记册里没有主控", "holder_not_dead": "主控 A 还活着或判活不确定，不能代签",
               "not_sealed_online": "只在「A 已封存 kit 且 B 已上线」时才能 adopt"}
    if result in reasons:
        return refuse(reasons[result])
    if result != "ok":
        return refuse("adopt 没有完成（%s）" % result)
    print("已代 A 补记签字（by=owner）。B 复核 kit 后需要自己运行 handoffd sign in，随后 daemon 换棒。")
    return 0


def parse_reply_eta(kind, text):
    """reply 的预计时长：只对 task；格式 <整数>m，15–180 分钟（含端点）。返回 (分钟|None, 错误文案|None)。"""
    if text is None:
        return None, None
    if kind != "task":
        return None, "预计时长只能跟在 reply task 后面（你答的是 %s）" % kind
    found = REPLY_ETA_FORMAT.fullmatch(text)
    if not found:
        return None, "预计时长格式应为 <整数>m，例如 60m（收到：%s）" % text
    minutes = int(found.group(1))
    if not REPLY_ETA_MIN <= minutes <= REPLY_ETA_MAX:
        return None, "预计时长应在 %dm–%dm 之间（收到：%s）" % (REPLY_ETA_MIN, REPLY_ETA_MAX, text)
    return minutes, None


def cmd_reply(cfg, sdir, caller, now, args=None, **_):
    eta, problem = parse_reply_eta(args.kind, args.eta)
    if problem:
        return refuse(problem)      # 先于一切读写：不合法就不写任何状态
    code = auto_and_in_session(sdir, caller, "reply")
    if code is not None:
        return code
    status, state = baton.read(sdir)
    if status == "error":
        return refuse("登记册 baton.json 读不出，不能确认你的角色")
    role, _info = baton.classify(state, caller.sid, caller.identity, caller.chain_fn)
    if role not in ("holder", "holder_sub", "b"):
        return refuse("只有主控或接手方能答询问（你的角色：%s）" % role)
    last_reply = {"kind": args.kind, "ms": now}
    if eta is not None:
        last_reply["eta_minutes"] = eta
    sessions.update_session(sdir, caller.sid, lambda record: record.update(last_reply=last_reply, last_event_ms=now))
    sessions.append_event(sdir, {"t_ms": now, "sid": caller.sid, "role": role, "event": "reply", "summary": args.kind + ("" if eta is None else " %dm" % eta)})
    print("已登记回复：%s%s。daemon 会核对（核得上就继续等%s；核不上会再问你一次）" % (args.kind, "" if eta is None else " " + args.eta, "" if eta is None else "，其间不再问"))
    return 0


def cmd_daemon(cfg, sdir, caller, now, args=None, **_):
    return daemon_module.main(cfg, sdir, once=args.once)


COMMANDS = {"on": cmd_on, "off": cmd_off, "status": cmd_status, "review-round": cmd_review_round, "notify": cmd_notify,
            "request": cmd_request, "seal": cmd_seal, "sign": cmd_sign, "adopt": cmd_adopt, "reply": cmd_reply, "daemon": cmd_daemon}


def main(argv=None, env=None, chain_fn=None, notifier=None, kill_fn=os.kill, now_fn=None, cwd_fn=None, lstart_fn=None):
    """命令行入口；env / chain_fn / notifier / kill_fn / now_fn 只供测试替换。"""
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "ring":
        return cmd_ring(args, notifier)
    try:
        cfg = find_project_config(args, env)
    except config.ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    if args.command == "test-alert":
        return cmd_test_alert(cfg, args, notifier)
    sdir, reason = resolve_sdir(cfg)
    if sdir is None:
        print("无法确定状态目录：%s" % reason, file=sys.stderr)
        return 1
    caller = Caller(env, chain_fn or lockcore.claude_chain)
    now = (now_fn or util.now_ms)()
    return COMMANDS[args.command](cfg, sdir, caller, now, args=args, notifier=notifier, kill_fn=kill_fn, cwd_fn=cwd_fn, lstart_fn=lstart_fn)


if __name__ == "__main__":
    sys.exit(main())
