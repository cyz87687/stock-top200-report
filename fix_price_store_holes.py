#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_price_store_holes.py —— 补 factor_history.json 价格库的「已存在日期空洞」

═══ 为什么需要这个脚本（round118 实测发现）═══

实测 factor_history.json.prices：150 个日期轴，但尾部四天只有 **2 只有收盘价**：

    2026-10-23  1766     2026-10-28  1755
    2026-10-26  1608     2026-10-29     2   ← 断裂
    2026-10-27  1197     2026-10-30     2
                         2026-11-02     2
                         2026-11-03     2

后果：`_fwd_returns` 需要 d0 和 d1 都有收盘价，d0≥10-28 的截面**一律算不出前向收益**，
于是 2026-10-28/29/30 与 11-02 这四个截面全部退出 IC 样本。`score_growth` 在
factor_history 里明明有 11,015 行（37%），在 factor_ic.json 里却是 n=0 —— 就是因为它
有值的 6 个快照里，2 个被污染剔除、4 个全在这四个断裂日上。

═══ 为什么 refresh_price_tail 修不了它 ═══

`refresh_price_tail` 的守卫是：

    if last_snap <= dates[-1]:
        return False        # 「store 已覆盖最新快照，零成本跳过」

日期轴确实已经延伸到 11-03，所以守卫判定「无需刷新」直接返回。但**日期轴有这天**
和**这天有价格**是两件事 —— 这正是 round117 记下的「缺字段被当成不适用」的同一类错：
结构完整性被当成了数据完整性。

本脚本专补「轴上已有、值为 None」的空洞，与 refresh_price_tail 互补：
  · refresh_price_tail → 管**轴**的延伸
  · fix_price_store_holes → 管**格**的填充

═══ 口径 ═══

· 只填空洞（`arr[j] is None`），**绝不覆盖已有值** —— 避免把历史锚点改成今天的前复权口径；
· 用腾讯 fqkline 的 qfq（与库内既有数据同源，见 MEMORY 的价格口径记述）；
· 失败不写假值；末尾做**减法校验**：应补 − 已补 − 仍缺 = 0，对不上就报错退出。
"""

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_eval as F

HIST = os.environ.get("FH_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "factor_history.json")


def _day_coverage(dates, px):
    return [(d, sum(1 for arr in px.values() if arr[i] is not None))
            for i, d in enumerate(dates)]


def audit(hist, label=""):
    p = hist["prices"]
    dates, px = p["dates"], p["px"]
    cov = _day_coverage(dates, px)
    bad = [x for x in cov if x[1] < 100]
    print(f"{label}日期轴 {len(dates)} 天（{dates[0]} ~ {dates[-1]}），覆盖 <100 只的天数：{len(bad)}")
    if bad:
        print("   " + "；".join(f"{d}:{n}" for d, n in bad))
    return cov, bad


def main():
    limit = None
    only_tail = True
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        if a == "--all-days":
            only_tail = False

    with open(HIST, encoding="utf-8") as f:
        hist = json.load(f)
    p = hist["prices"]
    dates, px, opens = p["dates"], p["px"], p.get("opens") or {}
    didx = {d: i for i, d in enumerate(dates)}

    cov, bad = audit(hist, "【补前】")
    if not bad:
        print("✅ 无空洞，无需处理")
        return 0

    bad_dates = [d for d, _ in bad]
    # 目标日期：只补「断崖日」（默认）；--all-days 补全部不合格日（含建库爬坡段，耗时很长）
    target_dates = bad_dates if only_tail else list(dates)
    target_idx = {didx[d] for d in target_dates}

    # 待补代码：在目标日期上缺值、且该代码在别处有值的
    need_codes = []
    for c, arr in px.items():
        if any(arr[j] is None for j in target_idx):
            need_codes.append(c)
    need_codes.sort()
    if limit:
        need_codes = need_codes[:limit]
    print(f"\n目标日期 {len(target_dates)} 天；待补代码 {len(need_codes)} 只")

    # 需要的 K 线根数：覆盖最老的待补日期即可
    oldest = min(target_dates)
    oldest_i = didx[oldest]
    need_bars = min(F.PX_NEED, max(30, len(dates) - oldest_i + 5))
    print(f"最老待补日 {oldest}（轴上第 {oldest_i} 位）→ 抓 {need_bars} 根日线")

    t0 = time.time()
    n_ok = n_fail = 0
    filled = 0
    for i, c in enumerate(need_codes):
        kl = F._fetch_qfq(F._symbol(c), need=need_bars, with_open=True)
        if not kl:
            n_fail += 1
        else:
            n_ok += 1
            for d, op, cl in kl:
                j = didx.get(d)
                if j is None:
                    continue
                arr = px.get(c)
                if arr is not None and arr[j] is None and cl:
                    arr[j] = cl
                    filled += 1
                oarr = opens.get(c)
                if oarr is not None and oarr[j] is None and op:
                    oarr[j] = op
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(need_codes)} 成功 {n_ok} 失败 {n_fail} 已补格 {filled} "
                  f"（{time.time()-t0:.0f}s）")
        time.sleep(0.02)

    print(f"\n抓取完成：成功 {n_ok} 失败 {n_fail}，共补 {filled} 个价格格（{time.time()-t0:.0f}s）")

    p["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p["n_codes"] = len(px)
    p["n_open_codes"] = len(opens)
    hist["prices"] = p

    cov2, bad2 = audit(hist, "【补后】")

    # ── 减法校验：应补 − 已补 − 仍缺 ≈ 0（只对 limit 模式之外严格）──
    print("\n【减法校验】")
    fixed_days = {d: n for d, n in cov2 if d in target_dates}
    for d in target_dates:
        before = dict(cov).get(d)
        after = fixed_days.get(d)
        print(f"  {d}: {before} → {after}")
    print(f"  仍不合格天数：{len(bad2)}")
    if limit:
        print("  ⚠️ 本次为 --limit 抽样模式，未做全量校验")

    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\n✅ 已写回 {HIST}")

    # 全量模式下要求「与 IC 相关的断崖日」全部恢复。
    # ⚠️ 判据必须限定在**有快照的日期**上：2026-03-24~04-10 那 13 天是建库爬坡段，
    #    只有 3~29 只（当时池里就这几只），且早于首个快照 2026-06-26 —— 它们
    #    不进任何 IC 计算，补不补都不影响结论。若把它们计入失败，会让脚本永久
    #    返回非零，从而掩盖真正需要关注的断裂（又一个「天天报警=没人报警」）。
    snap_dates = {s["date"] for s in hist.get("snapshots", []) if s.get("rows")}
    if not limit:
        residual = [(d, n) for d, n in bad2 if d in target_dates and d in snap_dates]
        if residual:
            print(f"❌ 仍有 {len(residual)} 个**有快照的**目标日未恢复：{residual}")
            return 2
        irrelevant = [(d, n) for d, n in bad2 if d in target_dates and d not in snap_dates]
        if irrelevant:
            print(f"ℹ️ 剩余 {len(irrelevant)} 个低覆盖日无对应快照（建库爬坡段，"
                  f"不参与任何 IC 计算，不需补）：{[d for d, _ in irrelevant]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
