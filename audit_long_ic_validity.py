"""audit_long_ic_validity.py —— 对本轮长样本 IC 结论做「自我攻击」审计。

必须在报告 h=20 NW t=4.4 之前回答的四个问题：

Q1 幸存者偏差：2029 只 domain 里有多少只「中途退市」？
   若几乎全部活到 2026-11 → 退市股被系统性排除 → IC 被扭曲。
   （注意方向不确定：剔除最差股票会「抬高」低分组的收益 → 低估 IC；
     但若模型给退市股高分，剔除又会「抬高」高分组的收益 → 高估 IC。
     两个方向都可能，唯一正确做法是**把退市股放回去**再算。）

Q2 白噪声基准：n=1635 序列、lag=19 的 NW t，纯噪声 95% 分位是多少？
   记忆实测表只有 n=60/lag=0→2.00。lag=19 时 NW 估计方差更大，
   临界值必然高于 2.0。必须实测，否则「4.4 显著」是拍脑袋。

Q3 重叠窗虚高：t_naive / nw_t 的比值（t_inflation）是否落在
   记忆实测区间 2.09~6.37？若远低于此，说明 NW lag 没起效。

Q4 分年度单调递增：2020=+0.001 → 2026=+0.106 一路单调上升。
   这是「alpha 稳定」还是「权重拟合在近期样本」的指纹？
   用「只用 2020-2022 数据重算」做对照 —— 但注意我们**没有**独立
   验证期的权重，所以这个对照只能定性，不能当样本外。
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.join(HERE, "data", "klines_2020.npz")
PANEL = os.path.join(HERE, "data", "panel_round124_long.json")
IC = os.path.join(HERE, "data", "ic_long_2020.json")


def q1_survivorship():
    print("=" * 70)
    print("Q1 幸存者偏差：domain 里有多少只中途退市？")
    z = np.load(KL, allow_pickle=False)
    close = z["X"][:, :, 1]
    codes = [str(c) for c in z["codes"]]
    dates = [str(d) for d in z["dates"]]
    n_d = close.shape[1]
    valid = np.isfinite(close)
    last_i = np.full(len(codes), -1)
    for i in range(len(codes)):
        w = np.where(valid[i])[0]
        if len(w):
            last_i[i] = w[-1]
    first_i = np.full(len(codes), n_d)
    for i in range(len(codes)):
        w = np.where(valid[i])[0]
        if len(w):
            first_i[i] = w[0]

    end = n_d - 1
    # 允许最后 5 个交易日内还有报价即视为「活到最后」
    alive = last_i >= end - 5
    dead = ~alive
    print(f"  domain 总数            : {len(codes)}")
    print(f"  活到 {dates[end]}      : {int(alive.sum())} ({alive.mean():.1%})")
    print(f"  中途「消失」(退市/长停) : {int(dead.sum())} ({dead.mean():.1%})")
    # 首次出现分布（判断是否含 2020 后上市新股）
    late_list = [codes[i] for i in range(len(codes)) if first_i[i] > 250]
    print(f"  2021 年后才首次出现     : {len(late_list)}（新股或数据缺口）")
    print("\n  中途消失样本（前 20，附最后交易日）:")
    for i in np.where(dead)[0][:20]:
        print(f"    {codes[i]}  最后 {dates[last_i[i]]}  "
              f"首现 {dates[first_i[i]]}")
    if dead.sum() > 20:
        print(f"    ... 共 {int(dead.sum())} 只")
    return {"n": len(codes), "alive": int(alive.sum()),
            "dead": int(dead.sum()), "dead_codes":
            [codes[i] for i in np.where(dead)[0]][:200]}


def q2_white_noise():
    print("\n" + "=" * 70)
    print("Q2 白噪声基准：n / lag 组合下 NW t 的 95% 分位（实测蒙特卡洛）")
    rng = np.random.default_rng(20261104)

    def nw_t_np(a, lag):
        n = len(a)
        m = a.mean()
        e = a - m
        s = float(e @ e) / n
        for k in range(1, lag + 1):
            if k >= n:
                break
            s += 2.0 * (1.0 - k / (lag + 1.0)) * float(e[k:] @ e[:-k]) / n
        return m / np.sqrt(s / n) if s > 0 else np.nan

    rows = []
    # 三组：真实 n=1635 / n_eff=82 等效拼接 / 旧样本 n=218
    for n, lag in [(1635, 19), (1635, 0), (82, 19), (82, 0),
                   (218, 19), (330, 4), (1653, 0)]:
        ts = []
        for _ in range(400):
            a = rng.standard_normal(n)
            t = nw_t_np(a, lag)
            if np.isfinite(t):
                ts.append(t)
        ts = np.array(ts)
        q95 = float(np.percentile(np.abs(ts), 95))
        rows.append({"n": n, "lag": lag, "abs_t_q95": round(q95, 2),
                     "abs_t_q99": round(float(np.percentile(np.abs(ts), 99)), 2)})
        print(f"  n={n:<5} lag={lag:<3} |t|95%={q95:.2f}  |t|99%="
              f"{np.percentile(np.abs(ts),99):.2f}")
    print("\n  ⚠️ 读法：报出的 NW t 必须超过同 (n,lag) 的 |t|95% 才算"
          "「5% 单检验显著」。跨 (n,lag) 套用 2.0 / 3.90 是错的。")
    return rows


def q3_inflation():
    print("\n" + "=" * 70)
    print("Q3 重叠窗虚高：t_naive / nw_t")
    with open(IC, encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    for h in ("h1", "h5", "h20"):
        r = d["results"][h]["liq"]
        print(f"  {h:<4} n={r['n']:<5} lag={r['nw_lag']:<3} "
              f"t_naive={r['t_naive']:>6} nw_t={r['nw_t']:>6} "
              f"inflation={r['t_inflation']}x")
        out[h] = r["t_inflation"]
    print("  记忆实测区间 2.09~6.37 → 若显著低于该区间，说明 HAC 未生效")
    return out


def q4_yearly():
    print("\n" + "=" * 70)
    print("Q4 分年度单调性 + 剔除最强年")
    with open(IC, encoding="utf-8") as f:
        d = json.load(f)
    for h in ("h1", "h5", "h20"):
        y = d["yearly_liq"].get(h) or {}
        ics = [v["ic"] for v in y.values()]
        print(f"  {h}: " + " ".join(f"{k}={v['ic']:+.3f}" for k, v in y.items()))
        if len(ics) >= 3:
            # Spearman(年份序, IC) —— 判断单调性
            from scipy.stats import spearmanr
            rho, p = spearmanr(range(len(ics)), ics)
            print(f"      年份 vs IC 单调性 ρ={rho:+.3f} p={p:.4f}"
                  + ("  ⚠️ 单调上升 → 疑似近期样本拟合" if rho > 0.7 else ""))
    print("\n  LOO（剔除单年后 h=20 IC 与 t）:")
    for k, v in (d["leave_one_year_out_liq"].get("h20") or {}).items():
        print(f"    {k:<12} n={v['n']:<5} IC={v['ic']:+.4f} NWt={v['nw_t']}")
    return True


def main():
    r1 = q1_survivorship()
    r2 = q2_white_noise()
    r3 = q3_inflation()
    q4_yearly()
    out = os.path.join(HERE, "data", "ic_long_validity_audit.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"q1_survivorship": r1, "q2_white_noise": r2,
                   "q3_t_inflation": r3}, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
