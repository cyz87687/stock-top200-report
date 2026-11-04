#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round39b：全市场分母下的 B 维 IC 重算（#305 最终验证）

## 先说清数学（否则极易写出同义反复的假结论）

B 维的定义是：
    syn = 0.5 × rank_pct(rev_share) + 0.5 × rank_pct(profit_share)   （申万二级组内）
    syn ≥ 7.0 → 进 B 维，得分 = syn

**分位（rank_pct）是分母升级的唯一作用点，但作用方式很微妙：**

- 若只看 `rev_share` 的**排序**：`rev_share = 个股营收 / 池内行业合计`，
  换成全市场分母只是给同行业所有股票乘同一个常数 1/k，
  **排序不变** → 单看营收排序时，分母升级毫无影响（这是同义反复，不是发现）。
- 但 syn 是**两个分位值相加**，而分位值本身 = (i+0.5)/n。
  n 从「池内只数」变成「全市场只数」后，同一只股票的分位**数值**会移动，
  且**不同行业移动幅度不同**（池外股票多的行业移动大）。
  两个分量数值非等比例变化 → 加权和 syn 变化 → **可能跨过 7.0 门槛**。

round39 已用 5 个抽样行业实测到这一点：连续排序 rho≈0.997（稳），
但 syn 均值系统性上偏 +0.27 分，龙头集合换手 **9.5%**（约 1/11 改变归属）。

**因此本脚本必须用绝对营收在全市场行业内排序，不能用 build_ab_dims 的 share**
（后者已除过池内合计，拿它算全市场分位是错的）。

## 数据
- 池内 2145 只：`fund_split.load_em_cache()`（含绝对营收/净利，只读）
- 池外 3073 只：`data/fin_cache_full/`（100% 覆盖）
- 行业：`sector/sw_map.json` 的 l2
- 价格：`data/klines_long.json`（前复权，算前瞻收益）

## 简化与披露
财务分母用「各股最新已披露报告期」的静态合计，未做逐期 PIT。
理由：本脚本要隔离的是**分母口径效应**（谁在分母里、n 多大），
不是 PIT 效应；混入 PIT 会让两个效应纠缠，反而说不清。
池内 PIT 口径下 B 维的 IC 已由 round38 给出（h20 −0.0282, t−2.69），可直接对照。

## 输出
audit_round39b.json
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audit_round38_size_ablation import (
    is_num, spearman, stat, HOPS, PANEL, KLONG,
)

CACHE_FULL = os.path.join(HERE, "data", "fin_cache_full")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
DOM_LEADER_THR = 7.0


def _tdays_of(px, min_ratio=0.5):
    """全市场交易日历（≥50% 个股出现即交易日），与 round38 内联逻辑同法。"""
    from collections import Counter
    cnt = Counter()
    for m in px.values():
        for d in m:
            cnt[d] += 1
    n = len(px)
    return sorted(d for d, c in cnt.items() if c >= n * min_ratio)


def _last_of(rec, fields):
    for fld in fields:
        d = rec.get(fld)
        if not isinstance(d, dict):
            continue
        ks = sorted([k for k in d if str(k).isdigit() and len(str(k)) == 8], reverse=True)
        for k in ks:
            v = d[k]
            if isinstance(v, (int, float)) and v == v:
                return float(v)
    return None


def load_full_fin():
    """池外财务 → {code: {'rev':abs, 'np':abs}}"""
    out = {}
    if not os.path.isdir(CACHE_FULL):
        return out
    for fn in os.listdir(CACHE_FULL):
        if not fn.endswith(".json"):
            continue
        code = fn[:-5]
        try:
            raw = json.load(open(os.path.join(CACHE_FULL, fn), encoding="utf-8"))
        except Exception:
            continue
        conv = {}
        for r in raw.get("records") or []:
            nm = str(r.get("指标") or "")
            if not nm:
                continue
            tgt = conv.setdefault(nm, {})
            for k, v in r.items():
                if str(k).isdigit() and len(str(k)) == 8:
                    tgt[str(k)] = v
        rev = _last_of(conv, ("营业总收入", "营业收入"))
        np_ = _last_of(conv, ("扣非净利润", "归母净利润"))
        if rev is not None and rev > 0:
            out[code] = {"rev": rev, "np": np_}
    return out


def rank_pct(vals):
    ok = {k: v for k, v in vals.items() if is_num(v)}
    if len(ok) < 3:
        return {}
    ks = sorted(ok, key=lambda c: ok[c])
    n = len(ks)
    return {c: (i + 0.5) / n for i, c in enumerate(ks)}


