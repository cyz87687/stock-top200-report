#!/usr/bin/env python3
"""将「板块60日动量 / 板块10日动量」并入评分模型 (stock-scorer v3.0-10dim)

v3.0-10dim（2026-10-13，依据《因子排名与10维评分模型设计_2026-10-13》）：
十维总分（权重来自 factor_weights.json，缺失/非法回退十维先验）：
  基本面0.18 | 消息0.18 | 板块动量0.10 | 超跌60 0.12 | 乖离低位0.10 |
  RSI反转0.06 | 量能反向0.10 | 超买反向0.06 | 题材内禀0.05 | 关注度0.05  (合计1.0)
  total = Σ(score_i × w_i) × 4
技术面/题材旧维度已拆解：均线/MACD/动量退出评分（三重负证据，仅 tech detail 展示）；
题材拆为内禀/关注度两维，板块动量子项归并 score_sector。

两种板块动量赋分方向(由 --mode 决定, 默认 evidence)：
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

# v3.0-10dim: 十维先验（全市场回测证据的先验映射；fund/news/sector 为池内 IC 已验证，
#   新拆 5 维 + 2 先验维标注 prior_pending，满 30+ 截面后 --optimize-weights 重拟合）
_W_PRIOR_10D = {
    "score_fund": 0.18, "score_news": 0.18, "score_sector": 0.10,
    "score_oversold60": 0.12, "score_bias60": 0.10, "score_rsi_rev": 0.06,
    "score_vol_pulse": 0.10, "score_overbought": 0.06,
    "score_theme_intrinsic": 0.05, "score_attention": 0.05,
}
# 旧五维 → 十维映射（兼容历史 scored json 重跑）
_W_MAP_FROM_5D = {
    "score_fund": [("score_fund", 1.0)],
    "score_news": [("score_news", 1.0)],
    "score_sector": [("score_sector", 1.0)],
    "score_tech": [("score_rsi_rev", 0.06 / 0.22), ("score_vol_pulse", 0.10 / 0.22),
                   ("score_overbought", 0.06 / 0.22)],
    "score_theme": [("score_theme_intrinsic", 0.5), ("score_attention", 0.5)],
}


def _load_factor_weights():
    path = os.path.join(HERE, "factor_weights.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        w = d.get("weights") or {}
        if all(k in w for k in _W_PRIOR_10D):            # 已是十维
            vals = {k: float(w[k]) for k in _W_PRIOR_10D}
        elif all(k in w for k in ("score_news", "score_tech", "score_fund",
                                  "score_theme", "score_sector")):  # 旧五维 → 映射
            vals = {k: 0.0 for k in _W_PRIOR_10D}
            for old, v in w.items():
                for new, s in _W_MAP_FROM_5D.get(old, []):
                    vals[new] += float(v) * s
            d = dict(d or {})
            d["_migrated_from_5d"] = True
        else:
            raise ValueError("weights 字段不全")
        if any(v < 0 for v in vals.values()) or abs(sum(vals.values()) - 1.0) > 0.02:
            raise ValueError("权重非法: %s" % vals)
        return vals, d
    except Exception:
        return dict(_W_PRIOR_10D), None


_FW, _FW_META = _load_factor_weights()
SCORER_VER = _FW_META.get("version", "v3.0-10dim") if _FW_META else "v3.0-10dim"
print(f"⚖️ 十维权重（{SCORER_VER}）: " + " ".join(f"{k.replace('score_', '')}={v}" for k, v in _FW.items()))

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
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json')), reverse=True)
              if '_watch_' not in f and '.bak' not in f]
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
    # v3.2 fix: 原打印引用十维重构时已删除的旧变量(W_NEWS/W_TECH/W_FUND/W_THEME_OLD/W_THEME_NEW/W_SECTOR)
    #   → 运行即 NameError（py_compile 查不出名字问题）。改为从 _FW 十维权重推导展示。
    if args.keep_weights:
        _wdisp = "keep-weights: 十维分不并入总分（仅写入板块字段）"
    else:
        _wdisp = " ".join(f"{k.replace('score_','')}={_FW[k]}" for k in _FW)
    print(f"权重: {_wdisp}")

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
            # v3.0-10dim: 十维总分（新字段缺失的历史 scored json 按旧五维公式回退）
            _has_10d = any('score_oversold60' in r for r in results)
            if _has_10d:
                total = round((float(r.get('score_fund') or 0) * _FW["score_fund"]
                               + float(r.get('score_news') or 0) * _FW["score_news"]
                               + float(r.get('score_oversold60') or 0) * _FW["score_oversold60"]
                               + float(r.get('score_bias60') or 0) * _FW["score_bias60"]
                               + float(r.get('score_rsi_rev') or 0) * _FW["score_rsi_rev"]
                               + float(r.get('score_vol_pulse') or 0) * _FW["score_vol_pulse"]
                               + float(r.get('score_overbought') or 0) * _FW["score_overbought"]
                               + float(r.get('score_theme_intrinsic') or 0) * _FW["score_theme_intrinsic"]
                               + float(r.get('score_attention') or 0) * _FW["score_attention"]
                               + score_sector * _FW["score_sector"]) * 20, 2)  # v3.3 问题2: ×20 (每维满分10/总分满分100)
            else:
                total = round((float(r.get('score_news') or 0) * _FW["score_news"]
                               + float(r.get('score_tech') or 0) * (_FW["score_rsi_rev"] + _FW["score_vol_pulse"] + _FW["score_overbought"])
                               + float(r.get('score_fund') or 0) * _FW["score_fund"]
                               + float(r.get('score_theme') or 0) * (_FW["score_theme_intrinsic"] + _FW["score_attention"])
                               + score_sector * _FW["score_sector"]) * 20, 2)
            r['total'] = total
            if total >= 85:
                rating, advice = "S", "优先买入，可重仓"
            elif total >= 65:
                rating, advice = "A", "逢低加仓，重点关注"
            elif total >= 45:
                rating, advice = "B", "波段操作，轻仓参与"
            elif total >= 25:
                rating, advice = "C", "观望，不新开仓"
            else:
                rating, advice = "D", "坚决回避，立即卖出"
            r['rating'] = rating
            r['advice'] = advice

    results.sort(key=lambda x: (-x['total'], -(x.get('turnover') or 0)))
    from collections import Counter
    rc = Counter(r['rating'] for r in results)
    data['results'] = results
    _meta = _FW_META or {}
    _ver_suffix = ", IC-driven weights" if _meta.get("adopted") == "ic_driven" else (
        ", prior-pending (全市场回测先验, 池内 IC 积累中)" if _meta.get("version", "").startswith("v3.0") else "")
    data['model'] = f"stock-scorer {SCORER_VER} (10-dim + AI + sector-momentum[{args.mode}]{_ver_suffix})"
    data['sector_momentum'] = {'date': mom_date, 'mode': args.mode,
                               'mom_long_days': mom.get('mom_long_days', 60),
                               'mom_short_days': mom.get('mom_short_days', 10),
                               'n_sectors': len(sec_info)}
    # v3.2 fix: 同上——原引用已删除的旧五维变量，改为十维权重映射输出
    if args.keep_weights:
        data['weights'] = dict(_FW)
        data['weights_note'] = 'keep-weights: 板块分未并入总分'
    else:
        data['weights'] = dict(_FW)
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
