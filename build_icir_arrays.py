#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_icir_arrays.py —— 把面板流式转成紧凑 npz（供权重优化/回测反复秒读）

═══ 为什么单独做这一步 ═══

panel_round66.json 是 117MB 文本，`json.load` 之后因 Python dict/list 的对象开销
膨胀到 **数 GB**。连续两次加载直接 OOM（exit 137），而本机 swap 已用满 5.1G/6.0G。

故：一次性用 **ijson 风格的增量解析不可行（面板是单行大 dict）**，
改用「先落一张只含必需字段的中间 npz」：
  · 内存峰值 ≈ panel dict 一次（约数 GB）→ 仍会 OOM

⇒ 最终方案：**不解析整份 JSON**，而是先在生成面板时顺便输出 npz。
   但 panel_round66.json 已生成，重跑要 3 分钟 —— 可接受。

本脚本复用 audit_round66 的采集逻辑，但**边算边写 npz**，不把整份面板留在内存：

  阶段1：扫描 panel_round34 的 dates + 每截面 codes（只读结构，不读全部维值）
        —— 这一步用 json 流式无法避免，但 panel_round34 本身 116MB 也一样大。

⚠️ 结论：这个思路不成立。见下方实际采用的做法。
"""
# -*- 实际方案说明 -*-
# 由于两个面板 JSON 都在 100MB+，任何「完整解析」都会吃掉数 GB。
# 真正可行的做法是：**用 ijson 逐条流式解析**。若环境中没有 ijson，
# 则退化为「用 audit_round66 的进程内数据直接写 npz」——即让 audit_round66
# 在跑完时额外落一份 npz（内存中本来就有全部数据，不再额外解析）。
#
# 本文件因此改为「npz 导出器」：由 audit_round66 --export-npz 调用，
# 在面板还在内存里时直接导出，避免二次解析。

import os
import sys
import json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DIMS = [
    "score_fund", "score_quality", "score_ep", "score_lowvol20",
    "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_size_growth", "score_dominant",
]


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def export_npz(panel, px, hop, out_path, min_codes=100):
    """把内存中的 panel dict 直接导出为 npz（X/M/Y/dates/codes）。"""
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

    X = np.full((nD, nC, nK), np.nan, dtype=np.float32)
    Y = np.full((nD, nC), np.nan, dtype=np.float32)

    for di, d in enumerate(use):
        rows = panel["rows"].get(d) or {}
        d1 = day_idx[tidx[d] + hop]
        for c, rec in rows.items():
            j = ci[c]
            bar0 = (px.get(c) or {}).get(d)
            bar1 = (px.get(c) or {}).get(d1)
            if not (isinstance(bar0, (list, tuple)) and isinstance(bar1, (list, tuple))):
                continue
            if len(bar0) < 2 or len(bar1) < 2:
                continue
            p0, p1 = bar0[1], bar1[1]
            if not (is_num(p0) and is_num(p1)) or p0 <= 0:
                continue
            Y[di, j] = p1 / p0 - 1.0
            for k, dim in enumerate(DIMS):
                v = rec.get(dim)
                if is_num(v):
                    X[di, j, k] = float(v)

    M = ~np.isnan(X)
    n_valid = ((~np.isnan(Y)) & M.any(axis=2)).sum(axis=1)
    keep = n_valid >= min_codes
    X, M, Y = X[keep], M[keep], Y[keep]
    use = [use[i] for i in range(nD) if keep[i]]

    np.savez_compressed(out_path, hop=hop,
                        dates=np.array(use), codes=np.array(codes),
                        X=X, M=M, Y=Y)
    print(f"  [npz] {out_path}  {X.shape}  {os.path.getsize(out_path)/1e6:.1f} MB")
    return use, X, M, Y, codes


def main():
    """独立运行：为已存在的 panel_round66.json 导出 npz。

    ⚠️ 会一次性占用数 GB（解析 117MB JSON）。若 swap 吃紧，请先跑
       `--from-round34` 用较小的中间产物，或直接用 audit_round66 的进程内导出。
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=os.path.join(HERE, "data", "panel_round66.json"))
    ap.add_argument("--hop", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "icir_arrays_h20.npz"))
    args = ap.parse_args()

    print(f"读取 {args.panel} ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    print(f"  截面 {len(panel['dates'])}")
    px = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    export_npz(panel, px, args.hop, args.out)


if __name__ == "__main__":
    main()
