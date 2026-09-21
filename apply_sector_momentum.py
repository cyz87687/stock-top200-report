#!/usr/bin/env python3
"""将「板块60日动量 / 板块10日动量」并入评分模型 (stock-scorer v2.40)

新增第五维 score_sector (0-5)，权重从题材维度切出：
  消息 0.20 | 技术 0.20 | 基本面 0.30 | 题材 0.18 | 板块动量 0.12   (合计 1.0)
  total = (news*0.20 + tech*0.20 + fund*0.30 + theme*0.18 + sector*0.12) * 4

两种赋分方向(由 --mode 决定, 默认 evidence)：
  momentum : 用户原始思路 —— 板块动量排名越靠前得分越高(R60=1 → 5.0)
  evidence : 回测证据 —— 申万二级板块 2021-2026 回测显示 R60 与后续 20 日超额
             呈完美负单调(R60=1 为 -1.82%, R60>70 为 +0.20%), 故动量末段得分更高

个股同时保留两个方向的分值(sector_mom_score / sector_rev_score)以便对照，
实际并入总分的为 score_sector。

用法:
  python3 apply_sector_momentum.py [scored_json] [--momentum-file sector/sector_momentum_latest.json]
                                   [--mode momentum|evidence] [--keep-weights] [--dry-run]
注意: 默认原地覆盖 scored_json(先备份 .bak_sector)
"""
import os
import sys
import json
import glob
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
MAP_PATH = os.path.join(SECTOR_DIR, 'sw_map.json')

W_NEWS, W_TECH, W_FUND = 0.20, 0.20, 0.30
W_THEME_NEW, W_SECTOR = 0.18, 0.12
W_THEME_OLD = 0.30

# 分档: (排名下界, 排名上界, 分值)
BANKS_MOM_60 = [(1, 1, 5.0), (2, 3, 4.2), (4, 5, 3.5), (6, 10, 2.8), (11, 20, 2.0), (21, 40, 1.3), (41, 70, 0.7), (71, 10 ** 6, 0.3)]
BANKS_MOM_10 = [(1, 3, 5.0), (4, 5, 4.2), (6, 10, 3.5), (11, 20, 2.6), (21, 40, 1.8), (41, 70, 1.0), (71, 10 ** 6, 0.5)]
# 证据方向: 按实测 h20 超额映射(动量越强 → 分越低)
BANKS_REV_60 = [(1, 1, 0.3), (2, 3, 0.8), (4, 5, 0.4), (6, 10, 1.0), (11, 20, 1.6), (21, 40, 2.4), (41, 70, 3.0), (71, 10 ** 6, 3.6)]
BANKS_REV_10 = [(1, 3, 0.3), (4, 5, 0.6), (6, 10, 1.0), (11, 20, 1.6), (21, 40, 2.4), (41, 70, 3.0), (71, 10 ** 6, 2.8)]

W_LONG, W_SHORT = 0.6, 0.4     # 60日动量为主、10日动量为辅(与用户规则一致)


def bank_score(rank, banks):
    if rank is None:
        return None
    for lo, hi, v in banks:
        if lo <= rank <= hi:
            return v
    return banks[-1][2]


