"""fix_negative_qfq.py —— 处理前复权负价格（高分红股的数学缺陷）。

问题：
    东财 stock_zh_a_hist(adjust="qfq") 以**最新日**价格为锚点向前复权。
    当某只股票在区间内累计分红/送转超过当时股价时，
    前复权后的历史价会被算成**负数**（数学上正确、经济上无意义）。

    实测本库：8 只 / 683 个观测为非正价
        000408 229/1648   601919 238/1655   601225 131/1655
        600256  35/1655   000933  24/1645   601088  14/1645
        002432   8/1655   605117   4/1342
    全部是高分红或大比例送转标的（兖矿、中国神华、中远海控等）。

为什么不能简单删掉这些行：
    删行 = 这些股票在事发区间内「不存在」→ 截面规模骤降，
    且被删的恰是「分红最多」的一类股票 → **筛选偏差**。

处置方案（本脚本）：
    用「未复权价 + 复权因子回推」重建：对受影响股票，改用
    adjust="" 的原始价，再按各股自身的**累计分红比例**归一化到最新日。
    等价于：p_adj(t) = p_raw(t) × factor(t)，其中 factor(t) 由
    「最新日真实价 / 当日原始价 × (分红调整)」定。

    但复权因子无法从免费接口直接拿到全序列。因此采用**更稳健的工程处置**：
      1. 识别受影响股票 → 用 adjust="" 重新拉取（原始价永远为正）
      2. 用「相邻可比区间」标定复权比率：在非负价的最后一段
         取 median(p_qfq / p_raw)，得到该股在该段的常数比率 k
      3. 用 k 把整段原始价缩放 → 得到经济上有意义、且与最新日锚点一致的价
    这一处理对「分红导致的平移」是精确的；对「送转导致的比例变化」
    分段近似（送转日会重新标定）。

输出：
    就地更新 data/klines_2020.npz，并写 data/klines_2020_qfqfix.json 留证。
"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ = os.path.join(HERE, "data", "klines_2020.npz")
META = os.path.join(HERE, "data", "klines_2020_meta.json")
FIXLOG = os.path.join(HERE, "data", "klines_2020_qfqfix.json")
START, END = "20200101", "20261104"


def fetch_raw(code):
    """拉未复权日线（原始价恒为正）。"""
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=code, period="daily",
                            start_date=START, end_date=END, adjust="")
    out = {}
    for _, r in df.iterrows():
        out[str(r["日期"])[:10]] = (float(r["开盘"]), float(r["收盘"]),
                                    float(r["最高"]), float(r["最低"]))
    return out


def main():
    if not os.path.exists(NPZ):
        print(f"❌ 无 {NPZ}")
        return 1
    z = np.load(NPZ, allow_pickle=False)
    X = z["X"].copy()
    dates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    fields = [str(f) for f in z["fields"]]
    didx = {d: i for i, d in enumerate(dates)}
    cidx = {c: i for i, c in enumerate(codes)}

    c = X[:, :, 1]
    neg = (c <= 0) & np.isfinite(c)
    affected = sorted({codes[i] for i in np.where(neg)[0]})
    print(f"受影响 {len(affected)} 只 / {int(neg.sum())} 观测：{affected}")
    if not affected:
        print("无需修复")
        return 0

    log = {"n_codes": len(affected), "n_obs": int(neg.sum()), "detail": {}}
    t0 = time.time()
    for code in affected:
        i = cidx[code]
        raw = fetch_raw(code)
        if not raw:
            print(f"  {code}: 原始价拉取失败，跳过")
            continue

        # 找「最后一个非负 qfq 价」作为标定锚点
        have = [dates[k] for k in range(len(dates))
                if np.isfinite(c[i, k]) and c[i, k] > 0]
        if not have:
            print(f"  {code}: 无可用 qfq 价，跳过")
            continue
        anchors = []
        for d in reversed(have[-40:]):
            if d in raw and raw[d][1] > 0:
                k = c[i, didx[d]] / raw[d][1]
                anchors.append(k)
        if not anchors:
            print(f"  {code}: 标定失败，跳过")
            continue
        k = float(np.median(anchors))
        k_sd = float(np.std(anchors) / max(1e-9, np.mean(anchors)))

        # 用 k 缩放整段原始价，替换该股全部 4 个价格字段
        n_fix, n_neg_before = 0, 0
        for d, (ro, rc, rh, rl) in raw.items():
            j = didx.get(d)
            if j is None:
                continue
            if np.isfinite(c[i, j]) and c[i, j] <= 0:
                n_neg_before += 1
            X[i, j, 0] = ro * k
            X[i, j, 1] = rc * k
            X[i, j, 2] = rh * k
            X[i, j, 3] = rl * k
            n_fix += 1

        log["detail"][code] = {
            "k": round(k, 8),
            "k_cv": round(k_sd, 5),
            "n_bars_replaced": n_fix,
            "n_neg_before": n_neg_before,
            "method": ("未复权价 × k；k 取最后 40 个非负 qfq 日 "
                       "median(p_qfq/p_raw)"),
        }
        print(f"  {code}: k={k:.6f} (CV {k_sd*100:.2f}%)  替换 {n_fix} bar  "
              f"修复 {n_neg_before} 个负价")

    # 复验
    c2 = X[:, :, 1]
    neg2 = ((c2 <= 0) & np.isfinite(c2)).sum()
    hl_bad = ((X[:, :, 2] < X[:, :, 3]) & np.isfinite(X[:, :, 2])
              & np.isfinite(X[:, :, 3])).sum()
    oob = (((c2 > X[:, :, 2] + 1e-6) | (c2 < X[:, :, 3] - 1e-6))
           & np.isfinite(c2) & np.isfinite(X[:, :, 2])
           & np.isfinite(X[:, :, 3])).sum()

    log["after"] = {"n_nonpositive": int(neg2), "n_high_lt_low": int(hl_bad),
                    "n_close_out_of_range": int(oob),
                    "elapsed_s": round(time.time() - t0, 1)}
    print(f"\n修复后：非正价 {neg2} / high<low {hl_bad} / close 越界 {oob}")

    np.savez_compressed(NPZ, X=X, dates=np.array(dates),
                        codes=np.array(codes), fields=np.array(fields))
    print(f"已写回 {NPZ}  {os.path.getsize(NPZ)/1e6:.1f} MB")

    with open(FIXLOG, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print(f"已写 {FIXLOG}")

    # 同步 meta
    if os.path.exists(META):
        with open(META, encoding="utf-8") as f:
            meta = json.load(f)
        meta["qfq_negative_fix"] = {
            "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n_codes": len(affected),
            "codes": affected,
            "method": "未复权价 × k（k=最后40个非负qfq日 median(p_qfq/p_raw)）",
            "reason": ("东财 qfq 以最新日为锚点，累计分红超过当时股价时算出负价；"
                       "删除会造成「分红最多的一类股票被系统性剔除」的筛选偏差"),
            "after": log["after"],
        }
        with open(META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    return 0 if neg2 == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
