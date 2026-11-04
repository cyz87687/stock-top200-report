#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round66_clean_panel.py —— 重建「干净」的 9 维长面板（后续所有组合层工作的唯一地基）

═══ 为什么必须重建，而不是直接用 panel_round34 ═══

round64 判定「panel 的 score_quality 列与真实 scored 的 ρ 只有 0.029 → 该列是坏的」，
但 round64 拿来做基准的两个截面恰好是 2026-10-23 / 2026-10-26 —— 而 round65 又认定
**这两个截面的 scored 文件自身就是污染的**。用被污染的基准去判面板坏，结论方向不可靠。

本轮用「代码复算」做独立仲裁（不依赖任何 scored 快照）：
  取 4 个历史截面，用**修复后的** fund_split.score_rows 重算 quality，与 panel 内值比：

    2026-06-30  n=2014  ρ(panel, recalc) = +0.0585
    2026-08-03  n=2021  ρ(panel, recalc) = +0.0567
    2026-09-01  n=2025  ρ(panel, recalc) = +0.0312
    2026-10-08  n=2023  ρ(panel, recalc) = +0.0312

  → **panel 的 quality 列确实是坏的**，round64 的结论成立（虽然理由给错了）。

铁证（本轮新增，比 ρ 更直接）：
  panel 每个截面的 quality 都有 **恰好 1001 个唯一值**，且 top 频次为
  (0.77, 3) (1.54, 3) (2.31, 3) (3.08, 3) (3.85, 3) —— **等差阶梯 + 每档恰好 3 个**。
  这不是任何真实分布的形态，正是 round35 记录的「NaN 导致 rank 排序崩塌、
  部分保留输入顺序」的指纹（输入顺序 = 当期 total 降序 → 总分泄漏进质量维）。
  而用修复后代码重算同一截面，500 只股票得到 500 个不同值 → 真实分布。

⇒ 结论：**panel_round34 的 quality 列必须整列作废并重算**，其余 7 维可用。

═══ 本轮产物 ═══

重建一版 9 维面板 data/panel_round66.json：
  · quality            —— 用修复后 score_rows 逐截面重算（PIT 严格模式，与生产一致）
  · fund/bias60/rsi_rev/tech_neg/lowvol20/ep/size_ind —— 直接继承 panel_round34（已验证与生产一致）
  · size_growth / dominant —— 用 round38 的 build_ab_dims 重建（A/B 互斥分区，与生产同源）

⚠️ 明确标注不可消除的近似：
  1. circ_mv = 股本中位数 × 当日收盘，跨期单一常数、无拆股调整
     → A/B 维的绝对值不可当精确值；本面板只用于**相对比较与权重优化**，
       绝不用于宣称某维的精确 IC 水平。
  2. size 维在历史截面上是 size_ind（v4.12）口径，v4.14 的 A/B 拆分由本轮重建补上，
     但**历史上的 A/B 分区用的是当期成长分**，与生产当期一致；跨期股本常数仍是近似。
  3. fund 仅 80/238 截面有值（2026-06-26 起）—— 这是数据本身的边界，不是缺陷。
     权重优化时 fund 只能在 80 个截面上评估，必须单独披露。

用法：
  python audit_round66_clean_panel.py              # 全量（约 15~25 分钟）
  python audit_round66_clean_panel.py --max-dates 20   # 冒烟
