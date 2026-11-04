#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fund 维历史填充可行性探针（一次性诊断，不写入任何生产数据）

目的：回答一个先决问题 ——
  能否用「PIT 可重算的静态 PE + PIT 财务」重建 fund 分，
  使其与 fwd_hist 中的真实 fund 分足够一致（从而可以回填历史）？

fund 分的入参可分三类：
  A 类 PIT 可重算：PE(静态, 由 PIT 扣非TTM + 当日价反推)、growth、roe、gross_margin、cagr3
  B 类 历史不可得：fwd_pe(机构前瞻PE)、pe_ladder(三年预测PE)、fwd_np(一致预期)、q3_info
  C 类 标签类：sector / sw_l2 / sub_theme_label（可由 name+code 解析）

探针逻辑：在 fwd_hist 有真实分的日期上，用 A+C 类重建（B 类置 None），
对比重建分 vs 真实分。若 Spearman 高 → 填充方案成立；若低 → 只能「标记为代理因子」。

用法：python probe_fund_backfill.py [--days 10]
"""
import os
import sys
import json
import glob
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import ep_size_ind as esi
import step2_stockscorer_v2 as s2
import audit_round21 as r21


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def spearman(xs, ys):
    """秩相关；并列取平均秩。"""
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
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx * dy < 1e-12:
        return None
    return num / (dx * dy)


def ttm_static_pe(ind, P, price):
    """静态 PE = 市值 / 扣非净利润TTM。与 esi.raw_ep 严格互为倒数，
    以保证「PIT 可得性」口径一致（同源股本反推法）。"""
    ep = esi.raw_ep(ind, P, price)
    if not is_num(ep) or ep <= 0:
        return None
    pe = 1.0 / ep
    return pe if (is_num(pe) and 0 < pe < 5000) else None


def growth_pit(ind, P):
    """PIT 报告期归母净利润累计同比（EM 口径，与 step2 的 growth 主源同族）。
    优先用 EM 现成增长率字段；缺失则用相邻可比期自算。"""
    g = (ind.get("归属母公司净利润增长率") or {}).get(P)
    if is_num(g):
        return g
    g2 = (ind.get("营业总收入增长率") or {}).get(P)
    if is_num(g2):
        return g2
    return None


def roe_pit(ind, P):
    v = (ind.get("摊薄净资产收益率") or {}).get(P)
    if is_num(v):
        return v          # EM 为百分数
    v2 = (ind.get("净资产收益率") or {}).get(P)
    return v2 if is_num(v2) else None


def gm_pit(ind, P):
    v = (ind.get("毛利率") or {}).get(P)
    return v if is_num(v) else None


def cagr3_pit(ind, P):
    """三年扣非净利 CAGR(%)。P 为 YYYYMMDD；用同季度的 P-3年 与 P 比较。"""
    ps = (ind.get("扣非净利润") or {})
    cur = ps.get(P)
    if not is_num(cur):
        return None
    y = int(str(P)[:4]) - 3
    base_key = str(y) + str(P)[4:]
    base = ps.get(base_key)
    if not is_num(base) or base <= 0 or cur <= 0:
        return None
    try:
        r = (cur / base) ** (1.0 / 3.0) - 1.0
    except Exception:
        return None
    return r * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="抽样天数")
    ap.add_argument("--start", default="", help="起始日期过滤")
    args = ap.parse_args()

    print("=" * 96)
    print("【fund 填充可行性探针】")
    print("=" * 96)

    em = esi.load_em_all()
    print(f"EM 财务缓存：{len(em)} 只")

    files = sorted(glob.glob("data/fwd_hist/*.json"))
    if not files:
        raise SystemExit("无 fwd_hist 文件")
    if args.start:
        files = [f for f in files if os.path.basename(f)[:10] >= args.start]
    # 均匀抽样，覆盖不同时段
    if len(files) > args.days:
        step = len(files) / args.days
        files = [files[int(i * step)] for i in range(args.days)]
    print(f"抽样 {len(files)} 个日期：{os.path.basename(files[0])[:10]} ~ {os.path.basename(files[-1])[:10]}")
    print()

    all_pairs = []          # (真实分, 重建分)
    per_date = []
    cmp_detail = []

    for fp in files:
        d0 = json.load(open(fp, encoding="utf-8"))
        date = d0.get("date")
        rows = d0.get("rows") or []

        real, recon, re_pe, re_fwdpe, re_g, re_roe, re_gm = {}, {}, {}, {}, {}, {}, {}
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
            g = growth_pit(ind, P)
            roe = roe_pit(ind, P)
            gm = gm_pit(ind, P)
            cag = cagr3_pit(ind, P)

            re_pe[c6] = pe_s
            re_fwdpe[c6] = r.get("fwd_pe")
            re_g[c6] = g
            re_roe[c6] = roe
            re_gm[c6] = gm

            # 重建：仅 A+C 类入参，B 类全 None
            name = r.get("name") or ""
            code_full = r.get("code") or ""
            sector, sw_l2, _src = s2.resolve_sector(name, code_full)
            sub = s2.get_sub_theme_label(name)
            try:
                fs, _fr, _fp, _gr, _ld, _su = s2.score_fund(
                    pe_s, None, g, code_full, sector,
                    pe_ladder_data=None, roe=roe, gross_margin=gm,
                    sub_theme_label=sub, hist_loss_free=False,
                    fwd_np=None, sw_l2=sw_l2, cagr3=cag, q3_info=None,
                    roe_unit="pct")
            except Exception:
                fs = None
            if is_num(fs):
                recon[c6] = fs
            sf = r.get("score_fund")
            if is_num(sf):
                real[c6] = sf

        common = sorted(set(real) & set(recon))
        rho = spearman([real[c] for c in common], [recon[c] for c in common]) if len(common) >= 20 else None

        # PE 口径对比：静态PE vs 历史 fwd_pe
        pe_cmp = [(re_pe[c], re_fwdpe[c]) for c in common
                  if is_num(re_pe.get(c)) and is_num(re_fwdpe.get(c))]
        rho_pe = spearman([a for a, _ in pe_cmp], [b for _, b in pe_cmp]) if len(pe_cmp) >= 20 else None

        per_date.append((date, len(rows), len(real), len(recon), len(common), rho, rho_pe, len(pe_cmp)))
        for c in common:
            all_pairs.append((real[c], recon[c], re_pe.get(c), re_fwdpe.get(c)))

        cmp_detail.append({
            "date": date, "n_rows": len(rows),
            "n_real": len(real), "n_recon": len(recon), "n_common": len(common),
            "rho_fund": rho, "rho_pe": rho_pe, "n_pe_cmp": len(pe_cmp),
        })
        print(f"  {date}  rows={len(rows):4d}  真实分={len(real):4d}  重建分={len(recon):4d}  "
              f"重叠={len(common):4d}  ρ(fund)={'%.3f' % rho if rho is not None else '  n/a'}  "
              f"ρ(PE)={'%.3f' % rho_pe if rho_pe is not None else '  n/a'}")

    print()
    print("=" * 96)
    print("【总体保真度】")
    if all_pairs:
        xs = [a for a, b, _, _ in all_pairs]
        ys = [b for a, b, _, _ in all_pairs]
        rho_all = spearman(xs, ys)
        n = len(all_pairs)
        mae = sum(abs(a - b) for a, b in zip(xs, ys)) / n
        exact = sum(1 for a, b in zip(xs, ys) if abs(a - b) < 1e-9)
        near = sum(1 for a, b in zip(xs, ys) if abs(a - b) <= 1.0)
        print(f"  样本对 n={n}")
        print(f"  Spearman ρ(真实, 重建) = {rho_all:.4f}" if rho_all is not None else "  ρ 不可算")
        print(f"  平均绝对误差 MAE = {mae:.3f} 分（满分 5）")
        print(f"  完全一致 = {exact}/{n} = {100*exact/n:.1f}%")
        print(f"  误差 ≤1.0 分 = {near}/{n} = {100*near/n:.1f}%")

        # PE 口径系统性偏移
        pe_pairs = [(a, b) for _, _, a, b in all_pairs if is_num(a) and is_num(b) and a > 0 and b > 0]
        if pe_pairs:
            ratio = sorted(b / a for a, b in pe_pairs)
            med = ratio[len(ratio) // 2]
            print()
            print(f"  【PE 口径偏移】静态PE / 前瞻PE 中位倍数 = {med:.3f}")
            print(f"    → 前瞻PE 系统性{'低于' if med > 1 else '高于'}静态PE "
                  f"{abs(1-med)*100:.1f}%（机构预期增长溢价）")
            print(f"    分位 p10={ratio[int(0.1*len(ratio))]:.2f}  p50={med:.2f}  p90={ratio[int(0.9*len(ratio))]:.2f}")

    print()
    print("【分母参照】面板域 2029 只；fwd_hist 单日仅记录当时的池内标的")
    print("=" * 96)

    out = {
        "probe": "fund_backfill_feasibility",
        "n_dates_sampled": len(files),
        "per_date": cmp_detail,
        "overall": {
            "n_pairs": len(all_pairs),
            "spearman": rho_all if all_pairs else None,
            "mae": mae if all_pairs else None,
        },
    }
    with open("probe_fund_backfill.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("→ probe_fund_backfill.json")


if __name__ == "__main__":
    main()
