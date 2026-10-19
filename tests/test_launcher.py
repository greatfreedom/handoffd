# 测试 S1、S2：开窗启动器 open_warp.sh（假 claude、假 open、临时 HOME 里的 .zshrc、临时 Warp 标签页配置目录；不会真的开窗）。
# S2a：open_terminal.sh → open_warp.sh：开 Terminal 改为写 Warp 标签页配置 + open -g warp://tab_config/<名>?new_window=true；--prompt-file 可选；窗口里另清 MAIN_LOCK_OVERRIDE、GIT_*（G2）。
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import MVP_ROOT, require, run_cases, temp_dir, write_exe  # noqa: E402

LAUNCHER = os.path.join(MVP_ROOT, "bin", "open_warp.sh")


def fake_claude(path, version_text, trace=None):
    """写假 claude：--version 打印给定文本；否则把参数写进 trace。"""
    body = "#!/bin/sh\n"
    body += "if [ \"$1\" = --version ]; then echo '%s'; exit 0; fi\n" % version_text
    if trace:
        body += "printf '%%s\\n' \"$*\" >> \"%s\"\n" % trace
        body += "env | grep '^CLAUDE' >> \"%s.env\"\n" % trace
    body += "exit 0\n"
    write_exe(path, body)


class Fixture:
    """一套隔离的启动器测试环境。"""

    def __init__(self, work):
        self.work = work
        self.bin = os.path.join(work, "fake bin")
        self.target_dir = os.path.join(work, "target with space")
        self.target = os.path.join(self.target_dir, "claude")
        self.decoy = os.path.join(self.bin, "claude")
        self.opened = os.path.join(work, "opened.log")
        self.trace = os.path.join(work, "model-trace")
        self.prompt = os.path.join(work, "prompt.txt")
        self.commands = os.path.join(work, "out")
        self.home = os.path.join(work, "home")
        self.tabs = os.path.join(work, "warp tabs")
        self.launches = os.path.join(work, "warp launches")
        os.makedirs(self.home)
        with open(self.prompt, "w", encoding="utf-8") as stream:
            stream.write("offline prompt")
        with open(os.path.join(self.home, ".zshrc"), "w", encoding="utf-8") as stream:
            stream.write('claude() { export CDM_TEST_KEY=present; command "$CDM_FAKE_INIT" "$@"; }\n')
        self.set_open_status(0)
        fake_claude(self.decoy, "Claude Code 2.0.67")

    def set_open_status(self, status):
        """假 open：记录每次调用参数并返回给定退出码。"""
        write_exe(os.path.join(self.bin, "open"), "#!/bin/sh\nprintf '%%s\\n' \"$*\" >> \"%s\"\nexit %d\n" % (self.opened, status))

    def open_calls(self):
        """假 open 被调用了几次。"""
        if not os.path.exists(self.opened):
            return 0
        with open(self.opened, encoding="utf-8") as stream:
            return len(stream.read().splitlines())

    def env(self, **extra):
        """启动器运行环境：PATH 前置假 bin，CDM_OPEN_BIN 指向假 open。"""
        base = dict(
            os.environ, HOME=self.home, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
            CDM_OPEN_BIN=os.path.join(self.bin, "open"), CDM_WARP_TAB_DIR=self.tabs, CDM_WARP_LAUNCH_DIR=self.launches,
            CDM_FAKE_INIT=self.decoy, PYTHONDONTWRITEBYTECODE="1",
        )
        base.update(extra)
        return base

    def launch(self, name="safe-name", min_version="2.1.284", required="CDM_TEST_KEY", extra_args=(), env=None, directory=None,
               claude_bin=None, with_prompt=True, script_args=()):
        """运行启动器，返回 CompletedProcess。"""
        args = [
            "bash", LAUNCHER, "--name", name, "--dir", directory or self.work,
            "--command-dir", self.commands, "--min-version", min_version, "--required-env", required,
            "--claude-bin", claude_bin or self.target,
        ]
        if with_prompt:
            args.extend(["--prompt-file", self.prompt])
        args.extend(script_args)
        for extra in extra_args:
            args.extend(["--extra-arg", extra])
        return subprocess.run(args, capture_output=True, text=True, env=env or self.env(), timeout=60)


