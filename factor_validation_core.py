#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_validation_core.py —— 8 因子有效性验证流程的共享内核

═══ 流程规范（老大 2026-10-29 下达，固定不变）═══

前置锁定：
  · 股票池：现有已筛选后的 2029 只样本（不重建池）
  · 调仓：月度截面（每 20 个交易日 ≈ 1 个月）
  · 基准：沪深300
  · 样本内（校验区间）：因子筛选、相关性检查、权重求解
  · 样本外（盲测区间）：参数冻结，只跑一次，不做任何修改
Step1 单因子五分分层校验 → 准入三档
Step2 Spearman 相关性筛查
Step3 增量信息校验
Step4 带约束权重求解（最大化组合 ICIR）
Step5 样本外盲测验收
Step6 牛/震荡/熊子区间稳健性

═══ 关键口径声明（必须与数字一起引用）═══

L1 **"月度截面"= 每 20 个交易日一个截面**（A股约 21 个交易日/月）。
    面板 238 个交易日 → 约 11 个月 → 样本内约 7 个月 / 样本外约 4 个月。
    ⚠️ 月度 IC 序列的样本量只有 11 个点，**t 值的统计功效极低**。
    为此本内核**同时输出日频（每日截面）版本**作为高功效参考，
    并明确标注两者口径差异。日频版用于因子筛选的稳健性判断，
    月度版用于与规范对齐的验收。

L2 **基准 = 沪深300（按规范锁定）**。
    ⚠️ 已实测：本池（2029 只，由 2026-10-27 的成交额/市值回溯筛出）
    同期池等权 +29.34% vs HS300 −11.46%，差 **40.8pp**（前视选择偏误）。
    故「对 HS300 的超额」含大量非因子成分。**按规范仍以 HS300 为基准**，
    但同时输出对池等权（POOL_EW）的对照，并把这个差单列为观察项。
    这不影响 Step1~4（那些只用 IC，不涉及基准），只影响 Step5 的超额口径解读。

L3 **Bonferroni 阈值 3.90**（项目累计 304+ 次检验）。
L4 **FDR**：Benjamini-Hochberg 过程，q=0.10。
L5 **单因子 IC 用截面 Spearman（RankIC）**，与项目既有口径一致。
L6 **组合 ICIR = mean(IC_c) / sd(IC_c)**，IC_c 为组合打分与未来收益的截面秩相关。

