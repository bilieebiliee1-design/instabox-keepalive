#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InstaBox 外部看门人（GitHub Actions keepalive runner）

无状态、幂等：全部状态真值存在 InstaBox 机器对象里
（state / runStartedAt / cooldownUntil），任意一轮失败不影响正确性。

每轮逻辑:
  running + 已连续运行 >= REFRESH_MINUTES → POST /suspend 主动换气
        （官方语义：suspend 保留内存与每个进程，resume 后原地继续，工作无感）
  running + 未到阈值                       → POST /wake（重置 30 分钟闲置计时）
  suspended/paused + 冷却已过              → POST /wake（无阻唤醒，双计时器归零）
  suspended/paused + 冷却未过              → 只记录，等待下轮
  stopped                                  → POST /start
INSTABOX_REFRESH_NOW=1 时立即执行完整换气（suspend→冷却→wake→验证），用于 E2E 测试。

背景：InstaBox 免费档 30 分钟闲置自动挂起 + 480 分钟单次运行硬顶（服务端固定，
无法配置解除）；Both timers restart on resume；沙箱内部无法唤醒自己。
本工作流即"沙箱外的看门人"。仅用 Python 标准库。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

API_URL = os.environ.get("INSTABOX_API_URL", "https://api.instabox.sh").rstrip("/")
API_KEY = os.environ.get("INSTABOX_API_KEY", "").strip()
MACHINE = os.environ.get("INSTABOX_MACHINE", "").strip()
try:
    REFRESH_MINUTES = int(os.environ.get("INSTABOX_REFRESH_MINUTES", "360") or "0")
except ValueError:
    REFRESH_MINUTES = 360
REFRESH_NOW = os.environ.get("INSTABOX_REFRESH_NOW", "") == "1"

HTTP_TIMEOUT = 30
WAKE_TIMEOUT = 75            # /wake 服务端可能长等，与官方 MCP 一致
RUNNING = "running"
STOPPED = "stopped"
SUSPENDED_STATES = {"suspended", "paused"}
SUSPEND_SETTLE_MAX = 90      # suspend 后等状态离开 running 的上限
COOLDOWN_CAP = 25 * 60       # 冷却休眠上限（防 cooldownUntil 异常）
WAKE_MAX_WAIT = 5 * 60       # 唤醒至 running 的轮询上限
POLL = 15                    # 状态轮询间隔


