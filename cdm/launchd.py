# daemon 的 launchd 启停（设计 §5.1、:103；规格:14、:24）：plist 写在 <S>/launchd/，不进 ~/Library/LaunchAgents（不开机自启，RunAtLoad 只在 bootstrap 时生效）；
# `handoffd on` 拉起、`handoffd off` 停。launchctl 封装、print 判加载、失败处理沿用早期 MVP 的 cli.py，另修 G7 三处：
#   ① bootout 失败不漏任务（仍在 / 不确定 ⇒ 报错、plist 保持原样）；② print 查询失败按「不确定」报错，不当作「没加载」；
#   ③ 替换（plist 内容变了、旧任务已 bootout）失败后，恢复旧 plist 并重新加载旧任务。
import hashlib
import os
import plistlib
import re
import subprocess
import sys

from cdm import sessions, util

MVP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_PREFIX = "local.handoffd."
NOT_FOUND_CODE = 113   # launchctl print / bootout 对「没有这个任务」的退出码（macOS）；真机首验要核对（S3 待办）


def launchctl_bin():
    """launchctl 路径，可由 CDM_LAUNCHCTL_BIN 替换（测试用）。"""
    return os.environ.get("CDM_LAUNCHCTL_BIN", "/bin/launchctl")


def label_of(cfg):
    """local.handoffd.<项目名>.<项目绝对路径的 8 位短哈希>：同名的两个项目不会撞 launchd 标签（Owner 裁定，S3 待裁 1）。"""
    digest = hashlib.sha256(os.path.abspath(cfg["project_dir"]).encode("utf-8")).hexdigest()[:8]
    return "%s%s.%s" % (LABEL_PREFIX, cfg["session_name_prefix"], digest)


def launchd_dir(sdir):
    return os.path.join(sdir, "launchd")


def plist_path(sdir, cfg):
    return os.path.join(launchd_dir(sdir), label_of(cfg) + ".plist")


def _domain():
    return "gui/%d" % os.getuid()


def _target(cfg):
    return "%s/%s" % (_domain(), label_of(cfg))


