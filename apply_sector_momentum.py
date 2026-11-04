#!/usr/bin/env python3
"""将「板块60日动量 / 板块10日动量」并入评分模型 (stock-scorer)

⚠️ 权重不再写在本文件 —— 唯一来源是 factor_weights.json（当前 v4.2-lag-robust）：
    fund 0.32 | news 0.16 | theme_neg 0.12 | size 0.12 | sector 0.10 |
    反转块 4×0.045 | overbought/theme_intrinsic/attention 0
   合计 1.0。文件缺失或非法时回退下列 _W_PRIOR_10D 硬编码先验。
   本段此前写成 v3.0 的「基本面0.18 | 消息0.18 | …」静态清单，早已与代码脱钩，
   会误导维护者以为是权威口径 —— 改为指向单一来源，避免再次失同步。

total = Σ(score_i × w_i) / Σ(w_i | 该股可用) × 20   ← 百分制
  注意：分母按「该股实际可用维度」归一，缺维（如无 circ_mv 时的 score_size）
  从分母剔除，不按 0 计入 —— 按 0 会被误读为「市值最大」，等于凭空扣分。

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

# v4.10（2026-10-27 P0-4）：复合分量尺唯一实现来源（分母恒定 + 缺维中性填充）
import scale_calc

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
MAP_PATH = os.path.join(SECTOR_DIR, 'sw_map.json')

# v3.6-fund-led: 基本面主导先验（与 factor_weights.json weights 同源，回退时必须一致）
_W_PRIOR_10D = {"score_fund": 0.200000, "score_news": 0.000000, "score_theme_neg": 0.000000, "score_size": 0.000000, "score_sector": 0.000000, "score_quality": 0.100000, "score_growth": 0.100000, "score_tech_neg": 0.100000, "score_oversold60": 0.000000, "score_bias60": 0.050000, "score_rsi_rev": 0.050000, "score_vol_pulse": 0.000000, "score_overbought": 0.000000, "score_theme_intrinsic": 0.000000, "score_attention": 0.000000, "score_lowvol20": 0.000000, "score_ep": 0.100000, "score_size_ind": 0.000000, "score_size_growth": 0.200000, "score_dominant": 0.100000}  # v4.14-evidence-aligned：quality 0.212→0.12（实证 h20 IC −0.147 负），fund 0.12→0.242（消融 t=+4.41 唯一>4），# tech_neg 0.06→0.03（σ 贡献仅 2.1% 近乎常数）；Σ=1.0
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


def _load_dim_max():
    """读 factor_weights.json 的 dim_max（各维满分量程）；失败回退内置默认。

    ⚠️ 2026-10-26 P0：量程不统一（12 维 0-5 制 / 4 维 0-10 制）导致
    ① 天花板仅 67.5 分 → S≥85/A≥65 永不可达（490 只评级只有 B/C/D）；
    ② 名义权重被扭曲（tech_neg 15%→22.2%，反转块 35%→26%，fund 30%→22.2%）。
    汇总前必须按量程归一化到 0-10。回退默认值须与 json 的 dim_max 一致。
    """
    _DEFAULT = {
        "score_fund": 5.0, "score_news": 5.0, "score_theme_neg": 10.0,
        "score_size": 10.0, "score_sector": 5.0, "score_quality": 10.0,
        "score_growth": 10.0, "score_tech_neg": 10.0, "score_oversold60": 5.0,
        "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_vol_pulse": 5.0,
        "score_overbought": 5.0, "score_theme_intrinsic": 5.0, "score_attention": 5.0,
        # v4.8/v4.9 池内分位族（量程 0-10）
        "score_lowvol20": 10.0, "score_ep": 10.0, "score_size_ind": 10.0,
        "score_size_growth": 10.0, "score_dominant": 10.0,
    }
    try:
        path = os.path.join(HERE, "factor_weights.json")
        with open(path, encoding="utf-8") as f:
            dm = (json.load(f) or {}).get("dim_max") or {}
        out = dict(_DEFAULT)
        for k in _DEFAULT:
            v = dm.get(k)
            if isinstance(v, (int, float)) and v > 0:
                out[k] = float(v)
        return out
    except Exception:
        return dict(_DEFAULT)


_DIM_MAX = _load_dim_max()
# ⚠️ 2026-10-26：原写「十维权重」并把 18 个键**全列**（含 10 个权重 0 的维），
#   读者会以为模型仍在用 18 维。改为只列权重>0 的生效维 + 明示维数。
_eff_fw = {k: v for k, v in (_FW or {}).items() if isinstance(v, (int, float)) and v > 0}
print(f"⚖️ 生效 {len(_eff_fw)} 维权重（{SCORER_VER}）: "
      + " ".join(f"{k.replace('score_', '')}={v}" for k, v in _eff_fw.items()))

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
                # v4.2（P0 修复）：**缺失维必须从分母剔除，绝不能 `or 0` 按 0 计入**。
                #   原实现写成 `float(r.get('score_theme_neg') or 0)`，
                #   于是任何缺该字段的行（所有 10-24 之前的历史 scored、以及个别
                #   上游没算出来的行）都会被当成「theme_neg = 0 分」参与加权，
                #   相当于凭空扣掉 0.12 权重：实测同一条记录 enhance 算 90.08、
                #   apply 算 77.80，差 12.28 分 —— apply 在最后一步覆盖 total，
                #   等于把正确分数改错，且 diff 静默不报错。
                #   与 step2 / enhance 的「逐行按可用维度归一」口径必须完全一致。
                # v4.4（round17 同类陷阱修复）：原实现是**硬编码元组列表**，
                #   factor_weights.json 新增维度（v4.1 的 theme_neg/size、v4.4 的
                #   quality/growth/tech_neg）不会自动进入 total → 权重静默不落地。
                #   改为与 enhance_scores._calc_total_10d 完全同口径的通用循环：
                #   遍历权重表全部键，缺维从分母剔除（isinstance 判断，禁 or 0）。
                # v2026-10-26（P0 量程修复）：汇总前按 dim_max 归一化到 0-10。
                #   量程不统一使天花板仅 67.5（S≥85/A≥65 永不可达）且名义权重
                #   被扭曲（tech_neg 15%→22.2%，反转块 35%→26%）。量程表唯一来源
                #   factor_weights.json 的 dim_max。
                # v2026-10-27（P0-4 量尺统一）：分母恒定为 Σ全部生效权重，
                #   缺维按中性分 DMAX/2 填充（详见 scale_calc.py 模块头）。
                #   修复前分母 = 该股可用维权重之和 → 缺维越多分数被抬得越高
                #   （实测放大 1.00~1.33 倍，最高虚高 16.33 分）。
                #   ⚠️ 本步包含 score_sector（板块动量），其权重也计入统一分母。
                _parts = []
                for _k, _wt in _FW.items():
                    if _wt <= 0:
                        continue
                    if _k == "score_sector":
                        continue          # 板块动量本步才有，下面单独加
                    _parts.append((_k, r.get(_k), _wt))
                # 板块动量本步才有，恒可用（同样按量程归一）
                _parts.append(("score_sector", score_sector, _FW["score_sector"]))
                _miss_s = []
                total, _ms = scale_calc.composite_total(
                    _parts, _DIM_MAX,
                    weights={k: v for k, v in _FW.items() if v > 0},
                    missing_out=_miss_s)
                # 逐只留痕「哪些维是中性填充」，供页面披露（禁让读者误当实测值）
                r["neutral_filled"] = list(_ms)
                r["composite_scale"] = "uniform_v1"
            else:
                # 旧五维回退路径（历史 scored 无 score_oversold60 字段）。
                # ⚠️ 2026-10-26 P0：原实现用原值加权再 ×10，同样受量程不统一影响
                #   （score_news/score_tech/score_fund/score_theme/score_sector 全是
                #   0-5 制）→ 天花板仅 50。现改为按各自量程归一化到 0-10。
                # ⚠️ 2026-10-27 P0-4：同样统一量尺 —— 分母 = 映射后「全部」权重和，
                #   缺维按中性分填充。此刻度与十维分支不可比（维度集不同），
                #   但内部一致：同批旧文件之间不再因缺维多少而尺子不同。
                # 旧五维把 score_tech 拆给三个反转子因子、score_theme 拆给内禀+关注度，
                #   这里按映射后的合并权重汇总，故 parts 直接给合并权重。
                _p5 = [
                    ("score_news", r.get('score_news'), _FW["score_news"]),
                    ("score_sector", score_sector, _FW["score_sector"]),
                    ("score_rsi_rev", r.get('score_tech'),
                     _FW["score_rsi_rev"] + _FW["score_vol_pulse"] + _FW["score_overbought"]),
                    ("score_theme_intrinsic", r.get('score_theme'),
                     _FW["score_theme_intrinsic"] + _FW["score_attention"]),
                    # ⚠️ fund 可能为 None（step2 兜底改造后），走中性填充而非 `or 0`
                    ("score_fund", r.get('score_fund'), _FW["score_fund"]),
                ]
                _miss5 = []
                total, _m5 = scale_calc.composite_total(
                    _p5, _DIM_MAX, missing_out=_miss5)
                r["neutral_filled"] = list(_m5)
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
    # v3.5: prior-pending 披露判定改为以 adopted 为准（原先依赖 version.startswith("v3.0")，
    #   版本号一旦升级会静默关闭诚实披露——违反口径诚实纪律）。adopted=prior_pending_10dim/
    #   prior/prior_weak 均属「先验」，只有 ic_driven 才是样本外验证通过。
    _adopted = _meta.get("adopted", "")
    _ver_suffix = ", IC-driven weights" if _adopted == "ic_driven" else (
        ", prior-pending (全市场回测先验, 池内 IC 积累中)"
        if _adopted.startswith("prior") or _meta.get("version", "").startswith("v3.0") else "")
    # ⚠️ 2026-10-26：原为硬编码 "(10-dim ...)" —— v4.8 起生效维已变（7 维→8 维），
    #   写死的维数会静默过期并在页面标题旁展示错信息。改为按权重配置实时计算。
    _n_eff = sum(1 for _k, _v in (_FW or {}).items() if isinstance(_v, (int, float)) and _v > 0)
    data['model'] = f"stock-scorer {SCORER_VER} ({_n_eff}-dim + AI + sector-momentum[{args.mode}]{_ver_suffix})"
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
