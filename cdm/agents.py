# 调用固定绝对路径的 `claude agents --json`，返回本项目的交互会话列表（不调用模型）。
import json
import os
import subprocess


def filter_entries(entries, project_dir):
    """只保留 kind=interactive 且 realpath(cwd) 等于项目目录的条目。"""
    wanted = os.path.realpath(project_dir)
    kept = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "interactive":
            continue
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and os.path.realpath(cwd) == wanted:
            kept.append(entry)
    return kept


def list_agents(claude_bin, project_dir, runner=subprocess.run):
    """失败（超时、非零退出、输出不是 JSON 数组）返回 None，调用方本轮跳过依赖它的动作。"""
    try:
        result = runner(
            [claude_bin, "agents", "--json", "--cwd", project_dir],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    return filter_entries(entries, project_dir)
