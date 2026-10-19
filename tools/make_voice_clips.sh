#!/bin/bash
# 可选工具：用 edge-tts（微软神经语音，需联网）把 assets/voice/phrases.txt 按 voices.txt 的音色生成成 mp3；守护程序播放时不联网。
# 用法：EDGE_TTS=/path/to/edge-tts bash tools/make_voice_clips.sh
# 先全部生成到 .staging，全部成功才替换；旧音色目录整体挪到 .previous/<时间>/（不删除）；生成或替换中途失败则整批回滚、原有文件不动。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EDGE_TTS=${EDGE_TTS:?"请把 EDGE_TTS 设为 edge-tts 可执行文件路径，见 assets/voice/README.md"}
VOICE_DIR="$ROOT/assets/voice"
STAGING="$VOICE_DIR/.staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"

voices=0
while read -r voice rate pitch || [ -n "$voice" ]; do
  case "$voice" in ''|'#'*) continue ;; esac
  voices=$((voices + 1))
  mkdir -p "$STAGING/$voice"
  n=0
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    n=$((n + 1))
    out=$(printf '%s/%s/%02d.mp3' "$STAGING" "$voice" "$n")
    if ! "$EDGE_TTS" --voice "$voice" --rate="$rate" --pitch="$pitch" --text="$line" --write-media "$out" </dev/null >/dev/null 2>&1 || [ ! -s "$out" ]; then
      echo "生成失败：${voice} 第 ${n} 句（原有文件未改动）" >&2
      rm -rf "$STAGING"
      exit 1
    fi
  done < "$VOICE_DIR/phrases.txt"
  [ "$n" -gt 0 ] || { echo "phrases.txt 里没有句子" >&2; rm -rf "$STAGING"; exit 1; }
  echo "${voice}：${n} 段"
done < "$VOICE_DIR/voices.txt"
[ "$voices" -gt 0 ] || { echo "voices.txt 里没有音色" >&2; rm -rf "$STAGING"; exit 1; }

BACKUP="$VOICE_DIR/.previous/$(date +%Y%m%d-%H%M%S)"
moved_old=()   # 已挪进备份的旧音色目录名
moved_new=()   # 已从 .staging 换进正式位置的新音色目录名

# 发布阶段任何一步失败都整批回滚（G9）：先撤掉已换上的新目录，再把已挪进备份的旧目录挪回，原有文件不改动
rollback() {
  local name
  for name in ${moved_new[@]+"${moved_new[@]}"}; do rm -rf "$VOICE_DIR/$name"; done
  for name in ${moved_old[@]+"${moved_old[@]}"}; do
    mv "$BACKUP/$name" "$VOICE_DIR/$name" || echo "回滚时挪回 $name 失败，请手动从 $BACKUP 取回" >&2
  done
  rmdir "$BACKUP" "$VOICE_DIR/.previous" 2>/dev/null || true   # 只删空目录（以前的备份在里面就保留）
  rm -rf "$STAGING"
}
fail_publish() {
  echo "替换失败：$1；已回滚，原有文件未改动" >&2
  rollback
  exit 1
}

mkdir -p "$BACKUP" || fail_publish "建不了备份目录"
for old in "$VOICE_DIR"/*/; do
  [ -d "$old" ] || continue
  name=$(basename "$old")
  if mv "$old" "$BACKUP/"; then moved_old+=("$name"); else fail_publish "旧音色 $name 挪不进备份"; fi
done
for new in "$STAGING"/*/; do
  name=$(basename "$new")
  if mv "$new" "$VOICE_DIR/"; then moved_new+=("$name"); else fail_publish "新音色 $name 换不上"; fi
done
rm -rf "$STAGING"
echo "完成；旧素材已备份到 ${BACKUP}"
