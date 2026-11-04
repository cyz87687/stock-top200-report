# -*- coding: utf-8 -*-
"""audit_round41_external_calibration.py —— #308 外部基准校准层

目标
----
把本池各维的 IC 与**外部全市场 47 因子基准**逐一对齐，输出「夸大/低估倍数」，
并把结论写成页面可直接引用的结构化 JSON。

为什么必须做
------------
本项目所有维度的 IC 都只在「成交额 TOP 池」内计算。池子已被流动性和规模筛过，
方差被压缩、风格被截断，得到 IC 与全市场口径不可直接比。若把池内 IC 当成
因子的真实效力，会系统性高估某些维（如低波、乖离），并可能在符号上被误导
（如质量、行业内市值在本池为负、在外部位正）。

外部基准文件
------------
`reference_final_ranking_halfyear.csv`（47 因子，全市场近半年）
  关键列：
    factor        因子名
    ic_daily      日频 IC 均值
    icir_daily    日频 ICIR
    ic_month_t    月频 IC 的 t 值
    direction     因子方向说明
    coverage      覆盖率
    exc_ann       多头超额年化

⚠️ 口径差异（必须与倍数一起读）
  1. 外部是**全市场**截面、本池是**TOP 池内**截面 → 范围内截断
  2. 外部 ic_daily 是**日频**（1 日前瞻），本池同时报 h1/h5/h20
     → 与外部比必须用 h1
  3. 外部 ICIR 用日频、本池 ICIR 用截面序列 → 量级不可比，只看符号与相对强弱
  4. 两边因子定义未必一致（ep 口径、size 口径、quality 合成方式）

输出
----
audit_round41.json + 终端报告
"""
import os
import sys
import json
import csv
import math
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audit_round38_size_ablation import (
    build_ab_dims, is_num, spearman, nw_t, PANEL, KLONG,
)

REF_CSV = os.path.join(HERE, "reference_final_ranking_halfyear.csv")
OUT = os.path.join(HERE, "audit_round41.json")
WEIGHTS_FILE = os.path.join(HERE, "factor_weights.json")


# ── 本池维度 → 外部基准因子名（按 CSV 实际 factor 值精确映射，勿靠模糊匹配）──
# 外部 47 因子真实名（reference_final_ranking_halfyear.csv）：
#   SIZE ROA ROE NAG BIAS60 TRIX12 ROIC MACDDIF MOM1M REV1M TAG NPM DEBT D2E
#   BIAS20 MFI14 OPM EP RSI14 MOM1W BBI AMT20 NPG EVEBIT CCI14 EPSG PSY12 KDJK
#   WR14 REVG MOM3M BP CUR DY SP MAX20 LVOL20 TURN20 GPM MOM6M MOM12_1 CFP
#   DIST52H ATR14 OCFG VOLR20 ILLIQ
DIM_TO_REF = {
    # 低波：外部有 LVOL20（20日波动率）与 MAX20（最大日收益），语义最贴近 LVOL20
    "score_lowvol20": ["LVOL20"],
    # 质量：外部拆成 ROE/ROA/GPM/OCFG/NPM 等，本项目 quality 是合成 → 主对照 ROE
    "score_quality": ["ROE"],
    # 估值：外部 EP 就是「扣非净利/市值」，与本项目 score_ep 定义最接近
    "score_ep": ["EP"],
    # fund：外部无资金流因子，最接近的是 TURN20（换手率，资金活跃度代理）
    "score_fund": ["TURN20"],
    # 乖离：外部 BIAS60 与本项目 score_bias60 同名同义
    "score_bias60": ["BIAS60"],
    # RSI 反转：外部 RSI14 与 REV1M（1月反转）都相关，主选 REV1M
    # （本项目 rsi_rev 是反转逻辑，不是 RSI 趋势逻辑）
    "score_rsi_rev": ["REV1M"],
    # 技术负向：外部无对应（本项目 tech_neg 是量价背离/超买惩罚，独有）
    "score_tech_neg": [],
    # A 维：本质是行业内小市值 → 外部 SIZE（规模，方向：值越小越好）
    "score_size_growth": ["SIZE"],
    # B 维：龙头壁垒，外部同样只有 SIZE 可参照（但方向预期相反）
    "score_dominant": ["SIZE"],
}


