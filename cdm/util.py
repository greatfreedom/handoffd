# 公共小工具：原子写文件、JSONL 日志、本地 ISO 时间、会话号安全检查。
import json
import os
import re
import tempfile
import time
from datetime import datetime

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def now_ms():
    """epoch 毫秒（UTC）。handoffd 的所有新增计时只用它：与时区无关，换时区超时不变（G1，设计:7）。"""
    return int(time.time() * 1000)


def iso(moment=None):
    """返回本地时间的 ISO 8601 字符串（带秒）。"""
    return (moment or datetime.now()).strftime(ISO_FORMAT)


def parse_iso(text):
    """解析 iso() 产生的字符串，失败抛 ValueError。"""
    return datetime.strptime(text, ISO_FORMAT)


def safe_id(value):
    """会话号只允许安全字符，避免拼进文件名时穿越目录；不安全返回 None。"""
    if isinstance(value, str) and SAFE_ID.match(value):
        return value
    return None


def atomic_write_text(path, text):
    """同目录临时文件 + os.replace 原子发布。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path, value):
    """原子写 JSON 文件。"""
    atomic_write_text(path, json.dumps(value, ensure_ascii=False))


def read_json(path):
    """读 JSON 文件，返回 (状态, 值)：missing / ok / error。坏 JSON 是 error，不当作不存在。"""
    try:
        with open(path, encoding="utf-8") as stream:
            return "ok", json.load(stream)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, TypeError, RecursionError):
        return "error", None


def append_jsonl(path, record):
    """尽力追加一行 JSON 日志；失败只返回 False，不影响任何决策。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
