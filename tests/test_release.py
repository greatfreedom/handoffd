# 测试 X1–X9：公开发布前修改的产品默认值与已知问题（不起 claude 会话、不开真窗口、不出声）。
#   X1 required_env 默认为空：默认配置不要求任何环境变量；启动器收到空的 --required-env 照常放行
#   X2 接手方 B（及旁观 / 被拦会话）可用只读工具 ListAgents：B 才找得到交出方 A 的会话名；写类工具仍拒绝
#   X3 默认铃声是 macOS 系统声音（Glass / Ping，文件真实存在），默认不挂人声目录；仓库不带 assets/tones、assets/voice 里的音频
#   X4 兜底清扫只认登记册：开窗时登记；没登记册就不列 ~/.warp；登记册指向别处 / 别的项目前缀的路径不删；同前缀但没登记的用户文件不删（阳性对照：收紧前的旧规则会选中它）
#   X5 docs/INSTALL.md 的可执行步骤（卸载脚本对安装根路径的写法——// 或 /./ 或尾斜杠——容忍，旧脚本一条都删不掉）：文档里的「只撤销 handoffd 钩子」脚本按安装时的确切条目匹配（不是子串），真能在样例 settings 上跑通并保留别的钩子（含提到 hook_relay.py 路径的用户 logger 钩子）；首次备份不被覆盖；关键的安装 / 卸载要点（备份、statusLine 必需与恢复、权限合并、再次 off 后确认 launchd 任务、chmod +x、PATH 验证）都写了，README 不再把状态栏转接说成可选
#   X6 hooks/hooks.json 启动层 fail-open：入口脚本缺失 / 不可读 ⇒ 11 个钩子都退出 0 且无输出（阳性对照：旧写法 python3 <缺失脚本> 退出 2，会阻断 UserPromptSubmit / PreToolUse）；脚本存在时 stdin / stdout / 退出码原样透传
#   X7 manual 下完全不碰 ~/.warp（有过期登记项也不 lexists / stat / unlink；阳性对照：auto 下同一文件被删）
#   X8 登记册任一条目不合法 ⇒ 整个登记册视为损坏、一个文件都不删、只记 events（阳性对照：只含合法条目的登记册照常删；旧实现会用其余条目继续删）
#   X9 docs/TERMINALS.md 写的开窗契约与源码一致（daemon 实际传给启动器的选项、45 秒超时、重试 / 确认超时默认值），README 有 Usage 与「换用其他终端」两节且指向它（阳性对照：文档少列一个选项就能被发现）
import json
import os
import re
import subprocess
import sys
import textwrap

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import patched_env, require, run_cases, temp_dir  # noqa: E402
from swap_helpers import scenario, start_handoff  # noqa: E402
from test_launcher import Fixture, fake_claude  # noqa: E402
from cdm import config, daemon as daemon_module, policy  # noqa: E402


def case_x1():
    """X1：required_env 默认 []（不再要求个人环境变量）；空的 --required-env 不挡开窗。"""
    with temp_dir() as work:
        cfg = config.default_config(work)
        require(cfg["required_env"] == [], "required_env 默认应为空列表：%r" % cfg["required_env"])
        fx = Fixture(work)
        fake_claude(fx.target, "Claude Code 2.1.290", trace=fx.trace)
        result = fx.launch(name="no-env", required="")
        require(result.returncode == 0 and fx.open_calls() == 1, "空 required_env 应放行并调用 open 一次：%r %s" % (result.returncode, result.stderr))
    return ["默认配置 required_env == []；启动器 --required-env '' → 退出 0，open 1 次"]


