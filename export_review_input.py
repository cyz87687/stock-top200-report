#!/usr/bin/env python3
"""
阶段 0 —— 复盘输入导出器（本地复盘链路的起点）。

职责（严格单一）：
  从当日算法评分产物中，**理出当日需要复盘的公司代码**，输出一份结构化数据包，
  供「本地大模型逐公司查阅资料」阶段（阶段 1）消费。

复盘对象口径（四类来源，按优先级去重）：
  1. 主力池 S/A 级        —— 评级最高，买入决策依赖它，必须核实驱动因素
  2. 前向组合标的          —— momentum / reversal 两条量化线的实际持仓
  3. 观察池精选 A/B 级     —— 200~1000 名中 AI(或规则)选出的次日候选
  4. 主力池异动极端股      —— 涨跌幅 |x| >= 9.5% 但未落入上面三类的（涨停/跌停/暴跌）

输出：review_input_<date>.json
  {
    date, generated_at, source_scored, model,
    market { 指数/涨跌家数/成交/赚钱效应/强弱题材 },
    sectors [ {name, count, avg_pct, mom60, mom10, r60, r10} ],
    themes  [ {name, count, avg_pct, leaders} ],
    candidates [ { code, name, sources[], sector*, price, pct_chg, rating, total,
                   scores{}, valuation{}, tech_brief{}, theme{}, focus[] } ],
    stats { n_candidates, by_source{} }
  }

设计原则：
  - 只做「选取 + 结构化 + 标注待查要点」，**不做任何主观结论**（结论属于阶段 1/2）。
  - 所有数字逐字段取自 scored json 真实字段，缺失即为 null，**绝不编造**。
  - `focus[]` 是按规则从真实字段推导的「该查什么」清单，不是结论。

用法：
  python3 export_review_input.py [scored_json] [--watch watch_scored_json] [--portfolio p.json]
                                 [--out review_input_<date>.json] [--max 40] [--list]
  --list : 只打印人读清单（代码 + 名称 + 来源 + 待查要点），不写文件
"""
import json
import os
import sys
import glob
import argparse
from collections import defaultdict, Counter
from datetime import datetime

# 复盘来源标签（顺序即优先级，数字越小优先级越高）
SRC_MAIN_SA = "主力池S/A级"
SRC_PORTFOLIO = "前向组合标的"
SRC_WATCH = "观察池精选(200~1000名)"
SRC_ANOMALY = "主力池异动股"
SRC_PRIORITY = {SRC_MAIN_SA: 0, SRC_PORTFOLIO: 1, SRC_WATCH: 2, SRC_ANOMALY: 3}

# 异动阈值（涨跌幅绝对值）
ANOMALY_PCT = 9.5

# 复盘深度分档（决定阶段 1 的检索强度，控制整链路成本）
#   deep  : 逐只联网深度检索（公告/研报/异动原因/机构目标价/预期差）
#   light : 轻量核实（只需确认当日涨跌驱动事件与有无突发利空）
TIER_DEEP = "deep"
TIER_LIGHT = "light"


def _tier_of(srcs):
    """按来源决定复盘深度：决策直接相关的走 deep，仅需解释异动的走 light。"""
    if SRC_MAIN_SA in srcs or SRC_PORTFOLIO in srcs:
        return TIER_DEEP
    return TIER_LIGHT


# ---------- 小工具 ----------
def _f(x):
    """安全转 float，失败返回 None（不编造）。"""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm_code(code):
    """统一为 小写交易所前缀 + 6位数字，如 sh600519；已归一者原样返回。"""
    if not code:
        return ""
    c = str(code).strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    d = "".join(ch for ch in c if ch.isdigit())
    if len(d) != 6:
        return c
    if d[0] == "6":
        return "sh" + d
    if d[0] in ("0", "3"):
        return "sz" + d
    if d[0] in ("4", "8"):
        return "bj" + d
    return c


def _fmt_pct(x):
    v = _f(x)
    return f"{v:+.2f}%" if v is not None else "—"


def _find_latest_scored(here):
    fs = [f for f in sorted(glob.glob(os.path.join(here, "top200_scored_*.json")), reverse=True)
          if "_watch_" not in f and ".bak" not in f]
    return fs[0] if fs else None


def _find_latest(here, pattern):
    fs = sorted(glob.glob(os.path.join(here, pattern)), reverse=True)
    fs = [f for f in fs if ".bak" not in f]
    return fs[0] if fs else None


