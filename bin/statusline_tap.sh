#!/bin/bash
# 状态栏读数：把 Claude Code 传给状态栏的 JSON 交给 ctx.py tap（auto 才按会话号落盘，manual 不落盘；不耗 token），再原样转给 claude-hud 继续显示。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="${CLAUDE_PROJECT_DIR:-$PWD}"
input=$(cat)
# 落盘与否由 tap 读 <S>/mode 决定；失败不得影响转交显示，所以忽略退出码和输出
printf '%s' "$input" | /usr/bin/python3 "$ROOT/cdm/ctx.py" tap "$PROJ" >/dev/null 2>&1
HUD="$(ls -d "$HOME"/.claude/plugins/cache/claude-hud/claude-hud/*/dist/index.js 2>/dev/null | tail -1)"
[ -n "$HUD" ] || exit 0
NODE="$(command -v node 2>/dev/null)"
[ -n "$NODE" ] || exit 0   # 没有 node 就不转交（claude-hud 是可选的状态栏显示，与交接功能无关）
printf '%s' "$input" | exec "$NODE" "$HUD"
