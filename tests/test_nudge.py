# 测试 N1（移植 poc/q2_unit_check.sh 与 poc/q2_concurrency_check.sh 的全部断言）和 N2（状态栏读数落盘）。
import io
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import MVP_ROOT, patched_env, require, run_cases, temp_dir  # noqa: E402
from cdm import hook_nudge  # noqa: E402

THRESHOLDS = [60, 75]
HOOK = os.path.join(MVP_ROOT, "cdm", "hook_nudge.py")
TAP = os.path.join(MVP_ROOT, "bin", "statusline_tap.sh")


class BrokenOutput:
    """写入即失败的输出，模拟"输出前被杀"。"""

    def write(self, value):
        raise OSError("simulated output-before kill")

    def flush(self):
        pass


def write_snapshot(state_dir, sid, size, pct):
    """写一份真实格式的状态栏快照。"""
    directory = os.path.join(state_dir, "ctx")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, sid + ".json"), "w", encoding="utf-8") as stream:
        json.dump({"session_id": sid, "context_window": {"context_window_size": size, "used_percentage": pct}}, stream)


def fire(inp, state_dir):
    """进程内跑一次钩子，返回 (令牌, 输出文本)。"""
    out = io.StringIO()
    token = hook_nudge.process(inp, out, state_dir, THRESHOLDS)
    return token, out.getvalue()