def case_s1():
    """S1：版本门槛、退出码契约、环境检查与窗口内复检。"""
    details = []
    with temp_dir() as work:
        fx = Fixture(work)

        for version in ("2.0.67", "2.1.283"):
            fake_claude(fx.target, "Claude Code " + version)
            result = fx.launch()
            require(result.returncode == 2 and fx.open_calls() == 0, "版本 %s 应退出 2 且不调用 open（退出码 %r，open %d 次）" % (version, result.returncode, fx.open_calls()))
        details.append("版本 2.0.67、2.1.283 → 退出码 2，open 调用 0 次")

        for version in ("2.1.284", "2.1.290"):
            before = fx.open_calls()
            fake_claude(fx.target, "Claude Code %s (build 9.9.9)" % version, trace=fx.trace)
            result = fx.launch(name="ok-" + version)
            require(result.returncode == 0, "版本 %s 应退出 0：%r %s" % (version, result.returncode, result.stderr))
            require(fx.open_calls() == before + 1, "版本 %s 应恰好调用 open 一次" % version)
            lines = result.stdout.splitlines()
            require(lines[0] == "CDM_LAUNCHER_VERSION=" + version, "版本行不对：%r" % lines)
            require(lines[1] == "CDM_LAUNCHER_TARGET=" + os.path.realpath(fx.target), "目标行不对：%r" % lines)
            require(lines[2].startswith("CDM_LAUNCHER_COMMAND=") and lines[2].endswith("ok-%s.command" % version), "命令行不对：%r" % lines)
            require(lines[3] == "CDM_LAUNCHER_TAB=" + os.path.join(fx.tabs, "ok-%s.toml" % version), "标签页配置行不对：%r" % lines)
        details.append("版本 2.1.284、2.1.290 → 各调用 open 1 次，输出四行 CDM_LAUNCHER_*（取第一个 x.y.z）")
        with open(fx.opened, encoding="utf-8") as stream:
            last_open = stream.read().splitlines()[-1]
        require(last_open == "-g warp://tab_config/ok-2.1.290?new_window=true", "open 参数应是 -g warp://tab_config/<名>?new_window=true：%r" % last_open)
        with open(os.path.join(fx.tabs, "ok-2.1.290.toml"), encoding="utf-8") as stream:
            tab = stream.read()
        command_path = os.path.join(fx.commands, "ok-2.1.290.command")
        require('name = "ok-2.1.290"' in tab and 'type = "terminal"' in tab and ('directory = "%s"' % work) in tab
                and 'commands = ["exec ' in tab and command_path.replace(" ", "\\ ") in tab, "Warp 标签页配置内容不对：%r" % tab)
        details.append("open 参数 = -g warp://tab_config/<名>?new_window=true；标签页配置含 name / directory / exec <命令文件>")

        before = fx.open_calls()
        fake_claude(fx.target, "Claude Code (no version here)")
        result = fx.launch()
        require(result.returncode == 2 and fx.open_calls() == before, "输出无版本号应退出 2 且不调用 open")
        details.append("输出无版本号 → 退出码 2，不调用 open")

        fake_claude(fx.target, "Claude Code 2.1.290", trace=fx.trace)
        fx.set_open_status(1)
        before = fx.open_calls()
        result = fx.launch(name="open-fails")
        require(result.returncode == 3 and fx.open_calls() == before + 1, "open 返回非零应退出 3：%r" % result.returncode)
        require(result.stdout.startswith("CDM_LAUNCHER_VERSION=2.1.290"), "退出 3 时也应输出版本行")
        fx.set_open_status(0)
        details.append("open 返回非零 → 退出码 3（open 已被调用 1 次）")

        # 移植 poc/q7_launcher_check.py 的其余断言
        before = fx.open_calls()
        result = fx.launch(name="init-fails", env=fx.env(CDM_FAKE_INIT=os.path.join(work, "missing-init")))
        require(result.returncode == 2 and fx.open_calls() == before, "登录 shell 初始化失败应阻止 open")
        result = fx.launch(name="missing-env", required="CDM_NO_SUCH_VAR")
        require(result.returncode == 2 and fx.open_calls() == before, "缺少必需环境变量应阻止 open")
        for bad_name in ("bad name", "a/b", ""):
            result = fx.launch(name=bad_name)
            require(result.returncode == 2 and fx.open_calls() == before, "非法名字 %r 应阻止 open" % bad_name)
        result = fx.launch(name="bad-extra", extra_args=("--x y",))
        require(result.returncode == 2 and fx.open_calls() == before, "非法额外参数应阻止 open")
        result = fx.launch(name="bad-min", min_version="2.x")
        require(result.returncode == 2 and fx.open_calls() == before, "非法最低版本应阻止 open")
        details.append("初始化失败 / 缺环境变量 / 非法名字 / 非法额外参数 / 非法最低版本 → 退出码 2，不调用 open")

        result = fx.launch(name="pinned", extra_args=("--model", "opus"))
        require(result.returncode == 0, "定位用例没有通过预检：%s" % result.stderr)
        command = os.path.join(fx.commands, "pinned.command")
        with open(command, encoding="utf-8") as stream:
            text = stream.read()
        require("CDM_TARGET" in text and os.path.realpath(fx.target).replace(" ", "\\ ") in text, "命令文件没有钉死解析后的目标路径")
        require("CDM_MIN_VERSION" in text and "2.1.284" in text, "命令文件没有带最低版本复检")
        details.append("目标路径按绝对路径钉死（含空格转义），PATH 里的旧版 claude 不会被用到")

        # 窗口内：环境清理 + 启动参数
        run_window = subprocess.run(["bash", command], capture_output=True, text=True,
                                    env=fx.env(CLAUDE_CODE_TEST="leak", CDM_FAKE_INIT=fx.decoy), timeout=60)
        require(run_window.returncode == 0, "窗口命令没有正常运行：%r %s" % (run_window.returncode, run_window.stderr))
        with open(fx.trace, encoding="utf-8") as stream:
            last_call = stream.read().splitlines()[-1]
        require(last_call == "--model opus --name pinned offline prompt", "窗口里启动参数不对：%r" % last_call)
        env_file = fx.trace + ".env"
        leaked = os.path.exists(env_file) and "CLAUDE_CODE_TEST" in open(env_file, encoding="utf-8").read()
        require(not leaked, "窗口没有清掉继承的 CLAUDE* 变量")
        details.append("窗口内启动参数 = 额外参数 + --name + 提示词文件内容；继承的 CLAUDE* 变量已清除")

        # 预检通过后目标被换成旧版：窗口内复检必须阻止模型启动
        os.unlink(fx.trace)
        fake_claude(fx.target, "Claude Code 2.0.67", trace=fx.trace)
        run_window = subprocess.run(["bash", command], capture_output=True, text=True, env=fx.env(), timeout=60)
        require(run_window.returncode != 0 and not os.path.exists(fx.trace), "目标被换成旧版后窗口仍启动了模型")
        details.append("预检后目标被换成 2.0.67：窗口内复检失败，未启动模型")
    return details


