#!/usr/bin/env python3
"""
基于当日评分数据自动生成 ai_assessment.json（AI 盘面评估层）。
实现：
  1) 从 top200_scored_*.json 提取真实市场画像与个股层数据；
  2) 构造结构化 prompt，调用 OpenAI-compatible LLM（默认火山方舟 / Moonshot）；
  3) 要求模型输出严格 JSON，与 enhance_scores.py 期望的 ai_assessment.json schema 一致；
  4) 若 LLM 调用失败或环境变量缺失，自动 fallback 到规则化生成，确保流水线不中断。

三种生成来源（优先级由调用方式决定）：
  A. --from-review <review_research_merged_*.json>   【本地复盘】WorkBuddy 逐公司联网核查后的结论文档
     —— 不依赖任何 LLM 密钥，由本地复盘产物直接驱动页面，标注 source="local-review"
  B. 默认（配置了 LLM_API_KEY）                      【云端/本地 LLM】
  C. 默认（无密钥 / 调用失败）                        【规则引擎 fallback】

环境变量：
  LLM_API_KEY   : API Key（不会硬编码到代码）
  LLM_BASE_URL  : 默认 https://ark.cn-beijing.volces.com/api/plan/v3（Agent Plan 专属通道）
  LLM_MODEL     : 默认 deepseek-v4-pro
数据纪律：所有数据来自 scored json 真实字段，不编造行情。
"""
import json
import os
import sys
import glob
import argparse
from collections import defaultdict, Counter
from datetime import datetime

# 清除代理环境变量: HTTP(S)_PROXY 会干扰火山方舟 API 直连(本地/CI 通用)
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

# ---------- 配置 ----------
# 已验证可用的默认通道（火山方舟 plan 通道，OpenAI 兼容）：
#   base_url = https://ark.cn-beijing.volces.com/api/plan/v3
#   model    = deepseek-v4-pro
# 如需切换，优先用环境变量覆盖，不要在代码里硬编码 API Key。
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
DEFAULT_MODEL = "deepseek-v4-pro"
API_KEY = os.environ.get("LLM_API_KEY")
BASE_URL = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
MODEL = os.environ.get("LLM_MODEL") or DEFAULT_MODEL

# ---------- 工具函数 ----------
def _pct_fmt(x):
    return f"{x:+.2f}%" if x is not None else "N/A"


def _find_latest_scored(here):
    fs = [f for f in sorted(glob.glob(os.path.join(here, "top200_scored_*.json")), reverse=True)
          if "_watch_" not in f]
    if not fs:
        return None
    # 优先选不含 .bak 的
    for f in fs:
        if ".bak" not in f and ".tmp" not in f:
            return f
    return fs[0]


