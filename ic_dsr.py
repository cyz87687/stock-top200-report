"""ic_dsr.py —— 把 IC 序列折成 DSR（P1-3：DSR 接入判定链）。

为什么需要：
    NW t 只回答「均值是否偏离 0」，不回答「这个数字是不是我试了很多次
    才捞出来的」。本轮在 1653 天上重算后 h=20 的 NW t 从 1.85 跳到 5.87，
    如果 n_trials 是 364~804，那么纯噪声下最大 |NW t| 的期望同样会被推高。
    ⇒ 必须用 DSR 折掉选择偏差，否则「5.87 显著」这句话是不完整的。

口径纪律（沿用 backtest_core 已实测的结论）：
    1) **sr 必须是每期 Sharpe，不是年化**（差 √252 倍）
    2) **n_obs 必须用不重叠样本数**（重叠窗口会让 sqrt(T-1) 虚高）
       本项目 h=20 → 不重叠观测 = n/20
    3) **n_trials 走 trial_log.n_trials()**，禁手填
    4) **sr_std 是假设值**（各次试验 Sharpe 的横截面标准差），
       必须标 sr_std_assumed=True。实测 N=352 纯噪声时 DSR 中位 0.02，
       `DSR>0.95` 比例 0/200 → 正确的自证判据是「DSR>0.95 比例≈0」
    5) DSR 未过时**强制 significant=False + dsr_veto=True**，
       禁两套结论并存

本模块只做「IC 序列 → DSR」的换算，不改 backtest_core 的实现。
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _moments(xs):
    """返回 (每期SR, 偏度, 峰度)。峰度为非超额（正态=3）。"""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    m2 = sum((x - mu) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    sd = math.sqrt(m2)
    m3 = sum((x - mu) ** 3 for x in xs) / n / (sd ** 3)
    m4 = sum((x - mu) ** 4 for x in xs) / n / (sd ** 4)
    return mu / sd, m3, m4


def ic_to_dsr(ics, h, n_trials, sr_std=0.05):
    """把逐截面 IC 序列折成 DSR。

    ⚠️ 关键换算：IC 序列的「每期 Sharpe」不是投资组合 Sharpe，
       而是「每期 IC 的均值/标准差」——即 IC 的信息比率（IC_IR）。
       对它的零假设同样是「均值=0，且这个均值是 N 次试验里最好的那次」。
       因此 DSR 的算术直接套用，但必须说明分母是 IC 的截面标准差，
       **不是**收益波动率。这一点在输出里显式声明，避免误读为组合 SR。
    """
    try:
        from backtest_core import deflated_sharpe
    except Exception as e:
        return {"available": False, "reason": f"backtest_core 不可用：{e}"}
    mm = _moments(ics)
    if not mm:
        return {"available": False, "reason": "IC 序列退化（n<5 或方差为 0）"}
    sr, skew, kurt = mm
    # 不重叠观测数（最关键的一步）
    n_obs = max(1, int(len(ics) // max(1, h)))
    d = deflated_sharpe(sr=sr, n_obs=n_obs, n_trials=n_trials,
                        sr_std=sr_std, skew=skew, kurt=kurt)
    d["available"] = True
    d["ic_ir"] = round(sr, 4)
    d["ic_skew"] = round(skew, 4)
    d["ic_kurt"] = round(kurt, 4)
    d["n_obs_nonoverlap"] = n_obs
    d["sr_std_assumed"] = True
    d["sr_std"] = sr_std
    d["h"] = h
    d["note"] = (
        "sr 口径 = 每期 IC 的均值/标准差（IC_IR），**不是**组合年化 Sharpe。"
        f"n_obs 用不重叠样本 {n_obs}（=n/h）；n_trials={n_trials} 取自 trial_log；"
        f"sr_std={sr_std} 是**假设值**（sr_std_assumed=True），"
        "因本项目没有多策略收益矩阵、无法实测该横截面离散度。"
        "⇒ DSR 结论的强度受此假设限制，须与 PBO 缺失一并披露。")
    return d


def main():
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--ic", default=os.path.join(HERE, "data", "ic_long_2020.json"))
    ap.add_argument("--sr-std", type=float, default=0.05)
    args = ap.parse_args()
    with open(args.ic, encoding="utf-8") as f:
        blob = json.load(f)
    try:
        import trial_log as T
        k = int(T.n_trials())
        src = "trial_log.n_trials()"
    except Exception as e:
        k, src = 364, f"fallback（trial_log 不可用：{e}）"
    print(f"n_trials = {k}  （{src}）")
    out = {}
    for hk, blk in (blob.get("results") or {}).items():
        h = int(hk[1:])
        r = (blk or {}).get("liq") or {}
        ics = r.get("ic_series") or []
        if not ics:
            continue
        d = ic_to_dsr(ics, h, k, args.sr_std)
        out[hk] = d
        if d.get("available"):
            print(f"  {hk}: n={r['n']} IC={r['ic_mean']:+.4f} "
                  f"NWt={r['nw_t']} → IC_IR={d['ic_ir']} "
                  f"DSR={d['dsr']} significant={d['significant']} "
                  f"(n_obs={d['n_obs_nonoverlap']})")
        else:
            print(f"  {hk}: {d.get('reason')}")
    p = os.path.join(HERE, "data", "ic_dsr_long.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"n_trials": k, "n_trials_source": src,
                   "sr_std_assumed": args.sr_std, "results": out},
                  f, ensure_ascii=False, indent=1)
    print(f"\n写出 {p}")
    print("\n⚠️ 读法：DSR>0.95 才算「已折掉选择偏差」。"
          "若 h=20 的 NW t 很高但 DSR 不达 0.95，"
          "说明该 t 可以被「试了 N 次」解释，不得作为独立 alpha 引用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
