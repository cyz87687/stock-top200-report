#!/usr/bin/env python3
"""观察池(全市场成交额 200~1000 名) AI 筛选脚本

流程:
  1) 读 top1000_all_a.json 的 watch_pool (800 只)
  2) 规则预筛(剔除 ST/北交所, 保留异动/活跃) → 约 120 只候选
  3) LLM 精选 20~30 只「需要复盘」标的(deepseek-v4-flash), 含理由; LLM 失败自动 fallback 规则打分
  4) 输出 watchlist_<date>.json, 其中 "top200" 字段兼容 step2_stockscorer_v2 直接评分

用法: python3 select_watchlist.py [--input top1000_all_a.json] [--pick 25]
前置: 清代理; .llm_env 提供 LLM_API_KEY/BASE_URL/MODEL
"""
import os
import sys
import json
import glob
import argparse
import urllib.request
from datetime import datetime

for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PICK = 25
PRE_FILTER_N = 120


def rule_prefilter(pool, n=PRE_FILTER_N):
    """规则预筛: 剔除 ST/北交所, 保留异动+活跃标的, 取 n 只"""
    cands = []
    for s in pool:
        name = s.get('name', '')
        code = s.get('code', '')
        if 'ST' in name.upper():
            continue
        if code.startswith('bj'):
            continue
        pct = s.get('pct_chg', 0) or 0
        amt = s.get('amount', 0) or 0
        if amt <= 0 or (s.get('price') or 0) <= 0:
            continue
        # 异动打分: 涨跌幅偏离 + 成交额占比
        amt_score = amt / max(1e-9, pool[0].get('amount', 0) or 0) if pool else 0
        mov = min(abs(pct) / 10.0, 1.0)
        score = 0.6 * mov + 0.4 * min(amt_score * 5, 1.0)
        s = dict(s)
        s['_mov_score'] = round(score, 4)
        cands.append(s)
    cands.sort(key=lambda x: -x['_mov_score'])
    return cands[:n]


def call_llm(cand_lines, date, model, key, base):
    from openai import OpenAI
    sys_prompt = (
        "你是A股资深盘面复盘分析师，擅长捕捉当日需要重点复盘研究的个股。"
        "你只依据给定的真实行情数据判断，不得编造任何数字。")
    user_prompt = (
        f"今天是 {date}。以下是全市场成交额排名200~1000名(观察池)经规则预筛后的候选标的"
        f"(共{len(cand_lines)}只)，格式为「序号 名称 代码 涨跌幅% 成交额(亿)」：\n"
        + "\n".join(cand_lines) +
        "\n\n请从中选出最需要复盘关注的标的。选择标准(可叠加)：\n"
        "1) 明显异动：大涨/大跌/放量(成交额突出)\n"
        "2) 趋势拐点：接近关键位置或刚突破/破位\n"
        "3) 题材联动：与当日市场主线(如AI算力/半导体/创新药等)相关的低位或补涨品种\n"
        f"4) 潜在高波动机会\n\n要求：\n"
        f"- 只能从给定名单中选，不得新增或虚构；选出 {DEFAULT_PICK} 只左右(20~35只均可)\n"
        "- 输出纯JSON，格式 {\"picks\":[{\"name\":\"股票名\",\"reason\":\"15~25字理由\"}]}\n"
        "- 不要输出任何解释文字")
    client = OpenAI(api_key=key, base_url=base, timeout=180)
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": user_prompt}],
        temperature=0.3, max_tokens=2500)
    txt = r.choices[0].message.content
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1]
        txt = txt.rsplit("```", 1)[0]
    j = json.loads(txt)
    return j.get("picks", [])


def fallback_pick(cands, n=DEFAULT_PICK):
    out = []
    for s in cands[:n]:
        pct = s.get('pct_chg', 0) or 0
        amt_yi = (s.get('amount', 0) or 0) / 1e8
        direction = "大跌" if pct < -2 else ("大涨" if pct > 2 else ("回落" if pct < 0 else "走强"))
        out.append({"name": s['name'], "code": s['code'],
                    "reason": f"{direction}{pct:+.2f}% 成交{amt_yi:.1f}亿"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', default='top1000_all_a.json')
    ap.add_argument('--pick', type=int, default=DEFAULT_PICK)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f"❌ 未找到 {args.input}，请先运行 fetch_top200_sina.py")
        return 1
    raw = json.load(open(args.input, encoding='utf-8'))
    pool = raw.get('watch_pool', [])
    date = raw.get('date', datetime.now().strftime('%Y-%m-%d'))
    if not pool:
        print("❌ 观察池为空")
        return 1
    print(f"观察池: {len(pool)} 只 (成交额 200~1000 名) | 日期 {date}")

    cands = rule_prefilter(pool)
    print(f"规则预筛后: {len(cands)} 只候选")

    # LLM 精选
    picks = []
    llm_used = False
    key = os.environ.get('LLM_API_KEY', '')
    base = os.environ.get('LLM_BASE_URL', '')
    model = os.environ.get('LLM_MODEL', '')
    cand_lines = [f"{i}. {s['name']} {s['code']} {s['pct_chg']:+.2f}% {(s.get('amount',0) or 0)/1e8:.1f}亿"
                  for i, s in enumerate(cands, 1)]
    if key:
        for _try in range(2):
            try:
                picks = call_llm(cand_lines, date, model, key, base)
                llm_used = True
                print(f"✅ LLM 精选: {len(picks)} 只")
                break
            except Exception as e:
                print(f"⚠️ LLM 尝试{_try+1}失败({str(e)[:80]})")
    if not picks:
        picks = fallback_pick(cands, args.pick)
        print(f"⚠️ 规则兜底: {len(picks)} 只")

    # 合并候选信息 + 校验名单(剔除不在观察池的)
    # 注入申万二级板块: 供 step2 正确标注 sector(否则全落"综合")
    _sw = {}
    _swp = os.path.join(HERE, 'sector', 'sw_map.json')
    if os.path.exists(_swp):
        _sw = json.load(open(_swp, encoding='utf-8'))
    _l2 = _sw.get('l2', {})
    _l2p = _sw.get('l2_parent', {})
    by_name = {s['name']: s for s in cands}
    selected, dedup = [], set()
    for p in picks:
        name = p.get('name', '')
        if name not in by_name or name in dedup:
            continue
        dedup.add(name)
        s = by_name[name]
        c6 = ''.join(ch for ch in s.get('code', '') if ch.isdigit())[-6:].zfill(6)
        sec_l2 = _l2.get(c6, '')
        selected.append({
            'name': name, 'code': s['code'], 'price': s.get('price'),
            'pct_chg': s.get('pct_chg'), 'turnover': s.get('amount', 0),
            'amount': s.get('amount', 0), 'rank': s.get('rank'),
            'sector': sec_l2 or _l2p.get(sec_l2, ''),   # 申万二级(供 step2 板块标注)
            'reason': p.get('reason', ''),
        })
    print(f"校验后有效标的: {len(selected)} 只 (申万二级注入 {sum(1 for s in selected if s['sector'])} 只)")

    out = {
        'date': date,
        'pool': 'watch_pool_200_1000',
        'generated_by': f"LLM({model})" if llm_used else "rule-fallback",
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pre_filter_n': len(cands),
        'selected': selected,
        # step2 兼容字段
        'top200': selected,
    }
    op = args.out or os.path.join(HERE, f"watchlist_{date}.json")
    with open(op, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 观察池筛选结果: {op}")
    print("选中标的:")
    for s in selected:
        print(f"  {s['name']:<8}{s['code']} {s['pct_chg']:+.2f}%  {s['reason']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