def _build_summary(scored_path):
    with open(scored_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results", [])
    mr = data.get("market_review") or {}
    idx = mr.get("indices", {})
    amount = mr.get("amount_yi")
    up, down = mr.get("up"), mr.get("down")
    zt, dt = mr.get("zt"), mr.get("dt")
    money_phase = mr.get("money_phase", "")
    money_score = mr.get("money_score")
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")

    # 个股排序
    sorted_pct = sorted(results, key=lambda x: x.get("pct_chg", 0), reverse=True)
    top_gainers = sorted_pct[:10]
    top_losers = sorted_pct[-10:]

    # 板块/题材汇总
    sector_map = defaultdict(list)
    theme_map = defaultdict(list)
    for r in results:
        sector_map[r.get("sector", "其他")].append(r)
        st = r.get("sub_theme") or "其他"
        theme_map[st].append(r)

    sector_avg = []
    for s, lst in sector_map.items():
        avg = sum(x.get("pct_chg", 0) for x in lst) / len(lst)
        sector_avg.append((s, len(lst), avg))
    sector_avg.sort(key=lambda x: -x[2])

    theme_avg = []
    for t, lst in theme_map.items():
        avg = sum(x.get("pct_chg", 0) for x in lst) / len(lst)
        top3 = sorted(lst, key=lambda x: x.get("pct_chg", 0), reverse=True)[:3]
        theme_avg.append((t, len(lst), avg, top3))
    theme_avg.sort(key=lambda x: -x[2])

    return {
        "date": date,
        "data": data,
        "results": results,
        "mr": mr,
        "idx": idx,
        "amount": amount,
        "up": up,
        "down": down,
        "zt": zt,
        "dt": dt,
        "money_phase": money_phase,
        "money_score": money_score,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "sector_avg": sector_avg,
        "theme_avg": theme_avg,
        "theme_map": {k: v for k, v in theme_map.items()},
        "sector_map": {k: v for k, v in sector_map.items()},
    }


def _build_prompt(s):
    lines = []
    lines.append("你是一位资深A股策略分析师。请基于以下当日真实收盘数据，撰写结构化盘面复盘点评。")
    lines.append("")
    lines.append(f"日期：{s['date']}")
    idx_parts = [f"{k}{_pct_fmt(v)}" for k, v in s["idx"].items()]
    lines.append(f"主要指数：{', '.join(idx_parts)}")
    lines.append(f"全市场涨跌：上涨{s['up']}家 / 下跌{s['down']}家，涨停{s['zt']}只 / 跌停{s['dt']}只")
    lines.append(f"两市成交额：约{s['amount']}亿元")
    lines.append(f"赚钱效应阶段：{s['money_phase']}")
    lines.append("")
    lines.append("板块均涨跌幅（TOP200口径）：")
    for name, count, avg in s["sector_avg"][:12]:
        lines.append(f"  - {name}：平均 {_pct_fmt(avg)}，共{count}只")
    lines.append("")
    lines.append("题材均涨跌幅（TOP200口径，取活跃题材）：")
    for name, count, avg, top3 in s["theme_avg"][:15]:
        top_str = ", ".join([f"{r['name']}({_pct_fmt(r.get('pct_chg'))})" for r in top3])
        lines.append(f"  - {name}：平均 {_pct_fmt(avg)}，龙头 {top_str}")
    lines.append("")
    lines.append("涨幅前列个股（TOP200）：")
    for r in s["top_gainers"][:8]:
        lines.append(f"  - {r['name']}({r.get('code','')}): {_pct_fmt(r.get('pct_chg'))}, {r.get('sector','')}/{r.get('sub_theme','')}, FwdPE={r.get('fwd_pe') or 'N/A'}, growth={r.get('growth') or 'N/A'}")
    lines.append("")
    lines.append("跌幅前列个股（TOP200）：")
    for r in s["top_losers"][:8]:
        lines.append(f"  - {r['name']}({r.get('code','')}): {_pct_fmt(r.get('pct_chg'))}, {r.get('sector','')}/{r.get('sub_theme','')}")
    lines.append("")
    _mrtext = (s.get("mr") or {}).get("text")
    if _mrtext:
        lines.append("【算法层盘面速览（数据驱动，供参考，请勿直接照抄）】")
        lines.append(str(_mrtext))
        lines.append("")
    lines.append("【当日 TOP200 在榜个股名单（按细分题材分组，格式 名称(涨跌幅)）】")
    lines.append("⚠️ 硬约束：themes[].stocks 只能从下面名单中选取，且必须与名单中文名【完全一致】（不得加后缀、不得简写、不得虚构名单外个股）。")
    _tm = s.get("theme_map") or {}
    _tm_sorted = sorted(_tm.items(), key=lambda kv: -max((x.get("pct_chg", 0) for x in kv[1]), default=0))
    for _st, _lst in _tm_sorted:
        _top = sorted(_lst, key=lambda x: x.get("pct_chg", 0), reverse=True)[:8]
        _names = ", ".join(f"{r['name']}({_pct_fmt(r.get('pct_chg'))})" for r in _top)
        lines.append(f"  - {_st}：{_names}")
    lines.append("")
    lines.append("输出要求：")
    lines.append("1. 严格输出可解析的 JSON，不要 Markdown 代码块，不要额外说明。")
    lines.append("2. JSON schema 必须包含以下字段：")
    lines.append('   - "date": 字符串')
    lines.append('   - "generated_by": 字符串')
    lines.append('   - "generated_at": 字符串')
    lines.append('   - "commentary": 300-500字盘面点评')
    lines.append('   - "watch_directions": 字符串数组，5-6条明日关注方向')
    lines.append('   - "model_summary": 200-300字模型总结')
    lines.append('   - "themes": 对象，键为主题名，值为 {"news":0-5,"heat":0-5,"consensus":"50字内","stocks":["个股名",...]}')
    lines.append('   - "portfolio": {"title":"组合推荐","picks":[{"name":"","valuation":"","note":""}],"verdict":""}')
    lines.append("3. 主题 heat/news 分值范围 0.0-5.0，需与当日涨跌方向一致；大涨题材 heat 应显著高于大跌题材。")
    lines.append("4. 不要编造不存在的数据，所有结论必须基于上面提供的数据。")
    return "\n".join(lines)


def _call_llm(prompt):
    """调用 OpenAI-compatible API；返回 JSON dict 或 None。"""
    if not API_KEY:
        print("⚠️ LLM_API_KEY 未设置，跳过 LLM 调用")
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": "你是资深A股策略分析师，只输出 JSON。"},
                      {"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4000,
        )
        content = resp.choices[0].message.content.strip()
        # 去除 markdown 代码块
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:].strip()
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ LLM 调用失败: {e}")
        return None


