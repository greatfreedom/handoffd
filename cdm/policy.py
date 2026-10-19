# 角色 × 工具矩阵、「新长任务」集合、`handoffd on|adopt` 拦截（设计:29、§5.2 PreToolUse）。纯函数，不读写任何文件。
import os
import re
import shlex

# 「新长任务」集合（设计:90，自定；65% 与 76% 用同一集合，设计 §9 §7-4 采 A）
NEW_LONG_TASK_TOOLS = ("Agent", "Workflow", "Monitor", "CronCreate", "ScheduleWakeup")
# 旁观 / B 可用的只读工具（S1 自定：默认拒绝、只放这些；MCP 与未知工具一律拒绝）
READ_ONLY_TOOLS = (
    "Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "AskUserQuestion",
    "ListAgents",  # 只列出可 SendMessage 的会话、无副作用：接手方 B 靠它找到交出方 A 的会话名
    "TodoWrite",  # 只改会话内待办、无文件副作用（F8）；Skill / Task* / Agent / EnterPlanMode 仍拒绝：可间接改文件或起长任务
)
OWNER_ONLY_SUBCOMMANDS = ("on", "adopt")
# B 可执行的 handoffd 子命令白名单（S1 R3 Opus P2-3、派单包 §1）：status / notify / reply 任意参数，sign 只许 `sign in`；B 不得 off / request / seal / sign out / review-round
B_HANDOFFD_WHITELIST = ("status", "notify", "reply")


def b_may_run_handoffd(call):
    """B 的 handoffd 调用是否在白名单里。call = literal_handoffd 的返回 (子命令, 参数列表)。"""
    if call is None:
        return False
    sub, args = call
    if sub in B_HANDOFFD_WHITELIST:
        return True
    return sub == "sign" and _without_config(args) == ["in"]


def _without_config(args):
    """去掉公共参数 `--config FILE` / `--config=FILE`（CLI 的每个子命令都接受它，argparse 允许放在子命令之后）；缺值的 `--config` 不去掉（⇒ 不等于白名单）。"""
    out, index, args = [], 0, list(args)
    while index < len(args):
        if args[index] == "--config" and index + 1 < len(args):
            index += 2
        elif args[index].startswith("--config="):
            index += 1
        else:
            out.append(args[index])
            index += 1
    return out
# B 调 handoffd 只认命令词是裸 `handoffd` 或本安装的绝对路径（mvp/bin/handoffd）；别的同名文件（./handoffd 等）不认
INSTALLED_HANDOFFD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "handoffd")
WRAPPERS = ("env", "sudo", "exec", "command", "nohup", "time", "nice", "builtin")
SHELLS = ("bash", "sh", "zsh")
MAX_DEPTH = 3