def code6(code):
    s = ''.join(ch for ch in str(code) if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scored', nargs='?', default=None)
    ap.add_argument('--momentum-file', default=os.path.join(SECTOR_DIR, 'sector_momentum_latest.json'))
    ap.add_argument('--mode', default='evidence', choices=['momentum', 'evidence'])
    ap.add_argument('--keep-weights', action='store_true', help='保持原有四维权重(题材0.30), 仅写入板块字段不并入总分')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    scored = args.scored
    if scored is None:
        fs = sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json')), reverse=True)
        scored = fs[0] if fs else None
    if not scored or not os.path.exists(scored):
        print("❌ 未找到 scored json")
        return 1
    if not os.path.exists(args.momentum_file):
        print(f"❌ 未找到板块动量文件 {args.momentum_file}，请先运行 sector_momentum_daily.py")
        return 1

    mom = json.load(open(args.momentum_file, encoding='utf-8'))
    sec_info = {s['sector']: s for s in mom['sectors']}
    mom_date = mom.get('date')
    sw = json.load(open(MAP_PATH, encoding='utf-8')) if os.path.exists(MAP_PATH) else {}
    code2l2 = sw.get('l2', {})
    l2_parent = sw.get('l2_parent', {})

    data = json.load(open(scored, encoding='utf-8'))
    results = data.get('results', [])
    print(f"scored: {os.path.basename(scored)} | 板块动量日期: {mom_date} | 板块数: {len(sec_info)} | mode: {args.mode}")
    print(f"权重: 消息{W_NEWS} 技术{W_TECH} 基本面{W_FUND} 题材{W_THEME_OLD if args.keep_weights else W_THEME_NEW} 板块动量{0.0 if args.keep_weights else W_SECTOR}"
          f"{' (keep-weights: 板块分不并入总分)' if args.keep_weights else ''}")

    hit = miss = 0
    for r in results:
        c6 = code6(r.get('code'))
        sec = code2l2.get(c6) if c6 else None
        info = sec_info.get(sec) if sec else None
        if info:
            hit += 1
            r60, r10 = info['r60'], info['r10']
            m60_score = bank_score(r60, BANKS_MOM_60)
            m10_score = bank_score(r10, BANKS_MOM_10)
            v60_score = bank_score(r60, BANKS_REV_60)
            v10_score = bank_score(r10, BANKS_REV_10)
            r['sector_l2'] = sec
            r['sector_parent'] = l2_parent.get(sec, info.get('parent', ''))
            r['sector_mom60'] = info['mom60']
            r['sector_mom10'] = info['mom10']
            r['sector_r60'] = r60
            r['sector_r10'] = r10
            r['sector_mom_score'] = round(W_LONG * m60_score + W_SHORT * m10_score, 2)
            r['sector_rev_score'] = round(W_LONG * v60_score + W_SHORT * v10_score, 2)
            score_sector = r['sector_mom_score'] if args.mode == 'momentum' else r['sector_rev_score']
        else:
            miss += 1
            r['sector_l2'] = sec or r.get('sector')
            r['sector_parent'] = ''
            r['sector_mom60'] = r['sector_mom10'] = None
            r['sector_r60'] = r['sector_r10'] = None
            r['sector_mom_score'] = r['sector_rev_score'] = 2.5   # 中性
            score_sector = 2.5
        r['score_sector'] = round(score_sector, 2)

        if not args.keep_weights:
            total = round((float(r.get('score_news') or 0) * W_NEWS
                           + float(r.get('score_tech') or 0) * W_TECH
                           + float(r.get('score_fund') or 0) * W_FUND
                           + float(r.get('score_theme') or 0) * W_THEME_NEW
                           + score_sector * W_SECTOR) * 4, 2)
            r['total'] = total
            if total >= 17:
                rating, advice = "S", "优先买入，可重仓"
            elif total >= 13:
                rating, advice = "A", "逢低加仓，重点关注"
            elif total >= 9:
                rating, advice = "B", "波段操作，轻仓参与"
            elif total >= 5:
                rating, advice = "C", "观望，不新开仓"
            else:
                rating, advice = "D", "坚决回避，立即卖出"
            r['rating'] = rating
            r['advice'] = advice

    results.sort(key=lambda x: (-x['total'], -(x.get('turnover') or 0)))
    from collections import Counter
    rc = Counter(r['rating'] for r in results)
    data['results'] = results
    data['model'] = f"stock-scorer v2.40 (AI + sector-momentum[{args.mode}])"
    data['sector_momentum'] = {'date': mom_date, 'mode': args.mode,
                               'mom_long_days': mom.get('mom_long_days', 60),
                               'mom_short_days': mom.get('mom_short_days', 10),
                               'n_sectors': len(sec_info)}
    data['weights'] = {'news': W_NEWS, 'tech': W_TECH, 'fund': W_FUND,
                       'theme': W_THEME_OLD if args.keep_weights else W_THEME_NEW,
                       'sector': 0.0 if args.keep_weights else W_SECTOR}
    data['stats']['rating_dist'] = {k: rc.get(k, 0) for k in "SABCD"}
    # 板块维度排名(供报告展示)
    sec_scores = {}
    for r in results:
        s = r.get('sector_l2') or '未分类'
        sec_scores.setdefault(s, []).append(r['total'])
    data['stats']['sector_l2_avg'] = {s: {'count': len(v), 'avg': round(sum(v) / len(v), 1)}
                                      for s, v in sorted(sec_scores.items(), key=lambda x: -sum(x[1]) / len(x[1]))}

    print(f"板块映射命中: {hit}/{len(results)} (未匹配 {miss})")
    print(f"评级分布: {dict(rc)}")
    print("Top8: " + " / ".join(f"{r['name']}({r['total']:.1f},{r['rating']},{r.get('sector_l2','-')})" for r in results[:8]))

    if args.dry_run:
        print("(--dry-run 未写盘)")
        return 0
    bak = scored + '.bak_sector'
    shutil.copy2(scored, bak)
    with open(scored, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 已写入 {scored} (备份 {os.path.basename(bak)})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
