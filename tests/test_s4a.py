# 测试 H101–H106：handoffd S4a「B 的模型 / 档位跟 A 实时一致」（假世界 + 可控时钟；不起 claude 会话、不开真窗口、不出声）。
#   H101 opus + high 继承（走真 run_launcher，捕获启动器参数；launch 记录、events、status 一行）
#   H102 haiku 无 effort：只继承 model，不凭空造 effort
#   H103 快照缺失 / 读不出 / 字段非法 ⇒ 完全按 claude_extra_args，events 说明原因
#   H104 config 里已有 --model / --effort（等号与空格两种写法）被替换，不重复
#   H105 非法 model.id / effort.level（空格、;、$()、方括号位置不对、大写后缀、未知档位）拒绝继承
#   H106 [1m] 端到端：config / 启动器校验放行；生成的命令文件真实执行，假 claude 收到的 argv 逐字等于 claude-opus-5-5[1m]（含 rc 开了 GLOB_SUBST 的情形）
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import require, run_cases, temp_dir, write_exe  # noqa: E402
from swap_helpers import scenario, start_handoff  # noqa: E402
from test_launcher import Fixture  # noqa: E402
from cdm import config, daemon as daemon_module, util  # noqa: E402

OPUS = "claude-opus-5-5[1m]"
HAIKU = "claude-haiku-4-5-20251001"


class Done:
    returncode, stdout, stderr = 0, "", ""


def write_snapshot(sc, sid, model=None, effort=None, raw=None):
    """按 ctx.tap 的落盘位置 <S>/ctx/<会话号>.json 写 A 的状态栏快照。"""
    value = {"session_id": sid, "context_window": {"context_window_size": 1000000, "used_percentage": 10}}
    if model is not None:
        value["model"] = {"id": model, "display_name": "x"}
    if effort is not None:
        value["effort"] = {"level": effort}
    path = os.path.join(sc.sdir, "ctx", sid + ".json")
    util.atomic_write_text(path, raw if raw is not None else json.dumps(value))


def capturing_launcher(sc, captured):
    """走真 run_launcher，只把它调的 subprocess.run 换成捕获器（仅在调用期间换，不影响 daemon 别处的子进程）。"""
    def launcher(cfg, sdir, handoff):
        original = subprocess.run
        subprocess.run = lambda args, **kw: captured.append(list(args)) or Done()
        try:
            code, tail, version = daemon_module.run_launcher(cfg, sdir, handoff)
        finally:
            subprocess.run = original
        sc.world.launches.append(handoff["id"])
        return 0, tail, "2.1.290"
    return launcher


def extra_args_of(args):
    return [args[i + 1] for i, item in enumerate(args) if item == "--extra-arg"]


def launch_with(extra_cfg=None, snapshot=None, **snap_kw):
    """开一次 B，返回 (启动器 --extra-arg 列表, launch 记录, events 里的 launch_inherit 行, status 输出)。snapshot='raw:<文本>' 写原文；None 且无 snap_kw ⇒ 不写快照。"""
    with scenario(cfg_overrides={"claude_extra_args": list(extra_cfg or [])}) as sc:
        a = sc.spawn("sA")
        captured = []
        dm = sc.daemon(launcher_fn=capturing_launcher(sc, captured))
        hid, uuid = start_handoff(sc, a)
        if isinstance(snapshot, str):
            write_snapshot(sc, "sA", raw=snapshot)
        elif snap_kw:
            write_snapshot(sc, "sA", **snap_kw)
        sc.tick(dm)
        require(len(captured) == 1, "应恰好调用启动器一次：%r" % captured)
        events = sc.events("daemon:launch_inherit")
        require(len(events) == 1, "launch_inherit 事件应恰好一行：%r" % events)
        code, out, err = sc.cli(["status"], a)
        return extra_args_of(captured[0]), sc.handoff()["launch"], events[0], out


def case_h101():
    """H101：A 是 opus 5.5 + high ⇒ B 的启动参数带 --model=claude-opus-5-5[1m] 与 --effort=high（各一个 token）；launch 记录与 status 一行。"""
    extra, launch, event, status = launch_with(["--verbose"], model=OPUS, effort="high")
    require(extra[0].startswith("--session-id=") and "--verbose" in extra, "原有额外参数应保留：%r" % extra)
    require(extra.count("--model=" + OPUS) == 1 and extra.count("--effort=high") == 1, "应继承 model 与 effort 各一次：%r" % extra)
    require(launch["b_model"] == OPUS and launch["b_effort"] == "high" and launch["b_inherit"] == "snapshot", "launch 记录不对：%r" % launch)
    require(event["src"] == "snapshot" and event["model"] == OPUS and event["effort"] == "high", "events 说明不对：%r" % event)
    require("  B 模型：%s / 档位：high" % OPUS in status.splitlines(), "status 缺「B 模型：… / 档位：…」一行：%r" % status)
    return ["opus+high：--extra-arg 含 --model=%s、--effort=high、保留 --verbose；launch.b_model/b_effort 与 status 一行一致" % OPUS]


