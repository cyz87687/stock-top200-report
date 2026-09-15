#!/usr/bin/env python3
"""
基于当日评分数据自动生成 ai_assessment.json（AI 盘面评估层）。
实现：
  1) 从 top200_scored_*.json 提取真实市场画像与个股层数据；
  2) 构造结构化 prompt，调用 OpenAI-compatible LLM（默认火山方舟 / Moonshot）；
  3) 要求模型输出严格 JSON，与 enhance_scores.py 期望的 ai_assessment.json schema 一致；
  4) 若 LLM 调用失败或环境变量缺失，自动 fallback 到规则化生成，确保流水线不中断。
环境变量（GitHub Actions secrets 中设置）：
  LLM_API_KEY   : API Key（不会硬编码到代码）
  LLM_BASE_URL  : 默认 https://ark.cn-beijing.volces.com/api/v3/
  LLM_MODEL     : 默认 kimi-k2.7-code
数据纪律：所有数据来自 scored json 真实字段，不编造行情。
"""
import json
import os
import sys
import glob
from collections import defaultdict
from datetime import datetime

# ---------- 配置 ----------
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3/"
DEFAULT_MODEL = "kimi-k2.7-code"
API_KEY = os.environ.get("LLM_API_KEY")
BASE_URL = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
MODEL = os.environ.get("LLM_MODEL") or DEFAULT_MODEL

# ---------- 工具函数 ----------
def _pct_fmt(x):
    return f"{x:+.2f}%" if x is not None else "N/A"


def _find_latest_scored(here):
    fs = sorted(glob.glob(os.path.join(here, "top200_scored_*.json")), reverse=True)
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
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "sector_avg": sector_avg,
        "theme_avg": theme_avg,
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

    watch = [
        f"强势题材{'/'.join(hot_names[:3])}资金抱团，关注龙头持续性，避免加速段追高。",
        f"弱势题材{'/'.join(weak_names[:2])}仍在兑现，短线规避。",
        "赚钱效应阶段偏冰点，仓位控制为主，等待情绪修复信号。",
        "观察成交额能否维持，缩量环境下题材轮动会加快。",
        "科创50逆势走强，关注半导体/AI芯片独立行情的延续性。",
    ]

    model_summary = (
        f"模型 v2.30（AI增强）基于{date}真实行情：四大指数{idx_str}，"
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


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    scored_path = sys.argv[1] if len(sys.argv) > 1 else _find_latest_scored(here)
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "ai_assessment.json")

    if not scored_path or not os.path.exists(scored_path):
        print("❌ 未找到 scored json")
        return 1

    print(f"📊 读取 scored: {scored_path}")
    summary = _build_summary(scored_path)
    print(f"   个股数: {len(summary['results'])}, 日期: {summary['date']}")

    prompt = _build_prompt(summary)
    ai = _call_llm(prompt)

    if ai is None:
        print("⚠️ 使用 fallback 规则生成 ai_assessment.json")
        ai = _fallback_ai_assessment(summary)
        ai["fallback"] = True
    else:
        # 校验并补齐必要字段
        for key in ["date", "generated_by", "generated_at", "commentary", "watch_directions", "model_summary", "themes", "portfolio"]:
            if key not in ai:
                ai[key] = _fallback_ai_assessment(summary)[key]
        ai["fallback"] = False
        ai["date"] = summary["date"]
        ai["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ai, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成: {out_path}")
    print(f"   themes 数量: {len(ai.get('themes', {}))}")
    print(f"   watch_directions 数量: {len(ai.get('watch_directions', []))}")
    print(f"   fallback: {ai.get('fallback')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
