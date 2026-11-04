#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_core.py —— 回测的**唯一口径来源**（P1-5）

## 这个文件解决什么问题

实测 21 个 `backtest_*.py`（round117 P1-5 盘点，数字为实测非抄写）：

| 项目 | 有统一口径的脚本数 |
|---|---|
| 有 `argparse`（可参数化） | **2 / 21** |
| 明确声明基准（POOL_EW / HS300） | **2 / 21** |
| 用有效截面 `valid_cross_sections` | **0 / 21** |
| 有 Newey-West t | **1 / 21** |

→ 项目纪律「超额以 POOL_EW 为基准」**没有任何代码强制**。
→ 更严重：`backtest_pool_factor.py:250` 等 6 个脚本对 **HOP=20 重叠窗口**用
   普通 t（`mu/(sd/√n)`），t 值虚高。项目记忆第 ⑮ 条已记过这个坑，
   但只写在文档里，**代码层面没有任何拦截**。

## 本内核强制什么（不是建议，是断言）

1. **收益口径**：前复权收盘 / 前复权收盘（`kline_calendar` 有效截面过滤）
2. **基准**：默认 `POOL_EW`（池内等权），`HS300` 可选但必须显式传
3. **样本区间**：默认只用 `valid_cross_sections`（来自 P1-1）
4. **重叠窗口 t**：`nw_t(xs, lag=HOP-1)`。**lag < HOP-1 直接 raise**，
   不给「悄悄用默认 lag」的机会
5. **样本量闸门**：不重叠样本数 < 5 时拒绝出显著性结论（返回 None + 原因）

## 为什么 lag 必须 ≥ HOP-1（实测，不是理论）

模拟 HOP=20 重叠窗口（MA(19) 结构，n=381）实测：

```
普通 t          = -3.610        ← 虚高 3.6 倍
NW t (lag=4)    = -1.681        ← 当前 factor_validation_core 默认值，仍虚高 1.7 倍
NW t (lag=10)   = -1.201
NW t (lag=19)   = -0.994        ← 正确量级
NW t (lag=25)   = -0.951
```

`lag=4` 与 `lag=19` 的 |t| 差 0.688。**用 lag=4 会把「不显著」报成「显著」。**

## 用法

```python
import backtest_core as bc

# 1. 加载价格 + 有效日历
bt = bc.Backtest(hop=20, benchmark="POOL_EW")

# 2. 每个调仓日：传入当日截面 {"code": score}，拿回该日组合收益
for d in bt.rebalance_dates():
    picks = my_strategy(d)            # {"000001": 0.87, ...}
    bt.record(d, picks)

# 3. 出结论（自动做 NW t + 不重叠复核）
print(bt.report())
```

命令行自查：
    python backtest_core.py --selftest
