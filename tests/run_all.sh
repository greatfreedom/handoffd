#!/bin/bash
# 运行全部离线测试；任一用例失败/报错/缺失都以非 0 退出，只有每个用例 ID 恰好出现一次 PASS 才打印 MVP-OFFLINE-ALL-PASS。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export HANDOFFD_SILENT=1   # 全局静音闸：离线测试里真实的 Notifier 也不调 osascript / afplay / say（notify.py _run）
export CDM_LAUNCHCTL_BIN="$HERE/fake_launchctl.sh"   # 同理：`on` / `off` / `status` 走假 launchctl，绝不碰真实 launchd
LOG="$(mktemp)"
WARP_TMP="$(mktemp -d)"
export CDM_WARP_TAB_DIR="$WARP_TMP/tab_configs" CDM_WARP_LAUNCH_DIR="$WARP_TMP/launch_configurations"   # 离线测试绝不读写真实的 ~/.warp
trap 'rm -f "$LOG"; rm -rf "$WARP_TMP"' EXIT

# 规格 §5 的全部用例 ID，外加本实现自加的 N2、A1、A2，以及 R1 复核后新增的 F6、F7、R1、N3、S2、C5、A3（R2、R3、D14–D17、C4 已随旧 daemon 删除）；
# handoffd S1 新增 H1–H21（test_hooks.py：H1–H13，test_handoffd.py：H14–H21）；R1 修复新增 H22–H29（H22–H27、H29 在 test_hooks.py，H28 在 test_handoffd.py）；R3 授权入口不变量验收 H30–H36（test_authz.py）
# handoffd S2a「换棒」新增 H37–H61：H37 开窗启动器（test_launcher.py）；H38–H41、H45–H47、H49、H50 场景 / 结构核对（test_swap.py）；H42–H44、H48 穷举 / 崩溃点 / 交错 / 目标核对（test_swap_exhaustive.py）；H51–H56、H60 R1 整合账的 K1 / K2 / K3 与 A7–A11（test_swap_k.py，oracle 独立于实现）；H57–H59 R2 整合账的 K4 / B2 / B3 / B5–B7，含 CLI 与 daemon 并发和真实子进程 kill -9（test_swap_off.py，crash_child.py）
# S2a 删除的旧用例（随旧 cdm 的 launchrec / 旧 daemon 换棒逻辑 / mark-ready 等）：D1–D17、R2、R3（test_daemon.py 整文件）、C1、C4（test_cli.py）
# handoffd S3「装成能用」新增 H62–H82（test_s3.py）：H62–H64 on / off / status 与 launchd；H65 daemon 抢锁；H66 旧 cdm 退役 + test-alert；H67、H68 G7；H69、H70 G9；H71 G10；H72 话术与 README；H73 label 哈希；H74、H75 daemon manual 自退与 on 的 kickstart；H76、H77 launch_config 开窗与临时 Warp 配置清理；H78 off 文案；H79 兜底清扫过期临时 Warp 配置；H80–H82 R1 收口（临时配置准确路径 / manual status 心跳 / on 对卡住 daemon kickstart -k）。
# handoffd S2b「看护」新增 H83–H100（test_s2b.py）：H83 :41 在途询问 ASK；H84 :42/:58 回复核对（四类 + 连 2 次核不上 + 清零）；H85 :47① 无回复 5 min / OPEN 不可达；H86 :40 API 退避 NUDGE；H87 :39 撞额度；H88 :59 问答超限；H89 :47② 非交接主控没了；H90 :65 复核轮次账；H91 :47⑥③ tick 异常 / 读不出连 3 轮；H92 时钟回拨 / 跳变；H93 Esc 打断（仅 prompt>stop 在途时先核 agents busy）；H94–H100 R1 整合账 F1–F7 回归。
# handoffd S4a「B 的模型 / 档位跟 A 一致」新增 H101–H106（test_s4a.py）：H101 opus+high 继承；H102 haiku 无 effort；H103 快照缺失 / 坏 / 非法走 config；H104 config 里已有 --model/--effort 被替换；H105 非法 model.id / effort 拒绝；H106 [1m] 端到端转义。
# handoffd「reply task 可带预计时长」新增 H108–H113（test_s2b.py，2026-10-15）：H108 reply task 60m ⇒ 59 分钟不问 / 60 分钟问；H109 带时长的重问之后回到 idle_ask_minutes；H110 非法时长报错且状态不变；H111 非 task 带时长报错；H112 idle_ask_minutes=90；H113 核不上时时长不生效。
# handoffd reply-ETA R1 裁定回归 H114–H116（test_s2b.py，2026-10-15）：H114 时长自回复时刻起算（回复后 daemon 停 59 分钟再核对）；H115 先核准后再答核不上 ⇒ 清旧时长；H116 持久化 eta_ms 越界 ⇒ 按 idle_ask_minutes。
# handoffd reply-ETA R3 检查点诊断回归 H117（test_s2b.py，2026-10-15）：时钟跳变 > 1 h 重算时预计时长随 ok_ms 一起作废。
# 公开发布前修改（test_release.py）：X9 docs/TERMINALS.md 开窗契约与源码一致；X7 manual 下清扫不碰 ~/.warp、X8 登记册任一条目损坏即整体不用（审查第 2 轮 P1）；X6 钩子启动层 fail-open（入口缺失 / 不可读退出 0）；X5 docs/INSTALL.md 的卸载脚本可执行 + 安装卸载要点齐全；X4 Warp 配置兜底清扫只认登记册（审查 P0，H79 随之收紧：同前缀未登记的用户文件不再被删）；X3 默认铃声改为系统声音、不带音频（产品默认值改变，相关旧断言随之更新，不是放宽；占位音频在 tests/fixtures/audio）；X2 ListAgents 加入只读工具（B 找不到交出方会话名的已知问题）；X1 required_env 默认为空（原默认要求作者个人的一个 API 密钥环境变量；产品默认值改变，不是放宽断言）。
# S3 删除的旧用例：C3（旧 install/uninstall-launchd，由 H62、H63、H67、H68 接替）；C5 只留 NaN / Infinity 配置部分。
EXPECTED_IDS="F1 F2 F3 F4 F5 F6 F7 R1 N1 N2 N3 S1 S2 C2 C5 A1 A2 A3 H1 H2 H3 H4 H5 H6 H7 H8 H9 H10 H11 H12 H13 H14 H15 H16 H17 H18 H19 H20 H21 H22 H23 H24 H25 H26 H27 H28 H29 H30 H31 H32 H33 H34 H35 H36 H37 H38 H39 H40 H41 H42 H43 H44 H45 H46 H47 H48 H49 H50 H51 H52 H53 H54 H55 H56 H57 H58 H59 H60 H61 H62 H63 H64 H65 H66 H67 H68 H69 H70 H71 H72 H73 H74 H75 H76 H77 H78 H79 H80 H81 H82 H83 H84 H85 H86 H87 H88 H89 H90 H91 H92 H93 H94 H95 H96 H97 H98 H99 H100 H101 H102 H103 H104 H105 H106 H107 H108 H109 H110 H111 H112 H113 H114 H115 H116 H117 X1 X2 X3 X4 X5 X6 X7 X8 X9"
SCRIPTS="test_lock.py test_interleaving.py test_nudge.py test_launcher.py test_cli.py test_hooks.py test_handoffd.py test_authz.py test_swap.py test_swap_exhaustive.py test_swap_k.py test_swap_off.py test_s3.py test_s2b.py test_s4a.py test_release.py"