def case_s2():
    """S2：--claude-bin 指向文件名不是 claude 的可执行文件时，启动器以它为目标；缺少或相对路径则预检失败。"""
    with temp_dir() as work:
        fx = Fixture(work)
        custom = os.path.join(fx.target_dir, "claude-custom")
        fake_claude(custom, "Claude Code 2.1.290", trace=fx.trace)
        result = fx.launch(name="custom", claude_bin=custom)
        require(result.returncode == 0, "自定义文件名应通过预检：%r %s" % (result.returncode, result.stderr))
        require(result.stdout.splitlines()[1] == "CDM_LAUNCHER_TARGET=" + os.path.realpath(custom), "目标应是 --claude-bin 指定的文件：%r" % result.stdout)
        with open(os.path.join(fx.commands, "custom.command"), encoding="utf-8") as stream:
            require(os.path.realpath(custom).replace(" ", "\\ ") in stream.read(), "命令文件应钉死自定义文件")
        before = fx.open_calls()
        result = fx.launch(name="relative", claude_bin="claude-custom")
        require(result.returncode == 2 and fx.open_calls() == before, "相对路径应预检失败")
        result = fx.launch(name="missing", claude_bin=os.path.join(fx.target_dir, "no-such-claude"))
        require(result.returncode == 2 and fx.open_calls() == before, "文件不存在应预检失败")
        args = ["bash", LAUNCHER, "--name", "nobin", "--dir", work, "--prompt-file", fx.prompt, "--command-dir", fx.commands,
                "--min-version", "2.1.284", "--required-env", "CDM_TEST_KEY"]
        missing_option = subprocess.run(args, capture_output=True, text=True, env=fx.env(), timeout=60)
        require(missing_option.returncode == 2 and fx.open_calls() == before, "缺少 --claude-bin 应预检失败")
    return ["目标 = --claude-bin 指定的文件（文件名不必是 claude）；相对路径、不存在、缺少该选项都退出 2 且不调用 open"]