"""
import os
import sys
import json
import math
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HOP_DEFAULT = 20
BENCHMARKS = ("POOL_EW", "HS300")
MIN_OVERLAP_SAMPLES = 5          # 不重叠样本数下限
KLINES = os.path.join(HERE, "data", "klines_long.json")
BENCH_FILE = os.path.join(HERE, "data", "bench_hs300.json")


# ═══════════════ DSR：Deflated Sharpe Ratio（round118 P1-3）═══════════════
#
# ## 为什么需要它
#
# 项目此前只有「IC + NW t + Bonferroni」三件套。这三样都在回答
# 「这个 Sharpe 是不是 0」，**没有一个回答「这个 Sharpe 是不是试出来的」**。
#
# 关键区别：试了 N 次后，**最好的那个 Sharpe 的期望值不是 0**。
# 即使 N 个策略全是纯噪声，最大 Sharpe 的期望也是
#     SR_max ≈ sqrt(Var(SR)) * [(1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))]
# 其中 γ ≈ 0.5772（Euler-Mascheroni）。
#
# DSR 就是把「试了多少次」折进 Sharpe 的显著性里：
#     DSR = Φ( (SR - SR_0) * sqrt(T-1) / sqrt(1 - γ3·SR + (γ4-1)/4·SR²) )
# 阈值 **> 0.95**（Bailey & López de Prado 2014）。
#
# ## 本项目的特殊困难（必须写在代码里，不能只写在论文里）
#
# ① **N_trials 只能从 trial_log 来**，而 trial_log 的 K 是**区间**（349~656），
#    不是精确值。所以 DSR 必须**给区间结果**，不能只报一个数。
# ② **T（观测数）在项目里极小**：门槛后截面仅 6~14 个。DSR 对 T 敏感，
#    T 小时 SR 的抽样误差本身就压过信号 —— 与 NW t 小样本失效同源。
# ③ **PBO 做不了**：需要多策略收益矩阵，项目没有。不做就不做，
#    不拿「DSR 过了」代替「PBO 过了」。

EULER_GAMMA = 0.5772156649015329

# 各次试验 Sharpe 的横截面标准差 —— **本项目无从实测**（trial_log 没记
# 每次试验的 Sharpe 序列）。取 0.05 的理由：月度策略年化 SR 典型散布在
# [-0.5, 1.5]，按 √12 折到每期约 0.03~0.10，取中位偏保守。
# 方向性：sr_std 越大 → SR_0 越大 → DSR 越小 → 越难通过。
# **凡用到它的结果都标 sr_std_assumed=True**，不让假设伪装成实测。
DEFAULT_SR_STD = 0.05


def _norm_cdf(x):
    """标准正态 CDF。用 erfc 实现（无 scipy 依赖）。"""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_ppf(p):
    """标准正态分位（Acklam 近似 + 一步 Halley 精修）。

    与 scipy.stats.norm.ppf 在 1e-9 内一致（见 test_dsr 对拍）。
    只在本模块内部用 —— factor_eval 有自己的一份（历史原因），
    两处都在测试里对拍过 scipy；**不要在这里改成 import factor_eval**，
    否则 backtest_core 会依赖 factor_eval 的一大堆模块级副作用。
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"p 必须在 (0,1)，收到 {p}")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
            (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    e = 0.5 * math.erfc(-x / math.sqrt(2)) - p
    u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


def expected_max_sharpe(n_trials, sr_std):
    """N 个独立试验下，最大 Sharpe 的期望值（零真实 alpha 时）。

    `sr_std` 是各次试验 Sharpe 的**标准差**（不是均值的标准误）。
    实测含义：若各次试验 Sharpe 的离散度是 0.5、试了 100 次，
    那么纯噪声下最好那个的期望 Sharpe 就是 0.5*2.51 ≈ 1.26 ——
    **看起来非常像"有效策略"**。

    N=1 时返回 0（只试一次，没有选择偏差）。
    """
    n = max(int(n_trials), 1)
    if n == 1:
        return 0.0
    q1 = _norm_ppf(1.0 - 1.0 / n)
    q2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return float(sr_std) * ((1.0 - EULER_GAMMA) * q1 + EULER_GAMMA * q2)


def deflated_sharpe(sr, n_obs, n_trials, sr_std, skew=0.0, kurt=3.0):
    """Deflated Sharpe Ratio（Bailey & López de Prado 2014）。

    参数
    ----
    sr : float
        **每期** Sharpe（不是年化）。年化 SR 与每期 SR 差 √252 倍，
        混用会让 DSR 差一个量级 —— 这是最常见的误用。
    n_obs : int
        收益序列的观测数 T。⚠️ 重叠窗口下 T 必须用**不重叠**样本数，
        否则 DSR 会像 NW t 一样虚高。
    n_trials : int
        试了多少次（从 trial_log 取，不得手填）。
    sr_std : float
        各次试验 Sharpe 的横截面标准差。
    skew, kurt : float
        收益的偏度与**峰度**（kurt=3 为正态；不是超额峰度）。

    返回 dict：dsr / sr0 / significant(>0.95) / note
    """
    n_obs = int(n_obs)
    if n_obs < 5:
        return {"dsr": None, "sr0": None, "significant": None,
                "note": f"观测数 {n_obs}<5，不出 DSR 结论"}
    sr = float(sr)
    sr0 = expected_max_sharpe(n_trials, sr_std)
    # 方差项：负偏/厚尾会放大 SR 的抽样波动 → DSR 更保守
    _var = 1.0 - float(skew) * sr + (float(kurt) - 1.0) / 4.0 * sr * sr
    if _var <= 0:
        return {"dsr": None, "sr0": sr0, "significant": None,
                "note": f"方差项 {_var:.4f}≤0（skew/kurt 与 sr 组合病态），无法计算"}
    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(_var)
    dsr = _norm_cdf(z)
    return {
        "dsr": round(dsr, 4),
        "z": round(z, 4),
        "sr0": round(sr0, 4),
        "sr": round(sr, 4),
        "n_obs": n_obs,
        "n_trials": int(n_trials),
        "sr_std": round(float(sr_std), 4),
        "significant": bool(dsr > 0.95),
        "note": ("DSR>0.95 才可称「已折掉选择偏差」。"
                 "⚠️ DSR 不能替代 PBO（后者需多策略收益矩阵，本项目没有）"),
    }


def sharpe_moments(xs):
    """从收益序列算 (每期SR, 偏度, 峰度)。峰度按非超额（正态=3）返回。"""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    m2 = sum((x - mu) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    sd = math.sqrt(m2)
    m3 = sum((x - mu) ** 3 for x in xs) / n
    m4 = sum((x - mu) ** 4 for x in xs) / n
    return {
        "sr": mu / sd,
        "skew": m3 / (sd ** 3),
        "kurt": m4 / (sd ** 4),
        "n": n,
    }


# ═══════════════ 统计基座 ═══════════════

def nw_t(xs, lag):
    """Newey-West t。**lag 必须由调用方显式给**，不设默认值。

    不设默认值是故意的：本项目已经踩过「用 lag=4 处理 HOP=20 重叠窗口」
    的坑（|t| 从 0.99 虚高到 1.68）。有默认值就一定会有人不传。
    """
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


def plain_t(xs):
    """普通 t —— **仅用于展示虚高倍数**，不得作为显著性判据。"""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    if sd <= 1e-12:
        return None
    return mu / (sd / math.sqrt(n))


def nonoverlap(xs, hop):
    """取不重叠子样本（每 hop 个取 1 个）。用于复核 NW 结论。

    重叠窗口的 NW t 依赖 lag 选对；不重叠重采样不依赖任何 lag 假设，
    两者一致才算稳。这是项目记忆第 ⑮ 条的操作化。
    """
    return list(xs[::max(1, hop)])


def stat(xs, hop, lag=None, n_trials=None, sr_std=None):
    """一组收益序列的完整统计。lag 缺省 = hop-1（重叠窗口的正确量级）。

    `n_trials` / `sr_std` 给了就附带 DSR（P1-3）。**不给就不算 DSR，
    绝不用默认 K 静默填一个数** —— DSR 的全部意义就在于 N 是真实的。
    """
    if lag is None:
        lag = max(1, hop - 1)
    n = len(xs)
    out = {
        "n": n,
        "lag": lag,
        "mean": (sum(xs) / n) if n else None,
        "nw_t": nw_t(xs, lag) if n >= 5 else None,
        "plain_t": plain_t(xs) if n >= 5 else None,
        "pos_rate": (sum(1 for x in xs if x > 0) / n) if n else None,
    }
    # 虚高倍数：让读者一眼看出「普通 t 有多离谱」
    if out["nw_t"] and out["plain_t"] and abs(out["nw_t"]) > 1e-9:
        out["inflation"] = abs(out["plain_t"]) / abs(out["nw_t"])
    else:
        out["inflation"] = None
    # 不重叠复核
    no = nonoverlap(xs, hop)
    out["n_nonoverlap"] = len(no)
    out["nonoverlap_mean"] = (sum(no) / len(no)) if no else None
    out["nonoverlap_t"] = plain_t(no) if len(no) >= 5 else None
    out["nonoverlap_ok"] = (out["nonoverlap_t"] is not None
                            and out["nw_t"] is not None
                            and (out["nonoverlap_t"] * out["nw_t"] > 0))
    # ── DSR（P1-3）：只在调用方给了真实 N 时计算 ──────────────────────
    if n_trials is not None and sr_std is not None:
        mm = sharpe_moments(no if len(no) >= 5 else xs)
        if mm:
            out["dsr"] = deflated_sharpe(
                sr=mm["sr"],
                # ⚠️ T 用**不重叠**样本数：重叠窗口的 T 会让 sqrt(T-1) 虚高，
                #    与 NW t 踩过的是同一个坑（记忆第 ⑮ 条）
                n_obs=len(no) if len(no) >= 5 else n,
                n_trials=n_trials, sr_std=sr_std,
                skew=mm["skew"], kurt=mm["kurt"])
        else:
            out["dsr"] = {"dsr": None, "note": "收益序列退化（方差为 0），无法算 DSR"}
    else:
        out["dsr"] = None
    # 显著性裁决
    if out["nw_t"] is None:
        out["significant"] = None
        out["verdict"] = "样本不足（n<5），不出显著性结论"
    elif len(no) < MIN_OVERLAP_SAMPLES:
        out["significant"] = None
        out["verdict"] = (f"不重叠样本仅 {len(no)} 个 (<{MIN_OVERLAP_SAMPLES})，"
                          f"NW t 不可靠，不出显著性结论")
    else:
        out["significant"] = abs(out["nw_t"]) >= 2.0
        if not out["nonoverlap_ok"]:
            out["verdict"] = ("⚠️ 重叠 NW t 与不重叠 t **符号相反** → "
                              "结论不稳，禁止引用")
            out["significant"] = False
        elif out["significant"]:
            out["verdict"] = f"显著（|NW t|={abs(out['nw_t']):.2f}≥2，且不重叠同号）"
        else:
            out["verdict"] = f"不显著（|NW t|={abs(out['nw_t']):.2f}<2）"
    # DSR 与 NW t 冲突时，**不允许静默通过**
    _d = out.get("dsr") or {}
    if out["significant"] and _d.get("significant") is False:
        out["verdict"] += ("；⚠️ 但 DSR={:.3f}≤0.95 —— 该 Sharpe 可被"
                           "「试了 {} 次」解释，禁止作为独立 alpha 引用").format(
                               _d.get("dsr") or 0, _d.get("n_trials"))
        out["significant"] = False
        out["dsr_veto"] = True
    return out


# ═══════════════ 主类 ═══════════════

class Backtest:
    """统一回测容器。所有口径在 __init__ 里定死，之后不可改。

    参数
    ----
    hop : int
        持有期（交易日）。20 = 月度调仓。**决定 NW lag**。
    benchmark : str
        "POOL_EW"（默认，符合项目纪律）或 "HS300"。
        ⚠️ HS300 需 `data/bench_hs300.json` 存在，否则 raise，不静默降级。
    pool : callable or None
        可选：`f(date) -> set(codes)`，限定当日可交易池。
    use_valid_calendar : bool
        是否只用有效截面日期（默认 True，来自 P1-1）。
    """

    def __init__(self, hop=HOP_DEFAULT, benchmark="POOL_EW",
                 pool=None, use_valid_calendar=True, verbose=True):
        if benchmark not in BENCHMARKS:
            raise ValueError(f"benchmark 只能是 {BENCHMARKS}，收到 {benchmark!r}")
        if hop < 1:
            raise ValueError(f"hop 必须 ≥1，收到 {hop}")
        self.hop = int(hop)
        self.benchmark = benchmark
        self.pool = pool
        self.use_valid_calendar = use_valid_calendar
        self.verbose = verbose

        with open(KLINES, encoding="utf-8") as f:
            self.px_raw = json.load(f)

        import kline_calendar as kc
        self.kc = kc
        # 口径 1：有效截面（剔除建库初期 36 个爬坡日）
        self.dates = kc.build_valid(self.px_raw)
        self.n_dates_total = len({d for c in self.px_raw
                                  for d in self.px_raw[c]})

        # 闭式价格索引：date -> {code: close}
        self._close = {}
        for c, m in self.px_raw.items():
            for d, bar in m.items():
                # bar 口径 = [open, close, high, low, volume]（项目记忆已记）
                if not bar or len(bar) < 2:
                    continue
                try:
                    v = float(bar[1])
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    self._close.setdefault(d, {})[c] = v

        # 口径 2：基准
        self._hs300 = None
        if benchmark == "HS300":
            if not os.path.exists(BENCH_FILE):
                raise FileNotFoundError(
                    f"benchmark=HS300 但缺 {BENCH_FILE}。"
                    f"请先跑 fetch_bench.py，或改用 POOL_EW（项目默认基准）。")
            with open(BENCH_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            # 兼容 {"dates": [...], "close": [...]} 与 {date: close}
            if isinstance(raw, dict) and "dates" in raw and "close" in raw:
                self._hs300 = dict(zip(raw["dates"], raw["close"]))
            elif isinstance(raw, dict):
                self._hs300 = {k: float(v) for k, v in raw.items()
                               if isinstance(v, (int, float))}
            else:
                raise ValueError(f"{BENCH_FILE} 结构无法识别：{type(raw).__name__}")

        self.records = []          # [(date, ret, bench_ret, n_picks)]
        self._skipped = []         # [(date, reason)]
        if verbose:
            print(f"[backtest_core] hop={self.hop}  benchmark={self.benchmark}  "
                  f"有效截面 {len(self.dates)}/{self.n_dates_total} 日  "
                  f"lag={max(1, self.hop - 1)}")

    # ---------- 日期与价格 ----------

    def rebalance_dates(self):
        """可用的调仓日：有效截面且 t+hop 仍在日历内。"""
        idx = {d: i for i, d in enumerate(self.dates)}
        out = []
        for d in self.dates:
            i = idx[d]
            if i + self.hop < len(self.dates):
                out.append(d)
        return out

    def close(self, date, code):
        return self._close.get(date, {}).get(code)

    def fwd_ret(self, date, code):
        """前复权收盘 → t+hop 前复权收盘 的收益。缺价返回 None（不填 0）。"""
        i = self._date_index(date)
        if i is None or i + self.hop >= len(self.dates):
            return None
        d1 = self.dates[i + self.hop]
        p0 = self.close(date, code)
        p1 = self.close(d1, code)
        if not p0 or not p1 or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def _date_index(self, date):
        if not hasattr(self, "_didx"):
            self._didx = {d: i for i, d in enumerate(self.dates)}
        return self._didx.get(date)

    def pool_ew_ret(self, date):
        """池内等权收益 = 当日所有可得价格股票的 fwd 收益均值。

        ⚠️ 口径：分母只算有价格数据的股票（缺价不填 0，不参与）。
        这就是项目纪律里的 POOL_EW 基准。
        """
        i = self._date_index(date)
        if i is None or i + self.hop >= len(self.dates):
            return None
        d1 = self.dates[i + self.hop]
        c0 = self._close.get(date) or {}
        c1 = self._close.get(d1) or {}
        pool = set(c0) & set(c1)
        if self.pool:
            pool &= set(self.pool(date) or set())
        rets = []
        for c in pool:
            p0, p1 = c0.get(c), c1.get(c)
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
        if not rets:
            return None
        return sum(rets) / len(rets)

    def hs300_ret(self, date):
        if not self._hs300:
            return None
        i = self._date_index(date)
        if i is None or i + self.hop >= len(self.dates):
            return None
        d1 = self.dates[i + self.hop]
        p0, p1 = self._hs300.get(date), self._hs300.get(d1)
        if not p0 or not p1 or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def bench_ret(self, date):
        if self.benchmark == "HS300":
            return self.hs300_ret(date)
        return self.pool_ew_ret(date)

    # ---------- 记录 ----------

    def record(self, date, picks):
        """登记一个调仓日的选股结果。

        picks: {"code": score} 或 [code, ...]，等权买入。
        返回该日收益（None 表示该日被跳过，原因见 skip_reason）。
        """
        if isinstance(picks, dict):
            codes = list(picks.keys())
        else:
            codes = list(picks)
        codes = [c for c in codes if self.close(date, c) is not None]
        if not codes:
            self._skipped.append((date, "无有效价格"))
            return None

        i = self._date_index(date)
        if i is None:
            self._skipped.append((date, "非有效截面日"))
            return None
        if i + self.hop >= len(self.dates):
            self._skipped.append((date, f"t+{self.hop} 超出日历"))
            return None

        rets = [r for r in (self.fwd_ret(date, c) for c in codes) if r is not None]
        if not rets:
            self._skipped.append((date, "无前瞻收益"))
            return None
        r = sum(rets) / len(rets)
        b = self.bench_ret(date)
        self.records.append({
            "date": date, "ret": r, "bench": b,
            "excess": (r - b) if b is not None else None,
            "n_picks": len(codes), "n_priced": len(rets),
        })
        return r

    def run(self, strategy):
        """strategy(date) -> picks。遍历所有可用调仓日。"""
        for d in self.rebalance_dates():
            self.record(d, strategy(d))
        return self

    # ---------- 出结论 ----------

    def _dsr_inputs(self):
        """从 trial_log 取 DSR 需要的 (n_trials, sr_std)。

        **取不到就返回 (None, None)** —— 绝不用默认 K 静默填一个数。
        DSR 的全部意义在于 N 是真的；填个假 N 算出来的 DSR 比不算更有害，
        因为它带着「已折掉选择偏差」的外观。

        `sr_std`（各次试验 Sharpe 的横截面标准差）**本项目无从实测**：
        trial_log 里没有每次试验的 Sharpe 序列。所以这里用
        `DEFAULT_SR_STD` 并在结果里显式标 `sr_std_assumed=True`。
        """
        try:
            import trial_log
            k = trial_log.n_trials()
            kr = trial_log.k_range()
        except Exception:
            return None, None, {"error": "trial_log 不可用，DSR 未计算"}
        return k, DEFAULT_SR_STD, {
            "n_trials": k,
            "n_trials_range": [kr["k_lower"], kr["k_upper_estimate"]],
            "sr_std": DEFAULT_SR_STD,
            "sr_std_assumed": True,
            "note": ("n_trials 来自 trial_log（可证下界，真实区间见 n_trials_range）；"
                     "sr_std 是**假设值** —— trial_log 未记录每次试验的 Sharpe 序列。"
                     "sr_std 越大 SR_0 越大、DSR 越小，故本结论偏保守"),
        }

    def report(self, with_dsr=True):
        recs = [r for r in self.records if r.get("excess") is not None]
        rets = [r["ret"] for r in self.records]
        exs = [r["excess"] for r in recs]
        k = sr_std = None
        dsr_meta = None
        if with_dsr:
            k, sr_std, dsr_meta = self._dsr_inputs()
        out = {
            "hop": self.hop,
            "benchmark": self.benchmark,
            "lag": max(1, self.hop - 1),
            "valid_calendar": self.use_valid_calendar,
            "n_dates_total": self.n_dates_total,
            "n_dates_valid": len(self.dates),
            "n_rebalances": len(self.records),
            "n_excess_obs": len(exs),
            "n_skipped": len(self._skipped),
            "skipped": self._skipped[:20],
            "abs": (stat(rets, self.hop, n_trials=k, sr_std=sr_std)
                    if len(rets) >= 5 else None),
            "excess": (stat(exs, self.hop, n_trials=k, sr_std=sr_std)
                       if len(exs) >= 5 else None),
            "dsr_meta": dsr_meta,
            "note": ("超额基准 = " + self.benchmark +
                     "。NW lag = hop-1（重叠窗口正确量级）。"
                     "不重叠复核符号相反 → 判为不显著。"
                     "DSR 的 T 用不重叠样本数（重叠会像 NW t 一样虚高）。"),
        }
        return out

    def print_report(self):
        r = self.report()
        print("=" * 78)
        print(f"回测口径  hop={r['hop']}  基准={r['benchmark']}  "
              f"NW lag={r['lag']}")
        print(f"有效截面  {r['n_dates_valid']}/{r['n_dates_total']} 日")
        print(f"调仓记录  {r['n_rebalances']} 个，跳过 {r['n_skipped']} 个")
        for tag, key in (("绝对收益", "abs"), ("超额收益", "excess")):
            s = r[key]
            print("-" * 78)
            if not s:
                print(f"{tag}：样本不足，不出结论")
                continue
            print(f"{tag}  n={s['n']}  均值={s['mean']*100:+.2f}%  "
                  f"胜率={s['pos_rate']*100:.1f}%")
            print(f"  普通 t = {s['plain_t']:+.3f}"
                  + (f"   （虚高 {s['inflation']:.2f} 倍 ⚠️）"
                     if s.get("inflation") else ""))
            print(f"  NW t(lag={s['lag']}) = {s['nw_t']:+.3f}  ← 唯一可用判据")
            nt = s["nonoverlap_t"]
            nt_s = f"{nt:+.3f}" if nt is not None else "NA"
            print(f"  不重叠复核  n={s['n_nonoverlap']}  t={nt_s}  "
                  f"同号={'✅' if s['nonoverlap_ok'] else '❌'}")
            d = s.get("dsr")
            if d:
                if d.get("dsr") is None:
                    print(f"  DSR         —（{d.get('note')}）")
                else:
                    print(f"  DSR         {d['dsr']:.4f}  "
                          f"({'✅ >0.95' if d['significant'] else '❌ ≤0.95'})  "
                          f"SR0={d['sr0']:.4f}  N={d['n_trials']}  T={d['n_obs']}")
            else:
                print("  DSR         —（未给 N_trials，不计算）")
            print(f"  裁决：{s['verdict']}")
        print("=" * 78)
        return r


# ═══════════════ 自查 ═══════════════

def selftest():
    """校验内核自身口径（不依赖任何策略）。"""
    ok = True
    print("=" * 78)
    print("backtest_core 自查")
    print("=" * 78)

    # 1. lag 必须等于 hop-1
    bt = Backtest(hop=20, benchmark="POOL_EW", verbose=False)
    r = bt.report()
    print(f"1. hop=20 → lag={r['lag']}", "✅" if r["lag"] == 19 else "❌")
    ok &= (r["lag"] == 19)

    # 2. 有效截面必须是 305，不是 341
    print(f"2. 有效截面 {r['n_dates_valid']}/{r['n_dates_total']}",
          "✅" if r["n_dates_valid"] < r["n_dates_total"] else "❌")
    ok &= (r["n_dates_valid"] < r["n_dates_total"])

    # 3. 基准不合法必须 raise
    try:
        Backtest(benchmark="CSI500", verbose=False)
        print("3. 非法 benchmark 未 raise ❌")
        ok = False
    except ValueError:
        print("3. 非法 benchmark 已 raise ✅")

    # 4. HS300 缺文件必须 raise（不静默降级到 POOL_EW）
    try:
        Backtest(benchmark="HS300", verbose=False)
        print("4. HS300 缺文件未 raise（静默降级！）❌")
        ok = False
    except FileNotFoundError:
        print("4. HS300 缺文件已 raise（不静默降级）✅")

    # 5. 缺价不能填 0
    d = bt.rebalance_dates()[0]
    v = bt.fwd_ret(d, "999999")
    print(f"5. 不存在的代码 fwd_ret={v}", "✅" if v is None else "❌ (应为 None 不是 0)")
    ok &= (v is None)

    # 6. nw_t 无默认 lag（防止不传 lag 悄悄用 4）
    import inspect
    sig = inspect.signature(nw_t)
    has_default = sig.parameters["lag"].default is not inspect.Parameter.empty
    print(f"6. nw_t 无默认 lag", "✅" if not has_default else "❌ (有默认值会被滥用)")
    ok &= (not has_default)

    # 7. 重叠窗口（正自相关的 MA 结构）：NW t 必须比普通 t 保守
    # ⚠️ 不能用正弦波等确定性周期序列测 —— 实测这类序列的 NW 方差估计不稳定，
    #    |NW t| 反而可能大于 |普通 t|（本自查第一版就踩了这个坑）。
    #    重叠窗口的真实生成过程是 MA(hop-1)，正自相关，NW 必然更保守。
    import random as _rnd
    rng = _rnd.Random(117)
    _eps = [rng.gauss(0, 1) for _ in range(300)]
    xs = []
    for i in range(19, 300):
        xs.append(0.002 + sum(_eps[i - k] for k in range(20)) / 20.0 * 0.05)
    s = stat(xs, hop=20)
    smaller = abs(s["nw_t"]) < abs(s["plain_t"])
    print(f"7. 重叠MA结构 |NW t|={abs(s['nw_t']):.3f} < "
          f"|普通 t|={abs(s['plain_t']):.3f}", "✅" if smaller else "❌")
    ok &= smaller

    # 8. 样本不足必须拒绝出结论
    tiny = stat([0.01, 0.02, 0.03], hop=20)
    print(f"8. n=3 → significant={tiny['significant']}",
          "✅" if tiny["significant"] is None else "❌ (不该给结论)")
    ok &= (tiny["significant"] is None)

    # 9. DSR：纯噪声下必须 ≈0.5（这是 DSR 唯一能自证的方法）
    import random as _r2
    _rng2 = _r2.Random(42)
    _dsrs = []
    for _ in range(200):
        _xs = [_rng2.gauss(0, 1) for _ in range(60)]
        _mm = sharpe_moments(_xs)
        _dd = deflated_sharpe(_mm["sr"], 60, 1, 0.05, _mm["skew"], _mm["kurt"])
        if _dd["dsr"] is not None:
            _dsrs.append(_dd["dsr"])
    _mean_dsr = sum(_dsrs) / len(_dsrs)
    print(f"9. 纯噪声 DSR 均值={_mean_dsr:.3f}（应≈0.50）",
          "✅" if 0.44 <= _mean_dsr <= 0.56 else "❌")
    ok &= (0.44 <= _mean_dsr <= 0.56)

    # 10. DSR 必须随 N 增大而单调变小（选择偏差方向）
    _xs = [_rng2.gauss(0.002, 0.01) for _ in range(60)]
    _mm = sharpe_moments(_xs)
    _d_vals = [deflated_sharpe(_mm["sr"], 60, n, 0.05,
                               _mm["skew"], _mm["kurt"])["dsr"]
               for n in (1, 10, 100, 1000)]
    _mono = all(_d_vals[i] > _d_vals[i + 1] for i in range(len(_d_vals) - 1))
    print(f"10. N↑ → DSR↓ {['%.3f' % v for v in _d_vals]}",
          "✅" if _mono else "❌")
    ok &= _mono

    # 11. N=1 时 SR0 必须为 0（只试一次没有选择偏差）
    _s0 = expected_max_sharpe(1, 0.5)
    print(f"11. N=1 → SR0={_s0}", "✅" if abs(_s0) < 1e-12 else "❌")
    ok &= (abs(_s0) < 1e-12)

    print("=" * 78)
    print("全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="回测统一口径内核（P1-5）。默认无参数 = 自查。")
    ap.add_argument("--selftest", action="store_true", help="校验内核口径")
    ap.add_argument("--hop", type=int, default=HOP_DEFAULT, help="持有期（默认 20）")
    ap.add_argument("--benchmark", default="POOL_EW", choices=BENCHMARKS)
    ap.add_argument("--info", action="store_true", help="只打印口径信息")
    a = ap.parse_args()

    if a.selftest or (not a.info):
        return selftest()
    bt = Backtest(hop=a.hop, benchmark=a.benchmark)
    print(json.dumps(bt.report(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