status=0
for script in $SCRIPTS; do
  echo "== $script =="
  /usr/bin/python3 "$HERE/$script" 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
  if [ "$code" -ne 0 ]; then
    echo "!! $script 以退出码 $code 结束"
    status=1
  fi
done

echo "== 核对每个用例 ID =="
for id in $EXPECTED_IDS; do
  passes=$(grep -c "^$id PASS\$" "$LOG")
  fails=$(grep -c "^$id FAIL" "$LOG")
  if [ "$passes" -ne 1 ] || [ "$fails" -ne 0 ]; then
    echo "!! 用例 ${id}：PASS 出现 $passes 次，FAIL 出现 $fails 次（要求恰好 1 次 PASS、0 次 FAIL）"
    status=1
  fi
done
unexpected=$(grep -E "^[A-Z][0-9]+ (PASS|FAIL)" "$LOG" | awk '{print $1}' | sort -u | tr '\n' ' ')
for id in $unexpected; do
  case " $EXPECTED_IDS " in
    *" $id "*) ;;
    *) echo "!! 出现了未登记的用例 ID：${id}"; status=1 ;;
  esac
done

if [ "$status" -ne 0 ]; then
  echo "离线测试未全部通过"
  exit 1
fi
echo "MVP-OFFLINE-ALL-PASS"
