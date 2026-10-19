#!/bin/sh
# 离线测试的默认假 launchctl（CDM_LAUNCHCTL_BIN）：不碰真实 launchd。print 一律报「找不到任务」（退出码 113），其余动作成功。
# 要看 launchctl 调用记录 / 模拟失败的用例（test_s3.py）自己写带状态的假程序，不用这个。
if [ "$1" = "print" ]; then
  echo "Could not find service" >&2
  exit 113
fi
exit 0