def case_h37():
    """H37（G2、规格:22）：open_warp.sh——不带 --prompt-file 也能开（B 先空闲，开场指令后到）；窗口命令清掉 CLAUDE*、MAIN_LOCK_OVERRIDE、GIT_*；
    标签页配置写不进去 ⇒ 预检失败（退出 2、不调 open）；标签页配置里 exec 的是命令文件；窗口里无提示词时 claude 的参数里没有提示词。"""
    details = []
    with temp_dir() as work:
        fx = Fixture(work)
        fake_claude(fx.target, "Claude Code 2.1.290", trace=fx.trace)
        result = fx.launch(name="no-prompt", with_prompt=False, extra_args=("--session-id=11111111-2222-4333-8444-555555555555",))
        require(result.returncode == 0 and fx.open_calls() == 1, "不带 --prompt-file 应能开：%r %s" % (result.returncode, result.stderr))
        command = os.path.join(fx.commands, "no-prompt.command")
        window = subprocess.run(["bash", command], capture_output=True, text=True, timeout=60,
                                env=fx.env(CLAUDE_CODE_TEST="leak", MAIN_LOCK_OVERRIDE="1", GIT_DIR="/x/.git", GIT_WORK_TREE="/x", GIT_INDEX_FILE="/x/i",
                                           CDM_FAKE_INIT=fx.decoy))
        require(window.returncode == 0, "窗口命令没有正常运行：%r %s" % (window.returncode, window.stderr))
        with open(fx.trace, encoding="utf-8") as stream:
            last_call = stream.read().splitlines()[-1]
        require(last_call == "--session-id=11111111-2222-4333-8444-555555555555 --name no-prompt", "无提示词时参数应只有额外参数与 --name：%r" % last_call)
        details.append("无 --prompt-file：窗口里 claude 参数 = --session-id=<预分配> --name <名>，没有提示词")
        # G2：假 claude 记录环境里以 CLAUDE / MAIN_LOCK / GIT_ 开头的变量
        leak_log = os.path.join(work, "leak.env")
        write_exe(fx.target, "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'Claude Code 2.1.290'; exit 0; fi\nenv | grep -E '^(CLAUDE|GIT_|MAIN_LOCK_OVERRIDE)' > \"%s\"\nexit 0\n" % leak_log)
        window = subprocess.run(["bash", command], capture_output=True, text=True, timeout=60,
                                env=fx.env(CLAUDE_CODE_TEST="leak", MAIN_LOCK_OVERRIDE="1", GIT_DIR="/x/.git", GIT_WORK_TREE="/x", GIT_INDEX_FILE="/x/i",
                                           CDM_FAKE_INIT=fx.decoy))
        require(window.returncode == 0, "窗口命令失败：%s" % window.stderr)
        require(not os.path.exists(leak_log) or open(leak_log, encoding="utf-8").read().strip() == "", "窗口环境里仍有 CLAUDE* / MAIN_LOCK_OVERRIDE / GIT_*：%r" % open(leak_log, encoding="utf-8").read())
        details.append("G2：窗口环境里 CLAUDE* / MAIN_LOCK_OVERRIDE / GIT_* 全清")
        # 标签页配置目录写不进去 ⇒ 退出 2、不调 open
        before = fx.open_calls()
        blocker = os.path.join(work, "blocker")
        with open(blocker, "w", encoding="utf-8") as stream:
            stream.write("file, not dir")
        fake_claude(fx.target, "Claude Code 2.1.290", trace=fx.trace)
        result = fx.launch(name="tab-fails", env=fx.env(CDM_WARP_TAB_DIR=os.path.join(blocker, "sub")))
        require(result.returncode == 2 and fx.open_calls() == before, "标签页配置写失败应退出 2 且不调 open：%r" % result.returncode)
        details.append("标签页配置写失败 → 退出 2，open 调用 0 次")
    return details


def main():
    """运行 S1、S2、H37。"""
    return run_cases([("S1", case_s1), ("S2", case_s2), ("H37", case_h37)])


if __name__ == "__main__":
    sys.exit(main())
