# 测试 C2、C5（配置校验）以及 A1–A3（agents 读取、通知器参数、铃声人声轮换）。
# S2a：旧 cdm 的 mark-ready / status / reset / launch-anyway 随 launchrec 删除，其专属用例 C1、C4 移除；提醒类别改为 waiting|problem|quiet，A2、A3 里原 exit_reminder 的铃声 / 人声轮换配置改挂在 problem 上。
# S3：旧 cdm 命令行整体退役（bin/cdm、cdm/cli.py 删除）：C2、C5 改用 `handoffd test-alert` 触发配置校验；C3（install/uninstall-launchd）与 C5 的 launchd 部分
# 由 test_s3.py 的 H62–H68（handoffd on / off、G7）接替。
import json
import os
import random
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import FIXTURE_TONES, FIXTURE_VOICE, MVP_ROOT, require, run_cases, temp_dir, write_exe  # noqa: E402
from cdm import agents, config, notify  # noqa: E402

HANDOFFD = os.path.join(MVP_ROOT, "bin", "handoffd")


def run_cdm(args, env=None):
    """以子进程运行 bin/handoffd（旧名沿用：这里只用它的 test-alert 触发配置校验）。"""
    base = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    base.update(env or {})
    return subprocess.run([HANDOFFD] + args, capture_output=True, text=True, env=base, timeout=60)