def split_commands(command):
    """把一条 Bash 命令切成简单命令（词列表）；`; & | ( ) < >` 和换行都是分隔。无法解析返回 None。"""
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    commands, current = [], []
    for token in tokens:
        if token and all(char in ";&|()<>" for char in token):
            if current:
                commands.append(current)
                current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _strip_prefix(tokens):
    """去掉行首的环境变量赋值和 env/sudo/exec 之类的包装词，返回命令词起的列表。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-") and token.split("=", 1)[0].replace("_", "a").isalnum():
            index += 1
        elif os.path.basename(token) in WRAPPERS:
            index += 1
        elif token.startswith("-") and index > 0 and (os.path.basename(tokens[index - 1]) in WRAPPERS or tokens[index - 1].startswith("-")):
            index += 2 if token in ("-u", "-C", "-S", "-g", "-h") else 1
        else:
            break
    return tokens[index:]


def handoffd_call(tokens):
    """若这条简单命令是在调用 handoffd，返回 (子命令, 其余参数)，否则 None。
    认 `handoffd ...`、`/路径/handoffd ...`、`python3 /路径/handoffd ...`；子命令前的 `--config X` 跳过。"""
    tokens = _strip_prefix(tokens)
    if not tokens:
        return None
    word = os.path.basename(tokens[0])
    rest = tokens[1:]
    if word.startswith("python"):
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if not rest or os.path.basename(rest[0]) != "handoffd":
            return None
        rest = rest[1:]
    elif word != "handoffd":
        return None
    index = 0
    while index < len(rest) and (rest[index] == "--config" or rest[index].startswith("--config=")):
        index += 1 if rest[index].startswith("--config=") else 2
    if index >= len(rest):
        return "", []
    return rest[index], rest[index + 1:]


def _shell_payload(tokens):
    """`bash -c '...'` / `sh -lc '...'` 形式：返回被执行的脚本字符串，否则 None。"""
    tokens = _strip_prefix(tokens)
    if not tokens or os.path.basename(tokens[0]) not in SHELLS:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            return tokens[index + 1] if index + 1 < len(tokens) else None
    return None


# F6 原文兜底：命令原文里 handoffd / handoffd_cli.py 后面（允许夹 --config X）紧跟子命令 on|adopt，不管它藏在 $( )、eval 还是引号里。
# 宁可多拦 echo "handoffd on" 这类文字，也不让命令替换 / eval / 直接调 handoffd_cli.py 绕过；子命令位置之外的词（如 notify 文案里的 on）不拦。
OWNER_ONLY_TEXT = re.compile(r"handoffd(?:_cli\.py)?(?:\s+--config(?:=\S+|\s+\S+))*\s+(?:on|adopt)\b")


def invokes_owner_only(command, depth=0):
    """Bash 命令是否要执行 `handoffd on|adopt`：token 判定（命令位置，含 bash -c 套壳）或原文正则兜底，任一命中即拦。"""
    if not isinstance(command, str):
        return False
    return bool(OWNER_ONLY_TEXT.search(command)) or _invokes_owner_only_tokens(command, depth)


def _invokes_owner_only_tokens(command, depth=0):
    """Bash 命令里是否有「命令位置上的」`handoffd on|adopt`（含 bash -c 套壳）。
    只看命令词，不看引号里的文字：`echo "handoffd on"`、`grep 'handoffd on' x` 不算。"""
    if not isinstance(command, str) or depth > MAX_DEPTH:
        return False
    commands = split_commands(command)
    if commands is None:
        return False
    for tokens in commands:
        call = handoffd_call(tokens)
        if call and call[0] in OWNER_ONLY_SUBCOMMANDS:
            return True
        payload = _shell_payload(tokens)
        if payload and _invokes_owner_only_tokens(payload, depth + 1):
            return True
    return False


def has_shell_meta(command):
    """命令里是否有 shell 元字符或展开（F3）：有就不能按「字面」匹配放行。按最小集合实现：
    任何位置：反引号、`$`、反斜杠、换行 / 回车 / 制表、`!`；
    引号外：`; & | > < ( ) { } * ? ~ [ ] #` 与空白以外的控制字符；
    单引号内全部按字面（无展开），双引号内只看上面「任何位置」那组；引号不闭合 ⇒ 视为有元字符。
    （`env -S "…"`、`$(…)`、`;`、重定向、通配、花括号展开等都落在这个集合里。）"""
    anywhere = set("`$\\\n\r\t!")
    outside = set(";&|><(){}*?~[]#")
    quote = None
    for char in command:
        if char in anywhere and quote != "'":
            return True
        if quote is None:
            if char in outside or (ord(char) < 32):
                return True
            if char in "\"'":
                quote = char
        elif char == quote:
            quote = None
    return quote is not None


def literal_handoffd(command):
    """B 的 Bash 放行用的「字面」匹配（F3）：先拒含元字符的命令，再要求命令词是裸 `handoffd` 或本安装的绝对路径。
    返回 (子命令, 参数列表) 或 None；`--config X` 只在子命令之前被跳过。"""
    if not isinstance(command, str) or has_shell_meta(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or not (tokens[0] == "handoffd" or (os.path.isabs(tokens[0])
                                                      and os.path.realpath(tokens[0]) == os.path.realpath(INSTALLED_HANDOFFD))):
        return None
    rest = tokens[1:]
    if len(rest) >= 2 and rest[0] == "--config":
        rest = rest[2:]
    if not rest:
        return None
    return rest[0], rest[1:]


def is_new_long_task(tool_name, tool_input):
    """「新长任务」：Agent / Workflow / Monitor / CronCreate / ScheduleWakeup，或 run_in_background 的 Bash。"""
    if tool_name in NEW_LONG_TASK_TOOLS:
        return True
    return bool(tool_name == "Bash" and isinstance(tool_input, dict) and tool_input.get("run_in_background") is True)


def decide_tool(role, tool_name, tool_input, over_threshold, b_verify_commands=()):
    """PreToolUse 决定：返回 (True, None) 放行，或 (False, 原因) 拒绝。

    放行依据只有三种：
    1. 只读工具（READ_ONLY_TOOLS，不看角色）；
    2. 角色 ∈ {holder, holder_sub, b}——这些角色只在「登记册槽位的进程 == 本进程（或祖先链）」时才会产出（baton.classify，不变量 I）：
       holder / holder_sub 放行一切，但上下文已过第一档（默认 65%）时拦「新长任务」；
       b 只放行 SendMessage、handoffd 白名单（sign in / status / notify / reply；命令词须是裸名或安装绝对路径）与配置里逐字一致的 b_verify_commands；
    3. fail_open（取不到本进程身份 / 会话号，规格:79、K2）——唯一不看登记册的放行，单列在此。
    其余角色（spectator / blocked / not_me）只有 1；旁观会话连 Bash 都没有（规格:82 只要求读、答；状态见启动提示与拒绝文案）。"""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if role == "fail_open":
        return True, None  # 规格:79 / K2：本进程身份取不到，放行（唯一的 fail-open 分支）
    if role in ("holder", "holder_sub"):
        if over_threshold and is_new_long_task(tool_name, tool_input):
            return False, "上下文已过提醒档：此后只准收尾与交接，不要再开新的长任务（Agent / Workflow / Monitor / 定时任务 / 后台 Bash）。"
        return True, None
    if tool_name in READ_ONLY_TOOLS:
        return True, None
    if role == "b":
        if tool_name == "SendMessage":
            return True, None
        command = tool_input.get("command") if tool_name == "Bash" else None
        if isinstance(command, str):
            if command in tuple(b_verify_commands):
                return True, None
            if b_may_run_handoffd(literal_handoffd(command)):
                return True, None
        return False, "交接期间你是接手方（B）：只能读、SendMessage、handoffd sign in / status / notify / reply，以及配置里逐字列出的实测命令。"
    return False, "你是只读旁观会话（本项目已有主控会话，或交接进行中）：只能读、答，不能改文件、不能执行 Bash、不参与交接。"
