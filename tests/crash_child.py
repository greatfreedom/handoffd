# 真实子进程崩溃验证（H59）：在 daemon 的某个切点上被 SIGKILL（kill -9：不执行任何 finally / atexit），父进程再做「重启」检查。
# 用法：crash_child.py <json 参数文件>；参数：mvp 根、项目目录、状态目录、世界表文件、日志文件、切点标签、当前毫秒时钟。
# 只用假的判活 / cwd / 信号 / 投递 / 通知器（都只往日志文件追加一行 JSON），不碰真实会话、真实进程。
import json
import os
import signal
import sys

args = json.load(open(sys.argv[1], encoding="utf-8"))
sys.dont_write_bytecode = True
sys.path.insert(0, args["mvp"])

from cdm import config, daemon  # noqa: E402

table = {int(pid): entry for pid, entry in json.load(open(args["world"], encoding="utf-8")).items()}


def log(kind, **fields):
    with open(args["log"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(kind=kind, **fields), ensure_ascii=False) + "\n")


class Notifier:
    def alert(self, kind, message, voice_text=None):
        log("alert", alert_kind=kind, message=message)


def proc_state(record):
    entry = table.get(record["pid"])
    return "live" if entry and entry["alive"] and entry["lstart"] == record["lstart"] else "dead"


def kill_fn(pid, sig):
    log("kill", pid=pid)
    if pid in table:
        table[pid]["alive"] = False


def msg_fn(pid, content, info=None):
    log("msg", pid=pid, content=content)
    return True, ""


def point(label):
    if label == args["label"]:
        log("point", label=label)
        os.kill(os.getpid(), signal.SIGKILL)       # 真正的 kill -9


cfg = config.build_config({"project_dir": args["project"]})
dm = daemon.Daemon(cfg, args["sdir"], Notifier(), msg_fn=msg_fn, kill_fn=kill_fn, proc_state_fn=proc_state,
                   cwd_fn=lambda pid: (table.get(pid) or {}).get("cwd"), point_fn=point)
dm.tick(args["now"])
log("finished")
