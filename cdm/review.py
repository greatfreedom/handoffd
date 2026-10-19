# 复核轮次账 <S>/review.json（设计:44、规格:32、:65）：{产物ID: {round, p0, ms}}，只增不减，跨交接保留。
# S1 只做登记与拒绝；「round≥3 ∧ p0>0 自动叫停」是 daemon 的事（S2，设计:68）。
import fcntl
import os
import re
import time

from cdm import util

PRODUCT_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
GUARD_WAIT_SECONDS = 1.5


class Refused(Exception):
    """登记被拒（参数不合法或轮次小于账上）。"""


def review_path(sdir):
    return os.path.join(sdir, "review.json")


def read_ledger(sdir):
    """返回 ("missing"|"ok"|"error", 账)。"""
    status, value = util.read_json(review_path(sdir))
    if status == "missing":
        return "missing", {}
    if status != "ok" or not isinstance(value, dict):
        return "error", None
    for entry in value.values():
        if not (isinstance(entry, dict) and isinstance(entry.get("round"), int) and isinstance(entry.get("p0"), int)):
            return "error", None
    return "ok", value


def register(sdir, product, round_number, p0, now_ms):
    """登记某产物第 N 轮复核、P0 数为 K。账上已有更大轮次 ⇒ 拒绝；同一轮次重复登记只更新 P0（S1 自定）。返回登记后的条目。"""
    if not isinstance(product, str) or not PRODUCT_ID.match(product):
        raise Refused("产物 ID 只能含字母数字和 ._:/-（最长 120）")
    for label, value, minimum in (("轮次", round_number, 1), ("P0 数", p0, 0)):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise Refused("%s必须是不小于 %d 的整数" % (label, minimum))
    os.makedirs(sdir, exist_ok=True)
    fd = os.open(review_path(sdir) + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        end = time.monotonic() + GUARD_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise Refused("复核账正被占用，请稍后重试")
                time.sleep(0.01)
        status, ledger = read_ledger(sdir)
        if status == "error":
            raise Refused("复核账 review.json 读不出（不覆盖，请人工检查）")
        current = ledger.get(product)
        if current and round_number < current["round"]:
            raise Refused("轮次只增不减：%s 账上已到第 %d 轮，不能登记第 %d 轮" % (product, current["round"], round_number))
        ledger[product] = {"round": round_number, "p0": p0, "ms": now_ms}
        util.atomic_write_json(review_path(sdir), ledger)
        return ledger[product]
    finally:
        os.close(fd)
