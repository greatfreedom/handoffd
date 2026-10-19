# 读取并严格校验项目配置 cdm.json（闭域：未知键即报错）。
import json
import math
import os
import re

MVP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")
EXTRA_ARG = re.compile(r"^[A-Za-z0-9._=:/@+-]+(\[[0-9a-z]+\])?$")   # S4a：允许 claude-opus-5-5[1m] 这种带上下文窗口后缀的值（命令文件里 printf %q 转义）
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TOP_KEYS = (
    "project_dir", "state_dir", "session_name_prefix", "claude_bin",
    "min_claude_version", "required_env", "claude_extra_args",
    "nudge_thresholds", "poll_seconds", "launch_confirm_timeout_seconds",
    "preflight_retry_seconds", "alerts",
    "idle_ask_minutes", "ask_reply_minutes", "heartbeat_stale_seconds", "qa_limit",
    "api_backoff_minutes", "b_verify_commands", "warp_open",
)
WARP_OPEN_MODES = ("launch_config", "tab_config")
# 响铃两类（waiting 等输入、problem 出问题）+ 无声一类（quiet：只进记录 + 无声横幅，规格:50、:64；三类划分为设计:15 自定）
ALERT_KINDS = ("waiting", "problem", "quiet")
ALERT_KEYS = ("sound", "sounds", "voice_clips_dir", "voice", "text", "repeat_minutes", "max_times")
PATH_KEYS = ("sound", "voice_clips_dir")

DEFAULT_ALERTS = {
    # 响铃两类：默认用 macOS 自带的两个系统声音轮换，只响铃声 + 横幅，不带人声（发布版不分发任何音频文件）。
    # 想要自己的铃声 / 人声：在项目 handoffd.json 的 alerts 里写 sounds / voice_clips_dir（相对路径按安装根目录解析），见 assets/voice/README.md。
    # sound 与 sounds 的优先级（G9）：sounds 非空就用 sounds（轮换），sound 被忽略；只有 sounds 为空 / 没有时才用 sound。
    # 所以默认配置里自己只写 "sound" 不生效——要同时写 "sounds": []。人声同理：voice_clips_dir 有素材时优先于 voice / text 的 say。
    "waiting": {"sounds": ["/System/Library/Sounds/Glass.aiff", "/System/Library/Sounds/Ping.aiff"], "voice_clips_dir": None},
    "problem": {"sounds": ["/System/Library/Sounds/Glass.aiff", "/System/Library/Sounds/Ping.aiff"], "voice_clips_dir": None},
    "quiet": {"sound": None},
}
DEFAULTS = {
    "state_dir": None,
    "session_name_prefix": None,
    "claude_bin": "~/.local/bin/claude",
    "min_claude_version": "2.1.284",
    "required_env": [],
    "claude_extra_args": [],
    "nudge_thresholds": [65, 76],
    "poll_seconds": 5,
    "launch_confirm_timeout_seconds": 120,
    "preflight_retry_seconds": 60,
    # handoffd 新增键（设计:15）：值出自规格:41（30/5 分钟）、:72（120 秒）、:40（3→60 分钟退避）；qa_limit 12 为设计 §7-7 提议
    "idle_ask_minutes": 30,
    "ask_reply_minutes": 5,
    "heartbeat_stale_seconds": 120,
    "qa_limit": 12,
    "api_backoff_minutes": [3, 60],
    "b_verify_commands": [],
    # 开 B 的窗口用哪种 Warp 配置：launch_config = Launch Configuration（1 个窗口 1 个标签，不带 Warp 自带的空白标签页）；tab_config = 标签页配置（回退）
    "warp_open": "launch_config",
}


class ConfigError(Exception):
    """配置文件不合法。"""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _resolve_path(value):
    """相对路径按 mvp/ 根目录解析；~ 展开。"""
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(MVP_ROOT, value)


def _check_string_list(key, value, pattern):
    if not isinstance(value, list):
        raise ConfigError("%s 必须是列表" % key)
    for item in value:
        if not isinstance(item, str) or not pattern.match(item):
            raise ConfigError("%s 含有不合法的项：%r" % (key, item))


def _check_alerts(alerts):
    """校验并合并 alerts：每类在默认值上逐键覆盖。"""
    if not isinstance(alerts, dict):
        raise ConfigError("alerts 必须是对象")
    merged = {kind: dict(values) for kind, values in DEFAULT_ALERTS.items()}
    for kind, values in alerts.items():
        if kind not in ALERT_KINDS:
            raise ConfigError("alerts 里有未知类别：%s" % kind)
        if not isinstance(values, dict):
            raise ConfigError("alerts.%s 必须是对象" % kind)
        for key, value in values.items():
            if key not in ALERT_KEYS:
                raise ConfigError("alerts.%s 里有未知键：%s" % (kind, key))
            if key in ("sound", "voice", "text", "voice_clips_dir"):
                if value is not None and not isinstance(value, str):
                    raise ConfigError("alerts.%s.%s 必须是字符串或 null" % (kind, key))
            elif key == "sounds":
                if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                    raise ConfigError("alerts.%s.sounds 必须是非空字符串列表" % kind)
            elif key == "repeat_minutes":
                if not _is_number(value) or value <= 0:
                    raise ConfigError("alerts.%s.repeat_minutes 必须是正数" % kind)
            elif key == "max_times":
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ConfigError("alerts.%s.max_times 必须是正整数" % kind)
            merged[kind][key] = value
    for values in merged.values():
        for key in PATH_KEYS:
            if values.get(key):
                values[key] = _resolve_path(values[key])
        if values.get("sounds"):
            values["sounds"] = [_resolve_path(item) for item in values["sounds"]]
    return merged