def unit_cases():
    """对应 q2_unit_check.sh。"""
    with temp_dir() as work, patched_env(CTX_WINDOW=None):
        state_dir = os.path.join(work, "cdm")
        write_snapshot(state_dir, "fault-before", 100, 65)
        payload = {"session_id": "fault-before", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        try:
            hook_nudge.process(payload, BrokenOutput(), state_dir, THRESHOLDS)
        except OSError:
            pass
        emitted_file = os.path.join(state_dir, "nudge", "fault-before.emitted.json")
        require(not os.path.exists(emitted_file), "输出失败后仍提交了状态")
        token, text = fire(payload, state_dir)
        require(token and "NUDGE-" in text, "输出失败后没有重试")
        token, text = fire(payload, state_dir)
        require(not text, "已提交的提醒又重复了")

        original_commit = hook_nudge.commit_emitted

        def killed_commit(*args):
            raise SystemExit("simulated output-after kill")

        write_snapshot(state_dir, "fault-after", 100, 65)
        duplicate = {"session_id": "fault-after", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        emitted = io.StringIO()
        hook_nudge.commit_emitted = killed_commit
        try:
            hook_nudge.process(duplicate, emitted, state_dir, THRESHOLDS)
        except SystemExit:
            pass
        finally:
            hook_nudge.commit_emitted = original_commit
        retry_token, retried = fire(duplicate, state_dir)
        require(retry_token and retry_token in emitted.getvalue() and retry_token in retried,
                "输出后被杀，重试没有用同一个令牌")

        write_snapshot(state_dir, "high", 100, 80)
        token, text = fire({"session_id": "high", "hook_event_name": "PostToolUse"}, state_dir)
        require(token and text, "高档没有发出")
        write_snapshot(state_dir, "high", 100, 65)
        token, text = fire({"session_id": "high", "hook_event_name": "PostToolUse"}, state_dir)
        require(not text, "高档之后又补发了低档")

        write_snapshot(state_dir, "sub", 100, 80)
        token, text = fire({"session_id": "sub", "agent_id": "child"}, state_dir)
        require(not text, "子代理没有被跳过")

        directory = os.path.join(state_dir, "ctx")
        with open(os.path.join(directory, "unknown.json"), "w", encoding="utf-8") as stream:
            json.dump({"context_window": {"used_percentage": 90}}, stream)
        token, text = fire({"session_id": "unknown"}, state_dir)
        require(not text, "窗口未知时仍发了提醒")

        with open(os.path.join(directory, "broken.json"), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        token, text = fire({"session_id": "broken"}, state_dir)
        require(not text, "坏快照没有当作无快照")
    return "输出前失败重试 / 输出后被杀令牌稳定 / 已提交去重 / 高档压低档 / 子代理跳过 / 窗口未知不提醒 / 坏快照当无快照"


def run_hook_process(project, payload, env):
    """以子进程方式运行钩子入口。"""
    return subprocess.Popen(
        [sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=project,
    ), json.dumps(payload)


def concurrency_cases():
    """对应 q2_concurrency_check.sh：八并发只发一次，高档先完成后低档迟到不发。"""
    with temp_dir() as work:
        project = os.path.join(work, "project")
        os.makedirs(os.path.join(project, ".claude"))
        with open(os.path.join(project, ".claude", "cdm.json"), "w", encoding="utf-8") as stream:
            json.dump({"project_dir": project, "nudge_thresholds": [60, 75]}, stream)
        state_dir = os.path.join(project, ".claude", "cdm")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project, PYTHONDONTWRITEBYTECODE="1")
        env.pop("CTX_WINDOW", None)
        write_snapshot(state_dir, "eight", 100, 65)
        payload = {"session_id": "eight", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        jobs = [run_hook_process(project, payload, env) for _ in range(8)]
        results = [(job.communicate(text, timeout=10), job.returncode) for job, text in jobs]
        require(all(code == 0 for _pair, code in results), "并发钩子有失败退出")
        require(sum("NUDGE-" in out for (out, _err), _code in results) == 1, "八个并发钩子没有恰好发出一次")
        require(all(not err for (_out, err), _code in results), "并发钩子写了标准错误")

        def one(text_payload):
            job, text = run_hook_process(project, text_payload, env)
            out, err = job.communicate(text, timeout=10)
            return job.returncode, out, err

        write_snapshot(state_dir, "high", 100, 80)
        code, out, _err = one({"session_id": "high", "hook_event_name": "PostToolUse"})
        require(code == 0 and "NUDGE-" in out, "高档没有发出")
        write_snapshot(state_dir, "high", 100, 65)
        code, out, _err = one({"session_id": "high", "hook_event_name": "PostToolUse"})
        require(code == 0 and not out, "迟到的低档发出了提醒")
    return "八并发只发一次；高档先完成后，迟到的低档不发"


def case_n1():
    """N1：提醒钩子单测 + 并发。"""
    return [unit_cases(), concurrency_cases()]


def case_n2():
    """N2：状态栏读数在 auto 下按会话号落盘（manual 不落盘，见 H1），落盘失败与找不到 claude-hud 都不报错。"""
    with temp_dir() as work:
        project = os.path.join(work, "project")
        home = os.path.join(work, "home")
        os.makedirs(project)
        os.makedirs(home)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project, HOME=home, PYTHONDONTWRITEBYTECODE="1")
        snapshot = json.dumps({"session_id": "tap-1", "context_window": {"used_percentage": 42, "context_window_size": 1000}})
        result = subprocess.run(["bash", TAP], input=snapshot, capture_output=True, text=True, env=env, timeout=20)
        require(result.returncode == 0, "读数脚本退出码 %r：%s" % (result.returncode, result.stderr))
        sdir = os.path.join(project, ".claude", "handoffd")
        path = os.path.join(sdir, "ctx", "tap-1.json")
        require(not os.path.exists(os.path.join(sdir)), "manual（没有 mode 文件）下不应落盘，也不应建状态目录")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "mode"), "w", encoding="utf-8") as stream:
            json.dump({"mode": "auto", "since_ms": 1}, stream)
        result = subprocess.run(["bash", TAP], input=snapshot, capture_output=True, text=True, env=env, timeout=20)
        require(result.returncode == 0, "读数脚本退出码 %r：%s" % (result.returncode, result.stderr))
        require(os.path.exists(path), "auto 下没有按会话号落盘")
        with open(path, encoding="utf-8") as stream:
            require(stream.read() == snapshot, "落盘内容与输入不一致")
        broken = subprocess.run(["bash", TAP], input="{not json", capture_output=True, text=True, env=env, timeout=20)
        require(broken.returncode == 0, "坏输入让读数脚本报错")
        require(not os.path.exists(os.path.join(sdir, "ctx", "unknown.json")), "坏输入不应落盘")
    return ["manual 不落盘；auto 下状态栏 JSON 原样按会话号落盘；坏输入不落盘也不报错"]


def case_n3():
    """N3：状态栏快照结构异常（context_window 非对象、NaN、字符串、非法窗口大小）时，提醒钩子正常返回且不提醒。"""
    bad_snapshots = {
        "context_window 是字符串": '{"context_window": "bad"}',
        "used_percentage 是 NaN": '{"context_window": {"context_window_size": 100, "used_percentage": NaN}}',
        "used_percentage 是字符串": '{"context_window": {"context_window_size": 100, "used_percentage": "80"}}',
        "used_percentage 是无穷": '{"context_window": {"context_window_size": 100, "used_percentage": Infinity}}',
        "窗口大小为负": '{"context_window": {"context_window_size": -5, "used_percentage": 90}}',
        "窗口大小是浮点": '{"context_window": {"context_window_size": 100.5, "used_percentage": 90}}',
        "整份快照是数组": "[1, 2]",
    }
    with temp_dir() as work, patched_env(CTX_WINDOW=None):
        state_dir = os.path.join(work, "cdm")
        directory = os.path.join(state_dir, "ctx")
        os.makedirs(directory)
        for index, (label, text) in enumerate(bad_snapshots.items()):
            sid = "bad%d" % index
            with open(os.path.join(directory, sid + ".json"), "w", encoding="utf-8") as stream:
                stream.write(text)
            token, out = fire({"session_id": sid, "hook_event_name": "PostToolUse"}, state_dir)
            require(token is None and not out, "%s：不应提醒" % label)
        write_snapshot(state_dir, "good", 100, 80)
        token, out = fire({"session_id": "good", "hook_event_name": "PostToolUse"}, state_dir)
        require(token and out, "合法快照应正常提醒（对照）")
    return ["7 种结构异常的快照都被当作无快照：钩子正常返回、不提醒；合法快照照常提醒"]


def main():
    """运行 N1、N2、N3。"""
    return run_cases([("N1", case_n1), ("N2", case_n2), ("N3", case_n3)])


if __name__ == "__main__":
    sys.exit(main())
