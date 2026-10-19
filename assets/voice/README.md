# 提醒的铃声与人声：怎么自己放

handoffd 需要你看一眼（`waiting` 等你输入、`problem` 出问题）时，先弹一条横幅，再响铃声；如果你配了人声素材，再放一段人声。

**发布版不带任何音频文件。** 默认铃声是 macOS 自带的系统声音（`/System/Library/Sounds/Glass.aiff`、`Ping.aiff`，轮流响），默认不放人声。想换成自己的，按下面做。播放时都**不联网**，用的是你本地的文件。

## 一、换铃声

在项目的 `.claude/handoffd.json` 里写 `alerts.<类别>.sounds`（`waiting` / `problem`，列表，轮流响），例如：

```json
{"alerts": {"problem": {"sounds": ["/System/Library/Sounds/Hero.aiff", "/path/to/my_tone.m4a"]}, "waiting": {"sounds": ["/System/Library/Sounds/Ping.aiff"]}}}
```

- 系统自带声音在 `/System/Library/Sounds/`，试听：`afplay /System/Library/Sounds/Hero.aiff`。
- 路径可写绝对路径；相对路径按 handoffd 安装根目录算。
- **优先级**：`sounds` 非空就只用 `sounds`（`sound` 被忽略）；想用单个铃声要同时写 `"sounds": []` 和 `"sound": "…"`。
- 自己的铃声请使用你有权使用的音频；本项目不分发铃声。

## 二、挂人声（可选）

1. 写话术：编辑 `assets/voice/phrases.txt`，**一行一句**（建议 8–12 句，空行跳过）。音色在 `assets/voice/voices.txt`，一行一个，格式 `音色名 语速 音调`，`#` 开头是注释，例如 `zh-CN-YunxiaNeural -5% +5Hz`。
2. 装一次生成工具（装在 `<安装目录>/.tts-venv`，不动系统）：
   ```bash
   cd <安装目录>
   /usr/bin/python3 -m venv .tts-venv && .tts-venv/bin/pip install edge-tts
   ```
3. 生成（**需要联网**；会把 `phrases.txt` 里的句子发给微软的语音服务，生成的音频的使用条款请你自行确认）：
   ```bash
   cd <安装目录>
   EDGE_TTS=.tts-venv/bin/edge-tts bash tools/make_voice_clips.sh
   ```
   旧音频不会被删，整批挪到 `assets/voice/.previous/<时间>/`；中途失败会整批回滚。查看可用中文音色：`.tts-venv/bin/edge-tts --list-voices | grep zh-`。
4. 让配置指向它：`handoffd.json` 里写 `{"alerts": {"problem": {"voice_clips_dir": "assets/voice"}, "waiting": {"voice_clips_dir": "assets/voice"}}}`（人声目录里每个子目录是一个音色，以 `.` 开头的目录会被跳过）。
5. 试听（会真的出声）：`handoffd test-alert problem`（`waiting` / `quiet` 同理）。`test-alert` 每次都从第一个铃声、第一个音色开始，只用来确认"能响、能说"。

## 三、只要铃声 + 一句系统语音

在 `handoffd.json` 的 `alerts.<类别>` 里写 `"voice": "Tingting"`（`say -v ?` 可查本机有哪些音色）和 `"text": "Claude 找你"`；这一句只在没有人声素材（`voice_clips_dir` 为空）时由系统 `say` 念出来。