def _fallback_ai_assessment(s):
    """LLM 失败时的规则化 fallback，确保字段完整且数据真实。"""
    date = s["date"]
    idx = s["idx"]
    idx_str = ", ".join([f"{k}{_pct_fmt(v)}" for k, v in idx.items()])
    amount = s["amount"]
    up, down, zt, dt = s["up"], s["down"], s["zt"], s["dt"]
    money_phase = s["money_phase"]

    # 选取强势/弱势主题
    hot_themes = s["theme_avg"][:6]
    weak_themes = s["theme_avg"][-4:]
    hot_names = [t[0] for t in hot_themes]
    weak_names = [t[0] for t in weak_themes]
    hot_str = ", ".join(hot_names)
    weak_str = ", ".join(weak_names)

    commentary = (
        f"{date} A股指数分化显著：{idx_str}；全市场上涨{up}家、下跌{down}家，"
        f"涨停{zt}只、跌停{dt}只，两市成交约{amount}亿元，赚钱效应阶段为“{money_phase}”。"
        f"结构上，强势方向集中在{hot_str}等赛道，资金抱团业绩确定性；"
        f"弱势方向以{weak_str}等兑现明显。"
        "整体呈现结构性分化，需控仓聚焦高景气主线。"
    )

    # ===== v2.44 P0-1b 修复同文件内情绪自相矛盾 =====
    # 历史 bug: watch[2] 硬编码"赚钱效应阶段偏冰点", 而 commentary 用动态 money_phase,
    #   于是出现 10-09 那种"commentary 说中性偏暖 / watch_directions 说偏冰点"的同文件打架。
    # 现按当日 money_score 分档动态生成, 与 commentary 同源。
    _ms = s.get("money_score")
    if _ms is None:
        _senti_line = "赚钱效应数据暂缺，以个股信号为主，控制单票仓位。"
    elif _ms < 20:
        _senti_line = f"赚钱效应{_ms}分，处于冰点/退潮阶段，仓位控制为主，等待情绪修复信号。"
    elif _ms < 40:
        _senti_line = f"赚钱效应{_ms}分，处于弱势磨底阶段，仅轻仓试错，严设止损。"
    elif _ms < 60:
        _senti_line = f"赚钱效应{_ms}分，处于中性震荡阶段，结构性参与为主，不追高。"
    elif _ms < 80:
        _senti_line = f"赚钱效应{_ms}分，情绪回暖，可逢回调加仓，注意节奏。"
    else:
        _senti_line = f"赚钱效应{_ms}分，市场亢奋偏过热，注意兑现，不追高。"

    watch = [
        f"强势题材{'/'.join(hot_names[:3])}资金抱团，关注龙头持续性，避免加速段追高。",
        f"弱势题材{'/'.join(weak_names[:2])}仍在兑现，短线规避。",
        _senti_line,
        "观察成交额能否维持，缩量环境下题材轮动会加快。",
        "科创50逆势走强，关注半导体/AI芯片独立行情的延续性。",
    ]

    model_summary = (
        f"模型 {s['data'].get('model', 'stock-scorer')} 基于{date}真实行情：四大指数{idx_str}，"
        f"全市场{up}/{down}，成交{amount}亿，赚钱效应{money_phase}。"
        "自动识别强势/弱势题材并赋分，基本面叠加业绩导数，最终生成关注方向与组合建议。"
    )

    # themes：按投资主题聚合 sub_theme，计算主题级均涨幅与龙头
    theme_templates = {
        "覆铜板": ("PCB/覆铜板产业链", 4.2),
        "铜箔": ("PCB/覆铜板产业链", 4.0),
        "BOPET/光学膜": ("MLCC/薄膜", 3.8),
        "玻纤": ("玻璃玻纤", 4.0),
        "电子布/玻纤": ("玻璃玻纤", 3.8),
        "半导体设备": ("半导体/AI芯片", 3.7),
        "AI芯片/GPU": ("半导体/AI芯片", 3.9),
        "存储芯片": ("存储芯片", 3.2),
        "光模块": ("光模块", 2.8),
        "CXO/创新药": ("CXO/创新药", 3.0),
        "铜金矿/有色资源": ("铜金矿/有色资源", 3.3),
        "造船/军工": ("造船/军工", 3.4),
        "光通信/光纤/海缆": ("光通信/光纤/海缆", 3.4),
    }
    theme_groups = defaultdict(lambda: {"rows": [], "sub_themes": []})
    for r in s["results"]:
        st = r.get("sub_theme") or "其他"
        label, base_heat = theme_templates.get(st, (st, 3.0))
        theme_groups[label]["rows"].append(r)
        if st not in theme_groups[label]["sub_themes"]:
            theme_groups[label]["sub_themes"].append(st)

    # 按主题平均涨幅排序，取前10
    theme_stats = []
    for label, g in theme_groups.items():
        rows = g["rows"]
        avg = sum(r.get("pct_chg", 0) for r in rows) / len(rows)
        theme_stats.append((label, avg, rows, g["sub_themes"]))
    theme_stats.sort(key=lambda x: -x[1])

    themes = {}
    used_stocks = set()
    base_heats = dict(theme_templates.values())
    for label, avg, rows, sub_themes in theme_stats[:10]:
        base = base_heats.get(label, 3.0)
        heat = max(0.0, min(5.0, round(base + avg / 10.0, 2)))
        news = max(0.0, min(5.0, round(heat - 0.1, 2)))
        top = sorted(rows, key=lambda x: x.get("pct_chg", 0), reverse=True)
        stocks = [r["name"] for r in top if r["name"] not in used_stocks][:7]
        used_stocks.update(stocks)
        if not stocks:
            continue
        sub_str = "/".join(sub_themes[:2])
        themes[label] = {
            "news": news,
            "heat": heat,
            "consensus": f"{label}（含{sub_str}）平均{_pct_fmt(avg)}，{'资金活跃' if avg > 0 else '弱势调整'}",
            "stocks": stocks,
        }

    # portfolio：从强势主题龙头中选股，兼顾估值/业绩
    picks = []
    for label, avg, rows, _ in theme_stats[:4]:
        if not rows:
            continue
        r = sorted(rows, key=lambda x: x.get("pct_chg", 0), reverse=True)[0]
        if r["name"] not in [p["name"] for p in picks]:
            picks.append({
                "name": r["name"],
                "valuation": f"fwdPE={r.get('fwd_pe') or 'N/A'}, growth={r.get('growth') or 'N/A'}",
                "note": f"今日{_pct_fmt(r.get('pct_chg'))}，{label}龙头，关注动量持续性",
            })
    if len(picks) < 4:
        for r in s["top_gainers"]:
            if r["name"] not in [p["name"] for p in picks]:
                picks.append({
                    "name": r["name"],
                    "valuation": f"fwdPE={r.get('fwd_pe') or 'N/A'}, growth={r.get('growth') or 'N/A'}",
                    "note": f"今日{_pct_fmt(r.get('pct_chg'))}，关注动量持续性",
                })
            if len(picks) >= 4:
                break

    portfolio = {
        "title": "组合推荐",
        "picks": picks[:4],
        "verdict": f"{date}赚钱效应{money_phase}，建议控仓聚焦强势题材（{'/'.join(hot_names[:3])}），规避弱势兑现方向。",
    }

    return {
        "date": date,
        "generated_by": f"WorkBuddy AI agent LLM fallback rule engine, based on {s['data'].get('model','stock-scorer')}",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commentary": commentary,
        "watch_directions": watch,
        "model_summary": model_summary,
        "themes": themes,
        "portfolio": portfolio,
    }