def case_x2():
    """X2：ListAgents 是只读工具（只列出可 SendMessage 的会话），B 角色必须放行；它不得因此放开别的工具。"""
    for role in ("b", "spectator", "blocked", "not_me"):
        allowed, reason = policy.decide_tool(role, "ListAgents", {}, False)
        require(allowed, "角色 %s 调用 ListAgents 应放行：%r" % (role, reason))
    for tool in ("Write", "Edit", "Task", "ListAgentsX"):
        allowed, _reason = policy.decide_tool("b", tool, {}, False)
        require(not allowed, "B 调用 %s 仍应拒绝" % tool)
    return ["b / spectator / blocked / not_me 调 ListAgents 放行；b 调 Write / Edit / Task / ListAgentsX 仍拒绝"]


def case_x3():
    """X3：发布版默认铃声 = 系统自带 Glass / Ping（不分发音频），默认不挂人声；仓库里不能有 mp3 / m4a 之外的真实音频被误带入。"""
    defaults = config.default_config("/tmp/x")["alerts"]
    system_sounds = ["/System/Library/Sounds/Glass.aiff", "/System/Library/Sounds/Ping.aiff"]
    for kind in ("waiting", "problem"):
        require(defaults[kind]["sounds"] == system_sounds and defaults[kind]["voice_clips_dir"] is None, "%s 默认值不对：%r" % (kind, defaults[kind]))
    if sys.platform == "darwin":
        for path in system_sounds:
            require(os.path.isfile(path), "macOS 应自带 %s" % path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    require(not os.path.exists(os.path.join(root, "assets", "tones")), "发布版不得带 assets/tones")
    shipped = [name for _base, _dirs, files in os.walk(os.path.join(root, "assets")) for name in files if name.endswith((".mp3", ".m4a", ".aiff", ".wav"))]
    require(not shipped, "assets/ 下不得有音频文件：%r" % shipped)
    return ["waiting / problem 默认 sounds = Glass、Ping（系统文件存在），voice_clips_dir = None；assets/ 下无音频"]


def case_x4():
    """X4：Warp 配置兜底清扫只认登记册（发布前审查 P0：旧规则凭前缀 + 扩展名 + mtime 会误删用户自己的 <prefix>-handoff-*.yaml）。"""
    details = []
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
        launch_dir = os.path.join(home, ".warp", "launch_configurations")
        os.makedirs(launch_dir)
        now = sc.clock.ms / 1000.0
        mine = os.path.join(launch_dir, "proj-handoff-notes.yaml")      # 用户自己的文件，恰好与开窗前缀同名开头
        with open(mine, "w", encoding="utf-8") as stream:
            stream.write("user's own launch configuration")
        os.utime(mine, (now - 5000, now - 5000))
        # 阳性对照：收紧前的旧规则（前缀 + 扩展名 + mtime）会选中这个文件
        prefix = sc.daemon().cfg["session_name_prefix"] + "-handoff-"
        old_rule = os.path.basename(mine).startswith(prefix) and mine.endswith((".yaml", ".toml")) and now - os.path.getmtime(mine) > 120
        require(old_rule, "前提：旧规则会选中这个用户文件（否则本用例测不出区别）")
        # ① 没有登记册：清扫不得列任何 Warp 配置目录，用户文件原封不动
        listed = []
        real_listdir = os.listdir

        def spy(path="."):
            if os.path.abspath(str(path)).startswith(os.path.join(home, ".warp")):
                listed.append(str(path))
            return real_listdir(path)

        dm = sc.daemon()
        os.listdir = spy
        try:
            for _ in range(2):
                sc.tick(dm)
        finally:
            os.listdir = real_listdir
        require(os.path.exists(mine) and not listed and not sc.events("daemon:warp_config_swept"), "没有登记册：不列目录、不删用户文件：%r" % listed)
        details.append("无登记册：不列 ~/.warp、同前缀用户文件不删（旧规则会删）")
        # ② 登记册被改成指向别处 / 别的项目前缀：不认
        elsewhere = os.path.join(home, "elsewhere", "proj-handoff-zzz.yaml")
        os.makedirs(os.path.dirname(elsewhere))
        other = os.path.join(launch_dir, "other-handoff-zzz.yaml")
        for path in (elsewhere, other):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("x")
            os.utime(path, (now - 5000, now - 5000))
            daemon_module.register_warp_config(sc.sdir, path, sc.clock.ms)
        sc.tick(dm)
        require(os.path.exists(elsewhere) and os.path.exists(other) and os.path.exists(mine), "登记册指向目录外 / 别的项目前缀：不删；未登记的用户文件仍在")
        details.append("登记册指向 ~/.warp 之外或别的项目前缀的路径：不删")
    # ③ 开窗时由 _launch 登记；登记过的过期文件才被清扫（阳性对照）
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
        a = sc.spawn("sA")
        dm = sc.daemon()
        start_handoff(sc, a)
        sc.tick(dm)
        expected = daemon_module.warp_config_path(sc.handoff()["b"]["name"], dm.cfg["warp_open"])
        registered = daemon_module.read_warp_registry(sc.sdir)
        require(expected in registered, "_launch 应在开窗前登记本次配置路径：%r 不在 %r" % (expected, registered))
        details.append("开窗时登记本次配置路径")
    return details


# e5bd4da 版 INSTALL.md 的卸载脚本（用 os.path.abspath 规范化卸载路径、安装条目保留原文）：只作阳性对照，证明下面的路径写法反例在旧脚本上是红的
OLD_UNINSTALL_SCRIPT = r'''
import json, os, sys
path, root = sys.argv[1], os.path.abspath(sys.argv[2])

def fill(value):
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", root)
    if isinstance(value, list):
        return [fill(item) for item in value]
    if isinstance(value, dict):
        return {key: fill(item) for key, item in value.items()}
    return value

def canon(hook):
    return json.dumps(hook, sort_keys=True, ensure_ascii=False)

with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as stream:
    shipped = json.load(stream)["hooks"]
ours = {canon(fill(hook)) for groups in shipped.values() for group in groups for hook in group["hooks"]}
with open(path, encoding="utf-8") as stream:
    settings = json.load(stream)
hooks = settings.get("hooks", {})
removed = 0
for event in list(hooks):
    groups = []
    for group in hooks[event]:
        kept = [hook for hook in group.get("hooks", []) if canon(hook) not in ours]
        removed += len(group.get("hooks", [])) - len(kept)
        group["hooks"] = kept
        if kept:
            groups.append(group)
    if groups:
        hooks[event] = groups
    else:
        del hooks[event]
if not hooks:
    settings.pop("hooks", None)
with open(path, "w", encoding="utf-8") as stream:
    json.dump(settings, stream, indent=2, ensure_ascii=False)
print("removed %d of %d handoffd hooks" % (removed, len(ours)))'''


def case_x5():
    """X5：INSTALL.md 的卸载脚本可执行且只删 handoffd 的钩子；安装 / 卸载要点齐全；README 不再说状态栏转接可选。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "INSTALL.md"), encoding="utf-8") as stream:
        doc = stream.read()
    with open(os.path.join(root, "README.md"), encoding="utf-8") as stream:
        readme = stream.read()
    for needle in ("Before you start: back up once", 'if [ -f "$F" ] && [ ! -e "$F.handoffd-backup" ]; then cp "$F" "$F.handoffd-backup"; fi', "is never overwritten",
                   "required, not optional", "merge, never replace", "restore the original value",
                   "chmod +x ~/.local/bin/handoffd", "command -v handoffd", "mkdir -p ~/.local/bin", "run `handoffd off` **again**", "launchctl list | grep local.handoffd",
                   "do **not** remove the plugin yet", "remove only the 11 hooks that you copied", "**and** in `statusLine`"):
        require(needle in doc, "INSTALL.md 缺：%s" % needle)
    require("cp .claude/settings.json .claude/settings.json.handoffd-backup" not in doc and doc.count("cp \"$F\" \"$F.handoffd-backup\"") == 1,
            "备份命令只能有「不存在才建」这一处（第二次备份会覆盖首次基线）")
    require("按官方规则只有退出码 2 会阻断" not in readme and "退出 0 时钩子仍可以靠输出的 JSON 决策阻断" in readme, "README 关于「退出码 2 阻断」的表述要限定在退出码规则内")
    require("optional but recommended" not in doc and "optionally point `statusLine`" not in readme, "状态栏转接不能再被说成可选")
    match = re.search(r"/usr/bin/python3 - \"\$S\" \"\$D\" <<'PY'\n(.*?)\n\s*PY\n", doc, re.S)
    require(match is not None, "INSTALL.md 里找不到卸载钩子的脚本")
    script = textwrap.dedent(match.group(1))
    # 样例：安装时写进去的 11 个钩子（按仓库自己的 hooks/hooks.json，把 ${CLAUDE_PLUGIN_ROOT} 换成「克隆目录」= 仓库根）+ 用户自己的钩子
    with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as stream:
        shipped = json.load(stream)["hooks"]
    ours_text = json.dumps(shipped).replace("${CLAUDE_PLUGIN_ROOT}", root)
    installed = json.loads(ours_text)
    own = {"type": "command", "command": "/bin/echo", "args": ["mine"]}
    logger = {"type": "command", "command": "/usr/bin/logger", "args": ["-t", "audit", root + "/cdm/hook_relay.py"]}     # 反例：用户自己的钩子，参数里只是提到该路径
    edited = json.loads(json.dumps(installed["SessionEnd"][0]["hooks"][0]))
    edited["timeout"] = 9                                                                                              # 反例：用户装完后改过的 handoffd 钩子条目 ⇒ 保守不动
    sample = {"model": "m", "hooks": {event: [dict(group, hooks=list(group["hooks"])) for group in groups] for event, groups in installed.items()}}
    sample["hooks"]["PreToolUse"].append({"matcher": "*", "hooks": [own, logger]})
    sample["hooks"]["SessionEnd"][0]["hooks"] = [edited]
    sample["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo keep-me"})
    old_rule_removes_logger = "cdm/hook_relay.py" in json.dumps(logger)
    require(old_rule_removes_logger, "前提：旧的子串规则会误删 logger 钩子（否则本用例测不出区别）")
    with temp_dir() as work:
        path = os.path.join(work, "settings.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(sample, stream)
        result = subprocess.run(["/usr/bin/python3", "-I", "-c", script, path, root], capture_output=True, text=True)   # -c 下 sys.argv[1:] 就是 path、root，和文档里 `python3 - "$S" "$D"` 一致
        require(result.returncode == 0, "卸载脚本应成功：%r" % result.stderr)
        require("removed 10 of 11 handoffd hooks" in result.stdout, "应报告删了 10 个（SessionEnd 那条被改过，保守不动）：%r" % result.stdout)
        with open(path, encoding="utf-8") as stream:
            after = json.load(stream)
    flat = [hook for groups in after["hooks"].values() for group in groups for hook in group["hooks"]]
    require(after["model"] == "m" and sorted(map(json.dumps, flat)) == sorted(map(json.dumps, [own, logger, edited, {"type": "command", "command": "echo keep-me"}])),
            "只该撤掉与安装时完全一致的 handoffd 钩子；用户自己的（含提到该路径的 logger）和改过的条目都保留：%r" % flat)
    # 路径写法反例（审查 R3 P1）：安装时用户把根路径写成带 // 或 /./ 的样子、卸载时传同一个目录（写法可以不同）⇒ 仍应删光 11 条，用户自己的钩子（含只提到路径的 logger）保留
    def hooks_installed_with(root_text):
        return json.loads(json.dumps(shipped).replace("${CLAUDE_PLUGIN_ROOT}", root_text))

    def run_script(script_text, hooks, argument):
        with temp_dir() as work2:
            path2 = os.path.join(work2, "settings.json")
            with open(path2, "w", encoding="utf-8") as stream:
                json.dump({"hooks": hooks}, stream)
            run2 = subprocess.run(["/usr/bin/python3", "-I", "-c", script_text, path2, argument], capture_output=True, text=True)
            require(run2.returncode == 0, "卸载脚本应成功：%r" % run2.stderr)
            with open(path2, encoding="utf-8") as stream:
                return run2.stdout, json.load(stream).get("hooks", {})

    variants = (("根路径含 //", root.replace("/", "//"), root), ("根路径含 /./", root + "/.", root),
                ("卸载时 D 带尾斜杠和 //", root, root + "//"), ("两边写法都不规范", root.replace("/", "//") + "/.", root + "/./"))
    for label, install_text, uninstall_arg in variants:
        hooks = hooks_installed_with(install_text)
        hooks["PreToolUse"].append({"matcher": "*", "hooks": [own, logger]})
        stdout2, left = run_script(script, hooks, uninstall_arg)
        flat2 = [hook for groups in left.values() for group in groups for hook in group["hooks"]]
        require("removed 11 of 11 handoffd hooks" in stdout2 and sorted(map(json.dumps, flat2)) == sorted(map(json.dumps, [own, logger])),
                "%s：同一个 D 应删光 11 条且只留用户自己的：%r %r" % (label, stdout2, flat2))
    old_out, _left = run_script(textwrap.dedent(OLD_UNINSTALL_SCRIPT), hooks_installed_with(variants[0][1]), variants[0][2])
    require("removed 0 of 11" in old_out, "阳性对照：e5bd4da 的旧脚本在「根路径含 //」时一条都删不掉（否则这些反例测不出区别）：%r" % old_out)
    return ["INSTALL.md 的卸载脚本只删与安装时完全一致的 10 个钩子，保留用户自己的（含提到 hook_relay.py 的 logger 钩子）和被改过的一条；备份命令不会覆盖首次基线；安装 / 卸载 / 更新要点齐全；README 不再说状态栏转接可选"]


def case_x6():
    """X6：钩子启动层 fail-open（发布前审查 P0：入口文件缺失时 python 退出 2，按官方规则会阻断 UserPromptSubmit / PreToolUse）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as stream:
        hooks = json.load(stream)["hooks"]
    require(len(hooks) == 11, "应有 11 个事件：%r" % sorted(hooks))
    entries = []
    for event, groups in hooks.items():
        for group in groups:
            for hook in group["hooks"]:
                entries.append((event, hook))
    require(len(entries) == 11, "每个事件一个钩子")
    scripts = set()
    for event, hook in entries:
        args = hook["args"]
        require(hook["type"] == "command" and hook["command"] == "/bin/sh" and args[0] == "-c" and args[2] == "handoffd-hook"
                and args[3] == "${CLAUDE_PLUGIN_ROOT}/cdm/hook_relay.py" and args[4] == event and len(args) == 5, "%s 钩子写法不对：%r" % (event, hook))
        scripts.add(args[1])
    require(len(scripts) == 1 and next(iter(scripts)).startswith('[ -r "$1" ] || exit 0;'), "11 个钩子共用同一段 fail-open 启动脚本：%r" % scripts)
    require(hooks["SessionEnd"][0]["hooks"][0].get("timeout") == 5, "SessionEnd 保留 timeout 5")

    def run(hook, plugin_root, stdin="{}", cwd=None, env_home=None):
        argv = [hook["command"]] + [arg.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root) for arg in hook["args"]]
        env = dict(os.environ, HOME=env_home or os.environ["HOME"])
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=cwd, env=env, timeout=30)

    with temp_dir() as work:
        empty_root = os.path.join(work, "no-such-plugin")
        for event, hook in entries:
            result = run(hook, empty_root)
            require(result.returncode == 0 and result.stdout == "", "%s：入口脚本不存在时应退出 0 且无输出：%r %r" % (event, result.returncode, result.stdout))
        # 阳性对照：旧写法（python3 直接开缺失的脚本）退出 2——正是会阻断会话的退出码
        old = subprocess.run(["/usr/bin/python3", os.path.join(empty_root, "cdm", "hook_relay.py"), "UserPromptSubmit"], input="{}", capture_output=True, text=True)
        require(old.returncode == 2, "前提：旧写法在入口缺失时退出 2（否则本用例测不出区别）：%r" % old.returncode)
        # 存在但不可读
        unreadable_root = os.path.join(work, "unreadable")
        os.makedirs(os.path.join(unreadable_root, "cdm"))
        script = os.path.join(unreadable_root, "cdm", "hook_relay.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("import sys\nsys.exit(2)\n")
        os.chmod(script, 0)
        try:
            result = run(entries[0][1], unreadable_root)
            require(result.returncode == 0, "入口脚本不可读：应退出 0：%r %r" % (result.returncode, result.stderr))
        finally:
            os.chmod(script, 0o644)
        # 脚本存在：stdin 原样进去，stdout / 退出码原样出来（不吞它的阻断决定）
        live_root = os.path.join(work, "live")
        os.makedirs(os.path.join(live_root, "cdm"))
        with open(os.path.join(live_root, "cdm", "hook_relay.py"), "w", encoding="utf-8") as stream:
            stream.write("import json, sys\nprint(json.dumps({'seen': sys.stdin.read(), 'event': sys.argv[1]}))\nsys.exit(3)\n")
        result = run(entries[7][1], live_root, stdin="payload")
        require(result.returncode == 3 and json.loads(result.stdout) == {"seen": "payload", "event": "PreToolUse"}, "脚本存在时应透传 stdin / stdout / 退出码：%r %r" % (result.returncode, result.stdout))
        # 真实入口：同一份仓库里的 hook_relay.py，空对象输入、临时 HOME 与项目目录，照常退出 0
        project = os.path.join(work, "project")
        os.makedirs(project)
        for event, hook in entries:
            result = run(hook, root, cwd=project, env_home=work)
            require(result.returncode == 0, "%s：真实入口应退出 0：%r %r" % (event, result.returncode, result.stderr))
    return ["11 个钩子共用 fail-open 启动脚本；入口不存在 / 不可读 ⇒ 退出 0 无输出（旧写法退出 2）；入口存在 ⇒ stdin / stdout / 退出码透传；真实入口 11 个事件空输入退出 0"]


def _stale_registered_file(sc, home):
    """造一个登记过的、已过期的临时配置文件（带本项目开窗前缀），返回路径。"""
    launch_dir = os.path.join(home, ".warp", "launch_configurations")
    os.makedirs(launch_dir, exist_ok=True)
    path = os.path.join(launch_dir, "proj-handoff-stale01.yaml")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("temp")
    now = sc.clock.ms / 1000.0
    os.utime(path, (now - 5000, now - 5000))
    return path


def case_x7():
    """X7（审查 R2 P1）：manual 下兜底清扫完全不碰 ~/.warp；回到 auto 才清。"""
    from cdm import sessions
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
        path = _stale_registered_file(sc, home)
        daemon_module.register_warp_config(sc.sdir, path, sc.clock.ms)
        sessions.write_mode(sc.sdir, "manual", 0)
        dm = sc.daemon()
        touched = []
        originals = {name: getattr(os.path, name) for name in ("lexists", "isfile", "getmtime")}
        original_unlink = os.unlink

        def spy(name):
            def wrapper(target, *args, **kwargs):
                if str(target).startswith(os.path.join(home, ".warp")):
                    touched.append((name, str(target)))
                return originals[name](target, *args, **kwargs)
            return wrapper

        def spy_unlink(target, *args, **kwargs):
            if str(target).startswith(os.path.join(home, ".warp")):
                touched.append(("unlink", str(target)))
            return original_unlink(target, *args, **kwargs)

        for name in originals:
            setattr(os.path, name, spy(name))
        os.unlink = spy_unlink
        try:
            for _ in range(3):
                sc.tick(dm)
        finally:
            for name, func in originals.items():
                setattr(os.path, name, func)
            os.unlink = original_unlink
        require(os.path.exists(path) and not touched and not sc.events("daemon:warp_config_swept"), "manual：不得碰 ~/.warp（lexists / stat / unlink）：%r" % touched)
        sessions.write_mode(sc.sdir, "auto", sc.clock.ms - 10_000)
        sc.tick(dm)
        require(not os.path.exists(path) and len(sc.events("daemon:warp_config_swept")) == 1, "阳性对照：auto 下同一个登记过的过期文件被清掉")
    return ["manual + 有过期登记项：文件保留、~/.warp 下零次 lexists / stat / unlink；auto 阳性对照：删除"]


def case_x8():
    """X8（审查 R2 P1）：登记册任一条目不合法 ⇒ 整个登记册视为损坏，一个文件都不删，只记 events。"""
    cases = (("条目值不是整数", {"paths": {"@VALID@": 123, "bad-entry": "invalid timestamp"}}),
             ("条目值是布尔", {"paths": {"@VALID@": 123, "other": True}}),
             ("paths 不是对象", {"paths": ["@VALID@"]}),
             ("顶层不是对象", ["@VALID@"]))
    for label, content in cases:
        with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
            path = _stale_registered_file(sc, home)
            registry = daemon_module.warp_registry_path(sc.sdir)
            with open(registry, "w", encoding="utf-8") as stream:
                json.dump(json.loads(json.dumps(content).replace("@VALID@", path)), stream)
            dm = sc.daemon()
            for _ in range(3):
                sc.tick(dm)
            require(os.path.exists(path) and not sc.events("daemon:warp_config_swept"), "%s：登记册损坏，不得删任何文件" % label)
            require(len(sc.events("daemon:warp_registry_corrupt")) == 1, "%s：只记一条 warp_registry_corrupt：%r" % (label, sc.events("daemon:warp_registry_corrupt")))
        with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
            path = _stale_registered_file(sc, home)
            with open(daemon_module.warp_registry_path(sc.sdir), "w", encoding="utf-8") as stream:
                stream.write("{坏 json")
            dm = sc.daemon()
            sc.tick(dm)
            require(os.path.exists(path) and len(sc.events("daemon:warp_registry_corrupt")) == 1, "坏 JSON：当作损坏，不删")
    with temp_dir() as home, patched_env(HOME=home, CDM_WARP_TAB_DIR=None, CDM_WARP_LAUNCH_DIR=None), scenario() as sc:
        path = _stale_registered_file(sc, home)
        daemon_module.register_warp_config(sc.sdir, path, sc.clock.ms)       # 阳性对照：只含合法条目的登记册照常清扫
        dm = sc.daemon()
        sc.tick(dm)
        require(not os.path.exists(path) and not sc.events("daemon:warp_registry_corrupt"), "阳性对照：合法登记册照常删")
    return ["条目值非整数 / 布尔、paths 非对象、顶层非对象、坏 JSON ⇒ 视为损坏：不删、只记一条 warp_registry_corrupt；合法登记册照常删（阳性对照）"]


def _documented_launcher_options(doc_text):
    """TERMINALS.md 英文部分「Arguments the daemon passes」表里列出的 --选项。"""
    table = doc_text.split("### Arguments the daemon passes")[1].split("### What the script must do")[0]
    return set(re.findall(r"^\| `(--[a-z-]+)` \|", table, re.M))


def case_x9():
    """X9：docs/TERMINALS.md 的开窗契约与源码一致；README 的 Usage / 换用其他终端两节在且互相指向。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "TERMINALS.md"), encoding="utf-8") as stream:
        doc = stream.read()
    with open(os.path.join(root, "README.md"), encoding="utf-8") as stream:
        readme = stream.read()
    captured = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(args, **kwargs):
        captured["args"], captured["timeout"] = args, kwargs.get("timeout")
        return Done()

    cfg = config.build_config({"project_dir": "/tmp/x", "claude_extra_args": ["--model=a-model"]})
    original = daemon_module.subprocess.run
    daemon_module.subprocess.run = fake_run
    try:
        daemon_module.run_launcher(cfg, "/tmp/x/S", {"a": {"sid": "sess-a"}, "b": {"name": "x-handoff-1", "uuid": "11111111-1111-1111-1111-111111111111"}})
    finally:
        daemon_module.subprocess.run = original
    args = captured["args"]
    require(args[:2] == ["/bin/bash", os.path.join(root, "bin", "open_warp.sh")], "启动器路径写死为 <安装目录>/bin/open_warp.sh：%r" % args[:2])
    passed = args[2::2]      # 选项名与值成对出现：/bin/bash 脚本 --选项 值 --选项 值 …
    require(all(item.startswith("--") for item in passed), "参数应是成对的 --选项 值：%r" % args)
    documented = _documented_launcher_options(doc)
    require(set(passed) == documented, "文档列的选项必须等于 daemon 实际传的：多 %r 少 %r" % (sorted(documented - set(passed)), sorted(set(passed) - documented)))
    require("--prompt-file" not in passed and "does **not** pass `--prompt-file`" in doc, "daemon 不传 --prompt-file，文档要写明")
    extras = [args[index + 1] for index, item in enumerate(args) if item == "--extra-arg"]
    require(extras[0].startswith("--session-id=") and extras[0].endswith("11111111-1111-1111-1111-111111111111") and "--model=a-model" in extras,
            "--extra-arg 的第一个是 --session-id=<B 的 uuid>，之后是 claude_extra_args：%r" % extras)
    require(captured["timeout"] == daemon_module.LAUNCHER_TIMEOUT_SECONDS == 45 and "45-second timeout" in doc and "45 秒" in doc, "超时 45 秒要与源码一致")
    require(cfg["preflight_retry_seconds"] == 60 and cfg["launch_confirm_timeout_seconds"] == 120 and "default 60" in doc and "default 120" in doc, "重试 / 确认超时默认值要与 config 一致")
    require(cfg["required_env"] == [] and "may be an empty string" in doc, "required_env 默认空，文档要写明可以是空字符串")
    # 阳性对照：文档少列一个选项，上面的比对就会发现
    shrunk = doc.replace("| `--warp-open` |", "| `--warp-xxx` |")
    require(_documented_launcher_options(shrunk) != set(passed), "前提：文档与源码不一致时本用例能发现")
    for needle in ("## Usage", "## Using another terminal", "docs/TERMINALS.md", "## 换用其他终端", "`!handoffd status`", "not** a `/` slash command", "planned for 0.2", "计划在 0.2", "Known limitation (0.1.0, “plan B”)", "按「方案 B」处理"):
        require(needle in readme or needle in doc, "缺：%s" % needle)
    require(readme.index("## Using another terminal") < readme.index("# 中文说明") < readme.index("## 换用其他终端"), "英文部分在前、中文部分在后")
    return ["TERMINALS.md 列的 8 个启动器选项 = daemon 实际传的；45 秒超时、重试 60 / 确认 120 默认值、required_env 可为空与源码一致；README 的 Usage 与换用其他终端（英文在前、中文在后）都在"]


CASES = [("X1", case_x1), ("X2", case_x2), ("X3", case_x3), ("X4", case_x4), ("X5", case_x5), ("X6", case_x6), ("X7", case_x7), ("X8", case_x8), ("X9", case_x9)]


def main():
    return run_cases(CASES)


if __name__ == "__main__":
    sys.exit(main())
