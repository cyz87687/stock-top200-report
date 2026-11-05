#!/usr/bin/env python3
"""quality_normalize.py —— 质量/成长子因子量纲修复的**候选内核**（默认不启用）。

round130 / P1-4
---------------
审计 §二：`fund_split.family_scores()` 直接对**原始值**取组内均值：

    Q_GROUPS 里 roe/roic/roa 是百分数，ocf_ni 是倍数，ocfg 是同比百分数，
    accruals 是百分数，profit_vol / gm_vol 是负标准差，debt/gw 是负百分数。

`ocfg` 动辄 ±100~1000 的量纲会淹没同组其他成员 → 质量分实际由现金流同比
主导（实测与 ~ROE 秩相关仅 0.26、与 OCFG 0.03 的结构性证据）。

修法：逐子因子先做**截面稳健标准化**，再组内等权，最后合成三组。
本模块提供两种预先声明的连续变换（禁止事后按头部名单挑阈值）：

  * `rank_z`     : 截面 rank → 正态分位（ndtri），clip 到 ±3，缺失记中性
  * `winsor_iqr` : 1/99 分位缩尾 → (x−median)/(IQR/1.349)，clip 到 ±3

⚠️ 为什么不直接改 `fund_split.score_rows()`
   P0-1 要求**冻结当前模型**；改量纲会改变 score_quality → 改变 total →
   改变排名与页面数字。因此本模块是**并行的候选内核**，生产链一行不动：
   `score_rows_normalized()` 供预注册/消融评估使用，只有经过预注册验收并
   显式裁决后才允许切换默认实现。禁止在看到头部名单后调参。
"""
import math

from fund_split import (G_GROUPS, G_W, Q_GROUPS, QUAL_SUBS, GROW_SUBS,
                        _is_num, _pit_period_actual, load_em_cache,
                        pit_period, rank_pct, sub_factors_pit)

VALID_METHODS = ("rank_z", "winsor_iqr")
CLIP = 3.0