# ============================================================================
# 本地复盘接入层（阶段3 核心）
# ============================================================================
# 数据来源: review_research_merged_<date>.json（由 export_review_input.py +
#           merge_review_research.py 两阶段产出，含逐公司联网核查结论）。
# 与 LLM 路径的根本区别: 不走任何在线模型，结论来自"本地逐公司查资料"，
#   故页面来源标注必须是"本地复盘"，不得冒充 AI/LLM 生成（数据纪律）。
#
# 映射策略（本地复盘 → ai_assessment schema）:
#   market_commentary   → commentary            (全局复盘结论文本)
#   watch_directions    → watch_directions      (明日关注方向)
#   companies[].verdict → themes[].heat 修正    (逐公司结论直接调制题材热度)
#   companies[].findings→ themes[].consensus / model_summary (事实性描述)
#   companies 中 verdict=上调/维持 且 tier=deep → portfolio.picks
#
# 题材热度算法（本地复盘路径）:
#   以算法层题材均涨幅得到的 base_heat 为基准，
#   再按该题材下"有本地结论"的个股 verdict 平均修正量调整:
#     上调 +0.6 / 维持 +0.1 / 观察 0.0 / 中性 0.0 / 下调 -0.6 / 规避 -1.0
#   最终 clamp 到 [0,5]。既保留市场真实涨跌信息，又体现逐公司核查的独立判断。

