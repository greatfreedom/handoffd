#!/bin/bash
# 开窗启动器（handoffd，设计:26）：预检固定的 claude 文件、版本（不低于最低版本）和登录 shell 环境后，开一个单独的新 Warp 窗口运行该绝对路径（RT:W1、W2）。
# --warp-open launch_config：写 Warp Launch Configuration（~/.warp/launch_configurations/<名>.yaml：1 个 window、1 个 tab，title=<名>，layout.cwd=项目绝对路径，
#   commands 的 exec 项运行命令文件）并 `open -g "warp://launch/<名>"`——不带 Warp 自带的空白标签页，B 退出后窗口随之关闭。
# --warp-open tab_config（缺省，回退）：写 Warp 标签页配置并 `open -g "warp://tab_config/<名>?new_window=true"`（会多一个空白标签页）。
# 临时配置文件由 daemon 在 B 上线后 / launch_confirm_timeout 到期后删除。
# 必须用 --claude-bin 给出 claude 的绝对路径（目录做 pwd -P，文件名保持不变）。--prompt-file 可选：B 先空闲起来，开场指令由 daemon 在 seal 后用消息投递（规格:27-28）。
# 退出码：2 = 预检失败且保证没调用 open；0 = open 返回 0；3 = open 返回非零（不确定窗口是否已开）。
# 环境变量（测试用）：CDM_OPEN_BIN、CDM_ZSH_BIN、CDM_WARP_TAB_DIR（Warp 标签页配置目录，缺省 ~/.warp/tab_configs）、CDM_WARP_LAUNCH_DIR（缺省 ~/.warp/launch_configurations）。
set -u

ZSH_BIN=${CDM_ZSH_BIN:-/bin/zsh}    # 测试可替换；生产固定为 macOS 默认的 /bin/zsh
OPEN_BIN=${CDM_OPEN_BIN:-/usr/bin/open}
WARP_TAB_DIR=${CDM_WARP_TAB_DIR:-$HOME/.warp/tab_configs}
WARP_LAUNCH_DIR=${CDM_WARP_LAUNCH_DIR:-$HOME/.warp/launch_configurations}

fail() {
  echo "CDM preflight failed: $1" >&2
  exit 2
}

# S4a：额外参数允许末尾带一个 [1m] 这样的方括号后缀（模型 id）；命令文件里经 printf %q 转义，窗口里 zsh 按空白切词且不展开通配
EXTRA_ARG_RE='^[A-Za-z0-9._=:/@+-]+(\[[0-9a-z]+\])?$'
NAME=""
DIR=""
PROMPT_FILE=""
COMMAND_DIR=""
MIN_VERSION=""
REQUIRED_ENV=""
CLAUDE_BIN=""
EXTRA_ARGS=""
WARP_OPEN="tab_config"
seen_name=0
seen_dir=0
seen_prompt=0   # --prompt-file 可选，不计入必填校验
seen_command_dir=0
seen_min=0
seen_env=0
seen_bin=0

while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || fail "option $1 needs a value"
  case "$1" in
    --name) NAME=$2; seen_name=1 ;;
    --dir) DIR=$2; seen_dir=1 ;;
    --prompt-file) PROMPT_FILE=$2; seen_prompt=1 ;;
    --command-dir) COMMAND_DIR=$2; seen_command_dir=1 ;;
    --min-version) MIN_VERSION=$2; seen_min=1 ;;
    --required-env) REQUIRED_ENV=$2; seen_env=1 ;;
    --claude-bin) CLAUDE_BIN=$2; seen_bin=1 ;;
    --warp-open)
      [[ "$2" == "launch_config" || "$2" == "tab_config" ]] || fail "bad warp-open mode"
      WARP_OPEN=$2
      ;;
    --extra-arg)
      [[ "$2" =~ $EXTRA_ARG_RE ]] || fail "bad extra arg"
      EXTRA_ARGS="${EXTRA_ARGS:+$EXTRA_ARGS }$2"
      ;;
    *) fail "unknown option $1" ;;
  esac
  shift 2
done