def write_config(work, **extra):
    """写一份最小配置，返回 (配置路径, 项目目录, 状态目录)。"""
    project = os.path.join(work, "project")
    os.makedirs(project, exist_ok=True)
    raw = {"project_dir": project, "session_name_prefix": "toy"}
    raw.update(extra)
    path = os.path.join(work, "cdm.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(raw, stream)
    return path, project, os.path.join(project, ".claude", "cdm")


def _three_voice_fixture():
    """轮换逻辑要多个音色才测得出来；发布版不带音频，所以临时造 3 个音色子目录（内容复制 tests/fixtures 里的占位片段），进程退出时删掉。"""
    import atexit
    import tempfile
    root = tempfile.mkdtemp(prefix="hd-voice-")
    atexit.register(shutil.rmtree, root, True)
    source = FIXTURE_VOICE
    for name in ("voice-a", "voice-b", "voice-c"):
        shutil.copytree(source, os.path.join(root, name))
    return root


SYSTEM_SOUNDS = ["/System/Library/Sounds/Glass.aiff", "/System/Library/Sounds/Ping.aiff"]   # 发布版默认铃声（产品默认值，不带音频文件）
ROTATION = {
    "sounds": [os.path.join(FIXTURE_TONES, "tone-a.m4a"), os.path.join(FIXTURE_TONES, "tone-b.m4a")],
    "voice_clips_dir": _three_voice_fixture(), "voice": "Tingting", "text": "Claude 找你，到你干活了", "repeat_minutes": 5, "max_times": 3,
}


def rich_alerts(defaults):
    """旧 exit_reminder 的铃声 / 人声轮换配置（S3 会重新决定它挂在哪一类上）挂到 problem 上，供通知器用例用。"""
    return dict(defaults, problem=dict(defaults["problem"], **ROTATION))


def case_c2():
    """C2：配置出现未知键（含已删除的 opening_prompt_template 与旧提醒类别 exit_reminder） / 缺 project_dir / 类型错误 → 报错退出非 0。"""
    with temp_dir() as work:
        bad_configs = {
            "未知键": {"project_dir": work, "bogus": 1},
            "缺 project_dir": {"session_name_prefix": "toy"},
            "相对路径": {"project_dir": "relative/path"},
            "未知告警类别": {"project_dir": work, "alerts": {"nope": {}}},
            "已删除的告警类别": {"project_dir": work, "alerts": {"exit_reminder": {}}},
            "已删除的开场模板": {"project_dir": work, "opening_prompt_template": "x"},
            "未知告警键": {"project_dir": work, "alerts": {"problem": {"loud": True}}},
            "阈值越界": {"project_dir": work, "nudge_thresholds": [0, 200]},
            "非法额外参数": {"project_dir": work, "claude_extra_args": ["a b"]},
        }
        details = []
        for label, raw in bad_configs.items():
            path = os.path.join(work, "bad.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(raw, stream)
            result = run_cdm(["test-alert", "problem", "--config", path])
            require(result.returncode != 0 and "配置错误" in result.stderr, "%s：应报错退出非 0，实际 %r %r" % (label, result.returncode, result.stderr))
            details.append(label)
        broken = os.path.join(work, "broken.json")
        with open(broken, "w", encoding="utf-8") as stream:
            stream.write("{not json")
        result = run_cdm(["test-alert", "problem", "--config", broken])
        require(result.returncode != 0, "坏 JSON 应报错")
        result = run_cdm(["test-alert", "problem", "--config", os.path.join(work, "missing.json")])
        require(result.returncode != 0, "配置文件不存在应报错")
        good = write_config(work)[0]
        cfg = config.load_config(good)
        require(cfg["nudge_thresholds"] == [65.0, 76.0] and cfg["alerts"]["problem"]["sounds"] == SYSTEM_SOUNDS and cfg["alerts"]["waiting"]["sounds"] == SYSTEM_SOUNDS and cfg["alerts"]["quiet"]["sound"] is None and all(cfg["alerts"][kind]["voice_clips_dir"] is None for kind in ("problem", "waiting")) and sorted(cfg["alerts"]) == ["problem", "quiet", "waiting"], "默认值不对")
    return ["报错：" + "、".join(details) + "、坏 JSON、文件缺失；合法配置的默认值正确"]


def case_a1():
    """A1：agents 读取——固定绝对路径的假 claude，过滤 kind 与 cwd，失败返回 None。"""
    with temp_dir() as work:
        project = os.path.join(work, "project")
        os.makedirs(project)
        record = os.path.join(work, "argv.log")
        entries = [
            {"kind": "interactive", "cwd": project, "name": "keep", "pid": 1},
            {"kind": "background", "cwd": project, "name": "bg", "pid": 2},
            {"kind": "interactive", "cwd": os.path.join(work, "other"), "name": "elsewhere", "pid": 3},
            "garbage",
        ]
        fake = os.path.join(work, "bin", "claude")
        write_exe(fake, "#!/bin/sh\nprintf '%%s\\n' \"$*\" > \"%s\"\ncat <<'EOF'\n%s\nEOF\n" % (record, json.dumps(entries)))
        result = agents.list_agents(fake, project)
        require([item["name"] for item in result] == ["keep"], "过滤结果不对：%r" % result)
        with open(record, encoding="utf-8") as stream:
            require(stream.read().strip() == "agents --json --cwd " + project, "调用参数不对")
        write_exe(fake, "#!/bin/sh\nexit 1\n")
        require(agents.list_agents(fake, project) is None, "非零退出应返回 None")
        write_exe(fake, "#!/bin/sh\necho 'not json'\n")
        require(agents.list_agents(fake, project) is None, "非 JSON 应返回 None")
        write_exe(fake, "#!/bin/sh\necho '{}'\n")
        require(agents.list_agents(fake, project) is None, "非数组应返回 None")
        require(agents.list_agents(os.path.join(work, "missing"), project) is None, "程序不存在应返回 None")
    return ["只留下本项目的交互会话；失败/坏输出/程序不存在都返回 None"]


def case_a2():
    """A2：通知器——osascript 用 on run argv 传参、文本不拼进脚本；失败只记日志；未知类别报错。"""
    calls = []
    logs = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    defaults = config.default_config("/tmp/x")["alerts"]
    alerts = rich_alerts(defaults)
    tricky = '带"引号"的\\文本; do shell script "x"'
    notifier = notify.Notifier(alerts, runner=runner, log_fn=lambda **fields: logs.append(fields))
    notifier.alert("problem", tricky, "语音文本")
    names = [os.path.basename(args[0]) for args, _kw in calls]
    require(names == ["osascript", "afplay", "afplay"], "有真人声素材时应依次调用 osascript/afplay(铃声)/afplay(人声)：%r" % names)
    require(calls[1][0][1].endswith(("tone-a.m4a", "tone-b.m4a")) and calls[2][0][1].endswith(".mp3"), "afplay 参数应是铃声与人声片段")
    calls.clear()
    no_clips = dict(alerts, problem=dict(alerts["problem"], voice_clips_dir=None))
    notify.Notifier(no_clips, runner=runner).alert("problem", tricky, "语音文本")
    osascript_args = calls[0][0]
    scripts = [osascript_args[index + 1] for index, item in enumerate(osascript_args) if item == "-e"]
    require(scripts[0] == "on run argv" and tricky not in " ".join(scripts), "文本不得拼进 AppleScript 源码")
    require(osascript_args[-2] == tricky, "文本应作为独立参数传入")
    require([os.path.basename(args[0]) for args, _kw in calls] == ["osascript", "afplay", "say"], "没有人声素材时应退回 say")
    require(calls[2][0][:3] == ["/usr/bin/say", "-v", "Tingting"] and calls[2][0][-1] == "语音文本", "say 参数不对")
    require(all(kwargs.get("timeout") for _args, kwargs in calls), "每个外部命令都要带超时")
    calls.clear()
    notify.Notifier(defaults, runner=runner).alert("quiet", "x")
    require([os.path.basename(args[0]) for args, _kw in calls] == ["osascript"], "quiet 类默认只发无声横幅")
    calls.clear()
    notify.Notifier(defaults, runner=runner).alert("problem", "x")
    require([os.path.basename(args[0]) for args, _kw in calls] == ["osascript", "afplay"] and calls[1][0][1] == SYSTEM_SOUNDS[0],
            "problem 类默认只发横幅和系统铃声（发布版不带人声素材）")
    calls.clear()
    notify.Notifier(defaults, runner=runner).alert("waiting", "x")
    require([os.path.basename(args[0]) for args, _kw in calls] == ["osascript", "afplay"] and calls[1][0][1] == SYSTEM_SOUNDS[0],
            "waiting 类默认只发横幅和系统铃声（发布版不带人声素材）")

    def failing(args, **kwargs):
        raise OSError("boom")

    quiet = notify.Notifier(alerts, runner=failing, log_fn=lambda **fields: logs.append(fields))
    quiet.alert("problem", "x")
    require(any(item.get("event") == "notify_failed" for item in logs), "失败应记日志")
    for dropped in ("bogus", "exit_reminder", "launched", "context"):
        try:
            notifier.alert(dropped, "x")
        except ValueError:
            pass
        else:
            raise AssertionError("未知 / 已删除的类别 %s 应抛 ValueError" % dropped)
    return ["osascript 参数化传文本；各类别发哪些通道正确；失败只记日志；未知类别报错"]


def case_a3():
    """A3：连续 6 次 problem（带轮换配置）：铃声交替、音色按子目录轮换、相邻两次不同片段；空目录退回 say。"""
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append(args)
        return Result()

    alerts = rich_alerts(config.default_config("/tmp/x")["alerts"])
    notifier = notify.Notifier(alerts, runner=runner, rng=random.Random(7))
    tones = []
    clips = []
    for _ in range(6):
        calls.clear()
        notifier.alert("problem", "x")
        afplays = [args[1] for args in calls if os.path.basename(args[0]) == "afplay"]
        require(len(afplays) == 2 and not any(os.path.basename(args[0]) == "say" for args in calls), "每次应是一段铃声加一段人声，且不调 say")
        tones.append(os.path.basename(afplays[0]))
        clips.append(afplays[1])
    require(tones == ["tone-a.m4a", "tone-b.m4a"] * 3, "铃声应交替：%r" % tones)
    voices = [os.path.basename(os.path.dirname(clip)) for clip in clips]
    expected = sorted(os.listdir(alerts["problem"]["voice_clips_dir"]))
    expected = [name for name in expected if os.path.isdir(os.path.join(alerts["problem"]["voice_clips_dir"], name))]
    require(len(expected) == 3 and voices == expected * 2, "音色应按 3 个子目录排序轮换：%r" % voices)
    names = [os.path.basename(clip) for clip in clips]
    require(all(a != b for a, b in zip(names, names[1:])), "相邻两次不应是同一句：%r" % names)
    require(notifier.rotation["tone_next"] == 6 and notifier.rotation["voice_next"] == 6, "轮换位置应记录在 rotation")
    resumed = notify.Notifier(alerts, runner=runner, rng=random.Random(1))
    resumed.rotation = dict(notifier.rotation)
    calls.clear()
    resumed.alert("problem", "x")
    require(os.path.basename(calls[1][1]) == "tone-a.m4a" and os.path.basename(os.path.dirname(calls[2][1])) == expected[0],
            "带着持久化的轮换位置应从上次接着轮")
    with temp_dir() as work:
        # 以点开头的备份目录（.previous / .staging）里的旧 mp3 永远不能被选中
        copy = os.path.join(work, "voice_copy")
        shutil.copytree(alerts["problem"]["voice_clips_dir"], copy)
        for hidden in (os.path.join(copy, ".previous", "x"), os.path.join(copy, ".staging")):
            os.makedirs(hidden)
            shutil.copyfile(os.path.join(copy, expected[0], "01.mp3"), os.path.join(hidden, "99.mp3"))
        with_hidden = dict(alerts, problem=dict(alerts["problem"], voice_clips_dir=copy))
        calls.clear()
        picker = notify.Notifier(with_hidden, runner=runner, rng=random.Random(3))
        for _ in range(30):
            picker.alert("problem", "x")
        played = [args[1] for args in calls if os.path.basename(args[0]) == "afplay" and args[1].endswith(".mp3")]
        require(len(played) == 30 and not any("/." in clip or clip.endswith("99.mp3") for clip in played), "点开头的备份目录里的片段不应被选中")
        require({os.path.basename(os.path.dirname(clip)) for clip in played} == set(expected), "应只在正常音色目录里轮换")
        empty = os.path.join(work, "voice")
        os.makedirs(os.path.join(empty, "some-voice"))
        fallback = dict(alerts, problem=dict(alerts["problem"], voice_clips_dir=empty))
        calls.clear()
        notify.Notifier(fallback, runner=runner).alert("problem", "x", "备用语音")
        require([os.path.basename(args[0]) for args in calls] == ["osascript", "afplay", "say"], "空目录应退回 say：%r" % calls)
        require(calls[2][-1] == "备用语音", "退回 say 应念给定文本")
        missing = dict(alerts, problem=dict(alerts["problem"], voice_clips_dir=os.path.join(work, "nope")))
        calls.clear()
        notify.Notifier(missing, runner=runner).alert("problem", "x")
        require(os.path.basename(calls[-1][0]) == "say", "目录缺失也应退回 say")
    return ["铃声交替；音色按子目录排序轮换；相邻两次不同片段；轮换位置可持久化；.previous/.staging 里的片段永不被选；空目录/缺目录退回 say"]


def case_c5():
    """C5：NaN/Infinity 配置被拒。"""
    with temp_dir() as work:
        for label, text in (("NaN", '{"project_dir": "%s", "poll_seconds": NaN}'), ("Infinity", '{"project_dir": "%s", "launch_confirm_timeout_seconds": Infinity}'),
                            ("阈值 NaN", '{"project_dir": "%s", "nudge_thresholds": [NaN]}'),
                            ("repeat NaN", '{"project_dir": "%s", "alerts": {"problem": {"repeat_minutes": Infinity}}}')):
            bad = os.path.join(work, "nan.json")
            with open(bad, "w", encoding="utf-8") as stream:
                stream.write(text % work)
            result = run_cdm(["test-alert", "problem", "--config", bad])
            require(result.returncode != 0 and "配置错误" in result.stderr, "%s 应被拒：%r" % (label, result.returncode))
    return ["NaN / Infinity 配置被拒"]


def main():
    """运行 C2、C5、A1–A3。"""
    return run_cases([("C2", case_c2), ("C5", case_c5),
                      ("A1", case_a1), ("A2", case_a2), ("A3", case_a3)])


if __name__ == "__main__":
    sys.exit(main())
