# handoffd S2b「看护」（设计 §4 :39–:43、:47①②③⑥、:58、:59、:65；派单包 cdm-06 §1）：daemon 每轮在 mode==auto 时对被看护的会话做的事。
# 以 mixin 形式并入 daemon.Daemon，复用它的 notice / alert / _send / _target_ok / _proc / _session / log / log_change；本模块只放 S2b 的逻辑，不碰 S2a 换棒。
# 被看护的会话 = 登记册 holder；交接中（handoff≠null 且未 committed）另加已上线的 B（S2b 自定，规格:41 只写「主控」）。
# 全部计时用 daemon 注入的时钟（self.now，epoch 毫秒）；账本在 daemon_state.json 的 "watch" 键（读整个、只改自己的键、原子写，同 _save_rotation）。
# 时钟（设计:7）：两轮之间的间隔为负或 > 1 h ⇒ 所有计时从当前时刻重算、不响铃（S2b 自定：用「轮间隔」判跳变，hook 写的时间戳若在未来则忽略）。
import hashlib

from cdm import agents as agents_module, hook_relay, msg, review, sessions, util

MINUTE_MS = 60_000
CLOCK_JUMP_MS = 3600 * 1000            # 设计:7：年龄为负或跳变 > 1 h
QUOTA_FALLBACK_MS = 60 * MINUTE_MS     # 设计:50、:123 c：rate_limit 后 60 min 既无 quota_auto_resume_* 也无新事件 ⇒ 转入退避 NUDGE 循环（60 min 自定）
PEER_EVENT_MS = 30 * MINUTE_MS         # 设计:53：reply peer 核对「对方 30 min 内有事件」
OPEN_UNREACHABLE_MS = 5 * MINUTE_MS    # 派单包 §1.3：B 已上线、kit 已封存后 OPEN 持续没送达 ≥ 5 min
BAD_TICK_LIMIT = 3                     # 设计:47 ⑥ / ③：连续 3 轮
REPLY_KINDS = ("task", "peer", "quota", "owner")
ETA_MIN, ETA_MAX = 15, 180        # reply task 预计时长的合法范围（分钟，与 handoffd_cli 一致；这里再核一次防状态文件被改）
WATCH_HID = "watch"                    # 不属于某次交接的 notice 的 hid（notice.cat=handoff 的补响机制要求 hid 非空）
REPLY_HINT = "回复方式：运行 `handoffd reply task|peer|quota|owner`（task=在等后台任务 / 定时任务，peer=在等对方会话的消息，quota=在等额度恢复，owner=在等 Owner 输入；在等后台任务时可加预计时长，例如 `handoffd reply task 60m`，15–180 分钟，其间不再问）。"


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class WatchMixin:
    # ---- 账本（daemon_state.json["watch"]）----

    def _wload(self):
        status, value = util.read_json(self._state_path())
        watch = value.get("watch") if status == "ok" and isinstance(value, dict) else None
        watch = watch if isinstance(watch, dict) else {}
        if not isinstance(watch.get("sessions"), dict):
            watch["sessions"] = {}
        if not isinstance(watch.get("open_since"), dict):
            watch["open_since"] = {}
        self._wstate = watch
        return watch

    def _wsave(self):
        if self._wstate is None:
            return
        status, value = util.read_json(self._state_path())
        state = value if status == "ok" and isinstance(value, dict) else {}
        state["watch"] = self._wstate
        try:
            util.atomic_write_json(self._state_path(), state)
        except OSError as exc:
            self.log("watch_state_save_failed", error=repr(exc))

    def _ts(self, value):
        """hook 写的时间戳：不是整数、或在未来（时钟回拨后）⇒ 按 0（不参与计时）。"""
        value = _int(value)
        return value if value is not None and 0 <= value <= self.now else 0

    def _ring(self, hid, key, what, why, ask, recommend, if_no_reply, options, sid=None):
        """响铃 notice 的薄封装：id = n-<hid>-<key>，存在即去重（重启后仍成立）。"""
        return self.notice(hid, key, dict(what=what, why=why, ask=ask, recommend=recommend, if_no_reply=if_no_reply), True, options, sid)

    # ---- 一轮 ----

    def _watch(self):
        """auto 下每轮在换棒逻辑之后调用。登记册读不出时不做（_fresh 已记并计入「读不出」）。"""
        state = self._fresh()
        if state is None:
            return
        w = self._wload()
        self._clock_check(w)
        watched = self._watched(state)
        alive = {sid for sid, _slot, _role in watched}
        for sid in list(w["sessions"]):
            if sid not in alive:
                del w["sessions"][sid]
        handoff = state["handoff"]
        active = bool(handoff and handoff.get("phase") != "committed")
        for sid, slot, role in watched:
            self._watch_session(state, sid, slot, role, w["sessions"].setdefault(sid, {}), active)
        self._watch_qa(state)
        self._watch_open(state, w)
        self._watch_gone(state)
        self._watch_review()
        w["tick_ms"] = self.now
        self._wsave()

    def _watched(self, state):
        out = []
        holder, handoff = state["holder"], state["handoff"]
        if holder:
            out.append((holder["sid"], holder, "holder"))
        if handoff and handoff.get("phase") != "committed" and (handoff.get("open") or {}).get("state") == "sent":
            # R1-F5：B 只在 OPEN 已送达之后才纳入看护，否则 30 min 到点 ASK 会抢在 OPEN 前成为 B 的首条消息（设计:102）
            b = handoff.get("b") or {}
            if b.get("online_ms") and b.get("pid") and b.get("lstart") and b.get("uuid"):
                out.append((b["uuid"], b, "b"))
        return out

    def _clock_check(self, w):
        """设计:7：两轮间隔为负或 > 1 h ⇒ 各计时从当前时刻重算（不响铃）。"""
        last = _int(w.get("tick_ms"))
        if last is None or 0 <= self.now - last <= CLOCK_JUMP_MS:
            return
        for ws in w["sessions"].values():
            for key in ("ask_ms", "ok_ms", "last_nudge_ms", "eta_ms", "eta_for_ms"):   # 预计时长随 ok_ms 一起作废
                ws.pop(key, None)
            ws["awaiting"] = False
            ws["floor"] = self.now
            if "since_ms" in ws:
                ws["since_ms"] = self.now
            if ws.get("xfer"):
                ws["xfer_ms"] = self.now
        w["open_since"] = {}
        self.log("watch_clock_reset", last_tick_ms=last)

    # ---- 一个会话 ----

    def _watch_session(self, state, sid, slot, role, ws, active):
        sess = self._session(sid)
        loop, quota_wait = self._backoff_state(sess, ws)
        if loop:
            self._nudge(sid, slot, sess, ws)
        paused = loop or quota_wait
        hid = state["handoff"]["id"] if state["handoff"] else WATCH_HID
        if not self._handle_reply(state, hid, sid, slot, role, sess, ws, active, paused):
            return
        stop, prompt = _int(sess.get("last_stop_ms")) or 0, _int(sess.get("last_prompt_ms")) or 0
        tasks, crons = _int(sess.get("bg_tasks")) or 0, _int(sess.get("bg_crons")) or 0
        inflight = tasks > 0 or crons > 0 or prompt > stop or active
        if paused:
            # 额度等待 / 退避期间不问、不响铃（S2b 自定：它答不了，询问等于一次 NUDGE）；在途计时作废，恢复后重新起算；已发的 ASK 的回复仍照常核对（上面）
            for key in ("since_ms", "ok_ms"):
                ws.pop(key, None)
            ws["awaiting"] = False
            return
        if not inflight:
            # 平时空闲（:43）：不问；计时与未决询问作废
            for key in ("since_ms", "ask_ms", "ok_ms"):
                ws.pop(key, None)
            ws["awaiting"] = False
            ws["mismatch"] = 0
            return
        ws.setdefault("since_ms", self.now)
        ask_ms, window = self._ts(ws.get("ask_ms")), int(self.cfg["ask_reply_minutes"] * MINUTE_MS)
        if ws.get("awaiting") and ask_ms and self.now - ask_ms >= window and self._noreply_due(sid, slot, ws):
            ws["awaiting"] = False
            self._ring(hid, "ask_noreply-%s-%d" % (sid, ask_ms),
                       "会话 %s 被问进展后 %s 分钟没有回复（或询问没投递成功）。" % (sid[:8], self.cfg["ask_reply_minutes"]),
                       "该会话在途（有后台任务 / 本轮没结束 / 交接中），daemon 问了它在等什么，它没答；机器不判断它是否卡死。",
                       "1) 去该窗口看 2) 放弃交接（handoffd off）", 1,
                       "不处理的话 daemon 隔一阵会再问；交接中则交接一直停着。", ["去该窗口看", "放弃交接（handoffd off）"], sid)
        base = max(self._ts(ws.get("since_ms")), self._ts(ws.get("ok_ms")), ask_ms)
        if self.now - base >= self._ask_interval_ms(ws):
            if tasks <= 0 and crons <= 0 and not active:
                # 「在途」仅因 prompt > stop：Owner 按 Esc 打断时 Stop 不触发（hooks.md:2525），空闲会话会一直像在途；先核 agents 里它是否真在 busy
                busy = self._agent_busy(slot)
                if busy is None:
                    return                      # 查询失败：本轮不问、下一轮再看（不当在途也不响）
                if not busy:
                    for key in ("since_ms", "ask_ms", "ok_ms"):
                        ws.pop(key, None)       # 同 :43 平时空闲：计时作废
                    ws["awaiting"] = False
                    ws["mismatch"] = 0
                    return
            ws.pop("eta_ms", None)              # 预计时长只用这一次：到点问了之后回到 idle_ask_minutes
            ws.pop("eta_for_ms", None)
            self._send_ask(sid, slot, ws, "你已经有一阵没有动静了，现在在做什么、在等什么？" + REPLY_HINT, "regular")

    def _ask_interval_ms(self, ws):
        """下次询问的间隔：上一次核得上的 reply task 带了预计时长、且它仍是当前计时基线 ⇒ 用该时长；否则 idle_ask_minutes。"""
        ok_ms = self._ts(ws.get("ok_ms"))
        eta_ms = ws.get("eta_ms")
        if ok_ms and ws.get("eta_for_ms") == ok_ms and isinstance(eta_ms, int) and not isinstance(eta_ms, bool) and 0 <= eta_ms <= ETA_MAX * MINUTE_MS:
            return eta_ms
        return int(self.cfg["idle_ask_minutes"] * MINUTE_MS)

    def _noreply_due(self, sid, slot, ws):
        """5 min 到点后真要按「无回复」响铃吗？F7：目标会话已不是 live（正常退出 / 崩溃，由 :47② 与 S2a 的铃管）⇒ 不响；
        F6（规格:41「还在跑就继续等」）：agents 显示它 busy ⇒ 视为还在跑，按核得上处理（重新计时，30 min 后再问）；agents 查询失败 ⇒ 本轮不判，下一轮再看。"""
        if self._proc(slot) != "live":
            ws["awaiting"] = False
            return False
        busy = self._agent_busy(slot)
        if busy is None:
            return False
        if busy:
            ws.update(awaiting=False, ok_ms=self.now, mismatch=0)
            self.log("ask_noreply_busy", sid=sid)
            return False
        return True

    def _agent_busy(self, slot):
        """agents 里该 pid 的 status 是否 busy；查询失败（None / 异常）⇒ None。"""
        try:
            entries = self.agents_fn()
        except Exception:
            return None
        if entries is None:
            return None
        return any(isinstance(e, dict) and e.get("pid") == slot.get("pid") and e.get("status") == "busy" for e in entries)

    def _send_ask(self, sid, slot, ws, body, kind):
        ok, why, info = self._target_ok(slot)
        if not ok:
            self.log_change("ask_target:" + sid, "ask_target_refused", sid=sid, reason=why)    # 目标核对不过：不投，记 events
            return False
        ws["ask_ms"], ws["awaiting"] = self.now, True
        self._wsave()                       # 先落盘再投：至多一次；投递失败照样按「无回复」走 5 min 门（设计:97）
        delivered, detail = self._send(slot["pid"], msg.format_message("ASK", body), info)
        self.log("ask", sid=sid, kind=kind, delivered=delivered, detail=detail)
        return True

    # ---- 回复核对（:42）----

    def _handle_reply(self, state, hid, sid, slot, role, sess, ws, active, paused=False):
        """处理 ASK 发出之后的新回复（每条只处理一次）。返回 False = 本轮不再继续（回复核对依赖的 agents 查询失败，下一轮重试）。"""
        reply = sess.get("last_reply")
        ask_ms = self._ts(ws.get("ask_ms"))
        reply_ms = self._ts(reply.get("ms")) if isinstance(reply, dict) else 0
        if not ask_ms or reply_ms <= ask_ms or reply_ms <= self._ts(ws.get("reply_seen_ms")):
            return True
        kind = reply.get("kind")
        if kind not in REPLY_KINDS:
            ws["reply_seen_ms"] = reply_ms
            return True
        if kind == "owner":
            ws.update(reply_seen_ms=reply_ms, awaiting=False, ok_ms=self.now, mismatch=0)
            self._ring(hid, "reply_owner-%s-%d" % (sid, reply_ms),
                       "会话 %s 说它在等你（Owner）输入。" % sid[:8],
                       "它回复 owner：需要你本人在该窗口处理（授权 / 回答 / 拍板）。",
                       "1) 去该窗口处理", 1, "该会话一直等着，不会自己继续。", ["去该窗口处理"], sid)
            return True
        ok, why = self._verify_reply(kind, state, slot, role, sess, active)
        if ok is None:
            return False
        ws["reply_seen_ms"] = reply_ms
        ws["awaiting"] = False
        if ok:
            ws.update(ok_ms=self.now, mismatch=0)       # 核得上：重新计时、无限等
            eta = reply.get("eta_minutes")              # reply task 附带的预计时长：只管紧接着的这一次重问（_ask_interval_ms）
            if kind == "task" and isinstance(eta, int) and not isinstance(eta, bool) and ETA_MIN <= eta <= ETA_MAX:
                ws.update(eta_ms=max(0, eta * MINUTE_MS - max(0, self.now - reply_ms)), eta_for_ms=self.now)   # 自回复时刻起算：核对晚于回复时扣掉差值
            else:
                ws.pop("eta_ms", None)
                ws.pop("eta_for_ms", None)
            self.log("reply_ok", sid=sid, kind=kind)
            return True
        if paused:
            # R1-F3：额度等待 / 退避期间（回复之后撞上 rate_limit / 临时错误）核不上不算数：不计数、不投纠正 ASK
            self.log("reply_mismatch_paused", sid=sid, kind=kind, reason=why)
            return True
        ws.pop("eta_ms", None)                           # 核不上：先前核准的时长作废，回到 idle_ask_minutes
        ws.pop("eta_for_ms", None)
        ws["mismatch"] = int(ws.get("mismatch") or 0) + 1
        self.log("reply_mismatch", sid=sid, kind=kind, reason=why, count=ws["mismatch"])
        if ws["mismatch"] >= 2:                          # 连续 2 次核不上 ⇒ 响铃（:58）
            ws.update(mismatch=0, ok_ms=self.now)
            self._ring(hid, "reply_mismatch-%s-%d" % (sid, reply_ms),
                       "会话 %s 说它在等「%s」，但 daemon 连续 2 次核对不上（%s）。" % (sid[:8], kind, why),
                       "它的回复与机器能看到的状态对不上，已自动纠正问过一次仍对不上。",
                       "1) 去该窗口看 2) 放弃交接（handoffd off）", 1,
                       "机器不再追问这一次；daemon 隔一阵会再问，交接中则交接一直停着。", ["去该窗口看", "放弃交接（handoffd off）"], sid)
        else:
            self._send_ask(sid, slot, ws, "你回复的是「%s」，但核对不上：%s。请重新核对实际在等什么，再用 `handoffd reply task|peer|quota|owner` 回复。" % (kind, why), "correct")
        return True

    def _verify_reply(self, kind, state, slot, role, sess, active):
        """返回 (True|False|None, 核不上的原因)；None = 暂时没法核（agents 查询失败）。"""
        if kind == "task":
            if (_int(sess.get("bg_tasks")) or 0) > 0 or (_int(sess.get("bg_crons")) or 0) > 0:
                return True, ""
            busy = self._agent_busy(slot)
            if busy is None:
                return None, ""
            if busy:
                return True, ""
            return False, "最近一次 Stop 的后台任务 / 定时任务都是空的，agents 里你也不在 busy"
        if kind == "quota":
            return (True, "") if _int(sess.get("quota_wait_ms")) else (False, "没有记录到撞额度（没有 rate_limit 事件）")
        # peer：交接中，对方会话 live 且 30 min 内有事件
        if not active:
            return False, "当前没有进行中的交接，没有对方会话可等"
        handoff = state["handoff"]
        if role == "holder":
            other, other_sid = handoff.get("b") or {}, (handoff.get("b") or {}).get("uuid")
        else:
            other, other_sid = state["holder"] or {}, (state["holder"] or {}).get("sid")
        if not other.get("pid") or not other_sid or self._proc(other) != "live":
            return False, "对方会话不在线"
        last = self._ts(self._session(other_sid).get("last_event_ms"))
        if not last or self.now - last > PEER_EVENT_MS:
            return False, "对方会话 30 分钟内没有任何事件"
        return True, ""

    # ---- 额度 / API 临时错误：退避 NUDGE（:39、:40）----

    def _backoff_state(self, sess, ws):
        """返回 (退避循环是否在转, 是否处于额度等待)。
        循环在转 = 最近错误是临时类（overloaded / server_error / max_output_tokens），或已「转入」（收到 quota_auto_resume_disabled，
        或 rate_limit 后 60 min 无 resume 无新事件）。恢复 = 正常 Stop：last_error 变 None 清退避步数；转入的另需 last_stop_ms 晚于转入时刻才清。"""
        err = sess.get("last_error") if isinstance(sess.get("last_error"), dict) else None
        stop = self._ts(sess.get("last_stop_ms"))
        if ws.get("xfer") and stop > self._ts(ws.get("xfer_ms")):
            for key in ("xfer", "xfer_ms", "bo_step", "last_nudge_ms"):
                ws.pop(key, None)
        elif not ws.get("xfer") and err is None and (ws.get("bo_step") or ws.get("last_nudge_ms")):
            for key in ("bo_step", "last_nudge_ms"):
                ws.pop(key, None)
        quota_wait = False
        if not ws.get("xfer"):
            qd, qw = _int(sess.get("quota_disabled_ms")), _int(sess.get("quota_wait_ms"))
            last_event = _int(sess.get("last_event_ms")) or 0
            if qd:
                ws["xfer"], ws["xfer_ms"] = True, self.now
                self.log("quota_to_backoff", reason="disabled")
            elif qw:
                # R1-F2：额度等待只在 fired（钩子清 quota_wait_ms）/ disabled / 正常 Stop 时结束；普通事件（后台任务、子代理的 PostToolUse）只把「60 min 无信号」的起点推后
                base = qw if qw <= self.now else (self._ts(ws.get("floor")) or self.now)
                base = max(base, self._ts(last_event))
                if self.now - base >= QUOTA_FALLBACK_MS:
                    ws["xfer"], ws["xfer_ms"] = True, self.now
                    self.log("quota_to_backoff", reason="no_signal_60min")
                else:
                    quota_wait = True
        temp = bool(err) and err.get("type") in hook_relay.TEMP_ERRORS
        return (temp or bool(ws.get("xfer"))), quota_wait

    def _nudge(self, sid, slot, sess, ws):
        """退避 NUDGE：3→6→12→24→48→60→60…分钟（api_backoff_minutes [3,60]，倍增、封顶），永不停发、永不响铃；每次写 events。
        步数只在正常 Stop 清零（_backoff_state）；NUDGE 引起的 UserPromptSubmit、NUDGE 之后又来的 StopFailure 都不清。"""
        low, high = self.cfg["api_backoff_minutes"]
        step = int(ws.get("bo_step") or 0)
        interval = int(min(high, low * (2 ** min(step, 30))) * MINUTE_MS)
        err = sess.get("last_error") if isinstance(sess.get("last_error"), dict) else {}
        base = max(self._ts(err.get("ms")), self._ts(ws.get("xfer_ms")), self._ts(ws.get("last_nudge_ms")),
                   self._ts(sess.get("last_prompt_ms")), self._ts(ws.get("floor")))
        if base == 0:
            base = ws["floor"] = self.now
        if self.now - base < interval:
            return
        ok, why, info = self._target_ok(slot)
        if not ok:
            self.log_change("nudge_target:" + sid, "nudge_target_refused", sid=sid, reason=why)
            return
        ws["last_nudge_ms"], ws["bo_step"] = self.now, step + 1
        self._wsave()
        delivered, detail = self._send(slot["pid"], msg.format_message("NUDGE", "继续"), info)
        self.log("nudge", sid=sid, step=step + 1, interval_min=interval // MINUTE_MS, delivered=delivered, detail=detail)

    # ---- 交接相关 ----

    def _watch_qa(self, state):
        """:59 B 问答超上限：每次交接响铃一次（notice id = 交接 id + qa_over）。"""
        handoff = state["handoff"]
        if not handoff or handoff.get("phase") == "committed":
            return
        if (_int(handoff.get("qa_count")) or 0) > self.cfg["qa_limit"]:
            self._ring(handoff["id"], "qa_over",
                       "接手方 B 向主控 A 提问已超过 %d 条（交接 %s）。" % (self.cfg["qa_limit"], handoff["id"]),
                       "来回问答太多，可能在互相卡住；机器不判断内容。",
                       "1) 放弃交接（handoffd off） 2) 去两个窗口看", 2, "交接继续进行，没人处理也不会自己停。", ["放弃交接（handoffd off）", "去两个窗口看"])

    def _watch_open(self, state, w):
        """:47① 补充（S2a R1 整合账挂起项）：B 已上线、kit 已封存后 OPEN 一直没送达 ≥ 5 min ⇒ 响铃一次。"""
        handoff = state["handoff"]
        open_since = w["open_since"]
        pending = bool(handoff and handoff.get("phase") != "committed" and handoff.get("kit")
                       and (handoff.get("b") or {}).get("online_ms") and (handoff.get("open") or {}).get("state") is None)
        if pending and not self._target_ok(handoff["b"])[0]:
            pending = False     # R1-F4：B 已死 / 目标核对不过，交给 S2a 已有的铃（b_died / open_refused），不再叠一个「不可达」
        for hid in list(open_since):
            if not pending or hid != handoff["id"]:
                del open_since[hid]
        if not pending:
            return
        since = open_since.setdefault(handoff["id"], self.now)
        if self.now - since >= OPEN_UNREACHABLE_MS:
            self._ring(handoff["id"], "open_unreachable",
                       "B 已上线、kit 已封存，但开场指令 5 分钟都没能送达 B（交接 %s）。" % handoff["id"],
                       "连不上 B 的收件箱（或目标核对不过），机器会一直重试但没成功。",
                       "1) 去 B 的窗口看 2) 放弃交接（handoffd off）", 1, "交接一直停在等 B 收到开场指令。", ["去 B 的窗口看", "放弃交接（handoffd off）"], (handoff.get("b") or {}).get("uuid"))

    # ---- 非交接时主控没了（:47②）与复核轮次账（:65）----

    def _watch_gone(self, state):
        holder = state["holder"]
        if not holder:
            return
        since = sessions.read_mode(self.sdir)[1]
        triple = [holder["sid"], holder["pid"], holder["lstart"], since]
        status = self._proc(holder)
        if status == "live":
            self._wstate["holder_seen"] = triple       # R1-F1：daemon 在本次 auto 期间（mode.since_ms）亲眼见过它活着
            return
        if state["handoff"] is not None or status != "dead" or self._wstate.get("holder_seen") != triple:
            return      # 没亲眼见过活着（例如 off 后在 manual 下正常退出、隔天 on）⇒ 不响
        sess = self._session(holder["sid"])
        ended = _int(sess.get("ended_ms"))
        if ended is not None and ended >= (_int(sess.get("started_ms")) or 0):
            return      # 有 SessionEnd 的正常退出不响
        digest = hashlib.sha1(("%s|%s|%s" % (holder["sid"], holder["pid"], holder["lstart"])).encode("utf-8")).hexdigest()[:10]
        self._ring(WATCH_HID, "holder_gone-%s-%s" % (holder["sid"], digest),
                   "主控会话 %s（pid %s）没了，而且没有收到它正常退出的记录（可能是关窗或崩溃）。" % (holder["sid"][:8], holder["pid"]),
                   "当前没有进行中的交接；新开窗口会直接成为新主控，这条只是提醒。",
                   "1) 无需处理：新开窗口即成新主控 2) 去看看出了什么事", 1, "没有影响；之前的工作停在原处。", ["无需处理", "去看看出了什么事"], holder["sid"])

    def _watch_review(self):
        status, ledger = review.read_ledger(self.sdir)
        if status == "error":
            self._unreadable = self._unreadable or "review.json"
            self.log_change("review_unreadable", "review_unreadable")
            return
        for product in sorted(ledger):
            entry = ledger[product]
            if entry["round"] >= 3 and entry["p0"] > 0:
                self._ring(WATCH_HID, "review_stuck-%s-%d" % (product, entry["round"]),
                           "产物 %s 复核到第 %d 轮仍有 %d 个 P0。" % (product, entry["round"], entry["p0"]),
                           "复核第 3 轮仍有 P0 ＝ 卡住了（规格:27–30）；复核轮次账不随交接重置。",
                           "1) 去主控窗口看复核为什么没收敛，决定改方案还是停", 1, "复核会继续往下轮，但没人拍板也不会自己收敛。", ["去主控窗口看"])

    # ---- 异常连 3 轮（:47⑥ 与 ③）----

    def _tick_health(self, failed):
        """连续 3 轮「tick 抛未预料异常 / 登记册或复核账读不出」⇒ 响铃一次；之后有一轮正常再重新计数。"""
        try:
            if sessions.read_mode(self.sdir)[0] != "auto":
                return
            reason = failed or (("%s 读不出" % self._unreadable) if self._unreadable else None)
            w = self._wload()
            if reason is None:
                if w.get("bad_streak") or w.get("bad_rung"):
                    w["bad_streak"], w["bad_rung"] = 0, False
                    self._wsave()
                return
            w["bad_streak"] = int(w.get("bad_streak") or 0) + 1
            if w["bad_streak"] >= BAD_TICK_LIMIT and not w.get("bad_rung"):
                episode = int(w.get("bad_episodes") or 0) + 1
                w["bad_episodes"] = episode
                key = "tick_bad-%d" % episode
                self._ring(WATCH_HID, key, "daemon 连续 %d 轮没能正常工作（最近一轮：%s）。" % (BAD_TICK_LIMIT, reason[:160]),
                           "守护程序每 %s 秒一轮，连续 3 轮出错；自动接力 / 看护可能已失效。" % self.cfg["poll_seconds"],
                           "1) 去看 daemon 的运行记录 2) 放弃交接（handoffd off）", 1, "daemon 会继续重试；问题不消失的话自动接力不会正常工作。",
                           ["去看 daemon 的运行记录", "放弃交接（handoffd off）"])
                w["bad_rung"] = sessions.notice_exists(self.sdir, "n-%s-%s" % (WATCH_HID, key))
            self._wsave()
        except Exception as exc:
            self.log("watch_health_failed", error=repr(exc))
