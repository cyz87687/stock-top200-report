#!/usr/bin/env python3
"""多因子精选组合（≤10 只）—— 权重全部由 factor_ic.json 的池内实证决定，不手拍

背景（老大第 3 项要求）：
  「今日候选组合数量太多，请用多因子的思路选出一个组合，今日组合总数不要超过 10 只」

原 pick_portfolio.py 的问题：
  它跑「动量线 + 反转线」两条路线，各出 10 只 → 页面上共 20 只，且两线逻辑互斥
  （动量线买板块 60 日涨幅前列，反转线买末列）。读者拿到 20 只无法执行。

本脚本的做法：
  ① 不按板块动量切路线 —— 板块动量 score_sector 池内 IC 全周期不显著（t=0.29/-1.20），
     没有证据支持它作为主选股轴。
  ② 按「因子实证强度」直接加权，且只使用有池内样本支撑的维度。
  ③ 三条硬排除（不是加分，是剔除）：attention / theme 显著负向，及其追高 RSI。
  ④ 组合约束：≤10 只、单一申万二级 ≤2、单一申万一级 ≤2，保证不押注单一赛道。

⚠️ 证据边界（必须随结果一起输出，禁止只报好消息）：
  - 反转块（oversold/bias/rsi_rev/vol_pulse/overbought）仅有 8 个截面，属弱证据；
  - 因此本组合实质是「fund 单因子 + news 短线辅助 + 反转块弱先验」，
    不是教科书意义上的均衡多因子模型；
  - 池内头组净收益历史为负（池内基准在跌），组合≠赚钱，须配合仓位纪律。

用法：
  python3 pick_multifactor.py [scored_json] [--top 10] [--out sector/mf_pick_<date>.json]
  python3 pick_multifactor.py --allow-off-zone     # 放宽：不要求现价在买区内（调试用）

⚠️ v4.3（2026-10-25 老大第 2 项要求）：**只选当日现价落在买入区间内的个股**。
  原实现选完 10 只后在报告层才发现「现价高于买区」，只能打个提示——
  实测 10-23：10 只里 **6 只现价高于买区上限**（巨人网络 +1.04%、鼎泰高科 +12.08%），
  页面写"10 只"但当日可成交的只有 4 只，读者按 10 只备资金必然落空。
  现在把这道闸前移到选股层：
    ① 用 AST 从 gen_report_top200.py **抽取 build_trade_plan 源码**复用
       （不重写：重写必然与页面口径漂移；不 import：会触发整份报告渲染）；
    ② 对全部合格候选算买区，剔除 `price > buy_hi` 或 `price < buy_lo` 的；
    ③ 把 buy_lo/buy_hi/stop/target/rr 一并落进 picks，报告层直接取用，
       不再各自推导，杜绝「json 与页面两套买区」。
  实证边界（audit_round16，A/B 同源同权重只差这一道闸，79 个入场日）：
     配对检验 B−A 超额：T+1 −0.05% / T+5 −0.15% / T+10 −0.67% / T+15 −1.14%，
     |NW t| 最大 1.54（<2）→ **代价在噪声内，不可断言有害**；
     而可执行性从「10 只中位数 4 只可当日成交」提升到「全部可当日成交」。
"""
import os
import sys
import json
import glob
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')

# ---------------------------------------------------------------------------
# 因子权重：由池内实证强度反推，含 Bonferroni 显著性判断
# 数据源 factor_ic.json（每次运行实时读取，不写死）
# Bonferroni：30 次检验 → |NW t| >= 3.08 才算池内已验证
# ---------------------------------------------------------------------------
BONF_T = 3.08

# 子分项 → 反转块聚合（弱证据块，块内等权）
REV_BLOCK = ("score_oversold60", "score_bias60", "score_rsi_rev",
             "score_vol_pulse", "score_overbought")

# 硬排除阈值（分位，0~1）：这些因子方向为负，高分即剔除
DROP_HIGH = {
    "score_attention": 0.80,   # T+5 NW t=-3.89 显著负
    "score_theme": 0.80,       # T+20 NW t=-2.70 负
}

# 组合约束
MAX_PER_L2 = 2
MAX_PER_L1 = 2