def _ndtri(p):
    """标准正态分位（Acklam 近似），避免为单一函数引入 scipy 依赖。"""
    p = min(max(float(p), 1e-12), 1.0 - 1e-12)
    # 有理逼近系数（Peter Acklam）
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425

    def _tail(q):
        num = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
               + c[5])
        den = ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        return num / den

    if p < plow:
        return _tail(math.sqrt(-2 * math.log(p)))
    if p > phigh:
        return -_tail(math.sqrt(-2 * math.log(1 - p)))
    q = p - 0.5
    r = q * q
    num = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r
           + a[5]) * q
    den = (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    return num / den


def _rankdata(vals):
    """平均秩（0-1 分位），并列取平均；仅对有效值调用。"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        p = ((i + j) / 2.0) / max(1, n - 1)
        # 拉开到 (0,1)，避免 ndtri(0)/ndtri(1) 发散
        p = min(max(p, 1.0 / (2 * n)), 1.0 - 1.0 / (2 * n))
        for k in range(i, j + 1):
            out[order[k]] = p
        i = j + 1
    return out


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def standardize_column(values, method="rank_z"):
    """单子因子截面稳健标准化 → [(z, ok), ...]，缺失 ok=False / z=0。"""
    if method not in VALID_METHODS:
        raise ValueError(f"method 必须是 {VALID_METHODS}，收到 {method!r}")
    good = [(i, float(v)) for i, v in enumerate(values) if _is_num(v)]
    out = [[0.0, False] for _ in values]
    if len(good) < 2:
        return [tuple(x) for x in out]
    idx = [i for i, _ in good]
    vs = [v for _, v in good]
    if method == "rank_z":
        pcts = _rankdata(vs)
        zs = [max(-CLIP, min(CLIP, _ndtri(p))) for p in pcts]
    else:
        sv = sorted(vs)
        lo, hi = _quantile(sv, 0.01), _quantile(sv, 0.99)
        med = _quantile(sv, 0.50)
        iqr = _quantile(sv, 0.75) - _quantile(sv, 0.25)
        scale = (iqr / 1.349) or 1.0
        zs = [max(-CLIP, min(CLIP, (max(lo, min(hi, v)) - med) / scale))
              for v in vs]
    for k, (i, z) in enumerate(zip(idx, zs)):
        out[i] = [z, True]
    return [tuple(x) for x in out]


def _fam(Z, i, groups, ws=None):
    num = den = 0.0
    for g, members in groups.items():
        vals = [Z[m][i][0] for m in members if Z[m][i][1]]
        if not vals:
            continue
        w = (ws or {}).get(g, 1.0)
        num += w * (sum(vals) / len(vals))
        den += w
    return num / den if den > 0 else None


def family_scores_matrix(subs_list, method="rank_z"):
    """批量版：逐子因子截面标准化 → 组内等权 → 族原始分。

    subs_list: [dict] 每行一个子因子字典；返回 (q_raw, g_raw)。
    """
    names = list(dict.fromkeys(list(QUAL_SUBS) + list(GROW_SUBS)))
    Z = {m: standardize_column([s.get(m) for s in subs_list], method)
         for m in names}
    q = [_fam(Z, i, Q_GROUPS) for i in range(len(subs_list))]
    g = [_fam(Z, i, G_GROUPS, G_W) for i in range(len(subs_list))]
    return q, g


def score_rows_normalized(rows, as_of, em=None, method="rank_z",
                          pit_mode="auto", min_present=5):
    """与 fund_split.score_rows 同接口，但用标准化候选内核。

    仅写 `score_quality` / `score_growth` 到传入的 rows（**就地**），
    返回 (n_q, n_g)。生产链不调用本函数。
    """
    if method not in VALID_METHODS:
        raise ValueError(f"method 必须是 {VALID_METHODS}")
    if em is None:
        em = load_em_cache()
    subs_q, subs_g, idx_map = [], [], []
    for r in rows:
        code = (r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
        ind = em.get(code)
        sq = sg = {}
        if ind:
            periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = pit_period(periods, as_of)
            P_g = (_pit_period_actual(periods, as_of) or P
                   if pit_mode == "actual_ok" else P)
            if P:
                sq = sub_factors_pit(ind, P)
            if P_g:
                sg = sub_factors_pit(ind, P_g)
        subs_q.append(sq)
        subs_g.append(sg)
        idx_map.append(len(idx_map))
    # 质量与成长用不同报告期 → 分别做截面标准化
    Zq = {m: standardize_column([s.get(m) for s in subs_q], method)
          for m in QUAL_SUBS}
    Zg = {m: standardize_column([s.get(m) for s in subs_g], method)
          for m in GROW_SUBS}
    q_raw = [_fam(Zq, i, Q_GROUPS) for i in range(len(rows))]
    g_raw = [_fam(Zg, i, G_GROUPS, G_W) for i in range(len(rows))]
    for raw, field in ((q_raw, "score_quality"), (g_raw, "score_growth")):
        present = [i for i, v in enumerate(raw) if _is_num(v)]
        if len(present) < min_present:
            continue
        rp = rank_pct([raw[i] for i in present])
        for k, i in enumerate(present):
            rows[idx_map[i]][field] = round(rp[k] * 10.0, 2)
    return (sum(1 for v in q_raw if _is_num(v)),
            sum(1 for v in g_raw if _is_num(v)))


if __name__ == "__main__":
    # 自检：一个量纲极大的子因子不得主导组内均值；所有输出必须被 clip 住
    _v = [1.0, 2.0, 3.0, 1000.0]
    for _m in VALID_METHODS:
        _z = standardize_column(_v, _m)
        assert all(abs(x[0]) <= CLIP and x[1] for x in _z), (_m, _z)
        print(f"{_m:12s}", [round(x[0], 3) for x in _z])
    _raw = sum(_v) / len(_v)
    _zm = sum(x[0] for x in standardize_column(_v, "rank_z")) / len(_v)
    print(f"原始均值 {_raw:.2f}（被 1000 主导） → rank_z 均值 {_zm:.3f}")
