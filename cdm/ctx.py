# 上下文百分比：由 poc/ctxlib.py 派生（状态目录参数化），并提供状态栏快照落盘（tap）入口。
# 官方公式（statusline 文档）：used = input_tokens + cache_creation_input_tokens + cache_read_input_tokens（不含 output）
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdm import config, lockcore, procs, sessions, util  # noqa: E402


def last_main_usage(transcript_path):
    """返回 (usage, timestamp, model)：最后一条非 sidechain 的 assistant 消息的 usage。"""
    last = None
    with open(transcript_path, encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            message = item.get("message") or {}
            if item.get("type") == "assistant" and not item.get("isSidechain") and message.get("usage"):
                last = item
    if not last:
        return None, None, None
    return last["message"]["usage"], last.get("timestamp"), last["message"].get("model")


def used_tokens(usage):
    """按官方公式合计已用 token。"""
    return (
        (usage.get("input_tokens", 0) or 0)
        + (usage.get("cache_creation_input_tokens", 0) or 0)
        + (usage.get("cache_read_input_tokens", 0) or 0)
    )


def snapshot_path(state_dir, session_id):
    """快照文件路径；会话号不安全时返回 None。"""
    sid = util.safe_id(session_id)
    if sid is None:
        return None
    return os.path.join(state_dir, "ctx", sid + ".json")


def valid_snapshot(value):
    """严格校验快照结构：context_window 须是对象；used_percentage 须是有限数或 null；窗口大小须是正整数。"""
    if not isinstance(value, dict):
        return False
    window = value.get("context_window")
    if window is None:
        return True
    if not isinstance(window, dict):
        return False
    pct = window.get("used_percentage")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct)):
        return False
    size = window.get("context_window_size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        return False
    return True


def statusline_snapshot(state_dir, session_id):
    """读状态栏快照；没有快照、内容异常或结构不合格都当"无快照"，不让钩子报错。"""
    path = snapshot_path(state_dir, session_id)
    if path is None:
        return None
    status, value = util.read_json(path)
    return value if status == "ok" and valid_snapshot(value) else None


def context_percent(state_dir, session_id, transcript_path, window=None):
    """优先用状态栏快照里的原生 used_percentage；否则用 transcript 计算。返回 (pct, source, detail)。"""
    snap = statusline_snapshot(state_dir, session_id)
    usage, stamp, model = (None, None, None)
    if transcript_path and os.path.exists(transcript_path):
        try:
            usage, stamp, model = last_main_usage(transcript_path)
        except OSError:
            usage, stamp, model = (None, None, None)
    if window is None:
        # 窗口大小只信显式配置或状态栏快照；会话记录里的模型名不带 [1m]，猜 200k 会把 1M 会话高估 5 倍
        try:
            window = int(os.environ.get("CTX_WINDOW", "0"))
        except ValueError:
            window = 0
        window = window or ((snap or {}).get("context_window") or {}).get("context_window_size")
    if not window:
        return None, "unknown-window", {"last_ts": stamp, "model": model}
    calc = round(used_tokens(usage) * 100.0 / window, 2) if usage else None
    native = ((snap or {}).get("context_window") or {}).get("used_percentage")
    if native is not None:
        return float(native), "statusline", {"transcript_calc": calc, "window": window, "last_ts": stamp, "model": model}
    return calc, "transcript", {"window": window, "last_ts": stamp, "model": model}


def tap(project_dir, raw_text):
    """状态栏窃听：auto 才把状态栏 JSON 按会话号原子落盘到 <S>/ctx/；manual 只转交 HUD、不落盘（规格:76、:85）。
    读不出 <S> 或 mode 一律按 manual 处理（不写）。返回 False 仅表示输入不是 JSON 对象。"""
    try:
        value = json.loads(raw_text)
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    cfg, _error = config.load_handoffd(project_dir)
    if cfg["state_dir_set"]:
        sdir = cfg["state_dir"]
    else:
        status, sdir = lockcore.state_dir(project_dir, procs.Deadline(1.5))
        if status != "ok":
            return True
    if sessions.read_mode(sdir)[0] != "auto":
        return True
    sid = util.safe_id(value.get("session_id")) or "unknown"
    util.atomic_write_text(os.path.join(sdir, "ctx", sid + ".json"), raw_text)
    return True


def main(argv):
    """命令行：ctx.py tap <project_dir>，从标准输入读状态栏 JSON。"""
    if len(argv) == 2 and argv[0] == "tap":
        return 0 if tap(argv[1], sys.stdin.read()) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