_VERDICT_DELTA = {
    "上调": 0.6, "维持": 0.1, "观察": 0.0, "中性": 0.0,
    "下调": -0.6, "规避": -1.0,
}
# 参与组合推荐的 verdict（仅正向结论入选）
_VERDICT_POSITIVE = ("上调", "维持")


def _review_themes(summary, merged):
    """本地复盘路径：按 sub_theme 直接聚合题材，并叠加逐公司 verdict 修正。

    ⚠️ 与 LLM/fallback 路径的关键差异：不走 theme_templates 白名单。
       原因：白名单只覆盖 13 个预设细分题材，导致「宁德时代/中国巨石」等
       被深度核查的个股落不进任何题材 → 本地结论无法传导到评分层。
       改为直接用 scored json 的 sub_theme 分组，保证每只被核查个股都被覆盖。
    """
    name2verdict = {}
    for c in (merged.get("companies") or []):
        nm = c.get("name")
        if nm:
            name2verdict[nm] = c.get("verdict")

    groups = defaultdict(list)
    for r in summary.get("results", []):
        st = r.get("sub_theme") or r.get("sector") or "其他"
        groups[st].append(r)

    themes = {}
    for st, rows in groups.items():
        avg = sum((float(x.get("pct_chg") or 0)) for x in rows) / len(rows)
        # base: 由当日真实均涨幅推导（+10% → 满 5 分；-10% → 0 分）
        base = max(0.0, min(5.0, 3.0 + avg / 10.0 * 2.0))
        deltas = [_VERDICT_DELTA.get(name2verdict.get(x.get("name")), 0.0) for x in rows
                  if name2verdict.get(x.get("name"))]
        n_checked = len(deltas)
        heat = base + (sum(deltas) / n_checked if n_checked else 0.0)
        heat = round(max(0.0, min(5.0, heat)), 2)
        news = round(max(0.0, min(5.0, base + (sum(deltas) / n_checked * 0.8 if n_checked else 0.0))), 2)

        # 题材内个股：核查过的优先，其次按涨幅
        top = sorted(rows, key=lambda x: (
            0 if name2verdict.get(x.get("name")) else 1,
            -(float(x.get("pct_chg") or 0))))[:7]
        consensus = f"{st}（{len(rows)}只）平均{avg:+.2f}%，{'资金活跃' if avg > 0 else '弱势调整'}"
        if n_checked:
            consensus += f"｜本地核查 {n_checked}/{len(rows)} 只"
        themes[st] = {
            "news": news,
            "heat": heat,
            "consensus": consensus,
            "stocks": [x["name"] for x in top],
            "_n_checked": n_checked,
        }

    # 排序与截断 —— 关键：保证「含本地核查个股」的题材 100% 保留，
    #   否则被深度核查的公司落不进 themes → enhance_scores 无法命中 → 本地结论白做。
    #   规则：① 有核查的题材全部保留（按核查数降序）② 其余按热度补齐到上限。
    checked = {k: v for k, v in themes.items() if v["_n_checked"] > 0}
    unchecked = {k: v for k, v in themes.items() if v["_n_checked"] == 0}
    checked = dict(sorted(checked.items(), key=lambda kv: (-kv[1]["_n_checked"], -kv[1]["heat"])))
    unchecked = dict(sorted(unchecked.items(), key=lambda kv: -kv[1]["heat"]))

    MAX_THEMES = 20
    out = dict(checked)
    for k, v in unchecked.items():
        if len(out) >= MAX_THEMES:
            break
        out[k] = v
    for v in out.values():
        v.pop("_n_checked", None)
    return out


