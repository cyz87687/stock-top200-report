#!/usr/bin/env python3
"""pit_store.py —— PIT（Point-In-Time）取数结构与可用时点守卫。

round130 / P1-3（2026-11-05）
------------------------------
审计发现（§四）：项目当前按「报告期法定披露截止日」筛财务缓存：

    `fund_split.disclosure_date()` / `build_panel_long.period_deadline()`

这只是**近似 PIT**。它不能覆盖：实际公告时间早于/晚于截止日、延期披露、
以及财报发布后的**历史修订（重述/更正）**。而 `data/fin_cache_em` 是单份
「当前快照」（每个文件只有 `cached_at`），没有 `announcement_time`、
`available_time` 或 `revision_id` —— 历史查询无法取「当时已知的那一版」。

本模块提供可落地的完整结构 + 守卫，并**如实量化本地数据缺口**：

* `make_pit_record()`  : 生成带完整时点/版本元数据的 PIT 记录；缺真实公告时间
                         时退回法定截止日并标 `provenance=statutory_deadline_approx`，
                         绝不冒充真实公告时点。
* `asof_view()`        : 只返回 `available_time <= asof` 的记录，按版本取最新，
                         可选 `require_revision` 严格模式。
* `assert_no_lookahead()`: 历史查询后的前视断言（供回测/面板脚本调用）。
* `audit_cache()`      : 遍历真实缓存，输出公告时点/修订号覆盖率与缺口结论。

⚠️ 边界：本模块**不能凭空创造历史修订数据**。要真正达到严格 PIT，
   需接入带公告时点与修订版本的历史财务库；届时把每条记录经
   `make_pit_record()` 落库即可，查询侧无需再改。当前 `audit_cache()` 的
   覆盖率数字即「还差多少」的可验证证据。
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(HERE, "data", "fin_cache_em")

PROVENANCE_ACTUAL = "actual_announcement"
PROVENANCE_APPROX = "statutory_deadline_approx"
REVISION_UNKNOWN = "UNKNOWN"


def statutory_deadline(period):
    """报告期 -> 法定披露截止日（与 fund_split.disclosure_date 同口径）。"""
    p = str(period)
    if len(p) != 8 or not p.isdigit():
        return None
    y, md = int(p[:4]), p[4:]
    return {"0331": "%d-04-30" % y, "0630": "%d-08-31" % y,
            "0930": "%d-10-31" % y, "1231": "%d-04-30" % (y + 1)}.get(md)


def make_pit_record(period, values, announcement_time=None, available_time=None,
                    revision_id=None, source=None, asof_note=None):
    """构造一条 PIT 记录。

    announcement_time : 真实公告时点（YYYY-MM-DD 或 ISO datetime），可为 None
    available_time    : 数据实际可查询时点；缺省 = announcement_time 或法定截止日
    revision_id       : 版本/修订号；None → UNKNOWN（严格模式下会被排除）
    """
    at = announcement_time or available_time
    prov = PROVENANCE_ACTUAL if announcement_time else PROVENANCE_APPROX
    avail = available_time or at or statutory_deadline(period)
    if not avail:
        raise ValueError(f"无法确定报告期 {period} 的 available_time")
    return {
        "period": str(period),
        "values": dict(values or {}),
        "announcement_time": announcement_time,
        "available_time": avail,
        "revision_id": revision_id or REVISION_UNKNOWN,
        "revision_known": revision_id is not None and revision_id != REVISION_UNKNOWN,
        "provenance": prov,
        "source": source,
        "note": asof_note,
    }


def asof_view(records, asof, require_revision=False):
    """取 asof 时点「当时已知」的记录视图。

    require_revision=True 时，修订号未知的记录一律排除（严格 PIT）；
    False 时保留但标 `degraded=True`，由调用方决定是否可用于结论。
    """
    asof = str(asof)[:10]
    avail, future, unknown_rev = [], [], []
    for r in records or []:
        at = str(r.get("available_time") or "")[:10]
        if not at or at > asof:
            future.append(r)
            continue
        if not r.get("revision_known"):
            unknown_rev.append(r)
            if require_revision:
                continue
        avail.append(r)
    # 同一报告期只保留 available_time 最新（其次 revision_id 字典序最大）的一条
    best = {}
    for r in avail:
        k = r["period"]
        cur = best.get(k)
        key = (str(r.get("available_time") or ""), str(r.get("revision_id") or ""))
        if cur is None or key > cur[0]:
            best[k] = (key, r)
    view = [v[1] for _, v in sorted(best.items(), reverse=True)]
    prov = Counter(str(r.get("provenance")) for r in view)
    return {
        "asof": asof,
        "records": view,
        "n_total": len(records or []),
        "n_available": len(view),
        "n_excluded_future": len(future),
        "n_revision_unknown": len(unknown_rev),
        "require_revision": bool(require_revision),
        "degraded": bool(unknown_rev and not require_revision) or
                    bool(prov.get(PROVENANCE_APPROX)),
        "provenance_counts": dict(prov),
        "strict_pit": bool(require_revision and not prov.get(PROVENANCE_APPROX)
                           and not view == []),
    }


def assert_no_lookahead(records, asof, context="PIT 查询"):
    """前视断言：任何 available_time > asof 的记录都不得进入历史特征。"""
    asof = str(asof)[:10]
    bad = [r for r in (records or [])
           if str(r.get("available_time") or "")[:10] > asof]
    if bad:
        raise ValueError(
            f"{context}: {len(bad)} 条记录 available_time > {asof}（前视泄漏），"
            f"示例 {[r.get('period') for r in bad[:3]]}")
    return True


def audit_cache(cache_dir=DEFAULT_CACHE, limit=None):
    """遍历真实财务缓存，量化 PIT 元数据覆盖率。返回可落盘的 dict。"""
    files = sorted(glob.glob(os.path.join(cache_dir, "*.json")))
    if limit:
        files = files[:limit]
    n_files = 0
    n_indicator_rows = 0
    n_period_values = 0
    periods = set()
    has_ann = 0
    has_avail = 0
    has_rev = 0
    cached_at = set()
    src_fields = Counter()
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        n_files += 1
        cached_at.add(str(d.get("cached_at") or "")[:10])
        for k in d.keys():
            src_fields[k] += 1
        for row in d.get("records") or []:
            n_indicator_rows += 1
            if (row.get("announcement_time") or row.get("公告日期")
                    or row.get("ann_date")):
                has_ann += 1
            if row.get("available_time"):
                has_avail += 1
            if row.get("revision_id"):
                has_rev += 1
            for k, v in row.items():
                if isinstance(k, str) and len(k) == 8 and k.isdigit():
                    n_period_values += 1
                    periods.add(k)
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cache_dir": os.path.relpath(cache_dir, HERE),
        "n_files": n_files,
        "n_indicator_rows": n_indicator_rows,
        "n_period_values": n_period_values,
        "n_distinct_periods": len(periods),
        "period_range": [min(periods), max(periods)] if periods else [],
        "snapshot_dates": sorted(x for x in cached_at if x),
        "coverage": {
            "rows_with_announcement_time": has_ann,
            "rows_with_available_time": has_avail,
            "rows_with_revision_id": has_rev,
            "announcement_time_pct": round(100.0 * has_ann / max(1, n_indicator_rows), 4),
            "revision_id_pct": round(100.0 * has_rev / max(1, n_indicator_rows), 4),
        },
        "observed_top_level_fields": dict(src_fields),
        "verdict": "PIT_INCOMPLETE",
        "reason": ("缓存为单份当前快照：无真实公告时点、无历史修订版本，"
                   "只能按法定披露截止日近似。此缺口无法由代码修补，"
                   "须接入带 ann_time/revision_id 的历史财务库。"),
        "required_schema": ["announcement_time", "available_time",
                            "revision_id", "source"],
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="审计真实缓存的 PIT 覆盖率")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "pit_audit_round130.json"))
    args = ap.parse_args()
    if not args.audit:
        ap.print_help()
        return 0
    r = audit_cache(args.cache_dir, args.limit)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("observed_top_level_fields",)},
                     ensure_ascii=False, indent=2))
    print(f"\n写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