def _fnum(x, default=None):
    """严格数值解析：拒绝 NaN / inf / 空串 / 非数。"""
    if x is None:
        return default
    if isinstance(x, (int, float)):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return default
        return float(x)
    s = str(x).strip()
    if not s or s.upper() in ("NA", "NAN", "NULL", "NONE", "-"):
        return default
    # 百分比列可能带 %
    s = s.rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def load_ref():
    """读外部 47 因子基准，返回原始行列表。

    ⚠️ 同时计算**日频 t 值**（ic_daily / (ic_daily_std/√n)）。
       不能用 CSV 自带的 ic_month_t 做显著性门控 —— 那一列只有 11 个月度观测，
       功效严重不足，几乎所有因子都会被判「不显著」。
       外部真正有功效的统计量是日频 t（n=222）。
    """
    if not os.path.exists(REF_CSV):
        raise SystemExit(f"🚨 外部基准文件不存在：{REF_CSV}")
    rows = []
    with open(REF_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            fac = (r.get("factor") or "").strip()
            if not fac:
                continue
            ic = _fnum(r.get("ic_daily"))
            sd = _fnum(r.get("ic_daily_std"))
            n = _fnum(r.get("ic_daily_n"))
            t_daily = None
            if is_num(ic) and is_num(sd) and is_num(n) and sd > 1e-12 and n >= 5:
                t_daily = ic / (sd / math.sqrt(n))
            rows.append({
                "factor": fac,
                "category": (r.get("category") or "").strip(),
                "definition": (r.get("definition") or "").strip(),
                "ic_daily": ic,
                "ic_daily_std": sd,
                "ic_daily_n": n,
                "t_daily": t_daily,          # ← 显著性门控用这个
                "icir_daily": _fnum(r.get("icir_daily")),
                "ic_month_t": _fnum(r.get("ic_month_t")),
                "ic_month": _fnum(r.get("ic_month")),
                "coverage": _fnum(r.get("coverage")),
                "exc_ann": _fnum(r.get("exc_ann")),
                "direction": (r.get("direction") or "").strip(),
            })
    return rows


def _orient(r):
    """把外部因子 IC 统一到「分数越高 → 收益越高」的方向。

    ⚠️ 这是本脚本最容易出错的地方，必须显式处理：
       外部 CSV 的 ic_daily 是「**因子原始值** 与 未来收益」的秩相关。
       若 direction 写「因子值越小越好」（如 SIZE=流通市值、EP 相反），
       则原始值 IC 为负，但该因子**作为正向评分使用时**实际效力是正的。

       例：外部 SIZE ic_daily = −0.0655、direction「因子值越小越好」
           ⇒ 小市值溢价的效力 = +0.0655（取反后）
           本池 score_size_growth 是「分数越高越好」⇒ 应比 +0.0655，不是 −0.0655。

    规则：direction 含「越小」→ IC 取反；含「越大」→ 不变。
    """
    ic = r.get("ic_daily")
    d = (r.get("direction") or "")
    if not is_num(ic):
        return None, False
    if "越小" in d:
        return -ic, True
    return ic, False


def _tdays_of(px, min_ratio=0.5):
    """全市场交易日历：某日期在 ≥min_ratio 比例个股的 K 线里出现即视为交易日。"""
    from collections import Counter
    cnt = Counter()
    for m in px.values():
        for d in m:
            cnt[d] += 1
    n = len(px)
    return sorted(d for d, c in cnt.items() if c >= n * min_ratio)


def fwd_ret(px, tidx, tdays, code, d0, hop):
    m = px.get(code)
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


def main():
    wj = json.load(open(WEIGHTS_FILE, encoding="utf-8"))
    w = wj["weights"]
    dmax = wj.get("dim_max") or {}
    print(f"权重版本 {wj.get('version')}  Σw={sum(w.values()):.6f}")

    ref_rows = load_ref()
    ref = {r["factor"]: r for r in ref_rows}
    print(f"外部基准：{len(ref_rows)} 个因子（{REF_CSV.split('/')[-1]}）")

    px = json.load(open(KLONG, encoding="utf-8"))
    tdays = _tdays_of(px)
    tidx = {d: i for i, d in enumerate(tdays)}
    panel = json.load(open(PANEL, encoding="utf-8"))
    print(f"本池面板：{len(panel['dates'])} 截面 × 约 2000 只")

    print("\n重建 A/B 维...")
    ab, _m = build_ab_dims(panel)

    # ── 本池各维 IC：与外部同口径用 h1（外部是日频 IC）──
    HOPS = (1, 5, 20)
    dims = [k for k, v in w.items() if v > 0]
    print(f"生效维度 {len(dims)} 个，计算 h1/h5/h20 单维 IC...")

    res = {}
    for dim in dims:
        res[dim] = {}
        for hop in HOPS:
            ics = []
            for d0 in panel["dates"]:
                if d0 not in tidx or tidx[d0] + hop >= len(tdays):
                    continue
                rows = panel["rows"].get(d0) or {}
                abr = ab.get(d0) or {}
                xs, ys = {}, {}
                for c, r in rows.items():
                    if dim == "score_size_growth":
                        v = (abr.get(c) or {}).get("A_G1")
                    elif dim == "score_dominant":
                        v = (abr.get(c) or {}).get("B_dom")
                    else:
                        v = r.get(dim)
                    if not is_num(v):
                        continue
                    fr = fwd_ret(px, tidx, tdays, c, d0, hop)
                    if fr is None:
                        continue
                    xs[c] = v
                    ys[c] = fr
                if len(xs) < 30:
                    continue
                ic, _n = spearman(xs, ys)
                if is_num(ic):
                    ics.append(ic)
            if len(ics) >= 20:
                m = st.mean(ics)
                sd = st.pstdev(ics) if len(ics) > 1 else None
                res[dim][f"h{hop}"] = {
                    "ic": m,
                    "n": len(ics),
                    "icir": (m / sd) if sd and sd > 1e-9 else None,
                    "nw_t": nw_t(ics) if len(ics) >= 10 else None,
                    "pos_rate": sum(1 for x in ics if x > 0) / len(ics),
                }
            else:
                res[dim][f"h{hop}"] = {"ic": None, "n": len(ics)}

    # ── 匹配外部因子 ──
    print("\n匹配外部基准因子...")
    match = {}
    for dim in dims:
        h1 = res[dim].get("h1") or {}
        loc_ic = h1.get("ic")
        cands = []
        for name in DIM_TO_REF.get(dim, []):
            if name in ref:
                r = ref[name]
                if is_num(r["ic_daily"]):
                    cands.append((name, r))
        if not cands:
            # 退一步：按 category 名字模糊找
            for r in ref_rows:
                if not is_num(r["ic_daily"]):
                    continue
                hay = (r["factor"] + r["category"] + r["definition"]).upper()
                for name in DIM_TO_REF.get(dim, []):
                    if name.upper() in hay:
                        cands.append((r["factor"], r))
                        break
        if not cands:
            match[dim] = None
            continue
        # 选主对照：优先「方向统一后」符号一致，其次 |IC| 最接近
        def score(item):
            name, r = item
            oic, _rev = _orient(r)
            same = (is_num(loc_ic) and is_num(oic) and
                    (loc_ic > 0) == (oic > 0))
            return (0 if same else 1,
                    abs(abs(oic or 0) - abs(loc_ic or 0)))
        cands.sort(key=score)
        name, r = cands[0]
        oic, reversed_ = _orient(r)
        match[dim] = {
            "ref_factor": name,
            "ref_category": r["category"],
            "ref_definition": r["definition"],
            "ref_ic_daily_raw": r["ic_daily"],
            "ref_ic_daily": oic,               # ← 方向统一后的可比 IC
            "ref_reversed": reversed_,
            "ref_t_daily": (r["t_daily"] if not reversed_
                            else (None if r["t_daily"] is None else -r["t_daily"])),
            "ref_icir_daily": r["icir_daily"],
            "ref_ic_month_t": r["ic_month_t"],
            "ref_coverage": r["coverage"],
            "ref_direction": r["direction"],
            "n_candidates": len(cands),
            "candidate_names": [c[0] for c in cands],
        }

    # ── 打印对照表 ──
    line = "=" * 104
    print("\n" + line)
    print("一、本池各维 vs 外部全市场基准（⚠️ 本池用 h1 对齐外部日频 IC）")
    print(line)
    print("  说明：外部 IC 已按 direction 统一到「分数越高→收益越高」的正向口径；")
    print("        「取反」列标 Y 表示该因子原 direction 为「值越小越好」，已翻转符号。")
    print()
    print(f"{'本池维度':<20}{'w':>7}{'本池h1 IC':>11}{'外部因子':>10}"
          f"{'外部IC':>10}{'取反':>5}{'倍数':>10}{'符号':>7}{'覆盖':>7}")
    for dim in dims:
        h1 = res[dim].get("h1") or {}
        loc = h1.get("ic")
        m = match.get(dim)
        if m is None:
            locs = f"{loc:>11.4f}" if is_num(loc) else f"{'n/a':>11}"
            print(f"{dim:<20}{w[dim]:>7.3f}{locs}{'（无匹配）':>10}")
            continue
        ric = m["ref_ic_daily"]
        ratio = (loc / ric) if (is_num(loc) and is_num(ric) and abs(ric) > 1e-9) else None
        sign = "同" if (is_num(loc) and is_num(ric) and (loc > 0) == (ric > 0)) else "**反**"
        rat_s = f"{ratio:+.2f}x" if ratio is not None else "n/a"
        rev_s = "Y" if m["ref_reversed"] else "-"
        locs = f"{loc:>11.4f}" if is_num(loc) else f"{'n/a':>11}"
        print(f"{dim:<20}{w[dim]:>7.3f}{locs}{m['ref_factor']:>10}"
              f"{ric:>10.4f}{rev_s:>5}{rat_s:>10}{sign:>7}{(m['ref_coverage'] or 0):>7.0%}")

    # ── 二、符号相反清单（最重要）──
    print("\n" + line)
    print("二、🚨 符号相反清单 —— 这些维在本池的方向与全市场基准相反")
    print(line)
    flips = []
    for dim in dims:
        m = match.get(dim)
        h1 = (res[dim].get("h1") or {}).get("ic")
        if not m or not is_num(h1) or not is_num(m["ref_ic_daily"]):
            continue
        if (h1 > 0) != (m["ref_ic_daily"] > 0):
            flips.append((dim, h1, m))
    if not flips:
        print("  （无）")
    else:
        for dim, h1, m in flips:
            print(f"  · {dim}（w={w[dim]:.3f}）")
            print(f"      本池 h1 IC = {h1:+.4f}   |   外部 {m['ref_factor']} IC = "
                  f"{m['ref_ic_daily']:+.4f}")
            print(f"      外部定义：{m['ref_definition']}（覆盖 {m['ref_coverage']:.0%}）")
            print(f"      ⇒ 可能原因：① 池内范围截断 ② 因子定义不同 ③ 真·regime 反转")
            print(f"      ⇒ 处置：**不得**用外部符号推翻本池实证，但必须在页面披露")

    # ── 三、夸大倍数排序 ──
    print("\n" + line)
    print("三、本池 IC 相对外部的夸大倍数（同号 + 外部显著者才可比较）")
    print(line)
    print("  ⚠️ 门控用**日频 t 值**（n=222 个交易日），不用 CSV 的 ic_month_t")
    print("     （后者只有 11 个月度观测，功效不足，几乎全判不显著）。")
    print("     外部因子自身日频 |t| < 1.96 时**不计算倍数**：若外部 IC 本身接近 0")
    print("     （如 LVOL20 ic=+0.0076 t=+0.45；TURN20 ic=−0.0092 t=−0.44），")
    print("     除以它会得到 4x~6x 的假倍数 —— 那不是「本池夸大」，")
    print("     而是「外部根本没有这个效应」。")
    print()
    ratios = []
    weak = []
    for dim in dims:
        m = match.get(dim)
        h1 = (res[dim].get("h1") or {}).get("ic")
        if not m or not is_num(h1) or not is_num(m["ref_ic_daily"]):
            continue
        if abs(m["ref_ic_daily"]) < 1e-9:
            continue
        if (h1 > 0) != (m["ref_ic_daily"] > 0):
            continue
        rt = h1 / m["ref_ic_daily"]
        t_ext = m.get("ref_t_daily")
        if not is_num(t_ext) or abs(t_ext) < 1.96:
            weak.append((dim, rt, h1, m))
        else:
            ratios.append((dim, rt, h1, m))
    ratios.sort(key=lambda x: -x[1])
    print(f"{'本池维度':<20}{'倍数':>9}{'本池IC':>10}{'外部IC':>10}{'外部日t':>9}  解读")
    for dim, rt, h1, m in ratios:
        if rt >= 3:
            note = "🚨 严重夸大"
        elif rt >= 1.5:
            note = "⚠️ 明显夸大"
        elif rt >= 0.8:
            note = "≈ 一致"
        else:
            note = "本池更弱（范围内截断）"
        print(f"{dim:<20}{rt:>+8.2f}x{h1:>10.4f}{m['ref_ic_daily']:>10.4f}"
              f"{(m.get('ref_t_daily') or 0):>+9.2f}  {note}")
    if weak:
        print()
        print("  以下维度外部基准**自身不显著**（日频 |t|<1.96），按规则不算倍数：")
        for dim, rt, h1, m in weak:
            print(f"    · {dim:<18} 本池h1 IC={h1:+.4f} | 外部{m['ref_factor']} "
                  f"ic={m['ref_ic_daily']:+.4f}, 日频t={(m.get('ref_t_daily') or 0):+.2f}"
                  f" → 若强行算倍数会得 {rt:+.2f}x（**伪精度，禁用**）")
        print("    ⇒ 正确解读：该因子在**全市场也不成立**。本池有值不代表本池有效，")
        print("      只代表「在本池这个子样本里恰好有」，须当噪声对待，不能当 alpha 依据。")

    # ── 四、口径边界 ──
    print("\n" + line)
    print("四、口径边界（倍数必须与此同读，否则会误用）")
    print(line)
    print("  1. 样本域：外部=全市场截面，本池=成交额TOP池内截面 → **范围内截断**")
    print("     本池已剔除小市值/低流动性，方差被压缩；因子在截断样本内 IC 变小是")
    print("     数学必然，不等于该因子失效（见 round38 §4.1）")
    print("  2. 频率：外部 ic_daily 是 1 日前瞻，故本表用 h1 对齐。")
    print("     本项目的决策周期是 T+10~T+20，**不应用 h1 的倍数去指导 h20 的权重**")
    print("  3. ICIR：外部是日频 ICIR，本池是截面序列 ICIR → 量级不可比，只看符号")
    print("  4. 定义：ep/size/quality 两边口径未必一致，匹配是按名近似，非严格对齐")
    print("  5. 结论用途：本表用于**披露偏离**，不用于**自动调权重**。")
    print("     自动调权需要 OOS 验证（P2-2 当前不满足条件）")

    # ── 落盘 ──
    out = {
        "round": 41,
        "purpose": "#308 外部基准校准层",
        "ref_file": os.path.basename(REF_CSV),
        "ref_n_factors": len(ref_rows),
        "weights_version": wj.get("version"),
        "weights": w,
        "local_ic": res,
        "match": match,
        "flips": [{"dim": d, "local_h1": h, "ref_factor": m["ref_factor"],
                   "ref_ic": m["ref_ic_daily"],
                   "ref_ic_month_t": m.get("ref_ic_month_t")} for d, h, m in flips],
        "ratios": [{"dim": d, "ratio": r, "local_h1": h,
                    "ref_ic": m["ref_ic_daily"], "ref_factor": m["ref_factor"],
                    "ref_t_daily": m.get("ref_t_daily")}
                   for d, r, h, m in ratios],
        "weak_ref_dims": [{"dim": d, "ratio_if_forced": r, "local_h1": h,
                           "ref_ic": m["ref_ic_daily"],
                           "ref_t_daily": m.get("ref_t_daily"),
                           "ref_factor": m["ref_factor"]}
                          for d, r, h, m in weak],
        "caveat": ("外部为全市场日频 IC，本池为 TOP 池内 h1 IC；范围截断与因子定义"
                   "差异使倍数只能作**披露**，不能作调权依据。"
                   "外部自身月频 |t|<1 的因子不算倍数（伪精度）。"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT}")


if __name__ == "__main__":
    main()