[ "$seen_name$seen_dir$seen_command_dir$seen_min$seen_env$seen_bin" = "111111" ] || fail "missing option"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "bad name"
[[ "$MIN_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "bad min version"
[[ -d "$DIR" ]] || fail "missing directory"
if [ "$seen_prompt" = 1 ]; then
  [[ -f "$PROMPT_FILE" ]] || fail "missing prompt file"
fi
[[ -x "$ZSH_BIN" ]] || fail "login shell unavailable"
for var in $REQUIRED_ENV; do
  [[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || fail "invalid required environment name"
done

[[ "$CLAUDE_BIN" == /* ]] || fail "claude bin must be an absolute path"
BIN_DIR="$(dirname "$CLAUDE_BIN")"
BIN_NAME="$(basename "$CLAUDE_BIN")"
[[ "$BIN_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "bad claude file name"
[[ -d "$BIN_DIR" ]] || fail "Claude bin directory unavailable"
TARGET="$(cd "$BIN_DIR" && pwd -P)/$BIN_NAME"
[[ -x "$TARGET" && ! -d "$TARGET" ]] || fail "target Claude file is not executable"

# 取目标 claude 的第一个 x.y.z 版本号并要求不低于最低版本；成功时打印版本号
check_version() {
  /usr/bin/python3 - "$1" "$2" <<'PY'
import re
import subprocess
import sys

path, minimum = sys.argv[1:]
try:
    result = subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=5)
except (OSError, subprocess.SubprocessError):
    sys.exit(1)
match = re.search(r"\d+\.\d+\.\d+", result.stdout)
if result.returncode or not match:
    sys.exit(1)
found = tuple(int(part) for part in match.group(0).split("."))
wanted = tuple(int(part) for part in minimum.split("."))
if found < wanted:
    sys.exit(1)
print(match.group(0))
PY
}

shell_initializes_required_env() {
  CDM_REQUIRED_ENV="$REQUIRED_ENV" "$ZSH_BIN" -lic '
    claude --version >/dev/null 2>&1 || exit 1
    for cdm_var in $CDM_REQUIRED_ENV; do
      eval "cdm_present=\${$cdm_var+x}"
      [ "$cdm_present" = x ] || exit 1
    done
  '
}

VERSION="$(check_version "$TARGET" "$MIN_VERSION")" || fail "target version is missing or lower than $MIN_VERSION"
shell_initializes_required_env || fail "login shell did not initialize required environment"

mkdir -p "$COMMAND_DIR" || fail "cannot create command directory"
COMMAND_DIR="$(cd "$COMMAND_DIR" && pwd -P)"
CMD="$COMMAND_DIR/$NAME.command"
{
  echo '#!/bin/bash'
  # G2：新窗口里清掉 CLAUDE*、MAIN_LOCK_OVERRIDE、GIT_* 变量，B 不继承 daemon / 调用方的环境
  cat <<'EOT0'
for v in $(env | awk -F= '/^CLAUDE/{print $1} /^GIT_/{print $1} /^MAIN_LOCK_OVERRIDE=/{print $1}'); do unset "$v"; done
EOT0
  printf 'cd %q || exit 1\n' "$DIR"
  printf 'export CDM_PROMPT_FILE=%q CDM_NAME=%q CDM_TARGET=%q CDM_REQUIRED_ENV=%q CDM_MIN_VERSION=%q CDM_EXTRA=%q\n' \
    "${PROMPT_FILE:-}" "$NAME" "$TARGET" "$REQUIRED_ENV" "$MIN_VERSION" "$EXTRA_ARGS"
  cat <<'EOT'
exec /bin/zsh -lic '
  unsetopt GLOB_SUBST   # S4a：用户 rc 万一开了 GLOB_SUBST，下面 ${=CDM_EXTRA} 里的 [1m] 会被当通配符
  claude --version >/dev/null 2>&1 || exit 1
  for cdm_var in $CDM_REQUIRED_ENV; do
    eval "cdm_present=\${$cdm_var+x}"
    [ "$cdm_present" = x ] || exit 1
  done
  /usr/bin/python3 - "$CDM_TARGET" "$CDM_MIN_VERSION" <<"PY"
import re
import subprocess
import sys

try:
    result = subprocess.run([sys.argv[1], "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=5)
except (OSError, subprocess.SubprocessError):
    sys.exit(1)
match = re.search(r"\d+\.\d+\.\d+", result.stdout)
if result.returncode or not match:
    sys.exit(1)
found = tuple(int(part) for part in match.group(0).split("."))
wanted = tuple(int(part) for part in sys.argv[2].split("."))
sys.exit(0 if found >= wanted else 1)
PY
  [ $? -eq 0 ] || exit 1
  if [ -n "$CDM_PROMPT_FILE" ]; then
    exec "$CDM_TARGET" ${=CDM_EXTRA} --name "$CDM_NAME" "$(cat "$CDM_PROMPT_FILE")"
  fi
  exec "$CDM_TARGET" ${=CDM_EXTRA} --name "$CDM_NAME"
'
EOT
} > "$CMD" || fail "cannot write command file"
chmod +x "$CMD" || fail "cannot make command file executable"

# Warp 标签页配置（W1）：命令是 `exec <命令文件>`；写失败 ⇒ 预检失败（还没调 open）
write_tab_config() {
  /usr/bin/python3 - "$WARP_TAB_DIR" "$NAME" "$DIR" "$CMD" <<'PY'
import json
import os
import shlex
import sys

directory, name, cwd, command = sys.argv[1:]
os.makedirs(directory, exist_ok=True)
quote = lambda value: json.dumps(value, ensure_ascii=False)
text = "# handoffd 生成：开 B 的单独 Warp 窗口（用完可删）\nname = %s\ntitle = %s\n\n[[panes]]\nid = \"main\"\ntype = \"terminal\"\ndirectory = %s\ncommands = [%s]\n" % (
    quote(name), quote(name), quote(cwd), quote("exec " + shlex.quote(command)))
target = os.path.join(directory, name + ".toml")
temporary = target + ".tmp"
with open(temporary, "w", encoding="utf-8") as stream:
    stream.write(text)
os.replace(temporary, target)
PY
}
# Warp Launch Configuration：windows → tabs → title / layout（cwd 必须是绝对路径）/ commands 的 exec 项（docs.warp.dev/terminal/sessions/launch-configurations）；
# 字符串用 JSON 引号写（YAML 的合法双引号标量）。命令 `exec <命令文件>`：claude 退出后标签页关闭，窗口随之消失。
write_launch_config() {
  /usr/bin/python3 - "$WARP_LAUNCH_DIR" "$NAME" "$DIR" "$CMD" <<'PY'
import json
import os
import shlex
import sys

directory, name, cwd, command = sys.argv[1:]
os.makedirs(directory, exist_ok=True)
quote = lambda value: json.dumps(value, ensure_ascii=False)
text = ("---\nname: %s\nwindows:\n  - tabs:\n      - title: %s\n        layout:\n          cwd: %s\n          commands:\n            - exec: %s\n"
        % (quote(name), quote(name), quote(cwd), quote("exec " + shlex.quote(command))))
target = os.path.join(directory, name + ".yaml")
temporary = target + ".tmp"
with open(temporary, "w", encoding="utf-8") as stream:
    stream.write(text)
os.replace(temporary, target)
PY
}

echo "CDM_LAUNCHER_VERSION=$VERSION"
echo "CDM_LAUNCHER_TARGET=$TARGET"
echo "CDM_LAUNCHER_COMMAND=$CMD"

if [ "$WARP_OPEN" = launch_config ]; then
  [[ "$DIR" == /* ]] || fail "project dir must be an absolute path"
  DIR="$(cd "$DIR" && pwd -P)"
  write_launch_config || fail "cannot write warp launch configuration"
  echo "CDM_LAUNCHER_LAUNCH=$WARP_LAUNCH_DIR/$NAME.yaml"
  OPEN_URL="warp://launch/$NAME"
else
  write_tab_config || fail "cannot write warp tab config"
  echo "CDM_LAUNCHER_TAB=$WARP_TAB_DIR/$NAME.toml"
  OPEN_URL="warp://tab_config/$NAME?new_window=true"
fi

if "$OPEN_BIN" -g "$OPEN_URL"; then
  exit 0
fi
exit 3
