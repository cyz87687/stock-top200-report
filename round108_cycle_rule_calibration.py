#!/usr/bin/env python3
"""
round108｜周期股两条规则的经验标定（只标定，不改分）
=====================================================
目的：老大给了规则1/规则2 的动机，但未给扣分幅度与阈值。本脚本用**真实历史财务序列**
标定，禁止发明数字。

规则1（盈利持续性／价格驱动）简化方案：
  近 4 季单季毛利率 std > 行业历史 60% 分位 → 判价格驱动 → 扣成长分 10%
  → 需要标定「行业历史 60% 分位」到底是多少（按申万二级分组、历史全样本池化）

规则2（二阶导可持续性惩罚）：
  周期股在大宗涨价阶段 ΔG_q 短期爆高但不可持续
  → 需实证：ΔG_q 是否均值回归（corr(ΔG_q_t, ΔG_q_{t+1}) < 0 ?）
  → 以及回归幅度分布，用于反推合理扣分档

输出：data/round108_cycle_rule_calibration.json
用法：python3 round108_cycle_rule_calibration.py
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
SCORED = os.path.join(HERE, "top200_scored_2026-11-02.json")
OUT = os.path.join(HERE, "data", "round108_cycle_rule_calibration.json")

Q_END = ("0331", "0630", "0930", "1231")


def load_sw():
    d = json.load(open(SW_MAP, encoding="utf-8"))
    code2l2 = d.get("l2") or {}
    l2_parent = d.get("l2_parent") or {}
    return code2l2, l2_parent


def norm_code(c):
    c = str(c)
    for p in ("sz", "sh", "bj", "SZ", "SH", "BJ"):
        if c.startswith(p):
            c = c[len(p):]
    return c


def get_series(recs, name):
    """指标名 → {period: value}（只要年内累计四档）"""
    for r in recs:
        if r.get("指标") == name:
            out = {}
            for k, v in r.items():
                if k in ("选项", "指标"):
                    continue
                if len(k) == 8 and k.isdigit() and k[4:] in Q_END:
                    if isinstance(v, (int, float)) and np.isfinite(v):
                        out[k] = float(v)
            if out:
                return out
    return {}


def single_quarter(cum):
    """累计 → 单季（按年分组自拆）"""
    by_year = defaultdict(dict)
    for p, v in cum.items():
        by_year[p[:4]][p[4:]] = v
    sq = {}
    for y, qs in by_year.items():
        prev = 0.0
        ok = True
        for q in Q_END:
            if q not in qs:
                ok = False
                continue
            sq[y + q] = qs[q] - prev
            prev = qs[q]
    return sq


def prev_q(p):
    y, q = int(p[:4]), p[4:]
    i = Q_END.index(q)
    if i == 0:
        return f"{y-1}1231"
    return f"{y}{Q_END[i-1]}"


def qoq_prev_q(p):
    """环比上一季（Q1 环比 = 上年 Q4）"""
    return prev_q(p)


def main():
    code2l2, l2_parent = load_sw()
    print(f"[sw] 二级映射 {len(code2l2)} 只, 二级行业 {len(l2_parent)} 个", flush=True)

    # 当前池（用于限定样本域）
    sc = json.load(open(SCORED, encoding="utf-8"))
    pool = {}
    for r in sc["results"]:
        pool[norm_code(r["code"])] = r
    print(f"[pool] {len(pool)} 只", flush=True)

    # ---- 老大原话强周期清单 → 申万二级 ----
    CYCLE_L2 = {
        "工业金属": ["工业金属"],
        "小金属": ["小金属", "金属新材料"],
        "稀土": ["小金属"],
        "航运": ["航运港口"],
        "煤炭": ["煤炭开采", "焦炭Ⅱ", "焦炭"],
        "化工原料": ["化学原料"],
        "化纤": ["化学纤维"],
        "造纸": ["造纸"],
        "生猪养殖": ["养殖业"],
        "有色冶炼": ["冶钢原料", "普钢", "特钢Ⅱ", "特钢"],
        "油气": ["油气开采Ⅱ", "油气开采", "油服工程", "炼化及贸易"],
        "化肥": ["农化制品"],
        "贵金属": ["贵金属"],
        "能源金属": ["能源金属"],
    }
    cycle_l2 = set()
    for v in CYCLE_L2.values():
        cycle_l2 |= set(v)

    # ---- 遍历缓存 ----
    files = [f for f in os.listdir(CACHE) if f.endswith(".json")]
    gm_vol = {}          # code -> {window_end_period: std}
    accel_rows = []      # (code, period, gq, accel, l2, is_cycle)
    n_ok = 0
    for i, fn in enumerate(files):
        code = fn[:-5]
        try:
            d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        recs = d.get("records") or []
        if not recs:
            continue
        rev = get_series(recs, "营业总收入")
        cost = get_series(recs, "营业成本")
        kf = get_series(recs, "扣非净利润")
        if not (rev and cost and kf):
            continue
        n_ok += 1
        l2 = code2l2.get(code) or code2l2.get(norm_code(code))
        is_cyc = bool(l2 and l2 in cycle_l2)

        # 单季毛利率 → 滚动 4 季 std
        sq_rev = single_quarter(rev)
        sq_cost = single_quarter(cost)
        gm = {}
        for p in sq_rev:
            if p in sq_cost and sq_rev[p] > 1e4:
                gm[p] = (sq_rev[p] - sq_cost[p]) / sq_rev[p] * 100.0
        periods = sorted(gm.keys())
        for j in range(3, len(periods)):
            win = [gm[p] for p in periods[j - 3:j + 1]]
            if len(win) == 4:
                mu = float(np.mean(win))
                if abs(mu) > 1e-9:
                    gm_vol.setdefault(code, {})[periods[j]] = float(np.std(win, ddof=1))

        # 单季扣非 → G_q / ΔG_q
        sq_kf = single_quarter(kf)
        for p in sorted(sq_kf.keys()):
            ply = f"{int(p[:4])-1}{p[4:]}"
            pprev = qoq_prev_q(p)
            if ply not in sq_kf or sq_kf[ply] <= 0:
                continue                     # 负基数无定义，同 round106
            gq = sq_kf[p] / sq_kf[ply] - 1.0
            # G_q(t-1) 用于 ΔG_q
            p1 = prev_q(p)
            ply1 = f"{int(p1[:4])-1}{p1[4:]}"
            if p1 not in sq_kf or ply1 not in sq_kf or sq_kf[ply1] <= 0:
                continue
            gq1 = sq_kf[p1] / sq_kf[ply1] - 1.0
            accel_rows.append((code, p, gq, (gq - gq1) * 100.0, l2, is_cyc))

        if (i + 1) % 500 == 0:
            print(f"  ..{i+1}/{len(files)}", flush=True)

    print(f"[load] 财务可用 {n_ok} 只；毛利率窗口 {sum(len(v) for v in gm_vol.values())} 个；"
          f"ΔG_q 观测 {len(accel_rows)} 条", flush=True)

    # ================= 规则1：行业历史毛利率波动 60% 分位 =================
    # 口径A（老大原话）：行业历史全样本池化（同一二级行业内所有个股、所有历史窗口）
    by_l2 = defaultdict(list)
    for code, wins in gm_vol.items():
        l2 = code2l2.get(code)
        if not l2:
            continue
        for p, s in wins.items():
            by_l2[l2].append(s)
    ind_p60 = {}
    for l2, arr in by_l2.items():
        if len(arr) >= 30:
            ind_p60[l2] = {
                "n": len(arr),
                "p50": round(float(np.percentile(arr, 50)), 2),
                "p60": round(float(np.percentile(arr, 60)), 2),
                "p70": round(float(np.percentile(arr, 70)), 2),
            }
    # 全池兜底 p60
    allv = [s for arr in by_l2.values() for s in arr]
    pool_p60 = round(float(np.percentile(allv, 60)), 2)

    # 当期（20260930 窗口）截面：周期组 vs 非周期组
    cur = "20260930"
    grp_c, grp_n = [], []
    for code, wins in gm_vol.items():
        s = wins.get(cur)
        if s is None:
            continue
        l2 = code2l2.get(code)
        (grp_c if (l2 in cycle_l2) else grp_n).append(s)
    def dist(a):
        if not a:
            return {}
        return {"n": len(a), "p50": round(float(np.percentile(a, 50)), 2),
                "p60": round(float(np.percentile(a, 60)), 2),
                "p70": round(float(np.percentile(a, 70)), 2),
                "p90": round(float(np.percentile(a, 90)), 2),
                "max": round(float(np.max(a)), 2)}

    # 命中率（用行业历史 p60 判定当期）
    hit_cyc = hit_nocyc = tot_cyc = tot_nocyc = 0
    for code, wins in gm_vol.items():
        s = wins.get(cur)
        l2 = code2l2.get(code)
        if s is None or not l2:
            continue
        thr = ind_p60.get(l2, {}).get("p60", pool_p60)
        if l2 in cycle_l2:
            tot_cyc += 1
            if s > thr:
                hit_cyc += 1
        else:
            tot_nocyc += 1
            if s > thr:
                hit_nocyc += 1

    # ================= 规则2：ΔG_q 均值回归实证 =================
    # 按个股时间序：corr(accel_t, accel_{t+1})；分组统计
    seq = defaultdict(dict)
    for code, p, gq, acc, l2, is_cyc in accel_rows:
        seq[code][p] = (acc, is_cyc, l2)
    def autocorr(only_cycle):
        xs, ys = [], []
        for code, mp in seq.items():
            ps = sorted(mp.keys())
            for i in range(len(ps) - 1):
                a1, cyc1, _ = mp[ps[i]]
                a2, cyc2, _ = mp[ps[i + 1]]
                if cyc1 != only_cycle:
                    continue
                # 只取相邻季（跨年连续）
                if prev_q(ps[i + 1]) != ps[i]:
                    continue
                xs.append(a1); ys.append(a2)
        if len(xs) < 50:
            return {"n": len(xs)}
        return {"n": len(xs),
                "corr": round(float(np.corrcoef(xs, ys)[0, 1]), 4),
                "mean_t": round(float(np.mean(xs)), 2),
                "mean_t1": round(float(np.mean(ys)), 2)}

    # 高加速分位 → 次季 ΔG_q 中位（不可持续的直接证据）
    def bucket(only_cycle):
        rows = []
        for code, mp in seq.items():
            ps = sorted(mp.keys())
            for i in range(len(ps) - 1):
                a1, cyc1, _ = mp[ps[i]]
                a2, cyc2, _ = mp[ps[i + 1]]
                if cyc1 != only_cycle:
                    continue
                if prev_q(ps[i + 1]) != ps[i]:
                    continue
                rows.append((a1, a2))
        if len(rows) < 100:
            return {}
        a = np.array(rows)
        out = {}
        edges = [(0, 60), (20, 60), (60, 120), (120, 9999)]
        for lo, hi in edges:
            m = (a[:, 0] >= lo) & (a[:, 0] < hi)
            if m.sum() >= 20:
                out[f"accel[{lo},{hi})"] = {
                    "n": int(m.sum()),
                    "next_median": round(float(np.median(a[m, 1])), 2),
                    "next_pct_neg": round(float((a[m, 1] < 0).mean() * 100), 1),
                    "next_pct_reverse": round(float((a[m, 1] < -abs(a[m, 0]) * 0.5).mean() * 100), 1),
                }
        return out

    result = {
        "date": "2026-11-02",
        "meta": {
            "fin_cache_usable": n_ok,
            "gm_windows": int(sum(len(v) for v in gm_vol.values())),
            "accel_obs": len(accel_rows),
            "cycle_l2": sorted(cycle_l2),
            "cycle_map": CYCLE_L2,
        },
        "rule1_gm_vol": {
            "note": "近4季单季毛利率标准差（pp）；口径A=行业历史全样本池化",
            "pool_p60_fallback": pool_p60,
            "industry_p60": ind_p60,
            "current_cross_section_cycle": dist(grp_c),
            "current_cross_section_nocycle": dist(grp_n),
            "hit_rate_at_ind_p60": {
                "cycle": f"{hit_cyc}/{tot_cyc}",
                "cycle_pct": round(hit_cyc / tot_cyc * 100, 1) if tot_cyc else None,
                "nocycle": f"{hit_nocyc}/{tot_nocyc}",
                "nocycle_pct": round(hit_nocyc / tot_nocyc * 100, 1) if tot_nocyc else None,
            },
        },
        "rule2_accel_persistence": {
            "note": "corr(ΔG_q_t, ΔG_q_{t+1})；负值=均值回归=不可持续",
            "cycle_autocorr": autocorr(True),
            "nocycle_autocorr": autocorr(False),
            "cycle_bucket": bucket(True),
            "nocycle_bucket": bucket(False),
        },
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n===== 规则1：毛利率波动 p60 =====")
    print("全池兜底 p60 =", pool_p60)
    print("当期截面 周期组 :", json.dumps(dist(grp_c), ensure_ascii=False))
    print("当期截面 非周期组:", json.dumps(dist(grp_n), ensure_ascii=False))
    print("命中率：", json.dumps(result["rule1_gm_vol"]["hit_rate_at_ind_p60"], ensure_ascii=False))
    print("\n----- 行业历史 p60 前20（样本≥30）-----")
    top = sorted(ind_p60.items(), key=lambda kv: -kv[1]["p60"])[:20]
    for l2, v in top:
        print(f"  {l2:10s} n={v['n']:5d} p50={v['p50']:6.2f} p60={v['p60']:6.2f} p70={v['p70']:6.2f}")
    print("\n===== 规则2：ΔG_q 可持续性 =====")
    print("周期  autocorr:", json.dumps(result["rule2_accel_persistence"]["cycle_autocorr"], ensure_ascii=False))
    print("非周期 autocorr:", json.dumps(result["rule2_accel_persistence"]["nocycle_autocorr"], ensure_ascii=False))
    print("周期 分档:")
    for k, v in result["rule2_accel_persistence"]["cycle_bucket"].items():
        print("  ", k, json.dumps(v, ensure_ascii=False))
    print("非周期 分档:")
    for k, v in result["rule2_accel_persistence"]["nocycle_bucket"].items():
        print("  ", k, json.dumps(v, ensure_ascii=False))
    print(f"\n[done] → {OUT}")


if __name__ == "__main__":
    main()