def main():
    px = json.load(open(KLONG, encoding="utf-8"))
    tdays = _tdays_of(px)
    tidx = {d: i for i, d in enumerate(tdays)}
    panel = json.load(open(PANEL, encoding="utf-8"))

    sw = json.load(open(SW_MAP, encoding="utf-8"))
    l2 = sw["l2"]

    import fund_split as fs
    em = fs.load_em_cache()
    full = load_full_fin()
    print(f"池内 {len(em)} 只 | 池外缓存 {len(full)} 只 | 行业映射 {len(l2)} 只")

    # ── 建立全市场绝对营收表：{code: (ind, rev, np)} ──
    glob_tab = {}
    for c, ind in l2.items():
        if c in em:
            rec = em.get(c) or {}
            rv = _last_of(rec, ("营业总收入", "营业收入"))
            npf = _last_of(rec, ("扣非净利润", "归母净利润"))
        elif c in full:
            rv, npf = full[c]["rev"], full[c]["np"]
        else:
            continue
        if rv is None or rv <= 0:
            continue
        glob_tab[c] = (ind, rv, npf)

    # 行业合计（全市场）
    ind_rev, ind_np = {}, {}
    for c, (ind, rv, npf) in glob_tab.items():
        ind_rev[ind] = ind_rev.get(ind, 0.0) + rv
        if isinstance(npf, (int, float)) and npf > 0:
            ind_np[ind] = ind_np.get(ind, 0.0) + npf
    print(f"全市场有效 {len(glob_tab)} 只，覆盖 {len(ind_rev)} 个行业")

    # 池内合计（对比口径）
    pool_set = set(em.keys()) & set(l2.keys())
    p_ind_rev, p_ind_np = {}, {}
    for c in pool_set:
        if c not in glob_tab:
            continue
        ind, rv, npf = glob_tab[c]
        p_ind_rev[ind] = p_ind_rev.get(ind, 0.0) + rv
        if isinstance(npf, (int, float)) and npf > 0:
            p_ind_np[ind] = p_ind_np.get(ind, 0.0) + npf

    # 各行业「全市场合计 / 池内合计」的倍数（衡量分母扩张幅度）
    ratios = []
    for ind in p_ind_rev:
        if ind in ind_rev and p_ind_rev[ind] > 0:
            ratios.append((ind, ind_rev[ind] / p_ind_rev[ind]))
    ratios.sort(key=lambda x: -x[1])
    print("\n分母扩张最大的 8 个行业（全市场合计 / 池内合计）:")
    for ind, r in ratios[:8]:
        print(f"  {ind:<12} ×{r:.2f}")
    if ratios:
        avg_r = sum(r for _i, r in ratios) / len(ratios)
        print(f"  平均扩张 ×{avg_r:.2f}")

    # ── 两种口径的 B 维得分（静态，与日期无关，故算一次复用）──
    # 池内口径：rank 对象 = 池内
    by_ind_pool = {}
    for c in pool_set:
        if c in glob_tab:
            by_ind_pool.setdefault(glob_tab[c][0], []).append(c)
    # 全市场口径：rank 对象 = 全市场
    by_ind_full = {}
    for c, (ind, rv, npf) in glob_tab.items():
        by_ind_full.setdefault(ind, []).append(c)

    b_pool, b_full = {}, {}
    for ind, cs in by_ind_pool.items():
        if len(cs) < 3:
            continue
        tr = p_ind_rev.get(ind, 0.0)
        tn = p_ind_np.get(ind, 0.0)
        rr = rank_pct({c: (glob_tab[c][1] / tr if tr > 0 else None) for c in cs})
        rn = rank_pct({c: (glob_tab[c][2] / tn
                           if (tn > 0 and isinstance(glob_tab[c][2], (int, float))
                               and glob_tab[c][2] > 0) else None) for c in cs})
        for c in cs:
            a, b = rr.get(c), rn.get(c)
            if a is None or b is None:
                continue
            v = (0.5 * a + 0.5 * b) * 10.0
            if v >= DOM_LEADER_THR:
                b_pool[c] = v

    for ind, cs in by_ind_full.items():
        if len(cs) < 3:
            continue
        tr = ind_rev.get(ind, 0.0)
        tn = ind_np.get(ind, 0.0)
        rr = rank_pct({c: (glob_tab[c][1] / tr if tr > 0 else None) for c in cs})
        rn = rank_pct({c: (glob_tab[c][2] / tn
                           if (tn > 0 and isinstance(glob_tab[c][2], (int, float))
                               and glob_tab[c][2] > 0) else None) for c in cs})
        for c in cs:
            if c not in pool_set:        # 只关心池内股票
                continue
            a, b = rr.get(c), rn.get(c)
            if a is None or b is None:
                continue
            v = (0.5 * a + 0.5 * b) * 10.0
            if v >= DOM_LEADER_THR:
                b_full[c] = v

    print(f"\nB 维覆盖（syn≥7.0）：池内口径 {len(b_pool)} 只 | 全市场口径 {len(b_full)} 只")
    enter = set(b_full) - set(b_pool)
    exit_ = set(b_pool) - set(b_full)
    print(f"  新进 {len(enter)} 只 | 退出 {len(exit_)} 只 | 换手 "
          f"{(len(enter)+len(exit_))/max(len(b_pool),1)*100:.1f}%")

    # ── 两套 B 维的 IC ──
    print("\n" + "-" * 100)
    print("两种分母下的 B 维 IC（同一批前瞻收益，只改 B 维取值）")
    print("-" * 100)
    OUT = {"pool_denom": {}, "full_denom": {}, "delta": {}}
    for tag, bmap in (("pool_denom", b_pool), ("full_denom", b_full)):
        for hop in HOPS:
            ics = []
            for d0 in panel["dates"]:
                xs, ys = {}, {}
                for c, sc in bmap.items():
                    fr = _fwd(px, c, d0, hop, tdays, tidx)
                    if fr is None:
                        continue
                    xs[c] = sc
                    ys[c] = fr
                if len(xs) >= 30:
                    ic, _n = spearman(xs, ys)
                    if ic is not None:
                        ics.append(ic)
            s = stat(ics)
            OUT[tag][f"h{hop}"] = s
            if is_num(s.get("ic")):
                print(f"  {tag:<12} h{hop:<3} IC={s['ic']:+.4f}  t={s['t']:+.2f}  "
                      f"ICIR={s['icir']:+.3f}  胜率={s['pos_rate']*100:.1f}%  n={s['n']}")
            else:
                print(f"  {tag:<12} h{hop:<3} 样本不足（n={s['n']}）")

    # ── 裁决 ──
    print("\n" + "=" * 96)
    print("裁决：分母升级是否改变 B 维的负 IC 结论？")
    print("=" * 96)
    p20 = OUT["pool_denom"].get("h20") or {}
    f20 = OUT["full_denom"].get("h20") or {}
    if is_num(p20.get("ic")) and is_num(f20.get("ic")):
        d_ic = f20["ic"] - p20["ic"]
        OUT["delta"]["h20"] = {"d_ic": d_ic, "pool": p20["ic"], "full": f20["ic"]}
        print(f"  池内分母   B 维 h20 IC = {p20['ic']:+.4f}（t {p20['t']:+.2f}）")
        print(f"  全市场分母 B 维 h20 IC = {f20['ic']:+.4f}（t {f20['t']:+.2f}）")
        print(f"  ΔIC = {d_ic:+.4f}")
        print()
        flip = (p20["ic"] < 0) != (f20["ic"] < 0)
        if flip:
            print("  🚨 **符号翻转**：分母口径改变了 B 维的方向判断。")
            print("     此前负 IC 至少部分是池内分母高估龙头份额造成的伪影 → 必须先统一分母再裁决。")
        elif abs(d_ic) < 0.005:
            print("  ⇒ 方向与幅度基本不变：B 维负 IC **不是分母口径造成的**，是真实方向问题。")
            print("     ⇒ 确认 round38c 结论：B 维仍需按「方向相反」处置（取负或归零）。")
        else:
            print("  ⇒ 幅度有可见变化但符号未变：负 IC 结论稳健，数值需按新口径重述。")
            print(f"     ⇒ 页面披露以全市场分母为准（IC {f20['ic']:+.4f}）。")
            print("     ⚠️ 但仍未解决方向问题——升级分母只是把口径修正，不改变 0.06 权重的方向错误。")
    else:
        print("  样本不足，无法裁决。")

    OUT["meta"] = {
        "n_pool_fin": len(pool_set), "n_full_fin": len(glob_tab),
        "n_ind": len(ind_rev), "n_ind_pool": len(p_ind_rev),
        "b_cover_pool": len(b_pool), "b_cover_full": len(b_full),
        "leader_enter": len(enter), "leader_exit": len(exit_),
        "avg_denom_ratio": (sum(r for _i, r in ratios) / len(ratios)) if ratios else None,
        "caveat_pit": "分母用各股最新已披露期的静态合计，未逐期 PIT；"
                      "目的是隔离分母口径效应，不与 PIT 效应纠缠。"
                      "池内 PIT 口径的 B 维 IC 见 round38（h20 −0.0282, t−2.69）。",
        "caveat_bstatic": "B 维取值在此为静态（同一天下所有截面共用），"
                          "因财务分母是静态的；真实生产按月更新，故 IC 幅度不可与 round38 精确对齐，"
                          "只能比较「两种分母的相对差异」。",
    }
    json.dump(OUT, open(os.path.join(HERE, "audit_round39b.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：audit_round39b.json")


def _fwd(px, c6, d0, hop, tdays, tidx):
    m = px.get(c6)
    i0 = tidx.get(d0)
    if not m or i0 is None or i0 + hop >= len(tdays):
        return None

    def _cl(d):
        b = m.get(d)
        if isinstance(b, (list, tuple)) and len(b) >= 2 and is_num(b[1]):
            return float(b[1])
        return float(b) if is_num(b) else None

    c0, c1 = _cl(tdays[i0]), _cl(tdays[i0 + hop])
    if not c0 or not c1:
        return None
    return (c1 - c0) / c0 * 100


if __name__ == "__main__":
    main()
