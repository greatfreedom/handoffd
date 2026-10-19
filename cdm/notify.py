# 通知/铃声/语音：横幅（osascript）、铃声（afplay）、真人声片段（afplay）或普通话语音（say）；失败只记日志。
import os
import random
import re
import subprocess

KINDS = ("waiting", "problem", "quiet")
BANNER_TITLE = "主控调度器"
OSASCRIPT = "/usr/bin/osascript"
AFPLAY = "/usr/bin/afplay"
SAY = "/usr/bin/say"


def list_voice_dirs(clips_dir):
    """clips_dir 下每个含 mp3 的子目录是一个音色，按名字排序；返回 [(音色名, [mp3 路径])]。"""
    try:
        names = sorted(os.listdir(clips_dir))
    except OSError:
        return []
    voices = []
    for name in names:
        directory = os.path.join(clips_dir, name)
        if name.startswith(".") or not os.path.isdir(directory):
            continue  # 跳过 .staging / .previous 等备份目录，里面的旧 mp3 不能被播放
        clips = sorted(item for item in os.listdir(directory) if item.endswith(".mp3"))
        if clips:
            voices.append((name, [os.path.join(directory, clip) for clip in clips]))
    return voices


OPTION_SPLIT = re.compile(r"(?:(?<=^)|(?<=\s))(\d+)(?:[)）、]|\.(?=\s))\s*")


def parse_options(ask):
    """把 "1) 去看 2) 放弃" 这种写法（或已拆好的列表）解析成 [(编号, 文字)]；编号必须从 1 连续递增，否则抛 ValueError。"""
    if isinstance(ask, (list, tuple)):
        options = [(index + 1, str(item).strip()) for index, item in enumerate(ask)]
    else:
        text = str(ask or "").strip()
        parts = OPTION_SPLIT.split(text)
        # split 结果：[前缀, 编号, 文字, 编号, 文字, ...]；前缀必须为空
        if len(parts) < 3 or parts[0].strip():
            raise ValueError("「要你做什么」必须是带编号的选项，例如 \"1) 去该窗口处理 2) 放弃交接\"（规格:50）")
        options = []
        for index in range(1, len(parts) - 1, 2):
            options.append((int(parts[index]), parts[index + 1].strip()))
    if not options or any(not label for _number, label in options):
        raise ValueError("选项不能为空")
    if [number for number, _label in options] != list(range(1, len(options) + 1)):
        raise ValueError("选项编号必须从 1 连续递增")
    return options


def format_notice(what, why, ask, recommend, if_no_reply):
    """规格:50 的四段文案：发生了什么 / 为什么停 / 要你做什么（含推荐）/ 不回复会怎样；选项带编号。"""
    for label, value in (("发生了什么", what), ("为什么停", why), ("不回复会怎样", if_no_reply)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s 不能为空" % label)
    options = parse_options(ask)
    numbers = [number for number, _label in options]
    if not isinstance(recommend, int) or isinstance(recommend, bool) or recommend not in numbers:
        raise ValueError("推荐项 %r 不在选项 %r 里" % (recommend, numbers))
    lines = ["发生了什么：" + what.strip(), "为什么停：" + why.strip(), "要你做什么："]
    for number, label in options:
        lines.append("  %d) %s%s" % (number, label, "（推荐）" if number == recommend else ""))
    lines.append("不回复会怎样：" + if_no_reply.strip())
    return "\n".join(lines)


def ring(alerts, kind, message, voice_text=None, runner=subprocess.run, log_fn=None):
    """按参数直接发一次声（横幅 + 铃声 + 人声），不读任何通知记录（设计:84 的 ring）；返回 Notifier 便于调用方查看。"""
    notifier = Notifier(alerts, runner=runner, log_fn=log_fn)
    notifier.alert(kind, message, voice_text)
    return notifier


class Notifier:
    """按配置 alerts.<kind> 依次发横幅、铃声、人声片段（或 say）。rotation 记录轮换位置，由调用方持久化。"""

    def __init__(self, alerts, runner=subprocess.run, log_fn=None, rng=None):
        self.alerts = alerts
        self.runner = runner
        self.log_fn = log_fn or (lambda **fields: None)
        self.rng = rng or random.Random()
        self.rotation = {}

    def _run(self, name, args, timeout):
        """带超时运行一个外部命令，失败只记日志。"""
        # 全局静音闸（离线测试用：run_all.sh 里 export HANDOFFD_SILENT=1）：只对真实的 subprocess.run 生效，不调 osascript / afplay / say，只记日志；
        # 测试注入的假 runner 不受影响。
        if os.environ.get("HANDOFFD_SILENT") == "1" and self.runner is subprocess.run:
            self.log_fn(event="notify_silenced", tool=name)
            return True
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log_fn(event="notify_failed", tool=name, error=type(exc).__name__)
            return False
        if result.returncode != 0:
            self.log_fn(event="notify_failed", tool=name, returncode=result.returncode)
            return False
        return True

    def _pick_tone(self, setting):
        """铃声按列表轮换；没有 sounds 时用单个 sound。"""
        sounds = setting.get("sounds") or []
        if not sounds:
            return setting.get("sound")
        count = self.rotation.get("tone_next", 0)
        self.rotation["tone_next"] = count + 1
        return sounds[count % len(sounds)]

    def _pick_clip(self, clips_dir):
        """音色按名字轮换，音色内随机且不与上一次同一句；没有素材返回 None。"""
        voices = list_voice_dirs(clips_dir)
        if not voices:
            return None
        count = self.rotation.get("voice_next", 0)
        self.rotation["voice_next"] = count + 1
        _name, clips = voices[count % len(voices)]
        last = self.rotation.get("last_clip")
        candidates = [clip for clip in clips if os.path.basename(clip) != last] or clips
        chosen = self.rng.choice(candidates)
        self.rotation["last_clip"] = os.path.basename(chosen)
        return chosen

    def alert(self, kind, message, voice_text=None):
        """发一次提醒；kind 必须是闭域之一。"""
        if kind not in KINDS:
            raise ValueError("未知通知类别：%s" % kind)
        setting = self.alerts.get(kind) or {}
        self.log_fn(event="alert", kind=kind, message=message)
        # 用 on run argv 传参，文本不拼进 AppleScript 源码
        self._run("osascript", [
            OSASCRIPT,
            "-e", "on run argv",
            "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e", "end run",
            message, BANNER_TITLE,
        ], 10)
        tone = self._pick_tone(setting)
        if tone:
            self._run("afplay", [AFPLAY, tone], 20)
        clip = self._pick_clip(setting["voice_clips_dir"]) if setting.get("voice_clips_dir") else None
        if clip:
            self._run("afplay", [AFPLAY, clip], 30)
        elif setting.get("voice"):
            spoken = voice_text or setting.get("text") or message
            self._run("say", [SAY, "-v", setting["voice"], spoken], 30)


class RecordingNotifier:
    """测试替身：只记录调用，不执行任何外部命令。"""

    def __init__(self):
        self.calls = []

    def alert(self, kind, message, voice_text=None):
        """记录一次调用。"""
        self.calls.append((kind, message, voice_text))

    def count(self, kind):
        """某一类别被调用的次数。"""
        return sum(1 for call in self.calls if call[0] == kind)
