# 离线测试的公共工具：临时目录、假可执行文件、真实进程、用例登记。
import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

sys.dont_write_bytecode = True
os.environ.setdefault("HANDOFFD_SILENT", "1")   # 直接单跑某个测试脚本时也静音：绝不让真实的 osascript / afplay / say 在测试里出声（run_all.sh 另有全局设置）

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if "CDM_WARP_TAB_DIR" not in os.environ and "CDM_WARP_LAUNCH_DIR" not in os.environ:
    # 离线测试绝不读写真实的 ~/.warp：Warp 配置目录默认指到临时目录（run_all.sh 已设时沿用；退出时清理）
    _WARP_TMP = tempfile.mkdtemp(prefix="cdm-warp-")
    os.environ["CDM_WARP_TAB_DIR"] = os.path.join(_WARP_TMP, "tab_configs")
    os.environ["CDM_WARP_LAUNCH_DIR"] = os.path.join(_WARP_TMP, "launch_configurations")
    atexit.register(shutil.rmtree, _WARP_TMP, ignore_errors=True)
os.environ.setdefault("CDM_LAUNCHCTL_BIN", os.path.join(TESTS_DIR, "fake_launchctl.sh"))   # 同理：`on` / `off` / `status` 在测试里绝不碰真实 launchd
MVP_ROOT = os.path.dirname(TESTS_DIR)
PROJECT_ROOT = os.path.dirname(MVP_ROOT)
POC_DIR = os.path.join(TESTS_DIR, "fixtures", "poc")   # 旧版（PoC）单主控锁脚本：F1/F3/F4/F5 拿它复现旧缺陷，原样拷自作者自己的 PoC
FIXTURE_TONES = os.path.join(TESTS_DIR, "fixtures", "audio", "tones")   # 占位文件（非真实音频）：只用来测铃声 / 人声轮换，发布版不带音频
FIXTURE_VOICE = os.path.join(TESTS_DIR, "fixtures", "audio", "voice", "voice-fixture")
sys.path.insert(0, MVP_ROOT)

from cdm import procs  # noqa: E402

GIT = "/usr/bin/git"


class CaseFailed(AssertionError):
    """用例断言失败。"""


def require(condition, message):
    """断言；失败抛 CaseFailed。"""
    if not condition:
        raise CaseFailed(message)


def run_cases(cases):
    """依次运行 (ID, 函数)；打印 `<ID> PASS` 或 `<ID> FAIL: 原因`，返回退出码。"""
    failed = 0
    for case_id, func in cases:
        try:
            details = func() or []
        except Exception as exc:
            failed += 1
            print("%s FAIL: %s" % (case_id, exc if isinstance(exc, CaseFailed) else repr(exc)))
            if not isinstance(exc, CaseFailed):
                traceback.print_exc(file=sys.stdout)
            continue
        print("%s PASS" % case_id)
        for line in details:
            print("  " + line)
    return 1 if failed else 0