# ---------- 复盘要点推导（规则，非结论）----------
def _derive_focus(rec, srcs, sector_rank):
    """按真实字段推导「该查什么」，供阶段 1 逐项检索核实。"""
    f = []
    rating = rec.get("rating")
    growth = _f(rec.get("growth"))
    fwd_pe = _f(rec.get("fwd_pe"))
    pe = _f(rec.get("pe"))
    pct = _f(rec.get("pct_chg"))
    fd = rec.get("fund_detail") or {}
    d2 = _f(fd.get("deriv2_delta"))
    rsi = _f((rec.get("tech") or {}).get("rsi"))
    trend = (rec.get("tech") or {}).get("trend") or ""
    lifecycle = rec.get("lifecycle") or ""

    # 1) 评级驱动核实
    if rating in ("S", "A"):
        f.append("评级驱动核实：查最近公告/研报，确认题材热度或技术动量是否有基本面支撑")
    # 2) 业绩方向
    if growth is not None and growth < 0:
        f.append(f"业绩负增长({growth:+.0f}%)：查最新财报与下季度指引，确认是否已触底")
    elif growth is not None and growth >= 50:
        f.append(f"业绩高增({growth:+.0f}%)：查增长来源与可持续性（一次性收益？订单驱动？）")
    # 3) 估值
    if fwd_pe is not None and fwd_pe > 80:
        f.append(f"估值偏高(FwdPE {fwd_pe:.0f}x)：查机构目标价与业绩兑现节奏，评估预期差")
    elif fwd_pe is not None and fwd_pe > 0 and fwd_pe < 15:
        f.append(f"低估值(FwdPE {fwd_pe:.0f}x)：查是否存在价值陷阱（周期顶点/减值风险）")
    if pe is not None and pe < 0:
        f.append("静态PE为负（亏损）：查亏损原因与扭亏时间表")
    # 4) 增速二阶导
    if d2 is not None and d2 < -20:
        f.append(f"增长减速({d2:+.0f}pt)：查行业景气度拐点信号与在手订单")
    # 5) 技术位置
    if rsi is not None and rsi >= 75:
        f.append(f"技术超买(RSI {rsi:.0f})：查是否处加速段，评估回踩风险")
    elif rsi is not None and rsi <= 30:
        f.append(f"技术超卖(RSI {rsi:.0f})：查是否有利空出尽/抄底催化")
    if "空头" in trend:
        f.append("均线空头排列：查中长期趋势走弱原因，判断是否左侧")
    # 6) 题材生命周期
    if lifecycle and ("退潮" in lifecycle or "衰退" in lifecycle):
        f.append(f"题材{lifecycle}：查板块资金流向，确认是轮动切换还是趋势终结")
    # 7) 板块位置
    if sector_rank is not None and sector_rank >= 100:
        f.append(f"所属板块动量排名靠后(第{sector_rank}名)：查是否有独立催化")
    # 8) 异动
    if pct is not None and abs(pct) >= ANOMALY_PCT:
        _dir = "涨停/大涨" if pct > 0 else "跌停/大跌"
        f.append(f"当日{_dir}({pct:+.2f}%)：查驱动事件（公告/政策/订单/资金）及龙虎榜")
    # 9) 观察池专属
    if SRC_WATCH in srcs:
        f.append("榜外标的：查近期成交放大原因与是否具备进入主力池的条件")
    if not f:
        f.append("常规核实：查最近公告与行业消息，确认无突发利空")
    return f


def _tech_brief(rec):
    t = rec.get("tech") or {}
    return {
        "trend": t.get("trend"),
        "rsi": _f(t.get("rsi")),
        "rsi_state": t.get("rsi_state"),
        "ma5": _f(t.get("ma5")),
        "ma10": _f(t.get("ma10")),
        "ma20": _f(t.get("ma20")),
        "ma60": _f(t.get("ma60")),
        "macd_dif": _f(t.get("macd_dif")),
        "macd_hist": _f(t.get("macd_hist")),
        "position": _f(t.get("position")),
        "vol_ratio": _f(t.get("vol_ratio")),
        "mom5": _f(t.get("mom5")),
        "mom20": _f(t.get("mom20")),
        "overall": t.get("overall"),
        "support_levels": t.get("support_levels"),
        "resistance_levels": t.get("resistance_levels"),
    }


