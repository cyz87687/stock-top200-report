"""audit_klines_2020.py —— 长历史库质量审计。

必须过审才能用于回测。审计项与 round124 对 panel_round123 的口径一致。
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ = os.path.join(HERE, "data", "klines_2020.npz")
META = os.path.join(HERE, "data", "klines_2020_meta.json")


def main():
    if not os.path.exists(NPZ):
        print(f"❌ 不存在 {NPZ}")
        return 1
    z = np.load(NPZ, allow_pickle=False)
    X, dates, codes, fields = z["X"], z["dates"], z["codes"], z["fields"]
    nC, nD, nK = X.shape
    o, c, h, l, v = X[:, :, 0], X[:, :, 1], X[:, :, 2], X[:, :, 3], X[:, :, 4]
    amt, to = X[:, :, 5], X[:, :, 6]

    print(f"库: {os.path.basename(NPZ)}  {os.path.getsize(NPZ)/1e6:.1f} MB")
    print(f"形状: {nC} 只 × {nD} 日 × {nK} 字段   {dates[0]} ~ {dates[-1]}")
    print()

    print("=== 1. 每日截面规模 ===")
    per_day = np.isfinite(c).sum(axis=0)
    print(f"  中位 {np.median(per_day):.0f} / 最少 {per_day.min()} / 最多 {per_day.max()}")
    print(f"  ≥200 的交易日: {(per_day>=200).sum()}/{nD} ({(per_day>=200).mean()*100:.1f}%)")
    print(f"  ≥1000 的交易日: {(per_day>=1000).sum()}/{nD}")
    # 分年
    import collections
    by_year = collections.defaultdict(list)
    for i, d in enumerate(dates):
        by_year[str(d)[:4]].append(per_day[i])
    for y in sorted(by_year):
        a = by_year[y]
        print(f"    {y}: {len(a):>4} 日  中位 {np.median(a):>4.0f} 只")

    print("\n=== 2. 价格完整性 ===")
    fin = np.isfinite(c)
    print(f"  收盘有值: {fin.sum():,} / {c.size:,} = {fin.mean()*100:.1f}%")
    print(f"  收盘 ≤0  : {int(((c<=0)&fin).sum())}")
    print(f"  high<low : {int(((h<l)&np.isfinite(h)&np.isfinite(l)).sum())}")
    oob = ((c>h+1e-6)|(c<l-1e-6))&np.isfinite(c)&np.isfinite(h)&np.isfinite(l)
    print(f"  close 超出 [low,high]: {int(oob.sum())}")

    print("\n=== 3. 涨跌幅异常（|r|>45%）===")
    with np.errstate(invalid="ignore", divide="ignore"):
        r = c[:, 1:] / c[:, :-1] - 1.0
    big = np.abs(r) > 0.45
    print(f"  |r|>45% 的观测: {int(np.isfinite(r[big]).sum())}")
    idx = np.where(np.isfinite(r) & big)
    for k in range(min(8, len(idx[0]))):
        ci, di = idx[0][k], idx[1][k]
        print(f"    {codes[ci]} {dates[di+1]}  r={r[ci,di]*100:+.1f}%  "
              f"prev_c={c[ci,di]:.2f} c={c[ci,di+1]:.2f}")

    print("\n=== 4. 成交量为 0 / 缺失（停牌） ===")
    vz = (v==0) & np.isfinite(v)
    print(f"  vol==0: {int(vz.sum())} ({vz.sum()/max(1,np.isfinite(v).sum())*100:.2f}%)")

    print("\n=== 5. 换手率可用性（反推市值的前提） ===")
    tf = np.isfinite(to) & (to>0)
    print(f"  turnover>0: {int(tf.sum()):,} ({tf.mean()*100:.1f}%)")
    shares = np.where(tf, v/np.where(tf, to/100.0, np.nan), np.nan)
    sh_ok = np.isfinite(shares) & (shares>0)
    print(f"  可反推股本: {int(sh_ok.sum()):,} ({sh_ok.mean()*100:.1f}%)")

    print("\n=== 6. 年份覆盖（每只股票的 bar 数分布）===")
    bars_per = np.isfinite(c).sum(axis=1)
    print(f"  中位 {np.median(bars_per):.0f} / 最少 {bars_per.min()} / 最多 {bars_per.max()}")
    print(f"  覆盖全区间(≥1600日): {(bars_per>=1600).sum()} 只")
    print(f"  半区间以内(<800日) : {(bars_per<800).sum()} 只（多为近年上市）")

    print("\n=== 7. 与旧库对比 ===")
    old = os.path.join(HERE, "data", "klines_long.json")
    if os.path.exists(old):
        oj = json.load(open(old, encoding="utf-8"))
        od = sorted({d for cc in oj for d in oj[cc]})
        print(f"  旧库 klines_long: {len(oj)} 只 × {len(od)} 日  {od[0]} ~ {od[-1]}")
        print(f"  新库 klines_2020: {nC} 只 × {nD} 日  {str(dates[0])} ~ {str(dates[-1])}")
        print(f"  ⇒ 交易日 {len(od)} → {nD}（{nD/len(od):.1f}×）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