def case_h102():
    """H102：A 是 haiku、状态栏没有 effort（模型不支持）⇒ 只继承 model；config 里的 --effort 不动。"""
    extra, launch, event, status = launch_with(["--effort=medium"], model=HAIKU)
    require("--model=" + HAIKU in extra, "应继承 haiku：%r" % extra)
    require(not any(item.startswith("--effort=") and item != "--effort=medium" for item in extra) and "--effort=medium" in extra,
            "没有 effort 就不凭空造，config 的 --effort 原样保留：%r" % extra)
    require(launch["b_model"] == HAIKU and launch["b_effort"] is None, "launch 记录不对：%r" % launch)
    require("effort.level" in event["note"], "events 应说明 effort 没继承：%r" % event)
    require("B 模型：%s / 档位：未指定（按配置）" % HAIKU in status, "status 档位应显示未指定：%r" % status)
    return ["haiku 无 effort：只带 --model=%s；config 的 --effort=medium 保留；status 档位显示「未指定（按配置）」" % HAIKU]


def case_h103():
    """H103：快照缺失 / 空文件 / 非 JSON / 顶层不是对象 / model 与 effort 都非法 ⇒ 不继承，--extra-arg 与 config 逐项一致，events 写明原因。"""
    cfg_extra = ["--model", "opus", "--verbose"]
    cases = [("缺失", None, "不存在"), ("坏 JSON", "{not json", "读不出"), ("顶层数组", "[1,2]", "读不出"),
             ("字段全非法", json.dumps({"model": {"id": "a b"}, "effort": {"level": "ultra"}}), "model.id")]
    for label, raw, expect_note in cases:
        extra, launch, event, status = launch_with(cfg_extra, snapshot=raw)
        require(extra[1:] == cfg_extra, "%s：应完全按 claude_extra_args（除 --session-id）：%r" % (label, extra))
        require(event["src"] == "config" and expect_note in event["note"], "%s：events 说明不对：%r" % (label, event))
        require(launch["b_model"] is None and launch["b_effort"] is None and launch["b_inherit"] == "config", "%s：launch 记录不对：%r" % (label, launch))
    return ["快照缺失 / 坏 JSON / 顶层数组 / 字段全非法：启动参数与 config 逐项一致，events 写明 src=config 与原因"]


def case_h104():
    """H104：config 里已有 --model（空格写法）/ --model=x / --effort 两种写法 ⇒ 继承后被替换，最终每种参数恰好一份。"""
    for cfg_extra in (["--model", "opus", "--effort", "low", "--verbose"], ["--model=sonnet", "--effort=low", "--verbose"]):
        extra, launch, event, status = launch_with(cfg_extra, model=OPUS, effort="max")
        require(sum(1 for item in extra if item.startswith("--model")) == 1 and "--model=" + OPUS in extra, "--model 应只剩继承的一份：%r" % extra)
        require(sum(1 for item in extra if item.startswith("--effort")) == 1 and "--effort=max" in extra, "--effort 应只剩继承的一份：%r" % extra)
        require("opus" not in extra and "low" not in extra and "sonnet" not in extra and "--verbose" in extra, "旧值应连同值一起去掉、无关参数保留：%r" % extra)
    # 只继承 model（A 没有 effort）时，config 的 --effort 不能被误删
    extra, launch, event, status = launch_with(["--model", "opus", "--effort", "low"], model=HAIKU)
    require(extra[1:] == ["--effort", "low", "--model=" + HAIKU], "未继承的 effort 应保留、model 被替换：%r" % extra)
    return ["config 的 --model/--effort（空格与等号两种写法）被继承值替换且连值一起去掉；A 没有 effort 时 config 的 --effort 保留"]