@contextlib.contextmanager
def temp_dir(prefix="cdm-test-"):
    """建临时目录并在结束时清理（返回 realpath，避免 /var 与 /private/var 混淆）。"""
    path = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def patched_env(**changes):
    """临时修改环境变量；值为 None 表示删除。"""
    saved = {key: os.environ.get(key) for key in changes}
    for key, value in changes.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_exe(path, body):
    """写一个可执行脚本。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(body)
    os.chmod(path, 0o755)


def git(*args, cwd=None):
    """用真实 git 跑一条命令，失败抛异常。"""
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    env.pop("GIT_DIR", None)
    subprocess.run([GIT] + list(args), cwd=cwd, check=True, capture_output=True, text=True, env=env)


def make_repo(path):
    """建一个带一次提交的真实 git 仓库。"""
    os.makedirs(path)
    git("init", "-q", cwd=path)
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)


def make_fake_git(bin_dir, mode):
    """PATH 前置的假 git：exit128 / empty / hang。"""
    bodies = {
        "exit128": "#!/bin/sh\necho 'fatal: simulated' >&2\nexit 128\n",
        "empty": "#!/bin/sh\nexit 0\n",
        "hang": "#!/bin/sh\nexec /bin/sleep 30\n",
    }
    write_exe(os.path.join(bin_dir, "git"), bodies[mode])


def make_fake_ps(bin_dir):
    """PATH 前置的假 ps：只对 FAKE_PS_PID（ANY 表示所有）注入 FAKE_PS_MODE 故障，其余转发给真实 ps。"""
    body = (
        "#!/bin/bash\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"$FAKE_PS_PID\" ] || [ \"$FAKE_PS_PID\" = ANY ]; then\n"
        "    case \"$FAKE_PS_MODE\" in\n"
        "      exit1) exit 1 ;;\n"
        "      empty) exit 0 ;;\n"
        "      hang) exec /bin/sleep 30 ;;\n"
        "    esac\n"
        "  fi\n"
        "done\n"
        "exec /bin/ps \"$@\"\n"
    )
    write_exe(os.path.join(bin_dir, "ps"), body)


def path_with(bin_dir):
    """把 bin_dir 放到 PATH 最前面。"""
    return bin_dir + os.pathsep + os.environ.get("PATH", "")


class ProcessPool:
    """管理测试里起的真实进程，结束时全部杀掉。"""

    def __init__(self, work):
        self.work = work
        self.children = []
        self.count = 0

    def spawn_claude(self):
        """把 /bin/sleep 软链成名为 claude 的文件并运行，返回 (Popen, 身份记录字段)。"""
        self.count += 1
        directory = os.path.join(self.work, "fakeclaude%d" % self.count)
        os.makedirs(directory)
        target = os.path.join(directory, "claude")
        # 复制系统二进制会被 macOS 直接杀掉（退出 137），所以用软链；ps 的 comm 显示的就是软链路径
        os.symlink("/bin/sleep", target)
        child = subprocess.Popen([target, "300"])
        self.children.append(child)
        status, comm = procs.ps_field(child.pid, "comm", procs.Deadline(5))
        require(status == "ok" and procs.is_claude(comm), "假 claude 进程的 comm 没通过 is_claude：%r" % comm)
        return child

    def identity(self, child, sid):
        """真实 ps 取 lstart，构造身份记录。"""
        status, lstart = procs.ps_field(child.pid, "lstart", procs.Deadline(5))
        require(status == "ok", "取不到 lstart")
        return {"session_id": sid, "pid": child.pid, "lstart": lstart}

    def kill(self, child):
        """杀掉并收尸，让 pid 确实不存在。"""
        child.kill()
        child.wait(timeout=5)

    def close(self):
        """清理所有进程。"""
        for child in self.children:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@contextlib.contextmanager
def process_pool(work):
    """ProcessPool 的上下文管理器。"""
    pool = ProcessPool(work)
    try:
        yield pool
    finally:
        pool.close()


def load_module(name, path):
    """按路径只读加载一个 Python 文件（不写字节码）。"""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_old_q6():
    """加载旧版 poc/q6_single_main_lock.py，并禁掉它往 poc/out 写日志的行为。"""
    module = load_module("old_q6", os.path.join(POC_DIR, "q6_single_main_lock.py"))
    module.best_effort_log = lambda *args, **kwargs: None
    return module


def run_old_hook(old, mode, session_id, project, pid):
    """在进程内调用旧版钩子；本会话身份（my_claude_pid）替换为 pid。"""
    old.my_claude_pid = lambda deadline: pid
    out = io.StringIO()
    payload = json.dumps({"session_id": session_id, "cwd": project})
    with patched_env(CLAUDE_PROJECT_DIR=project), contextlib.redirect_stdout(out):
        code = old.main([mode], stdin=io.StringIO(payload))
    return code, out.getvalue().strip()


def wait_gone(pid, seconds=5.0):
    """等一个进程彻底消失。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if procs.pid_exists(pid) is False:
            return True
        time.sleep(0.05)
    return False


def kill_pid(pid, sig=signal.SIGKILL):
    """按 pid 发信号，进程已不存在则忽略。"""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