# v4.3：现价必须落在买入区间内（老大 2026-10-25 第 2 项要求）。
# ⚠️ 容差口径（v4.3 修订，曾在这里犯错）：第一版抄了页面 `_mf_n_in` 的 ±2% 容差，
#   结果巨人网络现价 20.40 vs 买区上限 20.19（越界 1.04%）被放进组合 ——
#   老大要的是「只选当日**在**买入区间的个股」，1% 的越界也不算在区间内。
#   ±2% 那个口径只适用于页面打提示，不适用于**剔除**。
#   现改为**严格口径**：price 不得高于 buy_hi、不得低于 buy_lo。
#   保留的极小容差只为吸收 2 位小数舍入（页面展示值与内存值曾因此打架），
#   不构成「允许小幅越界」。
ZONE_TOL_PCT = 0.001      # 0.1% 相对容差（仅舍入吸收）
ZONE_TOL_ABS = 0.01       # 1 分钱绝对容差


def _load_trade_plan_fn():
    """从 gen_report_top200.py 抽取 build_trade_plan 源码并 exec 出来复用。

    为什么不用 import：gen_report_top200.py 是顶层脚本，import 即触发整份报告
      渲染（读 json、拼 2.7MB HTML），既慢又有副作用。
    为什么不用复制源码：两处副本必然漂移，而「买入区间」是本道闸的唯一依据，
      口径漂移会让过滤结果与页面展示不一致。
    抽取失败时返回 None → 调用方按「无买区数据」保守处理（不过滤，但如实计数）。
    """
    try:
        import ast
        p = os.path.join(HERE, "gen_report_top200.py")
        src = open(p, encoding="utf-8").read()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "build_trade_plan":
                ns = {}
                exec(compile(ast.get_source_segment(src, node),
                             "<build_trade_plan>", "exec"), ns)
                return ns["build_trade_plan"]
        print("⚠️ 未在 gen_report_top200.py 找到 build_trade_plan → 买区过滤不生效")
    except Exception as _e:
        print(f"⚠️ 抽取 build_trade_plan 失败({_e}) → 买区过滤不生效")
    return None


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def load_ic(path):
    """读取各因子的池内 IC 证据，返回 {dim: {h: {ic,t,n}}}"""
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    out = {}
    for k, v in (d.get("factors") or {}).items():
        hz = {}
        for h in ("1", "5", "20"):
            a = (v.get("horizons") or {}).get(h) or {}
            if a.get("n"):
                hz[h] = {"ic": _f(a.get("ic_mean")), "t": _f(a.get("nw_t")),
                         "n": a.get("n"), "wr": _f(a.get("win_rate"))}
        out[k] = hz
    return out


def derive_weights(ic):
    """按实证强度推导权重。

    规则（写死为规则而非手填数字，任何人可复算）：
      1. 只看 T+1 与 T+5（T+20 样本损失大，且组合持有期按 5 日计）
      2. 取两周期 NW t 的较小者（保守：两个周期都得站得住）
      3. t_min <= 0 的维度权重记 0（方向不成立，不参与）
      4. score_news 单独衰减：T+5 后失效（t20=0.68），只保留 T+1 强度的一半
      5. 反转块五维先合成为一个块分，块权重按块内平均 t 计
    """
    why = {}

    def t_min(dim):
        hz = ic.get(dim) or {}
        ts = []
        for h in ("1", "5"):
            if h in hz and hz[h].get("t") is not None:
                ts.append(hz[h]["t"])
        return min(ts) if ts else None

    raw = {}
    for dim in ("score_fund", "score_news"):
        tm = t_min(dim)
        if tm is None or tm <= 0:
            raw[dim] = 0.0
            why[dim] = "t_min<=0 或缺失，权重记 0"
            continue
        w = tm
        if dim == "score_news":
            hz = ic.get(dim) or {}
            t20 = (hz.get("20") or {}).get("t")
            # 长周期失效 → 砍半，避免把短线因子当长期因子用
            if t20 is None or t20 < 1.0:
                w *= 0.5
                why[dim] = f"t_min={tm:.2f}，但 T+20 t={t20}，长周期失效 → 砍半"
            else:
                why[dim] = f"t_min={tm:.2f}"
        else:
            hz = ic.get(dim) or {}
            t20 = (hz.get("20") or {}).get("t")
            why[dim] = (f"t_min={tm:.2f}（T+1/T+5 均显著；"
                        f"但 T+20 t={t20 if t20 is not None else 'n/a'}，"
                        f"长周期已明显衰减）")
        raw[dim] = w

    # 反转块：块内平均 t_min，再整体打折（样本仅 8 截面）
    rev_ts, rev_parts = [], []
    for dim in REV_BLOCK:
        tm = t_min(dim)
        if tm is not None and tm > 0:
            rev_ts.append(tm)
            rev_parts.append(dim)
    if rev_ts:
        rev_w = (sum(rev_ts) / len(rev_ts)) * 0.5   # 弱证据折半
        raw["__rev__"] = rev_w
        why["__rev__"] = (f"块内 {len(rev_ts)} 维均 t_min={sum(rev_ts)/len(rev_ts):.2f}，"
                          f"仅 8 截面 → 折半")
    else:
        raw["__rev__"] = 0.0
        why["__rev__"] = "块内无正向维度"

    tot = sum(raw.values()) or 1.0
    return {k: v / tot for k, v in raw.items()}, why, rev_parts