def case_h105():
    """H105：非法 model.id / effort.level 拒绝继承（不进启动参数）。"""
    bad_models = ["a b", "x;rm", "$(id)", "a`b`", "m[1m]x", "m[1M]", "m[]", "[1m]", "m\n", "", 5, None, ["x"]]
    for bad in bad_models:
        snapshot = json.dumps({"model": {"id": bad}, "effort": {"level": "high"}})
        extra, launch, event, status = launch_with(["--verbose"], snapshot=snapshot)
        require(not any(item.startswith("--model") for item in extra), "非法 model.id %r 不得继承：%r" % (bad, extra))
        require(launch["b_model"] is None and "--effort=high" in extra and launch["b_effort"] == "high", "合法的 effort 仍应继承：%r %r" % (extra, launch))
    for bad in ("ultracode", "HIGH", "high ", "", 3, None):
        snapshot = json.dumps({"model": {"id": OPUS}, "effort": {"level": bad}})
        extra, launch, event, status = launch_with([], snapshot=snapshot)
        require(not any(item.startswith("--effort") for item in extra) and "--model=" + OPUS in extra, "非法 effort.level %r 不得继承：%r" % (bad, extra))
    require(daemon_module.MODEL_ID.match(OPUS) and daemon_module.MODEL_ID.match(HAIKU) and daemon_module.MODEL_ID.match("opus"), "合法形态应放行")
    return ["非法 model.id（空格 / ; / $() / 反引号 / 方括号位置或大小写不对 / 非字符串）与非法 effort（ultracode / 大写 / 空 / 非字符串）都不继承，另一项照常继承"]


def case_h106():
    """H106：[1m] 端到端。config.EXTRA_ARG 与 open_warp.sh 放行 `--model=claude-opus-5-5[1m]`、拒绝坏形态；生成的命令文件真实执行，
    假 claude 收到的参数逐字等于 claude-opus-5-5[1m]（命令文件用 printf %q 转义；rc 里开着 GLOB_SUBST 也不被当通配符）。"""
    model_arg = "--model=" + OPUS
    config.build_config({"project_dir": "/tmp/x", "claude_extra_args": [model_arg, "--effort=high"]})
    for bad in ("--model=a[1m]b", "--model=a[", "--model=a[1M]", "--model=a[1m][2m]", "--model=[1m]x y"):
        try:
            config.build_config({"project_dir": "/tmp/x", "claude_extra_args": [bad]})
        except config.ConfigError:
            continue
        raise AssertionError("config 不应放行 %r" % bad)
    with temp_dir() as work:
        fx = Fixture(work)
        argv_log = os.path.join(work, "argv.log")
        body = ("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'Claude Code 2.1.290'; exit 0; fi\n"
                "for a in \"$@\"; do printf '%%s\\n' \"$a\"; done > \"%s\"\nexit 0\n" % argv_log)
        write_exe(fx.target, body)
        before = fx.open_calls()
        bad = fx.launch(name="bad-bracket", extra_args=("--model=a[1m]b",))
        require(bad.returncode == 2 and fx.open_calls() == before, "启动器应拒绝 a[1m]b：%r" % bad.returncode)
        for glob_subst in (False, True):
            if glob_subst:
                with open(os.path.join(fx.home, ".zshrc"), "a", encoding="utf-8") as stream:
                    stream.write("setopt GLOB_SUBST\n")
            name = "bracket-%s" % glob_subst
            result = fx.launch(name=name, with_prompt=False, extra_args=("--session-id=11111111-2222-4333-8444-555555555555", model_arg, "--effort=high"))
            require(result.returncode == 0, "含 [1m] 的额外参数应通过启动器预检：%r %s" % (result.returncode, result.stderr))
            command = os.path.join(fx.commands, name + ".command")
            text = open(command, encoding="utf-8").read()
            require("claude-opus-5-5\\[1m\\]" in text, "命令文件里 [ ] 应被转义：%r" % [line for line in text.splitlines() if "CDM_EXTRA" in line])
            if os.path.exists(argv_log):
                os.unlink(argv_log)
            window = subprocess.run(["bash", command], capture_output=True, text=True, env=fx.env(CDM_FAKE_INIT=fx.decoy), timeout=60, cwd=work)
            require(window.returncode == 0, "窗口命令没有正常运行（GLOB_SUBST=%s）：%r %s" % (glob_subst, window.returncode, window.stderr))
            argv = open(argv_log, encoding="utf-8").read().split("\n")[:-1]
            require(argv == ["--session-id=11111111-2222-4333-8444-555555555555", model_arg, "--effort=high", "--name", name],
                    "claude 收到的参数不对（GLOB_SUBST=%s）：%r" % (glob_subst, argv))
            require(model_arg in argv and "--model=claude-opus-5-5[1m]" == argv[1], "[1m] 必须逐字保留")
    return ["config 放行 --model=%s、拒绝 a[1m]b / 大写 / 重复后缀；启动器同；命令文件执行后假 claude 的 argv 逐字 = [--session-id=…, --model=%s, --effort=high, --name, 名]；rc 开 GLOB_SUBST 也一样" % (OPUS, OPUS)]


def main():
    """运行 H101–H106。"""
    return run_cases([("H101", case_h101), ("H102", case_h102), ("H103", case_h103), ("H104", case_h104), ("H105", case_h105), ("H106", case_h106)])


if __name__ == "__main__":
    sys.exit(main())
