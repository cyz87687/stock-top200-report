#!/usr/bin/env python3
"""每日 AI 焦点池生成器（v3.4，2026-10-13 老大指令）

背景与定位
----------
v3.1 删除「观察池」后，当日重点关注标的改由「互联网搜索」提供，但该环节此前
只挂在本地复盘脚本、且只做「机器源排名 + Agent 填空模板」，**没有真正落地
『每日本地 AI 联网筛选』**。本脚本把它升级为正式的「每日 AI 焦点池」：

  观察池（旧：按成交额 200~1000 名用内部评分选）
      ↓  替换为
  每日 AI 焦点池（新：机器源 + 本地 AI 联网检索，按当日真实信息选）

两层数据源
----------
A. 机器源（脚本自动跑，真实互联网接口，akshare 封装；单源失败自动降级）：
     1. stock_hot_rank_em        东方财富·个股人气榜（全市场热度排名）
     2. stock_zt_pool_em         东方财富·涨停池（当日涨停 + 连板数 + 封板时间）
     3. stock_zt_pool_dt_pool_em 东方财富·跌停池（异动另一端，防只看涨）
     4. stock_zt_pool_strong_em  东方财富·强势股池（非涨停的放量强势股）
B. 本地 AI 联网检索（由 Agent 执行，脚本只负责落格式与合并）：
     - 脚本生成 focus_pool_<date>.search.json 模板（含填写说明）；
     - Agent 联网检索当日「题材催化 / 公告 / 政策 / 龙虎榜 / 产业链」等，写入 additions；
     - 带 --merge-search 重跑：把 Agent 结论并入焦点池（优先级高于机器源，
       标 source=agent_search，score 加权）。

输出 focus_pool_<date>.json
---------------------------
  { date, generated_at, title, method, sources_used{}, n_raw, elapsed_s,
    agent_search{merged, n_additions}, ai_summary,
    focus[ {rank, name, code, score, sources[], reason, ai_reason,
            in_main_pool, sector, rating, total, price, pct_chg} ] }

字段 in_main_pool / sector / rating / total：与当日主力池 scored json 对照得出，
便于报告层直接渲染「池内焦点（带评级）vs 池外焦点」。

用法
----
  python3 fetch_focus_list.py [--date YYYY-MM-DD] [--top 40] [--merge-search] [--list]
  python3 fetch_focus_list.py --emit-template   # 只生成 Agent 联网检索模板
"""
import json
import os
import sys
import time
import argparse
from datetime import datetime, timedelta

# 清除代理环境变量: 行情接口需直连（与 fetch_top200_sina.py 一致）
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))


def bj_today():
    return (datetime.utcnow() + timedelta(hours=8)).date().isoformat()