def pct_rank(vals):
    """返回 {index: 分位(0~1)}，值越大分位越高；None 排最后"""
    idx = [(i, v) for i, v in enumerate(vals) if v is not None]
    idx.sort(key=lambda x: x[1])
    n = len(idx)
    out = {i: (0.0 if n <= 1 else k / (n - 1.0)) for k, (i, _) in enumerate(idx)}
    for i, v in enumerate(vals):
        if v is None:
            out.setdefault(i, 0.0)
    return out


def build(scored_path, top, per_l2, per_l1, require_zone=True):
    with open(scored_path, encoding='utf-8') as f:
        data = json.load(f)
    results = [r for r in data.get("results") or [] if r]
    date_str = data.get("date")

    ic_path = os.path.join(HERE, "factor_ic.json")
    ic = load_ic(ic_path)
    W, why, rev_parts = derive_weights(ic)

    # 资格过滤（与 pick_portfolio 同口径）
    elig, drop_log = [], {}
    for r in results:
        if r.get("is_new"):
            drop_log["次新"] = drop_log.get("次新", 0) + 1
            continue
        if r.get("suspended"):
            drop_log["停牌"] = drop_log.get("停牌", 0) + 1
            continue
        if not r.get("liquid_ok", True):
            drop_log["流动性不达标"] = drop_log.get("流动性不达标", 0) + 1
            continue
        elig.append(r)

    # v4.3（老大第 2 项要求）：现价必须落在买入区间内。
    # 买区由报告层同源函数 build_trade_plan 推导（AST 抽取复用，不重写、不 import），
    # 因此页面展示的买区与这里的判据**必然一致**，不会出现「json 说能买、页面说要等回踩」。
    btp = _load_trade_plan_fn()
    zone_map = {}          # index → plan dict
    zone_log = {"no_plan": 0, "above_zone": 0, "below_zone": 0, "in_zone": 0}
    if btp:
        for i, r in enumerate(elig):
            try:
                plan = btp(r)
            except Exception:
                plan = None
            price = r.get("price")
            if not plan or not isinstance(price, (int, float)) or price <= 0:
                zone_log["no_plan"] += 1
                continue
            lo, hi = plan.get("buy_lo"), plan.get("buy_hi")
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                zone_log["no_plan"] += 1
                continue
            # 严格口径：现价不得越出买区上下限；容差仅吸收 2 位小数舍入
            _tol_hi = max(hi * ZONE_TOL_PCT, ZONE_TOL_ABS)
            _tol_lo = max(lo * ZONE_TOL_PCT, ZONE_TOL_ABS)
            if price > hi + _tol_hi:
                zone_log["above_zone"] += 1
            elif price < lo - _tol_lo:
                zone_log["below_zone"] += 1
            else:
                zone_log["in_zone"] += 1
                zone_map[i] = plan
    else:
        # 抽不到 build_trade_plan → 不过滤，但把「未校验」如实计数，
        # 绝不静默当成「全部通过」
        zone_log["no_plan"] = len(elig)

    # 分位化（全部在合格样本内排名，避免绝对量纲问题）
    def col(key):
        return [(_f(r.get(key)) if isinstance(r.get(key), (int, float)) else None)
                for r in elig]

    def rev_score(i):
        vs = []
        for dim in rev_parts:
            vs.append(_f(elig[i].get(dim), 0.0) or 0.0)
        return sum(vs) / len(vs) if vs else 0.0

    pr = {}
    for dim in ("score_fund", "score_news"):
        pr[dim] = pct_rank(col(dim))
    pr["__rev__"] = pct_rank([rev_score(i) for i in range(len(elig))])
    pr_att = pct_rank(col("score_attention"))
    pr_thm = pct_rank(col("score_theme"))

    # 硬排除：attention / theme 高分（这两个方向实证为负）+ RSI 追高
    #   + v4.3「现价不在买入区间」剔除（老大第 2 项要求）
    keep_idx, exc_log = [], {}
    for i, r in enumerate(elig):
        bad = None
        if pr_att.get(i, 0) >= DROP_HIGH["score_attention"]:
            bad = "关注度过高(attention 前20%)"
        elif pr_thm.get(i, 0) >= DROP_HIGH["score_theme"]:
            bad = "题材过热(theme 前20%)"
        elif (_f((r.get("tech") or {}).get("rsi"), 0) or 0) > 70:
            bad = "RSI>70 追高"
        elif require_zone and btp and i not in zone_map:
            # 已算出买区但不满足 → 现价高于上限（需回踩）或已破位（低于下限）
            bad = "现价不在买入区间"
        if bad:
            exc_log[bad] = exc_log.get(bad, 0) + 1
        else:
            keep_idx.append(i)

    # 复合分
    scored = []
    for i in keep_idx:
        s = 0.0
        for k in ("score_fund", "score_news", "__rev__"):
            s += W.get(k, 0.0) * pr[k].get(i, 0.0)
        scored.append((i, s))
    scored.sort(key=lambda x: -x[1])

    # 组合约束
    picked, c_l2, c_l1 = [], {}, {}
    capped = []
    for i, s in scored:
        r = elig[i]
        l2 = r.get("sector_l2") or r.get("sector") or "未分类"
        l1 = r.get("sector_parent") or l2
        if c_l2.get(l2, 0) >= per_l2:
            capped.append({"name": r.get("name"), "reason": f"二级板块 {l2} 已满 {per_l2}"})
            continue
        if c_l1.get(l1, 0) >= per_l1:
            capped.append({"name": r.get("name"), "reason": f"一级行业 {l1} 已满 {per_l1}"})
            continue
        c_l2[l2] = c_l2.get(l2, 0) + 1
        c_l1[l1] = c_l1.get(l1, 0) + 1
        r2 = dict(r)
        r2["_mf_score"] = round(s, 4)
        r2["_mf_parts"] = {
            "fund_pct": round(pr["score_fund"].get(i, 0) * 100, 1),
            "news_pct": round(pr["score_news"].get(i, 0) * 100, 1),
            "rev_pct": round(pr["__rev__"].get(i, 0) * 100, 1),
            "att_pct": round(pr_att.get(i, 0) * 100, 1),
            "thm_pct": round(pr_thm.get(i, 0) * 100, 1),
        }
        # v4.3：把同源推导出的交易计划一并落盘，报告层直接取用，
        #   不再各自推导 —— 否则会出现「json 与页面两套买区」的口径分裂。
        _pl = zone_map.get(i)
        if _pl:
            r2["_plan"] = {k: _pl.get(k) for k in
                           ("buy_lo", "buy_hi", "stop", "target", "rr",
                            "entry_ref", "tgt_basis", "overbought", "flags")}
        picked.append(r2)
        if len(picked) >= top:
            break

    out = {
        "date": date_str,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "multifactor-v2（权重由池内实证反推，非手填；v4.3 起只选当日现价在买入区间内的个股）",
        "top": top,
        "n_eligible": len(elig),
        "n_scored": len(scored),
        "zone_filter": {
            "enabled": bool(require_zone and btp),
            "rule": ("现价须落在买入区间内（严格口径，容差仅吸收 2 位小数舍入："
                     "相对 0.1% 或绝对 1 分钱取大）；买区由 "
                     "gen_report_top200.build_trade_plan 同源推导，"
                     "多头取 ma5~ma10 回踩区、弱势取 ma10~ma20 企稳区"),
            "tolerance_pct": ZONE_TOL_PCT,
            "tolerance_abs": ZONE_TOL_ABS,
            "counts": zone_log,
            "note": ("build_trade_plan 中 buy_hi = min(ref_hi, price)，即买区上限永不高于现价"
                     " → 「现价在买区内」的实质含义是**股价已回踩到均线带内**，"
                     "而不是现价夹在区间上下限之间。price > buy_hi 即需等回踩，"
                     "当日挂单成交不了。⚠️ v4.3 曾误用页面 `_mf_n_in` 的 ±2% 容差做剔除，"
                     "导致现价越界 1.04% 的巨人网络被放进组合，已改为严格口径"),
        },
        "zone_stats": zone_log,
        "weights": {k: round(v, 4) for k, v in W.items()},
        "weight_reason": why,
        "rev_parts": rev_parts,
        "ic_evidence": {k: ic.get(k) for k in
                        ("score_fund", "score_news", "score_attention", "score_theme",
                         "score_sector", "score_tech") if k in ic},
        "drop_rules": {k: f"分位 >= {v:.0%} 剔除" for k, v in DROP_HIGH.items()},
        "excluded": exc_log,
        "eligibility_dropped": drop_log,
        "capped": capped,
        "picks": [{
            "name": r.get("name"), "code": r.get("code"),
            "sector_l2": r.get("sector_l2"), "sector_parent": r.get("sector_parent"),
            "price": r.get("price"), "pct_chg": r.get("pct_chg"),
            "total": r.get("total"), "rating": r.get("rating"),
            "mf_score": r.get("_mf_score"), "parts": r.get("_mf_parts"),
            "plan": r.get("_plan"),
            "score_fund": _f(r.get("score_fund")), "score_news": _f(r.get("score_news")),
            "score_theme": _f(r.get("score_theme")),
            "score_attention": _f(r.get("score_attention")),
            "score_sector": _f(r.get("score_sector")),
            "rsi": (r.get("tech") or {}).get("rsi"),
            "trend": (r.get("tech") or {}).get("trend"),
            "fwd_pe": _f(r.get("fwd_pe")), "growth": _f(r.get("growth")),
            "circ_mv_yi": round((_f(r.get("circ_mv"), 0) or 0) / 1e8, 2),
            "mom5": (r.get("tech") or {}).get("mom5"),
            "mom20": (r.get("tech") or {}).get("mom20"),
            "fund_reasons": (r.get("fund_reasons") or [])[:3],
            "news_reasons": (r.get("news_reasons") or [])[:2],
        } for r in picked],
        "boundaries": [
            "反转块仅 8 个截面 → 弱先验，不是已验证因子",
            "score_sector 池内 IC 全周期不显著（T+1 t=0.29 / T+5 t=-1.20）→ 未作为选股轴",
            "池内头组净收益历史上为负 → 组合≠赚钱，须配合仓位纪律与止损",
            "组合仅表达「相对排序」，不构成个股推荐",
            "v4.3 买区过滤的收益代价在噪声内（audit_round16 配对检验 |NW t| 最大 1.54 < 2），"
            "但把可执行性从「10 只中约 4 只可当日成交」提升到「全部可当日成交」",
        ],
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scored", nargs="?", default=None)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--per-l2", type=int, default=MAX_PER_L2)
    ap.add_argument("--per-l1", type=int, default=MAX_PER_L1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--allow-off-zone", action="store_true",
                    help="放宽：不要求现价落在买入区间内（仅调试对照用）")
    args = ap.parse_args()

    scored = args.scored
    if scored is None:
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
              if "_watch_" not in f and ".bak" not in f]
        scored = fs[0] if fs else None
    if not scored or not os.path.exists(scored):
        print("❌ 未找到 scored json")
        return 1

    out = build(scored, args.top, args.per_l2, args.per_l1,
                require_zone=not args.allow_off_zone)
    op = args.out or os.path.join(SECTOR_DIR, f"mf_pick_{out['date']}.json")
    os.makedirs(os.path.dirname(op), exist_ok=True)
    with open(op, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"scored={os.path.basename(scored)}  合格 {out['n_eligible']} 只 → 打分 {out['n_scored']} 只")
    print("权重:", {k: round(v, 3) for k, v in out["weights"].items()})
    for k, v in out["weight_reason"].items():
        print(f"   {k}: {v}")
    print("硬排除:", out["excluded"], "| 资格过滤:", out["eligibility_dropped"])
    print("买区统计:", out["zone_stats"],
          "| 过滤开关:", out["zone_filter"]["enabled"])
    print(f"\n=== 多因子组合（{len(out['picks'])} 只 · 现价均在买入区间内）===")
    for i, p in enumerate(out["picks"], 1):
        _pl = p.get("plan") or {}
        _zs = (f"买区 {_pl['buy_lo']:.2f}~{_pl['buy_hi']:.2f} · 止损 {_pl['stop']:.2f} · "
               f"目标 {_pl['target']:.2f} · RR {_pl['rr']:.1f}") if _pl else "无交易计划"
        print(f"{i:>2}. {p['name']:<6}{p['code']} {str(p['sector_l2']):<8} "
              f"mf={p['mf_score']:.3f} total={p['total']:.1f}({p['rating']}) "
              f"fund={p['score_fund']} news={p['score_news']} RSI={p['rsi']}")
        print(f"      现价 {p['price']:.2f} | {_zs}")
    print(f"\n✅ 写入 {op}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