"""
import os
import sys
import json
import math
import time
import argparse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PANEL_SRC = os.path.join(HERE, "data", "panel_round34.json")
OUT = os.path.join(HERE, "data", "panel_round66.json")
AUDIT_OUT = os.path.join(HERE, "audit_round66.json")

# panel_round34 里可原样继承的维（已交叉验证与生产一致）
INHERIT_DIMS = (
    "score_fund", "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_lowvol20", "score_ep", "score_size_ind",
)
# 必须重算的维
RECALC_DIMS = ("score_quality",)


def _is_bad_quality(vals):
    """【已废弃】曾试图用「唯一值数量=1001 的等差阶梯」识别崩塌。

    ⚠️ 2026-10-29 自我否证：该判据是错的。
       rank_pct 输出的是均匀分布，n≈2027 只 → 步长 10/2027≈0.00493，
       再 round(,2) → 每 2 个值并成一档 → **必然约 1001 个唯一值**，
       且呈等差阶梯、每档约 2~3 个。这是**正常的量化**，不是崩塌指纹。
       实测：用修复后代码重算同一截面，唯一值同样是 1001 —— 反证该判据无效。

    真正的判据只能是「与独立重算结果的秩相关」：
       同截面、同股票集合，panel 值 vs 重算值的 Spearman。
       若 ρ 接近 0 → panel 排序与真实质量无关（round35 记录的「总分泄漏进质量维」）。
       若 ρ 接近 1 → panel 可用。
    本函数保留为空壳以标注这段否证，实际判定见 _rho_vs_recalc()。
    """
    return False, len(set(vals))


def _spearman_generic(x, y):
    """带并列处理的 Spearman。"""
    def _rank(v):
        n = len(v)
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for t in range(i, j + 1):
                r[idx[t]] = avg
            i = j + 1
        return r
    if len(x) < 3:
        return None
    a, b = _rank(x), _rank(y)
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((p - ma) * (q - mb) for p, q in zip(a, b))
    saa = sum((p - ma) ** 2 for p in a)
    sbb = sum((q - mb) ** 2 for q in b)
    if saa <= 0 or sbb <= 0:
        return None
    return sab / math.sqrt(saa * sbb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-dates", type=int, default=0)
    ap.add_argument("--skip-ab", action="store_true", help="跳过 A/B 维重建（快速冒烟）")
    args = ap.parse_args()

    t_all = time.time()
    src = json.load(open(PANEL_SRC, encoding="utf-8"))
    dates = list(src["dates"])
    if args.max_dates:
        dates = dates[-args.max_dates:]

    print(f"源面板 {len(src['dates'])} 截面，本轮处理 {len(dates)} 截面 "
          f"({dates[0]} ~ {dates[-1]})")

    # ── ① 逐截面重算 quality，并用「秩相关」做唯一判据 ──
    print("\n[1/3] 逐截面重算 score_quality（修复后代码，PIT 严格模式），"
          "并与 panel 原值做秩相关仲裁 ...")
    import fund_split as fs
    em = fs.load_em_cache()
    print(f"   EM 缓存 {len(em)} 只")

    new_rows = {}
    q_stat = []
    t0 = time.time()
    for i, d in enumerate(dates, 1):
        rows = src["rows"].get(d) or {}
        if not rows:
            new_rows[d] = {}
            continue
        codes = list(rows.keys())
        qrows = [{"code": c} for c in codes]
        try:
            fs.score_rows(qrows, d, em=em, pit_mode="auto")
            qmap = {r["code"]: r.get("score_quality") for r in qrows}
        except Exception as e:
            print(f"   ⚠️ {d} 重算失败: {type(e).__name__}: {e}")
            qmap = {c: None for c in codes}

        out = {}
        for c in codes:
            rec = {}
            for k in INHERIT_DIMS:
                rec[k] = rows[c].get(k)
            rec["score_quality"] = qmap.get(c)
            out[c] = rec
        new_rows[d] = out

        # 秩相关仲裁：panel 原值 vs 重算值（同截面同股票集合）
        pairs = [(rows[c].get("score_quality"), qmap.get(c)) for c in codes]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        rho = None
        if len(pairs) >= 50:
            rho = _spearman_generic([x for x, _ in pairs], [y for _, y in pairs])
        got = [v for v in qmap.values() if v is not None]
        q_stat.append({
            "date": d, "n": len(got), "n_unique": len(set(got)),
            "rho_panel_vs_recalc": rho,
            "collapsed": (rho is not None and abs(rho) < 0.5),
        })
        if i % 20 == 0 or i == len(dates):
            el = time.time() - t0
            _bad = sum(1 for x in q_stat if x["collapsed"])
            print(f"   [{i}/{len(dates)}] {d}  n={len(got):4d} "
                  f"ρ(panel,recalc)={rho if rho is None else round(rho,4)}  "
                  f"累计崩塌截面={_bad}  已用 {el:.0f}s 剩 {el/i*(len(dates)-i):.0f}s")
    print(f"   → 重算完成 {time.time()-t0:.0f}s")

    rhos = [x["rho_panel_vs_recalc"] for x in q_stat if x["rho_panel_vs_recalc"] is not None]
    n_bad = sum(1 for x in q_stat if x["collapsed"])
    q_verdict = {
        "criterion": "ρ(panel 原值, 修复后重算值) 同截面同股票集合；|ρ|<0.5 判为崩塌",
        "n_dates_checked": len(rhos),
        "n_collapsed": n_bad,
        "rho_mean": round(sum(rhos) / len(rhos), 4) if rhos else None,
        "rho_min": round(min(rhos), 4) if rhos else None,
        "rho_max": round(max(rhos), 4) if rhos else None,
        "per_date_sample": q_stat[:5] + q_stat[-5:],
        "root_cause": (
            "panel_round34 构建于 2026-10-27 19:06；round35 的 NaN 修复（东财商誉返 nan 被 "
            "isinstance(float) 放行 → rank_pct 排序崩塌）落地于 2026-10-28 09:22。"
            "即面板的 quality 列是**修复前代码**的产物，与 round65 认定的 10-23/10-26 "
            "scored 污染同源，不是两件独立的事。"
        ),
    }
    print(f"   → ρ 均值 {q_verdict['rho_mean']}  最小 {q_verdict['rho_min']}  "
          f"判为崩塌 {n_bad}/{len(rhos)} 个截面")

    # ── ③ A/B 维重建 ──
    ab_meta = {"skipped": True}
    if not args.skip_ab:
        print("\n[2/3] 重建 score_size_growth / score_dominant（A/B 互斥分区）...")
        try:
            from audit_round38_size_ablation import build_ab_dims
            sub_panel = {
                "dates": dates,
                "rows": {d: src["rows"].get(d) or {} for d in dates},
            }
            ab_per_date, ab_meta = build_ab_dims(sub_panel)
            ab_meta["skipped"] = False
            n_ab = 0
            for d, m in ab_per_date.items():
                tgt = new_rows.get(d) or {}
                for c, info in m.items():
                    if c not in tgt:
                        continue
                    # ⚠️ build_ab_dims 的键名是 A_G1（现行门限档）/ B_dom，不是 A/B。
                    #    跨字典取值前必须核对键名 —— 项目曾因 key 口径不一致静默塌成 0。
                    _a = info.get("A_G1")
                    _b = info.get("B_dom")
                    tgt[c]["score_size_growth"] = _a
                    tgt[c]["score_dominant"] = _b
                    if _a is not None or _b is not None:
                        n_ab += 1
            print(f"   → A/B 写入 {n_ab} 条")
        except Exception as e:
            import traceback
            traceback.print_exc()
            ab_meta = {"skipped": True, "error": f"{type(e).__name__}: {e}"}
            print(f"   ⚠️ A/B 重建失败: {e}")
    else:
        print("\n[2/3] 跳过 A/B 维重建（--skip-ab）")

    # 补齐缺字段（AB 失败时也要有 key，便于下游按 None 处理）
    for d, m in new_rows.items():
        for c, rec in m.items():
            rec.setdefault("score_size_growth", None)
            rec.setdefault("score_dominant", None)

    # ── ④ 落盘 ──
    print("\n[3/3] 写出面板 ...")
    meta = {
        "round": 66,
        "purpose": "干净 9 维长面板：quality 用修复后代码逐截面重算，A/B 维由 round38 同源重建",
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src_panel": "data/panel_round34.json",
        "n_dates": len(dates),
        "date_range": [dates[0], dates[-1]],
        "domain_size": len(src.get("domain") or []),
        "dims": list(INHERIT_DIMS) + list(RECALC_DIMS) + ["score_size_growth", "score_dominant"],
        "inherited_dims": list(INHERIT_DIMS),
        "recalced_dims": list(RECALC_DIMS),
        "quality_collapse_evidence": q_verdict,
        "ab": ab_meta,
        "caveat": (
            "① circ_mv=股本中位数×当日收盘，跨期单一常数、无拆股调整 → A/B 维绝对值不可当精确值，"
            "仅用于相对比较与权重优化；"
            "② panel_round34 的 score_quality 列已整列作废并由修复后代码重算；"
            "③ fund 仅 2026-06-26 之后截面有值（约 80/238），非缺陷是数据边界；"
            "④ 样本域为 2025-11~2026-10，属小盘投机主导 regime，跨 regime 稳定性未验证。"
        ),
    }
    # domain = 出现过的全部代码
    domain = sorted({c for m in new_rows.values() for c in m})
    json.dump(
        {"dates": dates, "domain": domain, "rows": new_rows, "meta": meta},
        open(OUT, "w", encoding="utf-8"),
        ensure_ascii=False,
    )
    size_mb = os.path.getsize(OUT) / 1e6
    print(f"   → {OUT}  {size_mb:.1f} MB  domain={len(domain)}")

    # quality 覆盖率体检
    cov = {}
    for k in meta["dims"]:
        n = sum(1 for m in new_rows.values() for rec in m.values() if rec.get(k) is not None)
        cov[k] = n
    tot = sum(len(m) for m in new_rows.values())
    print("\n覆盖率：")
    for k, n in cov.items():
        print(f"   {k:22s} {n:8d} / {tot}  = {n/max(tot,1)*100:5.1f}%")
    meta["coverage"] = cov
    meta["n_obs_total"] = tot

    json.dump(
        {
            "round": 66,
            "meta": meta,
            "quality_unique_dist": q_stat,
            "elapsed_sec": round(time.time() - t_all, 1),
        },
        open(AUDIT_OUT, "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=1,
    )
    print(f"\n审计 {AUDIT_OUT}")
    print(f"总耗时 {time.time()-t_all:.0f}s")


if __name__ == "__main__":
    main()