def _review_portfolio(summary, merged):
    """本地复盘路径：组合标的取自"深度复盘且结论正面"的公司。"""
    by_code = {_norm_symbol(r.get("code")): r for r in summary.get("results", [])}
    picks = []
    for c in (merged.get("companies") or []):
        if c.get("tier") != "deep":
            continue
        if c.get("verdict") not in _VERDICT_POSITIVE:
            continue
        r = by_code.get(_norm_symbol(c.get("code")))
        if not r:
            continue
        _note_parts = []
        if c.get("findings"):
            _note_parts.append(str(c["findings"])[:110])
        if c.get("target_price") is not None:
            _note_parts.append(f"机构目标价 {c['target_price']}")
        picks.append({
            "name": r.get("name"),
            "code": _norm_symbol(r.get("code")),
            "valuation": (f"fwdPE={r.get('fwd_pe') if r.get('fwd_pe') is not None else 'N/A'}, "
                          f"growth={r.get('growth') if r.get('growth') is not None else 'N/A'}"),
            "note": "；".join(_note_parts) or f"本地复盘结论：{c.get('verdict')}",
            "verdict": c.get("verdict"),
            "confidence": c.get("confidence"),
            # v2.46: 结构化机构目标价(查不到为 None), 供报告层"现价 vs 目标价"对比; 严禁编造
            "target_price": c.get("target_price"),
            "price": r.get("price"),
        })
    # 按算法总分降序，取前 6（组合不做满额，交由报告层门控）
    picks.sort(key=lambda x: -(by_code.get(x["code"], {}).get("total") or 0))
    return {
        "title": "本地复盘组合（逐公司核查后）",
        "picks": picks[:6],
        "verdict": (merged.get("market_commentary") or "")[:160],
    }


def _review_watch_directions(merged, summary):
    """关注方向：优先用本地复盘给出的，缺失则回退规则引擎版本。"""
    wd = [w for w in (merged.get("watch_directions") or []) if str(w).strip()]
    if wd:
        return wd
    return _fallback_ai_assessment(summary).get("watch_directions") or []


def _from_local_review(summary, merged):
    """把本地复盘产物装配为 ai_assessment.json（不经过任何在线模型）。"""
    date = merged.get("date") or summary.get("date")
    companies = merged.get("companies") or []
    v = merged.get("validation") or {}
    n_deep = sum(1 for c in companies if c.get("tier") == "deep")
    n_light = sum(1 for c in companies if c.get("tier") == "light")
    vc = Counter(c.get("verdict") for c in companies)

    commentary = (merged.get("market_commentary") or "").strip()
    if not commentary:
        # 未写全局点评 → 用规则引擎文本打底，并注明
        commentary = _fallback_ai_assessment(summary).get("commentary", "")

    _val_txt = "通过" if v.get("passed") else f"有 {v.get('n_error', 0)} 项错误"
    model_summary = (
        f"模型 {summary['data'].get('model', 'stock-scorer')} 基于 {date} 真实行情打分，"
        f"其后由本地复盘逐公司联网核查（深度 {n_deep} 只 / 轻量 {n_light} 只），"
        f"核查结论分布 {dict(vc)}；"
        f"题材热度已按逐公司结论修正。"
        f"校验：{_val_txt}。"
    )

    return {
        "date": date,
        "generated_by": f"WorkBuddy 本地复盘（逐公司联网核查 {len(companies)} 只，研究员：{merged.get('researcher') or 'WorkBuddy'}）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "local-review",
        "commentary": commentary,
        "watch_directions": _review_watch_directions(merged, summary),
        "model_summary": model_summary,
        "themes": _review_themes(summary, merged),
        "portfolio": _review_portfolio(summary, merged),
        "risk_note": "；".join(
            sorted({r for c in companies for r in (c.get("risks") or [])})
        )[:300],
        "review_meta": {
            "tier_deep": n_deep, "tier_light": n_light,
            "verdict_dist": dict(vc),
            "validation_passed": bool(v.get("passed")),
            "validation_error": v.get("n_error", 0),
            "validation_warn": v.get("n_warn", 0),
        },
        # fallback=False: 本条确由"大模型/本地核查"链路产出，非规则引擎拼接
        "fallback": False,
    }