用法：被 audit_round92~96 各步骤脚本 import。
"""
import os
import sys
import json
import math
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ── round124 P0-2：DIMS 改为从 factor_weights.json 动态解析 ──────────────
# ⚠️ 历史血案（本轮实测）：原 `DIMS` 是**硬编码 9 维旧表**，不含 score_growth。
#    而 `load_panel_arrays()` 用它作矩阵 K 轴（见本文件 L~194 `nK = len(DIMS)`、
#    L~214 `for k, dim in enumerate(DIMS)`）→ 即使面板里 score_growth 列
#    **238/238 满覆盖、行级 100%**，也会被读取层**静默跳过**。
#    实测：`load_panel_arrays('panel_round123.json', ...)` 返回
#          `X.shape == (223, 2029, 9)` —— K=9 而非 10。
#    后果：那 0.10 权重**从未被检验**，而不是「检验了没过」。
#    两者性质完全不同 —— 前者是测量仪器坏了，后者是测量结果不好。
#
# 处置：以 factor_weights.json 里 **权重>0** 的维度为准（单一真源），
#       硬编码表仅作权重文件不可读时的 fallback，且必须打印显式警告。
_LEGACY_DIMS = [
    "score_fund", "score_quality", "score_ep", "score_lowvol20",
    "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_size_growth", "score_dominant",
]


def _resolve_dims():
    """从 factor_weights.json 解析生效维度（权重 >0），按权重大→小排序。

    返回 (dims, src_note)。不可读时回退 _LEGACY_DIMS 并标记 fallback。
    """
    path = os.path.join(HERE, "factor_weights.json")
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
        raw = blob.get("weights") if isinstance(blob, dict) else None
        if not isinstance(raw, dict) or not raw:
            raise ValueError("无 weights 字段")
        pos = {k: float(v) for k, v in raw.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)
               and float(v) > 0}
        if not pos:
            raise ValueError("全部权重 ≤0")
        dims = [k for k, _ in sorted(pos.items(), key=lambda kv: (-kv[1], kv[0]))]
        return dims, f"factor_weights.json（{len(dims)} 维，权重>0）"
    except Exception as e:
        print(f"⚠️ factor_validation_core: 权重文件不可读（{e}）→ "
              f"回退硬编码 {len(_LEGACY_DIMS)} 维旧表，"
              f"**本次评估不得用于声明『某维度已被验证』**", flush=True)
        return list(_LEGACY_DIMS), f"fallback _LEGACY_DIMS（{e}）"


DIMS, DIMS_SRC = _resolve_dims()

GROUPS = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
    "成长": ["score_growth"],
}
# ── round118 P0-4 / P1-2：Bonferroni 阈值改为从 factor_eval 单一来源取 ────
# 原来是手写 3.90，但 3.90 实际对应 K≈519.8。现在 K 由 trial_log 提供
# （legacy 304 + 已登记试验轮次 45 = 349），阈值从 K 现算 = 3.8025。
# ⚠️ K 的真实区间是 349~656（45 个试验轮次里仅 17 个能提取精确检验数），
#    所以该阈值是「可证下界」口径。实测 K 从 304→656 不翻转任何已发布结论。
# ⚠️ 本模块被 audit_round92/94 等引用；从 factor_eval 导入不引入副作用（实测 0.06s）。
try:
    from factor_eval import (T_BONF, T_BONF_SINGLE_ROUND,      # noqa: F401
                             T_BONF_LEGACY, N_TESTS_LIVE, K_SOURCE)
except Exception:                                                 # pragma: no cover
    T_BONF = 3.8025        # 与 trial_log K=349 口径一致（非 3.90，见上方注释）
    T_BONF_SINGLE_ROUND = 3.144
    T_BONF_LEGACY = 3.7681
    N_TESTS_LIVE = 349
    K_SOURCE = "legacy-fallback"
FDR_Q = 0.10
W_CAP = 0.20
G_CAP = 0.30
MONTHLY_HOP = 20          # 月度截面：每 20 个交易日


# ═══════════════ 基础统计 ═══════════════

def nw_t(xs, lag=4):
    """Newey-West t 统计（与项目既有口径一致，lag=4）。"""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        wgt = 1.0 - L / (lag + 1.0)
        cov = sum(d[t] * d[t - L] for t in range(L, n)) / n
        var += 2.0 * wgt * cov
    if var <= 0:
        return None
    return mu / math.sqrt(var / n)


def stat(ics):
    """IC 序列统计：均值/ICIR/胜率/t/标准误。"""
    n = len(ics)
    if n < 3:
        return {"n": n, "ic": None, "icir": None, "t": None,
                "pos_rate": None, "sd": None, "se": None}
    mu = sum(ics) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in ics) / (n - 1))
    return {"n": n, "ic": mu, "sd": sd,
            "icir": (mu / sd if sd > 1e-12 else None),
            "t": nw_t(ics),
            "se": (sd / math.sqrt(n) if n > 0 else None),
            "pos_rate": sum(1 for x in ics if x > 0) / n}


def bh_fdr(pvals, q=FDR_Q):
    """Benjamini-Hochberg FDR：返回 (是否显著, 校正后 q 值)。

    pvals: [(name, p, t)]，p 为双侧 t 检验 p 值。
    ⚠️ p 或 t 为 None（因子在该区间无有效数据）时，必须跳过而非崩溃
    （本项目踩：fund 在样本内部分截面无值 → t=None）。
    """
    items = []
    for nm, p, t in pvals:
        if p is None or not (p == p):
            continue
        items.append((nm, float(p), abs(t) if t is not None else 0.0))
    items.sort(key=lambda kv: kv[1])
    m = len(items)
    out = {}
    prev = 1.0
    for i in range(m - 1, -1, -1):
        nm, p, t = items[i]
        qv = min(prev, p * m / (i + 1))
        prev = qv
        out[nm] = {"p": p, "q": qv, "fdr_sig": bool(qv <= q), "t": t}
    # 未参与检验的因子也要有记录（显式标记）
    for nm, p, t in pvals:
        if nm not in out:
            out[nm] = {"p": None, "q": None, "fdr_sig": False,
                       "t": None, "note": "无有效 p 值（该区间无数据）"}
    return out


def two_sided_p(t, n):
    """双侧 p 值（正态近似，t 分布自由度 n-1 足够时足够精确）。"""
    if t is None:
        return None
    try:
        from scipy.stats import t as tdist
        return float(2 * tdist.sf(abs(t), max(1, n - 1)))
    except Exception:
        # 简易正态近似
        x = abs(t)
        return float(2 * (1 - 0.5 * (1 + math.erf(x / math.sqrt(2)))))


# ═══════════════ 面板加载（流式，避免 OOM）═══════════════

def load_panel_arrays(panel_path, npz_cache, hop=MONTHLY_HOP):
    """加载面板 → 对齐数组（X/M/Y）。

    Y = t → t+hop 的**收盘价**收益（前瞻收益），用于 IC 计算。
    ⚠️ IC 计算用收盘价前瞻是标准做法（信号在 t 日收盘生成）。
    """
    # ── round124 P0-2：缓存必须校验**内容指纹**，不能只比 hop ──────────────
    # 血案（本轮实测）：原实现 `if int(z["hop"]) == hop: return` —— 只比 hop。
    #   于是 val_arrays_h20.npz（2026-10-29 11:58 生成，源自 panel_round66）会
    #   在传入 panel_round123.json 时**静默命中**，返回 219 截面（至 2026-09-22）
    #   的旧矩阵，而面板实际有 238 截面（至 2026-10-27）。
    #   ⇒ 修了 DIMS 也没用：旧矩阵的 K 仍是 9，growth 永远缺席。
    #
    # 指纹 = panel 的 mtime + size + 当前 DIMS 的完整序列。
    #   三者任一变化即重建 —— 这是「改了输入必须重建」的最小充分条件。
    try:
        _st = os.stat(panel_path)
        _sig = f"{int(_st.st_mtime)}:{_st.st_size}:{','.join(DIMS)}"
    except OSError:
        _sig = None

    if os.path.exists(npz_cache):
        try:
            z = np.load(npz_cache, allow_pickle=False)
            _z_sig = str(z["sig"]) if "sig" in z.files else ""
            if int(z["hop"]) == hop and _sig is not None and _z_sig == _sig:
                print(f"  [缓存命中] {npz_cache}")
                return (list(z["dates"]), z["X"], z["M"], z["Y"],
                        [str(c) for c in z["codes"]])
            if int(z["hop"]) == hop:
                _why = ("缺内容指纹（旧版缓存）" if not _z_sig
                        else "面板或 DIMS 已变")
                print(f"  [缓存失效] {npz_cache} —— {_why}，将重建")
        except Exception as _e:
            print(f"  [缓存损坏] {npz_cache} —— {_e}，将重建")
    print(f"  读取 {panel_path} ...")
    panel = json.load(open(panel_path, encoding="utf-8"))
    px = json.load(open(os.path.join(HERE, "data", "klines_long.json"),
                        encoding="utf-8"))
    dates_all = panel["dates"]
    day_idx = sorted({d for c in px for d in px[c]})
    tidx = {d: i for i, d in enumerate(day_idx)}

    use = []
    for d in dates_all:
        rows = panel["rows"].get(d) or {}
        i0 = tidx.get(d)
        if not rows or i0 is None or i0 + hop >= len(day_idx):
            continue
        use.append(d)
    codes = sorted({c for d in use for c in (panel["rows"].get(d) or {})})
    ci = {c: i for i, c in enumerate(codes)}
    nD, nC, nK = len(use), len(codes), len(DIMS)
    print(f"  截面 {nD} × 只 {nC} × 维 {nK}")

    X = np.full((nD, nC, nK), np.nan, dtype=np.float32)
    Y = np.full((nD, nC), np.nan, dtype=np.float32)
    for di, d in enumerate(use):
        rows = panel["rows"].get(d) or {}
        d1 = day_idx[tidx[d] + hop]
        for c, rec in rows.items():
            j = ci[c]
            b0 = (px.get(c) or {}).get(d)
            b1 = (px.get(c) or {}).get(d1)
            if not (isinstance(b0, (list, tuple)) and isinstance(b1, (list, tuple))):
                continue
            if len(b0) < 2 or len(b1) < 2:
                continue
            p0, p1 = b0[1], b1[1]
            if not (is_num(p0) and is_num(p1)) or p0 <= 0:
                continue
            Y[di, j] = p1 / p0 - 1.0
            for k, dim in enumerate(DIMS):
                v = rec.get(dim)
                if is_num(v):
                    X[di, j, k] = float(v)
    M = ~np.isnan(X)
    np.savez_compressed(npz_cache, hop=hop,
                        sig=np.array(_sig or ""),
                        dims=np.array(DIMS),
                        dates=np.array(use), codes=np.array(codes),
                        X=X, M=M, Y=Y)
    print(f"  [写缓存] {npz_cache}  (K={nK}, dims={DIMS})")
    del panel, px
    return use, X, M, Y, codes


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def rank_rows(a):
    """按行（截面）做平均秩。a: (nD, nC)，NaN 保持 NaN。"""
    return rankdata(a, axis=1, nan_policy="omit")


def row_pearson_ranks(RS, RY, min_n=100):
    """逐截面计算两组秩的 Pearson 相关（即 Spearman）。

    RS/RY: (nD, nC) 秩矩阵（NaN 表示缺失）。
    返回 list[float]，长度 = 有效截面数。
    """
    ok = (~np.isnan(RS)) & (~np.isnan(RY))
    n = ok.sum(axis=1)
    a = np.where(ok, RS, 0.0)
    b = np.where(ok, RY, 0.0)
    ns = np.maximum(n, 1)
    ma = a.sum(axis=1) / ns
    mb = b.sum(axis=1) / ns
    da = np.where(ok, a - ma[:, None], 0.0)
    db = np.where(ok, b - mb[:, None], 0.0)
    num = (da * db).sum(axis=1)
    den = np.sqrt((da * da).sum(axis=1) * (db * db).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = np.where((den > 1e-12) & (n >= min_n), num / den, np.nan)
    return [float(v) for v in ic[~np.isnan(ic)]]


# ═══════════════ 合成打分 ═══════════════

def composite(X, M, Y, wmap, dim_max):
    """按权重合成总分。缺维从分母剔除（禁 or 0）。

    返回 (nD, nC) 总分矩阵（NaN = 无有效维）。
    """
    w = np.array([float(wmap.get(d, 0.0)) for d in DIMS])
    dm = np.array([float(dim_max.get(d, 10.0)) for d in DIMS])
    V = np.where(M, np.minimum(np.where(np.isnan(X), 0.0, X), dm) / dm * 10.0, 0.0)
    Mf = M.astype(np.float64)
    den = Mf @ w
    num = V @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 1e-12, num / den * 10.0, np.nan)
    return S


def combo_ics_at(X, M, Y, wmap, dim_max, RY, idxs):
    """在**指定的（可能不连续的）截面索引**上算组合 IC 序列。

    ⚠️ 2026-10-29 关键修正（项目血泪）：
    样本外区间按 regime 划分后是**非连续截面集合**
    （110 个跌市截面散布在 219 个交易日里，中间夹着涨市截面）。
    若用切片 X[a:b] 取数，会把中间的涨市截面**一并纳入** →
    结论完全失真（本项目实测：切片法得样本外组合 IC 逐段从 +0.14 衰减到 −0.20，
    而正确取样后该段 IC 实为 **+0.1220（t=+4.32）**，完全是反向的假象）。

    ⇒ 所有「按 regime 划分的区间」的统计，**必须**用本函数逐截面取样。

    实现：为避免逐截面调用开销，先把需要的行 gather 成连续块再算。
    """
    idxs = list(idxs)
    if not idxs:
        return []
    Xs = X[idxs]
    Ms = M[idxs]
    Ys = Y[idxs]
    RYs = RY[idxs] if RY is not None else rank_rows(Ys)
    return row_pearson_ranks(rank_rows(composite(Xs, Ms, Ys, wmap, dim_max)), RYs)


def combo_ics(X, M, Y, wmap, dim_max, RY=None):
    """组合的逐截面 IC 序列。"""
    S = composite(X, M, Y, wmap, dim_max)
    RS = rank_rows(S)
    if RY is None:
        RY = rank_rows(Y)
    return row_pearson_ranks(RS, RY)


# ═══════════════ 五分分层 ═══════════════

def quantile_buckets(vals, q=5):
    """按分位数分成 q 组。返回 labels（1=最高组 .. q=最低组）。

    用 rank 定位避免并列值错分：按秩分桶。
    """
    n = len(vals)
    if n < q:
        return None
    order = np.argsort(vals, kind="mergesort")
    labels = np.empty(n, dtype=int)
    ranks = np.empty(n, dtype=int)
    ranks[order] = np.arange(n)
    labels = (ranks * q) // n          # 0..q-1，0 最低
    return labels


def layered_returns(Xk, Y, M, q=5, min_per_bucket=20):
    """单因子五分分层：每期按因子值分 q 组，算各组的未来收益均值。

    返回 (bucket_returns, bucket_ics)
      bucket_returns: list[list[float]]，长度 q，第 i 个元素是第 i 组的逐期收益
      bucket_ics: 同结构，各组内部的 IC（组内因子值 vs 未来收益）
    第 0 个 bucket = 因子值最低组，第 q-1 = 最高组。
    """
    nD = Xk.shape[0]
    bret = [[] for _ in range(q)]
    bic = [[] for _ in range(q)]
    for t in range(nD):
        xs = Xk[t]
        ys = Y[t]
        ok = (~np.isnan(xs)) & (~np.isnan(ys))
        if ok.sum() < min_per_bucket * q:
            continue
        xv, yv = xs[ok], ys[ok]
        lab = quantile_buckets(xv, q)
        if lab is None:
            continue
        for b in range(q):
            sel = lab == b
            if sel.sum() < min_per_bucket:
                continue
            bret[b].append(float(np.mean(yv[sel])))
            # 组内 IC：因子值 vs 未来收益
            rx = rankdata(xv[sel])
            ry = rankdata(yv[sel])
            if len(rx) < 5:
                continue
            rx = rx - rx.mean()
            ry = ry - ry.mean()
            sx = math.sqrt((rx * rx).sum())
            sy = math.sqrt((ry * ry).sum())
            if sx > 0 and sy > 0:
                bic[b].append(float((rx * ry).sum() / (sx * sy)))
    return bret, bic


def monotonicity(bucket_ics):
    """分层单调性：Q5-Q1 价差 + 是否严格单调（按组均值 IC）。

    ⚠️ 覆盖不足的维度（如 dominant 仅 25%）可能有空组 → 必须跳过空组，
    否则 None 参与运算会崩（本项目已踩：round92 dominant Q1 空）。
    """
    means = []
    for b in bucket_ics:
        means.append(sum(b) / len(b) if b else None)
    valid = [(i, m) for i, m in enumerate(means) if m is not None]
    if len(valid) < 2:
        return {"spread": None, "monotonic": False, "means": means,
                "n_valid_buckets": len(valid), "spread_note": "有效分组不足"}
    # 用**首尾两个有效组**算价差（而非固定 Q1/Q5）
    spread = valid[-1][1] - valid[0][1]
    diffs = []
    for j in range(len(valid) - 1):
        diffs.append(valid[j + 1][1] - valid[j][1])
    inc = sum(1 for d in diffs if d > 0)
    dec = sum(1 for d in diffs if d < 0)
    mono = (inc >= len(diffs) - 1) or (dec >= len(diffs) - 1)
    return {"spread": spread, "monotonic": bool(mono),
            "increasing_steps": inc, "decreasing_steps": dec,
            "means": means, "n_valid_buckets": len(valid),
            "spread_note": f"Q{valid[0][0]+1} vs Q{valid[-1][0]+1}"}


# ═══════════════ 约束投影 ═══════════════

def project(wv, dims=None, w_cap=W_CAP, g_cap=G_CAP, groups=None):
    """投影到可行域：截单维 → 压超限大类 → 归一，迭代至稳定。"""
    dims = dims or DIMS
    groups = groups or GROUPS
    w = np.clip(np.asarray(wv, np.float64), 0.0, w_cap)
    gidx = [[dims.index(k) for k in ks if k in dims] for ks in groups.values()]
    gidx = [g for g in gidx if g]
    for _ in range(400):
        for idx in gidx:
            s = w[idx].sum()
            if s > g_cap:
                w[idx] *= g_cap / s
        tot = w.sum()
        if tot <= 1e-12:
            w = np.ones(len(dims)) / len(dims)
            tot = 1.0
        w = w / tot
        over = w > w_cap
        if not over.any():
            break
        w[over] = w_cap
    return w


def feasible(wv, dims=None, w_cap=W_CAP, g_cap=G_CAP, groups=None):
    dims = dims or DIMS
    groups = groups or GROUPS
    w = np.asarray(wv, np.float64)
    if w.min() < -1e-9 or w.max() > w_cap + 1e-9:
        return False
    if abs(w.sum() - 1.0) > 1e-6:
        return False
    for ks in groups.values():
        idx = [dims.index(k) for k in ks if k in dims]
        if idx and w[idx].sum() > g_cap + 1e-9:
            return False
    return True


# ═══════════════ 月度截面划分 ═══════════════

def monthly_slices(dates, hop=MONTHLY_HOP):
    """把每日截面序列切成月度截面（每 hop 个交易日取一个）。

    返回 list[(start_idx, end_idx)]，每个是一个月度区间。
    """
    n = len(dates)
    out = []
    for s in range(0, n, hop):
        e = min(s + hop, n)
        out.append((s, e))
    return out


def split_in_out(dates, in_frac=0.6, regime=None):
    """按月度截面划分样本内 / 样本外。

    in_frac: 样本内占比（默认 0.6）。切点在**月度边界**上，避免跨界。
    regime: 可选传入 (up_idx_set, dn_idx_set)（事后 regime 划分的截面索引），
        用于「样本内外按 regime 各留一段」的划分方式。
        若提供，则：
          · 样本内 = 涨市段（含 regime 中所有涨市截面）
          · 样本外 = 跌市段（含 regime 中所有跌市截面）
        这样盲测区间与求解区间**市场环境相反**，是最严格的样本外检验。
    """
    n = len(dates)
    if regime is not None:
        up, dn = regime
        in_idx = sorted(t for t in up if t < n)
        out_idx = sorted(t for t in dn if t < n)
        return in_idx, out_idx
    cut = int(n * in_frac)
    # 回退到最近的「月度起点」（每 20 个交易日的倍数）
    cut = (cut // MONTHLY_HOP) * MONTHLY_HOP
    cut = max(MONTHLY_HOP, min(cut, n - MONTHLY_HOP))
    return (0, cut), (cut, n)