def _valuation_brief(rec):
    return {
        "pe": _f(rec.get("pe")),
        "fwd_pe": _f(rec.get("fwd_pe")),
        "growth": _f(rec.get("growth")),
        "cagr3": _f(rec.get("cagr3")),
        "pe_ladder": rec.get("pe_ladder"),
        "week_chg": _f(rec.get("week_chg")),
        "fund_reasons": rec.get("fund_reasons"),
    }


def _sector_map_from_scored(results):
    """板块均涨幅 + 动量排名（用于板块级复盘）。"""
    agg = defaultdict(lambda: {"n": 0, "sum_pct": 0.0, "mom60": None, "mom10": None,
                               "r60": None, "r10": None})
    for r in results:
        s = r.get("sector_l2") or r.get("sector_parent") or r.get("sector")
        if not s:
            continue
        a = agg[s]
        a["n"] += 1
        p = _f(r.get("pct_chg")) or 0.0
        a["sum_pct"] += p
        if a["mom60"] is None and _f(r.get("sector_mom60")) is not None:
            a["mom60"] = _f(r.get("sector_mom60"))
            a["mom10"] = _f(r.get("sector_mom10"))
            a["r60"] = r.get("sector_r60")
            a["r10"] = r.get("sector_r10")
    out = []
    for name, a in agg.items():
        out.append({
            "name": name, "count": a["n"],
            "avg_pct": round(a["sum_pct"] / a["n"], 2) if a["n"] else None,
            "mom60": a["mom60"], "mom10": a["mom10"],
            "r60": a["r60"], "r10": a["r10"],
        })
    out.sort(key=lambda x: -(x["avg_pct"] if x["avg_pct"] is not None else -99))
    return out


def _theme_map_from_scored(results):
    agg = defaultdict(list)
    for r in results:
        st = r.get("sub_theme") or "其他"
        agg[st].append(r)
    out = []
    for name, lst in agg.items():
        avg = sum((_f(x.get("pct_chg")) or 0.0) for x in lst) / len(lst)
        leaders = sorted(lst, key=lambda x: -(_f(x.get("pct_chg")) or -99))[:3]
        out.append({
            "name": name, "count": len(lst), "avg_pct": round(avg, 2),
            "leaders": [f"{x['name']}({_fmt_pct(x.get('pct_chg'))})" for x in leaders],
        })
    out.sort(key=lambda x: -x["avg_pct"])
    return out


