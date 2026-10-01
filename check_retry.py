#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重试门禁：决定本次触发是否真的执行续期。

设计：workflow 每小时被 cron 触发一次，但只有「本轮还没成功」时才真跑。
一个「本轮」= 从当天计划槽(UTC 01:00, 即北京 09:00)开始到下一个计划槽之前。

规则：
- 本轮内已有 run 以 success 结束  -> 跳过（今天已办完，不再打扰）
- 本轮内已完成 run 数 >= 1(每日) + 10(重试) -> 跳过，并在刚好超限时通知放弃
- 其它情况 -> 执行
- force=true (手动触发参数) -> 无条件执行

门禁自身异常时默认放行，避免因 API 抖动导致整天不续期。
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

DAILY_SLOT_UTC_HOUR = 1      # 对应 cron 的 0 1 * * *
MAX_RETRIES = 10             # 每天最多补跑次数
API = "https://api.github.com"


def emit(should_run, reason, attempts, detail):
    out = os.environ.get("GITHUB_OUTPUT")
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"should_run={'true' if should_run else 'false'}\n")
        f.write(f"reason={reason}\n")
        f.write(f"attempts={attempts}\n")
    print(f"[gate] should_run={should_run} reason={reason} attempts={attempts} | {detail}")


def cycle_start(now):
    slot = now.replace(hour=DAILY_SLOT_UTC_HOUR, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= datetime.timedelta(days=1)
    return slot


def fetch_runs(token, repo, wf_file):
    url = (f"{API}/repos/{repo}/actions/workflows/{wf_file}/runs"
           f"?per_page=50&status=completed")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hiden-renew-gate",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("workflow_runs", [])


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    wf_file = os.environ.get("WORKFLOW_FILE") or "HidenCloud_Renew.yml"
    cur_run = os.environ.get("RUN_ID") or ""
    force = (os.environ.get("FORCE") or "").lower() in ("1", "true", "yes")

    if force:
        emit(True, "forced", 0, "手动 force 触发，无条件执行")
        return

    if not token or not repo:
        emit(True, "gate_error", 0, "缺少 token/repository，门禁放行")
        return

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        start = cycle_start(now)
        runs = fetch_runs(token, repo, wf_file)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as e:
        emit(True, "gate_error", 0, f"查询失败，门禁放行: {e}")
        return

    in_cycle = []
    for r in runs:
        if str(r.get("id")) == str(cur_run):
            continue
        created = (r.get("created_at") or "").replace("Z", "+00:00")
        try:
            ts = datetime.datetime.fromisoformat(created)
        except ValueError:
            continue
        if ts >= start:
            in_cycle.append((ts, r.get("conclusion")))

    in_cycle.sort()
    attempts = len(in_cycle)
    succeeded = any(c == "success" for _, c in in_cycle)

    if succeeded:
        emit(False, "already_done", attempts,
             f"本轮第 {attempts + 1} 次之前已成功，跳过")
        return

    if attempts >= 1 + MAX_RETRIES:
        # 刚好超限的那一次通知放弃，之后保持安静，避免每小时刷屏
        reason = "giveup" if attempts == 1 + MAX_RETRIES else "exhausted"
        emit(False, reason, attempts,
             f"本轮已执行 {attempts} 次仍无成功，超出每日 {MAX_RETRIES} 次重试上限")
        return

    kind = "daily" if attempts == 0 else f"retry#{attempts}"
    emit(True, kind, attempts, f"本轮第 {attempts + 1} 次执行（{kind}）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 任何意外都放行，保证续期不被门禁卡死
        print(f"[gate] 未预期异常: {e}", file=sys.stderr)
        try:
            emit(True, "gate_error", 0, f"未预期异常，放行: {e}")
        except Exception:
            pass