def build_config(raw, default_project_dir=None):
    """校验原始配置字典并补全默认值，返回解析后的配置字典。"""
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")
    for key in raw:
        if key not in TOP_KEYS:
            raise ConfigError("配置里有未知键：%s" % key)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    project_dir = cfg.get("project_dir", default_project_dir)
    if not isinstance(project_dir, str) or not os.path.isabs(project_dir):
        raise ConfigError("project_dir 必填，且必须是绝对路径")
    cfg["project_dir"] = project_dir
    state_dir = cfg["state_dir"]
    if state_dir is None:
        state_dir = os.path.join(project_dir, ".claude", "cdm")
    if not isinstance(state_dir, str) or not os.path.isabs(os.path.expanduser(state_dir)):
        raise ConfigError("state_dir 必须是绝对路径或 null")
    cfg["state_dir"] = os.path.expanduser(state_dir)
    # 显式写了 state_dir 才算"指定"；否则 handoffd 的钩子与命令按 lockcore.state_dir 取 <S>（设计:9）
    cfg["state_dir_set"] = raw.get("state_dir") is not None
    prefix = cfg["session_name_prefix"]
    if prefix is None:
        prefix = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(project_dir.rstrip("/"))) or "cdm"
    if not isinstance(prefix, str) or not NAME_CHARS.match(prefix):
        raise ConfigError("session_name_prefix 只能含字母数字和 ._-")
    cfg["session_name_prefix"] = prefix
    if not isinstance(cfg["claude_bin"], str) or not cfg["claude_bin"]:
        raise ConfigError("claude_bin 必须是非空字符串")
    cfg["claude_bin"] = os.path.expanduser(cfg["claude_bin"])
    if not os.path.isabs(cfg["claude_bin"]):
        raise ConfigError("claude_bin 必须是绝对路径")
    if not isinstance(cfg["min_claude_version"], str) or not VERSION.match(cfg["min_claude_version"]):
        raise ConfigError("min_claude_version 必须形如 2.1.284")
    _check_string_list("required_env", cfg["required_env"], ENV_NAME)
    _check_string_list("claude_extra_args", cfg["claude_extra_args"], EXTRA_ARG)
    thresholds = cfg["nudge_thresholds"]
    if not isinstance(thresholds, list) or not thresholds:
        raise ConfigError("nudge_thresholds 必须是非空列表")
    for item in thresholds:
        if not _is_number(item) or not 0 < item <= 100:
            raise ConfigError("nudge_thresholds 的每一项必须在 (0, 100]")
    cfg["nudge_thresholds"] = sorted(float(item) for item in thresholds)
    for key in ("poll_seconds", "launch_confirm_timeout_seconds", "idle_ask_minutes", "ask_reply_minutes",
                "heartbeat_stale_seconds"):
        if not _is_number(cfg[key]) or cfg[key] <= 0:
            raise ConfigError("%s 必须是正数" % key)
    if not isinstance(cfg["qa_limit"], int) or isinstance(cfg["qa_limit"], bool) or cfg["qa_limit"] < 1:
        raise ConfigError("qa_limit 必须是正整数")
    backoff = cfg["api_backoff_minutes"]
    if (not isinstance(backoff, list) or len(backoff) != 2 or not all(_is_number(item) and item > 0 for item in backoff)
            or backoff[0] > backoff[1]):
        raise ConfigError("api_backoff_minutes 必须是 [起点, 封顶] 两个正数且起点不大于封顶")
    if cfg["warp_open"] not in WARP_OPEN_MODES:
        raise ConfigError("warp_open 必须是 %s 之一" % " / ".join(WARP_OPEN_MODES))
    commands = cfg["b_verify_commands"]
    if not isinstance(commands, list) or not all(isinstance(item, str) and item for item in commands):
        raise ConfigError("b_verify_commands 必须是非空字符串列表")
    if not _is_number(cfg["preflight_retry_seconds"]) or cfg["preflight_retry_seconds"] < 0:
        raise ConfigError("preflight_retry_seconds 必须是非负数")
    cfg["alerts"] = _check_alerts(cfg.get("alerts", {}))
    return cfg


def load_config(path, default_project_dir=None):
    """读取并校验配置文件，不合法抛 ConfigError。"""
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except OSError as exc:
        raise ConfigError("无法读取配置文件 %s：%s" % (path, exc))
    except ValueError as exc:
        raise ConfigError("配置文件不是合法 JSON：%s" % exc)
    return build_config(raw, default_project_dir)


def default_config(project_dir):
    """配置文件缺失时的全默认配置。"""
    return build_config({}, project_dir)


def load_handoffd(project_dir):
    """handoffd 钩子与命令用：读 <project>/.claude/handoffd.json；缺失或不合法都退回默认，返回 (配置, 错误说明或 None)。"""
    path = os.path.join(project_dir, ".claude", "handoffd.json")
    if not os.path.exists(path):
        return default_config(project_dir), None
    try:
        return load_config(path, project_dir), None
    except ConfigError as exc:
        return default_config(project_dir), str(exc)


def load_for_hook(project_dir):
    """钩子用：读 <project>/.claude/cdm.json；缺失或不合法都退回默认，返回 (配置, 错误说明或 None)。"""
    path = os.path.join(project_dir, ".claude", "cdm.json")
    if not os.path.exists(path):
        return default_config(project_dir), None
    try:
        return load_config(path, project_dir), None
    except ConfigError as exc:
        return default_config(project_dir), str(exc)