def build_plist(cfg, sdir, config_path):
    """daemon 的 plist：KeepAlive SuccessfulExit=false（崩溃被拉起，正常退出不拉）；WorkingDirectory=项目目录，
    daemon 与命令一样从那里向上找 .claude/handoffd.json；只有 on 带了 --config 才把它写进参数。"""
    directory = launchd_dir(sdir)
    arguments = ["/usr/bin/python3", os.path.join(MVP_ROOT, "bin", "handoffd"), "daemon"]
    if config_path:
        arguments += ["--config", os.path.abspath(config_path)]
    return {
        "Label": label_of(cfg),
        "ProgramArguments": arguments,
        "WorkingDirectory": cfg["project_dir"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": os.path.join(directory, "daemon.out.log"),
        "StandardErrorPath": os.path.join(directory, "daemon.err.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"},
    }


def run_launchctl(*args):
    """运行 launchctl；起不来 / 超时返回 None（调用方按「不确定」处理）。"""
    try:
        return subprocess.run([launchctl_bin()] + list(args), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def _print(cfg):
    """launchctl print：返回 (状态, 标准输出)；状态是 "loaded" / "not_loaded" / "unknown"。只有明确的「找不到任务」才算没加载；其它失败（超时、起不来、别的退出码）都是不确定（G7）。"""
    result = run_launchctl("print", _target(cfg))
    if result is None:
        return "unknown", ""
    if result.returncode == 0:
        return "loaded", result.stdout or ""
    if result.returncode == NOT_FOUND_CODE or "Could not find service" in (result.stderr or "") + (result.stdout or ""):
        return "not_loaded", ""
    return "unknown", ""


def query(cfg):
    return _print(cfg)[0]


def _loaded_but_not_running(text):
    """print 输出里有 `state = …` 且不是 running ⇒ 任务加载着、进程没在跑（daemon 在 manual 下收完尾自己退出就是这样，KeepAlive 对 0 退出不拉）。没有 state 行不判。"""
    match = re.search(r"^\s*state = (.+?)\s*$", text, re.MULTILINE)
    return bool(match) and match.group(1) != "running"


def _read_bytes(path):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def _detail(result):
    return "未执行" if result is None else "退出码 %d：%s" % (result.returncode, (result.stderr or "").strip())


def _restore_plist(path, original):
    """把 plist 恢复成 original（None = 原来没有，删掉）。"""
    if original is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    else:
        with open(path, "wb") as stream:
            stream.write(original)


def start(cfg, sdir, config_path, now_ms=None):
    """`handoffd on`：拉起 daemon。返回退出码（0 = 已在 launchd 里）。已装且 plist 一致 ⇒ 不重复装（没在跑 ⇒ kickstart；在跑但心跳超过
    heartbeat_stale_seconds ⇒ kickstart -k 重启）；不一致 ⇒ 替换。"""
    path = plist_path(sdir, cfg)
    wanted = build_plist(cfg, sdir, config_path)
    state, text = _print(cfg)
    if state == "unknown":
        print("daemon：查询 launchd 失败，结果不确定（不知道它有没有在运行），没有改动任何东西；请稍后再敲一次 handoffd on", file=sys.stderr)
        return 1
    original = _read_bytes(path)
    try:
        existing = plistlib.loads(original) if original is not None else None
    except Exception:
        existing = None
    if state == "loaded" and existing == wanted:
        if _loaded_but_not_running(text):
            result = run_launchctl("kickstart", _target(cfg))
            if result is None or result.returncode != 0:
                print("daemon：任务已加载但没在运行，kickstart 拉起失败（%s）" % _detail(result), file=sys.stderr)
                return 1
            print("daemon：任务已加载但没在运行，已用 kickstart 拉起（%s）" % label_of(cfg))
            return 0
        heartbeat = sessions.read_heartbeat(sdir)
        now_ms = util.now_ms() if now_ms is None else now_ms
        if heartbeat is not None and (now_ms - heartbeat) / 1000.0 > float(cfg["heartbeat_stale_seconds"]):
            result = run_launchctl("kickstart", "-k", _target(cfg))
            if result is None or result.returncode != 0:
                print("daemon：在运行但心跳已停 %d 秒，kickstart -k 重启失败（%s）" % ((now_ms - heartbeat) // 1000, _detail(result)), file=sys.stderr)
                return 1
            print("daemon：在运行但心跳已停 %d 秒（卡住），已用 kickstart -k 重启（%s）" % ((now_ms - heartbeat) // 1000, label_of(cfg)))
            return 0
        print("daemon：已在 launchd 里（%s），不重复安装" % label_of(cfg))
        return 0
    if state == "loaded":
        result = run_launchctl("bootout", _target(cfg))
        if (result is None or result.returncode != 0) and query(cfg) != "not_loaded":
            print("daemon：旧任务要换新，但 bootout 失败（%s），旧任务仍在运行（或不确定）；plist 没有改动，没有换新" % _detail(result), file=sys.stderr)
            return 1
    os.makedirs(launchd_dir(sdir), exist_ok=True)
    with open(path, "wb") as stream:
        plistlib.dump(wanted, stream)
    result = run_launchctl("bootstrap", _domain(), path)
    if result is None or result.returncode != 0:
        _restore_plist(path, original)
        note = "已恢复原 plist"
        if state == "loaded" and original is not None:
            back = run_launchctl("bootstrap", _domain(), path)
            note += "，旧任务%s" % ("已重新加载" if back is not None and back.returncode == 0 else "没能重新加载（请自己 handoffd on 重试，或看 launchctl print）")
        print("daemon：launchctl bootstrap 失败（%s），%s" % (_detail(result), note), file=sys.stderr)
        return 1
    print("daemon：已拉起（%s，plist 在 %s）" % (label_of(cfg), path))
    return 0


def stop(cfg, sdir):
    """`handoffd off`（无未收尾意图时）：bootout 并删 plist。返回退出码。bootout 失败而任务仍在 / 不确定 ⇒ 报错、保留 plist（不漏任务）。"""
    state = query(cfg)
    if state == "unknown":
        print("daemon：查询 launchd 失败，结果不确定（不知道它有没有在运行），没有改动任何东西；请稍后再敲一次 handoffd off", file=sys.stderr)
        return 1
    note = ""
    if state == "loaded":
        result = run_launchctl("bootout", _target(cfg))
        if result is None or result.returncode != 0:
            if query(cfg) != "not_loaded":
                print("daemon：bootout 失败（%s），任务仍在运行（或不确定），已保留 plist" % _detail(result), file=sys.stderr)
                return 1
            note = "（bootout 报错，但任务已不在）"
    else:
        note = "（本来就不在 launchd 里）"
    try:
        os.unlink(plist_path(sdir, cfg))
    except FileNotFoundError:
        pass
    print("daemon：已停止%s" % note)
    return 0


def describe(cfg):
    """status 用：一句话说 daemon 在不在 launchd 里。"""
    return {"loaded": "已在 launchd 里（%s）" % label_of(cfg), "not_loaded": "不在 launchd 里",
            "unknown": "查询 launchd 失败，不确定在不在"}[query(cfg)]
