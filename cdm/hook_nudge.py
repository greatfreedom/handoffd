# PostToolUse 提醒钩子：由 poc/q2_ctx_nudge_hook.py 派生；每会话 guard 内先输出、后原子提交最高已输出档位。
import fcntl
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdm import config, ctx, util  # noqa: E402

MSG = {
    0: "已到第一档：请在当前工作单元的下一个自然节点（一个子任务收口、一次复核整合完成之后）发起交接，不要在半截改动中间交接；现在起不再允许开新的长任务（Agent / Workflow / Monitor / 定时任务 / 后台 Bash）。",
    1: "已到第二档：钩子已替你登记一条「待发起」的交接申请。此后只准收尾——不要再开新的工作单元，只做当前子任务收口与交接所需的工作。",
}


def stable_token(sid, level):
    """令牌按会话号 + 档位固定，重试时不变。"""
    digest = hashlib.sha256((str(sid) + ":" + str(level)).encode("utf-8")).hexdigest()[:6].upper()
    return "NUDGE-" + digest


def read_emitted(path):
    """返回最高已输出档位；文件不存在 None；内容坏 "invalid"。"""
    status, value = util.read_json(path)
    if status == "missing":
        return None
    level = value.get("highest_level") if status == "ok" and isinstance(value, dict) else None
    return level if isinstance(level, int) and not isinstance(level, bool) and level >= 0 else "invalid"


def commit_emitted(path, level, token, pct):
    """原子记录最高已输出档位。"""
    util.atomic_write_json(path, {"highest_level": level, "token": token, "pct": pct, "t": util.iso()})


def process(inp, output, state_dir, thresholds, on_fire=None):
    """处理一次钩子调用；返回本次发出的令牌，没发返回 None。on_fire(档位序号, 百分比, 令牌) 在提醒已输出并提交之后调用一次。"""
    sid = inp.get("session_id", "unknown")
    event = inp.get("hook_event_name", "PostToolUse")
    levels = sorted(float(x) for x in thresholds)
    record = {
        "t": util.iso(), "session_id": sid, "event": event,
        "tool": inp.get("tool_name"), "agent_id": inp.get("agent_id"),
    }
    log_path = os.path.join(state_dir, "nudge_log.jsonl")
    if inp.get("agent_id"):
        record["skipped"] = "subagent"
        util.append_jsonl(log_path, record)
        return None
    safe_sid = util.safe_id(sid)
    if safe_sid is None:
        record["skipped"] = "unsafe_session_id"
        util.append_jsonl(log_path, record)
        return None
    pct, source, detail = ctx.context_percent(state_dir, sid, inp.get("transcript_path"))
    record.update(pct=pct, source=source, detail=detail, thresholds=levels, fired=None, token=None)
    if pct is None:
        util.append_jsonl(log_path, record)
        return None
    crossed = [index for index, level in enumerate(levels) if pct >= level]
    if not crossed:
        util.append_jsonl(log_path, record)
        return None
    level = crossed[-1]
    directory = os.path.join(state_dir, "nudge")
    os.makedirs(directory, exist_ok=True)
    state = os.path.join(directory, safe_sid + ".emitted.json")
    fd = os.open(os.path.join(directory, safe_sid + ".guard"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            record["skipped"] = "guard_busy"
            return None
        highest = read_emitted(state)
        if highest == "invalid":
            record["skipped"] = "invalid_emitted_state"
            return None
        if highest is not None and highest >= level:
            record["skipped"] = "already_emitted"
            return None
        token = stable_token(sid, level)
        message = (
            "[调度器提醒 %s] 本会话上下文已用 %.1f%%（第 %d 档阈值 %g%%，来源 %s）。"
            % (token, pct, level + 1, levels[level], source)
            + MSG[min(level, 1)] + "本档提醒正常完成时只发一次。"
        )
        payload = json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}, ensure_ascii=False)
        output.write(payload + "\n")
        output.flush()
        commit_emitted(state, level, token, pct)
        record.update(fired=level + 1, token=token)
        if on_fire is not None:
            on_fire(level, pct, token)
        return token
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            util.append_jsonl(log_path, record)


def main():
    """钩子入口：项目目录取 CLAUDE_PROJECT_DIR，否则取输入里的 cwd。"""
    inp = json.load(sys.stdin)
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or inp.get("cwd") or os.getcwd()
    cfg, config_error = config.load_for_hook(proj)
    if config_error:
        util.append_jsonl(os.path.join(cfg["state_dir"], "nudge_log.jsonl"),
                          {"t": util.iso(), "event": "config_error", "error": config_error})
    return process(inp, sys.stdout, cfg["state_dir"], cfg["nudge_thresholds"])


if __name__ == "__main__":
    main()