def log(msg):
    print("[%s] %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg), flush=True)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def api(method, path, body=None, timeout=HTTP_TIMEOUT):
    headers = {"X-API-Key": API_KEY, "Accept": "application/json",
               "User-Agent": "instabox-keepalive-gha/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method in ("POST", "PATCH", "DELETE"):
        headers["Idempotency-Key"] = str(uuid.uuid4())
    req = urllib.request.Request(API_URL + path, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(8 << 20).decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read(64 << 10).decode("utf-8", "replace") if e.fp else ""
    except Exception as e:
        log("API %s %s 网络异常: %s" % (method, path, e))
        return None, None
    took = (time.monotonic() - t0) * 1000
    parsed = None
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        pass
    err = ""
    if status >= 400 and isinstance(parsed, dict):
        err = " %s" % json.dumps(parsed.get("error") or {}, ensure_ascii=False)
    log("API %s %s -> HTTP %d (%.0fms)%s" % (method, path, status, took, err))
    return status, parsed


def get_machine():
    st, data = api("GET", "/v1/machines/" + MACHINE)
    return (data or {}) if st == 200 else None


def wait_state(pred, max_wait, what):
    t0 = time.monotonic()
    while True:
        m = get_machine()
        if m and pred(m):
            log("OK %s 达成 (耗时 %.0f 秒, state=%s)" % (what, time.monotonic() - t0, m.get("state")))
            return True
        if time.monotonic() - t0 > max_wait:
            log("TIMEOUT %s 等待 %.0f 秒未达成 (state=%s)"
                % (what, time.monotonic() - t0, (m or {}).get("state")))
            return False
        time.sleep(POLL)


def do_refresh(mid):
    """完整换气：suspend → 等冷却窗口 → wake → 轮询至 running。"""
    log("REFRESH 开始换气: suspend → 冷却 → wake → 验证 running")
    st, _ = api("POST", "/v1/machines/%s/suspend" % mid, body={})
    if not (200 <= (st or 0) < 300):
        log("REFRESH suspend 失败 (HTTP %s)，放弃" % st)
        return False
    if not wait_state(lambda m: m.get("state") != RUNNING, SUSPEND_SETTLE_MAX, "挂起生效"):
        return False
    m = get_machine() or {}
    cd = parse_ts(m.get("cooldownUntil"))
    now = datetime.now(timezone.utc)
    if cd and cd > now:
        wait_s = min((cd - now).total_seconds() + 2, COOLDOWN_CAP)
        log("REFRESH 冷却至 %s，休眠 %.0f 秒" % (m.get("cooldownUntil"), wait_s))
        time.sleep(wait_s)
    else:
        log("REFRESH 未返回冷却时间，按 60 秒兜底冷却")
        time.sleep(60)
    st, _ = api("POST", "/v1/machines/%s/wake" % mid, body={}, timeout=WAKE_TIMEOUT)
    if not (200 <= (st or 0) < 300):
        log("REFRESH wake 失败 (HTTP %s)" % st)
        return False
    return wait_state(lambda m: m.get("state") == RUNNING
                      and (m.get("runtime") or {}).get("ready") is not False,
                      WAKE_MAX_WAIT, "唤醒至 running")


def main():
    if not API_KEY or not MACHINE:
        log("FATAL 缺少 INSTABOX_API_KEY 或 INSTABOX_MACHINE 环境变量")
        return 1
    m = get_machine()
    if not m:
        log("FATAL 机器 %s 状态不可得" % MACHINE)
        return 1
    mid = m.get("id") or MACHINE
    now = datetime.now(timezone.utc)
    rs = parse_ts(m.get("runStartedAt"))
    state = m.get("state")
    mins = ((now - rs).total_seconds() / 60.0) if rs else None
    log("MACHINE %s(%s): state=%s%s cooldownUntil=%s maxRunMinutes=%s" % (
        m.get("name"), mid, state,
        (" 已连续运行 %.0f 分钟" % mins) if mins is not None else " (非运行态)",
        m.get("cooldownUntil"), m.get("maxRunMinutes")))
    cd = parse_ts(m.get("cooldownUntil"))

    if REFRESH_NOW:
        return 0 if do_refresh(mid) else 1

    if state == RUNNING:
        if REFRESH_MINUTES and mins is not None and mins >= REFRESH_MINUTES:
            return 0 if do_refresh(mid) else 1
        st, _ = api("POST", "/v1/machines/%s/wake" % mid, body={}, timeout=WAKE_TIMEOUT)
        ok = 200 <= (st or 0) < 300
        log("HEARTBEAT wake %s" % ("成功 (闲置计时已重置)" if ok else "失败 (HTTP %s)" % st))
        return 0 if ok else 1
    if state in SUSPENDED_STATES:
        if cd and cd > now:
            log("WAIT 冷却未过 (至 %s)，本轮不动作" % m.get("cooldownUntil"))
            return 0
        st, _ = api("POST", "/v1/machines/%s/wake" % mid, body={}, timeout=WAKE_TIMEOUT)
        ok = 200 <= (st or 0) < 300
        log("WAKE %s" % ("已触发" if ok else "失败 (HTTP %s)" % st))
        if ok:
            wait_state(lambda m: m.get("state") == RUNNING, WAKE_MAX_WAIT, "唤醒至 running")
        return 0 if ok else 1
    if state == STOPPED:
        st, _ = api("POST", "/v1/machines/%s/start" % mid, body={})
        log("START HTTP %s" % st)
        return 0 if 200 <= (st or 0) < 300 else 1
    log("OBSERVE 过渡态 %s，本轮仅观察" % state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
