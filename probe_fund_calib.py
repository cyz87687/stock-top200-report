#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fund 填充方案的第二轮探针：PE 标定校准能否把保真度拉到可用水平。

第一轮结论（probe_fund_backfill.py）：
  · ρ(静态PE, 前瞻PE) = 0.86~0.93 —— 两者高度同向
  · 但 前瞻PE / 静态PE 中位倍数 = 1/0.646 ≈ 1.548 → 前瞻PE 系统性**高于**静态PE 54.8%
    （注意：ratio=静态/前瞻=0.646，故前瞻 = 静态/0.646 = 静态×1.548）
  · 直接用静态PE 重建 fund → ρ(真实,重建) 仅 0.5261，MAE 1.526/5

本轮测三个变体，判断是「口径偏移」还是「信息缺失」导致降级：
  V0 原样（基线）
  V1 全局标定：静态PE × k，k 取全样本 前瞻/静态 中位倍数
  V2 分时段标定：k 按月重估（防止 regime 漂移）
  V3 排序口径：不追求复现分档，只测「若把重建分当作独立因子，其与原分的截面秩相关」
      —— 这决定它能否用于 IC 计算（IC 只用秩，不用绝对值）

用法：python probe_fund_calib.py
"""
import os
import sys
import json
import glob
import math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import ep_size_ind as esi
import step2_stockscorer_v2 as s2
import audit_round21 as r21


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    def _rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    nu = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return nu / (dx * dy) if dx * dy > 1e-12 else None


def med(v):
    v = sorted(x for x in v if is_num(x))
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def ttm_static_pe(ind, P, price):
    ep = esi.raw_ep(ind, P, price)
    if not is_num(ep) or ep <= 0:
        return None
    pe = 1.0 / ep
    return pe if (is_num(pe) and 0 < pe < 5000) else None


def growth_pit(ind, P):
    for k in ("归属母公司净利润增长率", "营业总收入增长率"):
        v = (ind.get(k) or {}).get(P)
        if is_num(v):
            return v
    return None


def _f(ind, k, P):
    v = (ind.get(k) or {}).get(P)
    return v if is_num(v) else None


def cagr3_pit(ind, P):
    ps = (ind.get("扣非净利润") or {})
    cur = ps.get(P)
    if not is_num(cur) or cur <= 0:
        return None
    y = int(str(P)[:4]) - 3
    base = ps.get(str(y) + str(P)[4:])
    if not is_num(base) or base <= 0:
        return None
    try:
        return ((cur / base) ** (1.0 / 3.0) - 1.0) * 100.0
    except Exception:
        return None


def build_score(pe_scaled, g, roe, gm, cag, name, code_full):
    sector, sw_l2, _ = s2.resolve_sector(name, code_full)
    sub = s2.get_sub_theme_label(name)
    try:
        fs, _fr, _fp, _gr, _ld, _su = s2.score_fund(
            pe_scaled, None, g, code_full, sector,
            pe_ladder_data=None, roe=roe, gross_margin=gm,
            sub_theme_label=sub, hist_loss_free=False,
            fwd_np=None, sw_l2=sw_l2, cagr3=cag, q3_info=None,
            roe_unit="pct")
    except Exception:
        return None
    return fs if is_num(fs) else None


def main():
    print("=" * 100)
    print("【fund 填充 · PE 标定校准探针】")
    print("=" * 100)

    em = esi.load_em_all()
    files = sorted(glob.glob("data/fwd_hist/*.json"))
    print(f"EM {len(em)} 只 · fwd_hist {len(files)} 天")
    print()

    # ---- Pass 1: 收集全样本，估标定系数
    per_date_raw = {}      # date -> list of dict(real, pe_static, pe_fwd, inputs...)
    ratios = []
    for fp in files:
        d0 = json.load(open(fp, encoding="utf-8"))
        date = d0.get("date")
        rows = d0.get("rows") or []
        recs = []
        for r in rows:
            c6 = str(r.get("code") or "")[-6:]
            ind = em.get(c6)
            if not ind:
                continue
            periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = r21.pit_period(periods, date)
            if P is None:
                continue
            price = r.get("price")
            if not (is_num(price) and price > 0):
                continue
            pe_s = ttm_static_pe(ind, P, price)
            pe_f = r.get("fwd_pe")
            sf = r.get("score_fund")
            if not (is_num(pe_s) and pe_s > 0 and is_num(pe_f) and pe_f > 0):
                continue
            ratios.append(pe_f / pe_s)      # 前瞻 / 静态
            recs.append({"c6": c6, "real": sf if is_num(sf) else None,
                         "pe_s": pe_s, "pe_f": pe_f,
                         "g": growth_pit(ind, P),
                         "roe": _f(ind, "摊薄净资产收益率", P),
                         "gm": _f(ind, "毛利率", P),
                         "cag": cagr3_pit(ind, P),
                         "name": r.get("name") or "",
                         "code": r.get("code") or ""})
        if recs:
            per_date_raw[date] = recs

    k_global = med(ratios)
    print(f"【标定系数】前瞻PE = 静态PE × k，k 全样本中位 = {k_global:.4f}"
          f"（n={len(ratios)}）")
    qs = sorted(ratios)
    print(f"  分位 p10={qs[int(.1*len(qs))]:.3f}  p25={qs[int(.25*len(qs))]:.3f}  "
          f"p50={k_global:.3f}  p75={qs[int(.75*len(qs))]:.3f}  p90={qs[int(.9*len(qs))]:.3f}")
    print()

    # ---- Pass 2: 三个变体
    res = {f"V{i}": {"xs": [], "ys": [], "per_date": []} for i in range(4)}
    for date in sorted(per_date_raw):
        recs = per_date_raw[date]
        k_month = med([r["pe_f"] / r["pe_s"] for r in recs]) or k_global

        for vi, kk in enumerate(["raw", "global", "month", "shift"]):
            xs, ys = [], []
            for r in recs:
                if r["real"] is None:
                    continue
                if kk == "raw":
                    pe_use = r["pe_s"]
                elif kk == "global":
                    pe_use = r["pe_s"] * k_global
                elif kk == "month":
                    pe_use = r["pe_s"] * k_month
                else:
                    pe_use = r["pe_s"] * k_global
                sc = build_score(pe_use, r["g"], r["roe"], r["gm"], r["cag"],
                                 r["name"], r["code"])
                if sc is None:
                    continue
                xs.append(r["real"])
                ys.append(sc)
            if len(xs) >= 20:
                rho = spearman(xs, ys)
                res[f"V{vi}"]["xs"] += xs
                res[f"V{vi}"]["ys"] += ys
                res[f"V{vi}"]["per_date"].append((date, rho, len(xs)))

    labels = {"V0": "原样（静态PE，不校准）", "V1": "全局标定 k",
              "V2": "按月标定 k", "V3": "全局标定（同V1，用于秩口径对照）"}
    print("=" * 100)
    print("【各变体保真度】")
    for k in sorted(res):
        xs, ys = res[k]["xs"], res[k]["ys"]
        if len(xs) < 20:
            continue
        rho = spearman(xs, ys)
        n = len(xs)
        mae = sum(abs(a - b) for a, b in zip(xs, ys)) / n
        exact = sum(1 for a, b in zip(xs, ys) if abs(a - b) < 1e-9) / n * 100
        near = sum(1 for a, b in zip(xs, ys) if abs(a - b) <= 1.0) / n * 100
        rhos = [r for _, r, _ in res[k]["per_date"] if r is not None]
        print(f"  {k} {labels[k]:28s} n={n:5d}  ρ={rho:.4f}  MAE={mae:.3f}  "
              f"完全一致={exact:5.1f}%  ≤1分={near:5.1f}%  "
              f"逐日ρ中位={med(rhos):.3f}" if rhos else "")

    print()
    print("=" * 100)
    print("【判读标准】")
    print("  ρ ≥ 0.85 → 可作等价填充（历史 IC 可代表真实 fund）")
    print("  0.70 ≤ ρ < 0.85 → 可作代理因子，但必须在页面/报告标注「历史为代理口径」")
    print("  ρ < 0.70 → 不得回填历史；只能缩短评估窗口到 fund 真实覆盖区间")
    print("=" * 100)

    out = {
        "k_global": k_global,
        "n_ratio": len(ratios),
        "variants": {k: {"n": len(v["xs"]),
                         "spearman": spearman(v["xs"], v["ys"]) if len(v["xs"]) >= 20 else None,
                         "mae": (sum(abs(a - b) for a, b in zip(v["xs"], v["ys"])) / len(v["xs"])
                                 if v["xs"] else None)}
                     for k, v in res.items()},
    }
    with open("probe_fund_calib.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("→ probe_fund_calib.json")


if __name__ == "__main__":
    main()