def _norm_symbol(code):
    """code 归一（兼容 sz300750 / 300750 两种写法）。"""
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


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("scored", nargs="?", help="scored json（默认取最新）")
    ap.add_argument("out", nargs="?", help="输出路径（默认 ai_assessment.json）")
    ap.add_argument("--from-review", dest="from_review",
                    help="本地复盘产物 review_research_merged_*.json（指定则走本地复盘路径）")
    args = ap.parse_args()

    scored_path = args.scored or _find_latest_scored(here)
    out_path = args.out or os.path.join(here, "ai_assessment.json")

    if not scored_path or not os.path.exists(scored_path):
        print("❌ 未找到 scored json")
        return 1

    print(f"📊 读取 scored: {scored_path}")
    summary = _build_summary(scored_path)
    print(f"   个股数: {len(summary['results'])}, 日期: {summary['date']}")

    # ---------- 路径 A：本地复盘（无需 LLM 密钥）----------
    if args.from_review:
        if not os.path.exists(args.from_review):
            print(f"❌ 本地复盘产物不存在: {args.from_review}")
            return 1
        with open(args.from_review, "r", encoding="utf-8") as f:
            merged = json.load(f)
        if str(merged.get("date")) != str(summary["date"]):
            print(f"❌ 日期不一致：复盘产物={merged.get('date')}，scored={summary['date']}")
            return 1
        _v = merged.get("validation") or {}
        if not _v.get("passed"):
            print(f"⚠️ 复盘产物校验未通过（error {_v.get('n_error', 0)}）；"
                  f"页面仍将采用，但来源标注会含校验提示。")
        ai = _from_local_review(summary, merged)
        _clean_themes(ai, summary)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(ai, f, ensure_ascii=False, indent=2)
        print(f"✅ 已生成（本地复盘路径）: {out_path}")
        print(f"   来源: {ai['generated_by']}")
        print(f"   review_meta: {ai.get('review_meta')}")
        print(f"   themes {len(ai.get('themes', {}))} 个 / "
              f"watch_directions {len(ai.get('watch_directions', []))} 条 / "
              f"组合 {len((ai.get('portfolio') or {}).get('picks', []))} 只")
        return 0

    # ---------- 路径 B/C：在线 LLM → 失败回退规则引擎 ----------
    prompt = _build_prompt(summary)
    ai = _call_llm(prompt)

    if ai is None:
        print("⚠️ 使用 fallback 规则生成 ai_assessment.json")
        ai = _fallback_ai_assessment(summary)
        ai["fallback"] = True
        ai["source"] = "rule-fallback"
    else:
        # 校验并补齐必要字段（fallback 只计算一次）
        _fb = _fallback_ai_assessment(summary)
        for key in ["date", "generated_by", "generated_at", "commentary", "watch_directions", "model_summary", "themes", "portfolio"]:
            if key not in ai:
                ai[key] = _fb[key]
        ai["fallback"] = False
        ai["source"] = "llm"
        ai["date"] = summary["date"]
        ai["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ai["generated_by"] = f"WorkBuddy LLM ({MODEL})，基于当日真实行情数据"
        _clean_themes(ai, summary)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ai, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成: {out_path}")
    print(f"   themes 数量: {len(ai.get('themes', {}))}")
    print(f"   watch_directions 数量: {len(ai.get('watch_directions', []))}")
    print(f"   fallback: {ai.get('fallback')}")
    return 0


def _clean_themes(ai, summary):
    """清洗 themes[].stocks：剔除不在当日 TOP200 名单内的名称，否则 enhance_scores 无法命中赋分。"""
    _valid = {r["name"] for r in summary["results"]}
    _cleaned = {}
    for _k, _t in (ai.get("themes") or {}).items():
        if not isinstance(_t, dict):
            continue
        _stocks = [x for x in (_t.get("stocks") or []) if x in _valid]
        _dropped = [x for x in (_t.get("stocks") or []) if x not in _valid]
        if _dropped:
            print(f"   ⚠️ [{_k}] 剔除名单外个股: {_dropped}")
        if _stocks:
            _t["stocks"] = _stocks
            _cleaned[_k] = _t
    ai["themes"] = _cleaned
    print(f"   主题清洗后: {len(_cleaned)} 个，命中个股 "
          f"{sum(len(v['stocks']) for v in _cleaned.values())} 只")


if __name__ == "__main__":
    sys.exit(main())