# ---------- 主流程 ----------
def build(scored_path, watch_path=None, portfolio_path=None, max_n=40):
    with open(scored_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results", [])
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
    mr = data.get("market_review") or {}

    by_code = {}
    for r in results:
        c = _norm_code(r.get("code"))
        if c:
            by_code[c] = r

    candidates = {}   # code -> record

    def _add(rec, src, note=None):
        c = _norm_code(rec.get("code"))
        if not c:
            return
        if c in candidates:
            if src not in candidates[c]["sources"]:
                candidates[c]["sources"].append(src)
        else:
            candidates[c] = {
                "code": c,
                "name": rec.get("name"),
                "sources": [src],
                "source_note": note,
                "sector": rec.get("sector"),
                "sector_l2": rec.get("sector_l2") or rec.get("sector"),
                "sector_parent": rec.get("sector_parent"),
                "price": _f(rec.get("price")),
                "pct_chg": _f(rec.get("pct_chg")),
                "amount_yi": (round((_f(rec.get("turnover")) or 0) / 1e8, 2)
                              if _f(rec.get("turnover")) else None),
                "rating": rec.get("rating"),
                "total": _f(rec.get("total")),
                "advice": rec.get("advice"),
                "scores": {
                    "news": _f(rec.get("score_news")),
                    "tech": _f(rec.get("score_tech")),
                    "fund": _f(rec.get("score_fund")),
                    "theme": _f(rec.get("score_theme")),
                    "sector": _f(rec.get("score_sector")),
                },
                "valuation": _valuation_brief(rec),
                "tech_brief": _tech_brief(rec),
                "theme": {
                    "sub_theme": rec.get("sub_theme"),
                    "lifecycle": rec.get("lifecycle"),
                    "sustain_note": rec.get("sustain_note"),
                    "news_recency": rec.get("news_recency"),
                    "news_reasons": rec.get("news_reasons"),
                    "news_items": (rec.get("news_items") or [])[:3],
                },
                "sector_pos": {
                    "r60": rec.get("sector_r60"), "r10": rec.get("sector_r10"),
                    "mom60": _f(rec.get("sector_mom60")), "mom10": _f(rec.get("sector_mom10")),
                },
                "focus": [],
            }

    # 来源 1：主力池 S/A 级
    n_main = 0
    for r in results:
        if r.get("rating") in ("S", "A"):
            _add(r, SRC_MAIN_SA)
            n_main += 1

    # 来源 4：主力池异动极端股（涨跌幅 >= 9.5%）
    n_anom = 0
    for r in results:
        p = _f(r.get("pct_chg"))
        if p is not None and abs(p) >= ANOMALY_PCT:
            _add(r, SRC_ANOMALY)
            n_anom += 1

    # 来源 2：前向组合标的
    n_pf = 0
    rel_pf = os.path.relpath(portfolio_path, os.path.dirname(os.path.abspath(scored_path))) if portfolio_path else None
    if portfolio_path and os.path.exists(portfolio_path):
        with open(portfolio_path, "r", encoding="utf-8") as f:
            pf = json.load(f)
        for line, blk in (pf.get("portfolios") or {}).items():
            for pk in (blk.get("picks") or []):
                c = _norm_code(pk.get("code"))
                base = by_code.get(c)
                if base is not None:
                    _add(base, SRC_PORTFOLIO, note=f"组合线={line}")
                else:
                    # 组合里但不在主力池（少见）→ 用组合自带字段构造
                    _add(pk, SRC_PORTFOLIO, note=f"组合线={line}（非主力池）")
                n_pf += 1

    # 来源 3：观察池精选
    n_watch = 0
    watch_results = []
    rel_watch = None
    if watch_path and os.path.exists(watch_path):
        with open(watch_path, "r", encoding="utf-8") as f:
            w = json.load(f)
        watch_results = w.get("results", [])
        rel_watch = os.path.relpath(watch_path, os.path.dirname(os.path.abspath(scored_path)))
        for r in watch_results:
            if r.get("rating") in ("S", "A", "B"):
                _add(r, SRC_WATCH)
                n_watch += 1

    # 板块/题材（主力池口径）
    sectors = _sector_map_from_scored(results)
    themes = _theme_map_from_scored(results)
    sector_rank = {s["name"]: s["r60"] for s in sectors}

    # 补 focus + 分级 + 排序
    for c in candidates.values():
        c["focus"] = _derive_focus(c, c["sources"], c["sector_pos"].get("r60"))
        c["sources"].sort(key=lambda s: SRC_PRIORITY.get(s, 9))
        c["tier"] = _tier_of(c["sources"])

    ordered = sorted(
        candidates.values(),
        key=lambda c: (0 if c["tier"] == TIER_DEEP else 1,
                       min(SRC_PRIORITY.get(s, 9) for s in c["sources"]),
                       -(c.get("total") or 0)),
    )

    by_source = Counter()
    tier_cnt = Counter()
    for c in ordered:
        for s in c["sources"]:
            by_source[s] += 1
        tier_cnt[c["tier"]] += 1

    # 分级配额：深度档不设上限（决策刚需），轻量档受 --light-max 约束
    trimmed = False
    if max_n and max_n > 0:
        deep = [c for c in ordered if c["tier"] == TIER_DEEP]
        light = [c for c in ordered if c["tier"] == TIER_LIGHT]
        if len(light) > max_n:
            light = light[:max_n]
            trimmed = True
        ordered = deep + light

    pkg = {
        "date": date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": "export_review_input.py (阶段0 复盘输入导出)",
        "source_scored": os.path.basename(scored_path),
        "source_watch": os.path.basename(watch_path) if (watch_path and os.path.exists(watch_path)) else None,
        "source_portfolio": os.path.basename(portfolio_path) if (portfolio_path and os.path.exists(portfolio_path)) else None,
        "model": data.get("model"),
        "market": {
            "indices": mr.get("indices"),
            "index_level": mr.get("index_level"),
            "amount_yi": _f(mr.get("amount_yi")),
            "up": mr.get("up"), "down": mr.get("down"),
            "zt": mr.get("zt"), "dt": mr.get("dt"),
            "money_score": mr.get("money_score"),
            "money_phase": mr.get("money_phase"),
            "hot": mr.get("hot"), "weak": mr.get("weak"),
            "text": mr.get("text"),
        },
        "stats": {
            "n_candidates": len(ordered),
            "n_candidates_total": len(candidates),
            "trimmed_by_max": trimmed,
            "by_tier": dict(tier_cnt),
            "raw_counts": {
                "main_sa": n_main, "anomaly": n_anom,
                "portfolio_picks": n_pf, "watch_selected": n_watch,
            },
            "by_source": dict(by_source),
            "rating_dist": dict(Counter(r.get("rating") for r in results)),
        },
        "sectors": sectors,
        "themes": themes,
        "candidates": ordered,
    }
    return pkg


def print_list(pkg):
    print(f"📅 {pkg['date']}  模型 {pkg.get('model')}")
    m = pkg.get("market") or {}
    idx = ", ".join(f"{k}{_fmt_pct(v)}" for k, v in (m.get("indices") or {}).items())
    print(f"   指数: {idx}")
    print(f"   涨跌: {m.get('up')}/{m.get('down')}  成交 {m.get('amount_yi')}亿  "
          f"赚钱效应 {m.get('money_score')}分 {m.get('money_phase')}")
    print(f"   待复盘公司: {pkg['stats']['n_candidates']} 只 "
          f"(来源: {pkg['stats']['by_source']})")
    print(f"   复盘深度: {pkg['stats'].get('by_tier')}")
    print()
    print(f"{'代码':<10}{'名称':<12}{'评级':<5}{'涨跌':<10}{'板块':<14}{'深度':<6}来源")
    print("-" * 100)
    for c in pkg["candidates"]:
        print(f"{c['code']:<10}{str(c['name']):<12}{str(c['rating']):<5}"
              f"{_fmt_pct(c['pct_chg']):<10}{str(c['sector_l2'] or c['sector']):<14}"
              f"{c.get('tier',''):<6}{'/'.join(c['sources'])}")
    print()
    for _tier, _label in ((TIER_DEEP, "深度复盘（逐只联网检索）"),
                          (TIER_LIGHT, "轻量核查（仅确认异动驱动）")):
        _grp = [c for c in pkg["candidates"] if c.get("tier") == _tier]
        if not _grp:
            continue
        print(f"\n{'=' * 100}\n=== {_label} · {len(_grp)} 只 ===\n{'=' * 100}")
        for c in _grp:
            print(f"\n【{c['name']} {c['code']}】{c['rating']} / 总分{c['total']} / "
                  f"{_fmt_pct(c['pct_chg'])} / {c['sector_l2'] or c['sector']}"
                  f"（板块60日动量第{c['sector_pos'].get('r60')}名）")
            for fc in c["focus"]:
                print(f"   · {fc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scored", nargs="?", help="主力池 scored json（默认取最新）")
    ap.add_argument("--watch", help="观察池 scored json（默认取最新 _watch_）")
    ap.add_argument("--portfolio", help="组合 json（默认取最新 sector/portfolio_*.json）")
    ap.add_argument("--out", help="输出路径（默认 review_input_<date>.json）")
    ap.add_argument("--max", type=int, default=20,
                    help="轻量档候选上限（默认 20；深度档不受限，0=同时放开轻量档）")
    ap.add_argument("--list", action="store_true", help="只打印人读清单，不写文件")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    scored = args.scored or _find_latest_scored(here)
    if not scored or not os.path.exists(scored):
        print("❌ 未找到主力池 scored json")
        return 1
    scored = os.path.abspath(scored)

    watch = args.watch
    if watch is None:
        w = _find_latest(here, "top200_scored_watch_*.json")
        # 日期需与主力池同日，避免用到隔夜观察池
        _d = os.path.basename(scored).replace("top200_scored_", "").replace(".json", "")
        w_same = os.path.join(here, f"top200_scored_watch_{_d}.json")
        watch = w_same if os.path.exists(w_same) else w
    portfolio = args.portfolio or _find_latest(here, "sector/portfolio_*.json")

    print(f"📥 主力池: {os.path.basename(scored)}")
    print(f"📥 观察池: {os.path.basename(watch) if watch else '(无)'}")
    print(f"📥 组合  : {os.path.basename(portfolio) if portfolio else '(无)'}")

    pkg = build(scored, watch, portfolio, max_n=args.max)

    if args.list:
        print_list(pkg)
        return 0

    out = args.out or os.path.join(here, f"review_input_{pkg['date']}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)
    print(f"✅ 已导出复盘输入: {out}")
    print(f"   候选 {pkg['stats']['n_candidates']} 只（去重前 {pkg['stats']['n_candidates_total']}）")
    print(f"   来源分布: {pkg['stats']['by_source']}")
    print(f"   板块 {len(pkg['sectors'])} 个 / 题材 {len(pkg['themes'])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
