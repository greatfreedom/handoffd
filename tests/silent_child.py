# 静音闸验证（H61）：在子进程里用「真实的」notify.Notifier（不注入 runner）跑两条 CLI 路径——`handoffd off`（走到 run_daemon_once 里
# 新建的 Notifier）与 `handoffd notify`——并把 notify 模块里的 osascript / afplay / say 路径换成只记录调用的假程序。
# 绝不调用真实的 osascript / afplay / say。用法：silent_child.py <json 参数文件>。
import json
import os
import sys

args = json.load(open(sys.argv[1], encoding="utf-8"))
sys.dont_write_bytecode = True
sys.path.insert(0, args["mvp"])

from cdm import baton, handoffd_cli, notify, util  # noqa: E402

notify.OSASCRIPT, notify.AFPLAY, notify.SAY = args["fake_osascript"], args["fake_afplay"], args["fake_say"]
project, sdir = args["project"], args["sdir"]
os.makedirs(sdir, exist_ok=True)
now = 1_790_000_000_000
b = {"uuid": "uB", "name": "b", "pid": 4242, "lstart": "Thu Oct  1 12:00:01 2026", "online_ms": now}
handoff = baton.new_handoff("h-silent", "goal", {"sid": "sA", "pid": 4141, "lstart": "Thu Oct  1 12:00:00 2026"}, None, "uB", "b", now)
handoff["b"] = b
util.atomic_write_json(baton.baton_path(sdir), {"version": 1, "holder": {"sid": "sA", "pid": 4141, "lstart": "Thu Oct  1 12:00:00 2026", "claimed_ms": 1},
                                                  "handoff": handoff, "retire": []})
util.atomic_write_json(os.path.join(sdir, "mode"), {"mode": "auto", "since_ms": now - 1000})
baton.default_proc_state = lambda record: "live"           # 让 B 看起来活着；它的会话文件不存在 ⇒ cwd_unknown ⇒ 拒绝发信号 ⇒ 响铃（走真实 Notifier）
env = {"CLAUDE_PROJECT_DIR": project}
code_off = handoffd_cli.main(["off"], env=env, chain_fn=lambda deadline: [], now_fn=lambda: now)
code_notify = handoffd_cli.main(["notify", "--cat", "rule", "--what", "x", "--why", "y", "--ask", "1) a", "--recommend", "1", "--if-no-reply", "z"],
                                env=env, chain_fn=lambda deadline: [], now_fn=lambda: now)
print(json.dumps({"off": code_off, "notify": code_notify}))