def norm_code6(code):
    """任意格式代码 → 6位数字（sh600519→600519, 600519→600519）"""
    s = ''.join(ch for ch in str(code or '') if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


def market_prefix(code6):
    return 'sh' if code6.startswith('6') else ('bj' if code6.startswith(('4', '8', '9')) else 'sz')


def _norm_hhmmss(v):
    """封板时间归一化 → 'HH:MM:SS'。兼容 6 位 HHMMSS / 已带冒号 / 数值 三种形态。"""
    s = ''.join(ch for ch in str(v or '') if ch.isdigit())
    if len(s) >= 6:
        s = s[:6]
        return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
    if len(s) == 4:
        return f"{s[0:2]}:{s[2:4]}:00"
    return ''


# ---------------------------------------------------------------
# 机器源：akshare 封装（单源 try/except，失败降级）
# ---------------------------------------------------------------
def src_hot_rank(n=30):
    """东财人气榜 → [{name, code, rank, score, reason}]

    注意: 东财人气榜排名列名为「当前排名」(旧版曾为「序号」), 做兼容取值。
    """
    import akshare as ak
    df = ak.stock_hot_rank_em()
    out = []
    if df is None or df.empty:
        return out
    for _, r in df.head(n).iterrows():
        c6 = norm_code6(r.get('代码'))
        if not c6:
            continue
        rank_raw = r.get('当前排名', r.get('序号', 0))
        try:
            rank = int(rank_raw or 0)
        except Exception:
            rank = 0
        if rank <= 0:            # 排名缺失时按行序兜底, 避免整榜退化为"第0位"
            rank = len(out) + 1
        out.append({'name': str(r.get('股票名称', '')),
                    'code': c6,
                    'score': max(0.5, (n - rank + 1) / n * 3.0),
                    'reason': f"东财人气榜第{rank}位",
                    'sources': ['人气榜']})
    return out


def src_zt_pool(date_str, dt=False):
    """涨停/跌停池 → 连板数与封板时间计入分数

    注意: 「首次封板时间」为 6 位字符串 HHMMSS(如 092500), 与带冒号的
    时刻串比较恒为假。此处先归一化为 HH:MM:SS 再判早盘。
    """
    import akshare as ak
    d = date_str.replace('-', '')
    try:
        df = (ak.stock_zt_pool_dt_pool_em(date=d) if dt
              else ak.stock_zt_pool_em(date=d))
    except Exception:
        return []
    out = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        c6 = norm_code6(r.get('代码'))
        if not c6:
            continue
        lban = r.get('连板数', 0) or 0
        try:
            lban = int(lban)
        except Exception:
            lban = 0
        first = _norm_hhmmss(r.get('首次封板时间', ''))
        early = 1.0 if first and first < '10:00:00' else 0.0
        tag = '跌停' if dt else '涨停'
        out.append({'name': str(r.get('名称', '')),
                    'code': c6,
                    'score': (2.0 if not dt else 2.5) + lban * 0.5 + early,
                    'reason': f"{tag}{f'{lban}连板' if lban > 1 else ''}"
                              f"{f' 首板{first[:5]}' if first else ''}",
                    'sources': [tag + '池']})
    return out


def src_strong_pool(date_str, n=20):
    """强势股池（非涨停的放量强势股）→ 带入选理由"""
    import akshare as ak
    try:
        df = ak.stock_zt_pool_strong_em(date=date_str.replace('-', ''))
    except Exception:
        return []
    out = []
    if df is None or df.empty:
        return out
    for _, r in df.head(n).iterrows():
        c6 = norm_code6(r.get('代码'))
        if not c6:
            continue
        why = str(r.get('入选理由', '') or '').strip()
        out.append({'name': str(r.get('名称', '')),
                    'code': c6,
                    'score': 2.0,
                    'reason': f"强势股池{f'（{why}）' if why else '（放量走强）'}",
                    'sources': ['强势股池']})
    return out


# ---------------------------------------------------------------
# 主力池对照：读当日 scored json，得到 code → 池内字段（用于 in_main_pool 标记）
# ---------------------------------------------------------------
def load_scored_map(here, date_str, scored_path=None):
    """→ (set(code6), dict(code6 → {name, sector, rating, total, price, pct_chg}))

    优先显式路径，其次同日 top200_scored_<date>.json，最后取最新一份。
    """
    cands = []
    if scored_path:
        cands.append(scored_path)
    cands.append(os.path.join(here, f"top200_scored_{date_str}.json"))
    try:
        import glob
        latest = sorted((f for f in glob.glob(os.path.join(here, "top200_scored_*.json"))
                         if ".bak" not in f and "_watch_" not in f), reverse=True)
        cands += latest[:1]
    except Exception:
        pass
    for p in cands:
        try:
            if p and os.path.exists(p):
                data = json.load(open(p, encoding='utf-8'))
                m = {}
                for r in (data.get("results") or []):
                    c6 = norm_code6(r.get("code"))
                    if not c6:
                        continue
                    m[c6] = {"name": r.get("name"),
                             "sector": r.get("sector"),
                             "rating": r.get("rating"),
                             "total": r.get("total"),
                             "price": r.get("price"),
                             "pct_chg": r.get("pct_chg")}
                return set(m.keys()), m
        except Exception:
            continue
    return set(), {}


# ---------------------------------------------------------------
def merge_sources(items):
    """同 code 合并：分数取 max，sources/reason 累加"""
    agg = {}
    for it in items:
        c6 = norm_code6(it.get('code'))
        if not c6:
            continue
        cur = agg.get(c6)
        if cur is None:
            agg[c6] = {'name': it.get('name'), 'code': c6,
                       'score': float(it.get('score') or 0),
                       'sources': list(it.get('sources') or []),
                       'reason': it.get('reason') or '', 'ai_reason': ''}
        else:
            cur['score'] = max(cur['score'], float(it.get('score') or 0))
            cur['sources'] = sorted(set(cur['sources']) | set(it.get('sources') or []))
            if it.get('reason') and it['reason'] not in cur['reason']:
                cur['reason'] = (cur['reason'] + '；' + it['reason']).strip('；')
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='数据日期(默认北京当日)')
    ap.add_argument('--top', type=int, default=40, help='输出清单条数(默认40)')
    ap.add_argument('--scored', default=None, help='当日主力池 scored json(用于池内对照)')
    ap.add_argument('--merge-search', action='store_true',
                    help='合并 Agent 联网检索补充(focus_pool_<date>.search.json)')
    ap.add_argument('--emit-template', action='store_true', help='只生成 Agent 填写模板')
    ap.add_argument('--list', action='store_true', help='只打印人读清单，不写文件')
    args = ap.parse_args()

    date_str = args.date or bj_today()
    search_path = os.path.join(HERE, f'focus_pool_{date_str}.search.json')
    out_path = os.path.join(HERE, f'focus_pool_{date_str}.json')

    if args.emit_template:
        tpl = {
            'date': date_str,
            'filled_by': 'agent-web-search',
            'instructions': (
                '由本地 Agent 通过互联网搜索填写「当日需要重点关注的个股/方向」。'
                '每条: name(必需), code(6位或带前缀), reason(必需, 一句话说明为何关注), '
                'source(如 龙虎榜/公告/政策/题材催化/产业链/研报), url(可留空)。'
                'ai_summary: 一句话总结当日焦点主线(可留空)。'
                '只收真实检索到的信息，查不到就留空，不要编造。'),
            'ai_summary': '',
            'additions': [],
        }
        json.dump(tpl, open(search_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'✅ Agent 联网检索模板 → {os.path.basename(search_path)}')
        return 0

    # ---- 机器源 ----
    items, sources_used = [], {}
    t0 = time.time()
    try:
        hot = src_hot_rank(30)
        items += hot
        sources_used['hot_rank'] = len(hot)
        print(f'  人气榜: {len(hot)} 条')
    except Exception as e:
        print(f'  ⚠️ 人气榜失败(降级): {str(e)[:60]}')
    try:
        zt = src_zt_pool(date_str, dt=False)
        items += zt
        sources_used['zt_pool'] = len(zt)
        print(f'  涨停池: {len(zt)} 条')
    except Exception as e:
        print(f'  ⚠️ 涨停池失败(降级): {str(e)[:60]}')
    try:
        dt = src_zt_pool(date_str, dt=True)
        items += dt
        sources_used['dt_pool'] = len(dt)
        print(f'  跌停池: {len(dt)} 条')
    except Exception as e:
        print(f'  ⚠️ 跌停池失败(降级): {str(e)[:60]}')
    try:
        strong = src_strong_pool(date_str, 20)
        items += strong
        sources_used['strong_pool'] = len(strong)
        print(f'  强势股池: {len(strong)} 条')
    except Exception as e:
        print(f'  ⚠️ 强势股池失败(降级): {str(e)[:60]}')

    agg = merge_sources(items)

    # ---- Agent 联网检索补充 ----
    n_add, ai_summary = 0, ''
    if args.merge_search and os.path.exists(search_path):
        try:
            srch = json.load(open(search_path, encoding='utf-8'))
            ai_summary = str(srch.get('ai_summary', '') or '').strip()
            for a in (srch.get('additions') or []):
                c6 = norm_code6(a.get('code'))
                if not c6 or not (a.get('name') or '').strip():
                    continue
                reason = str(a.get('reason', '')).strip()
                if not reason:
                    continue
                src_tag = str(a.get('source', '') or '').strip()
                tag = f"[联网检索·{src_tag}] " if src_tag else "[联网检索] "
                cur = agg.get(c6)
                if cur is None:
                    agg[c6] = {'name': a['name'], 'code': c6, 'score': 6.0,
                               'sources': ['agent_search'],
                               'reason': f"{tag}{reason}", 'ai_reason': reason}
                else:
                    cur['score'] += 3.0   # 人工/Agent 检索确认 → 加权
                    cur['sources'] = sorted(set(cur['sources']) | {'agent_search'})
                    cur['ai_reason'] = reason
                    if reason not in cur['reason']:
                        cur['reason'] += f"；{tag}{reason}"
                n_add += 1
            print(f'  Agent 检索补充合并: {n_add} 条')
        except Exception as e:
            print(f'  ⚠️ Agent 检索文件解析失败: {str(e)[:60]}')
    elif args.merge_search:
        print(f'  ⚠️ 未找到 {os.path.basename(search_path)}（可先 --emit-template 生成模板）')

    # ---- 主力池对照（in_main_pool 标记 + 评级/板块富集）----
    pool_codes, pool_map = load_scored_map(HERE, date_str, args.scored)

    ranked = sorted(agg.values(), key=lambda x: -x['score'])[:args.top]
    for i, it in enumerate(ranked, 1):
        it['rank'] = i
        c6 = it['code']
        meta = pool_map.get(c6) or {}
        it['in_main_pool'] = c6 in pool_codes
        it['sector'] = meta.get('sector')
        it['rating'] = meta.get('rating')
        it['total'] = meta.get('total')
        it['price'] = meta.get('price')
        it['pct_chg'] = meta.get('pct_chg')
        if not it.get('name') and meta.get('name'):
            it['name'] = meta['name']
        it['code'] = market_prefix(c6) + c6  # 统一带前缀(与生产口径一致)

    n_in = sum(1 for x in ranked if x['in_main_pool'])
    out = {
        'date': date_str,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'title': '每日 AI 焦点池',
        'method': '机器源(东财人气榜/涨跌停池/强势股池) + 本地AI联网检索(题材催化/公告/政策/龙虎榜)',
        'generated_by': 'fetch_focus_list.py (互联网搜索: 东财人气榜/涨跌停池/强势股池 + Agent 检索)',
        'sources_used': sources_used,
        'n_raw': len(agg),
        'elapsed_s': round(time.time() - t0, 1),
        'agent_search': {'merged': bool(n_add), 'n_additions': n_add},
        'ai_summary': ai_summary,
        'n_in_pool': n_in,
        'n_out_pool': len(ranked) - n_in,
        'focus': ranked,
    }

    if args.list:
        print(f"\n📅 {date_str} 每日 AI 焦点池 {len(ranked)} 只"
              f"（机器源 {sources_used}；池内 {n_in} / 池外 {len(ranked)-n_in}）")
        for it in ranked:
            badge = f"[{it['rating']}]" if it.get('rating') else ("[池内]" if it['in_main_pool'] else "[池外]")
            print(f"  {it['rank']:>2}. {it['name']:<8}{it['code']}  {it['score']:>4.1f}分  "
                  f"{badge:<4} {'+'.join(it['sources']):<22} {it['reason'][:46]}")
        return 0

    json.dump(out, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n✅ 每日 AI 焦点池 → {os.path.basename(out_path)}"
          f"（{len(ranked)} 只，池内 {n_in} / 池外 {len(ranked)-n_in}，机器源 {sources_used}）")
    if not os.path.exists(search_path):
        print(f"💡 下一步：python3 fetch_focus_list.py --emit-template 生成 Agent 联网检索模板，"
              f"填写后 --merge-search 重跑即完成「AI 联网筛选」")
    return 0


if __name__ == '__main__':
    sys.exit(main())
