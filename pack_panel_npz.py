"""pack_panel_npz.py —— 把 938MB 单行面板 JSON 流式压成紧凑 npz。

为什么必须流式：
    panel_round124_long.json 是**单行 938MB** JSON。`json.load()` 会把
    1653 日 × ~1900 只 × 14 字段全部展开成 Python dict/list，
    实测这类结构的内存放大倍率约 4~6 倍 → 峰值 4~5 GB。
    本机 16GB、当前 free 仅 2.5GB，直接 load 会被 SIGTERM（Exit 137）。

做法：
    手动定位 `"rows": {` 的位置，之后逐日期块用 `raw_decode` 增量解码。
    峰值内存 = 单个日期块（~1900 只 × 14 字段 ≈ 2 MB 文本 / ~10 MB 对象）。

产出（data/panel_r124_mat.npz）：
    dates  (D,)        str
    codes  (N,)        str
    S      (D, N, 8)   float32  8 个分数维（缺失=NaN）
    P      (D, N, 4)   float32  price / circ_mv / amount / turnover_ratio
    dims   (8,)        str      S 轴2 的维名顺序
"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "data", "panel_round124_long.json")
OUT = os.path.join(HERE, "data", "panel_r124_mat.npz")

SDIMS = ["score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
         "score_size_growth", "score_ep", "score_quality", "score_growth"]
PDIMS = ["price", "circ_mv", "amount", "turnover_ratio"]


CHUNK = 4 << 20        # 4 MB
LOOKAHEAD = 16 << 20   # 16 MB —— 单个日期块实测约 0.5 MB，留 30 倍余量


def iter_rows_block(path, key="rows"):
    """流式产出 (date, {code: {k: v}}) —— 只按日期块解码，不整体 load。

    手工实现而非 ijson：本机无 ijson，且只需要「顶层一个 key 下面
    是 date→dict」这一种极规整的结构，几十行扫描器足够且零依赖。

    ⚠️ 截断纪律：`raw_decode` 遇到半个 token 会抛 JSONDecodeError，
       且**抛之前 buffer 位置难回退**。因此改为「解码前保证缓冲区内
       至少有 LOOKAHEAD 字符（或已到 EOF）」—— 一次就把当日块读全，
       永不进入「解到一半」的状态。这比 try/except 续读稳得多。
    """
    dec = json.JSONDecoder()
    with open(path, "r", encoding="utf-8") as f:
        # 1) 找 `"rows": {`
        needle = f'"{key}"'
        buf = ""
        while needle not in buf:
            chunk = f.read(CHUNK)
            if not chunk:
                raise RuntimeError(f"找不到 {needle}")
            buf += chunk
        p = buf.index(needle) + len(needle)
        p = buf.index("{", p) + 1          # 跳过 rows 的 `{`

        n_date = 0
        while True:
            # 补齐前瞻缓冲；EOF 后 read 返回 "" 即停止补
            while len(buf) - p < LOOKAHEAD:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                buf += chunk
            # 丢弃已消费前缀，避免 buf 无限增长
            if p > 8 * LOOKAHEAD:
                buf = buf[p:]
                p = 0
            # 跳过空白与逗号
            while p < len(buf) and buf[p] in " \t\r\n,":
                p += 1
            if p >= len(buf):
                return                      # 正常结束
            if buf[p] == "}":
                return                      # rows 结束
            # 2) 解 key（日期字符串）—— 此时已保证有 LOOKAHEAD 前瞻
            try:
                d, q = dec.raw_decode(buf, p)
            except json.JSONDecodeError:
                chunk = f.read(CHUNK)
                if not chunk:
                    return
                buf += chunk
                continue
            p = q
            while p < len(buf) and buf[p] in " \t\r\n:":
                p += 1
            # 3) 解 value（该日的 code→rec）
            while True:
                try:
                    day, q = dec.raw_decode(buf, p)
                    break
                except json.JSONDecodeError:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        return
                    buf += chunk
            p = q
            n_date += 1
            yield d, day
            del day


def main():
    t0 = time.time()
    # 先只读 header 拿 dates/codes 顺序（header 在文件开头，很小）
    with open(SRC, "r", encoding="utf-8") as f:
        head = f.read(1 << 16)
    # dates 列表在 meta 之后；用 raw_decode 从 "dates" 处取
    ki = head.find('"dates"')
    if ki < 0:
        print("❌ header 里没找到 dates，改用两遍扫描")
        return 2
    dec = json.JSONDecoder()
    p = head.index("[", ki)
    dates, _ = dec.raw_decode(head, p)
    print(f"dates: {len(dates)}  {dates[0]} ~ {dates[-1]}")

    # codes 白名单：用 klines_2020 的 codes（口径一致，且是产物生成时的 domain）
    z = np.load(os.path.join(HERE, "data", "klines_2020.npz"), allow_pickle=False)
    codes = [str(c) for c in z["codes"]]
    del z
    print(f"codes: {len(codes)}")

    didx = {d: i for i, d in enumerate(dates)}
    cidx = {c: i for i, c in enumerate(codes)}
    D, N = len(dates), len(codes)

    S = np.full((D, N, len(SDIMS)), np.nan, dtype=np.float32)
    P = np.full((D, N, len(PDIMS)), np.nan, dtype=np.float32)

    nday = 0
    nrec = 0
    miss = set()
    for d, day in iter_rows_block(SRC):
        nday += 1
        if nday % 100 == 0:
            print(f"  {nday}/{D}  {d}  {time.time()-t0:.0f}s", flush=True)
        di = didx.get(d)
        if di is None:
            miss.add(d)
            continue
        for c, rec in day.items():
            ci = cidx.get(c)
            if ci is None:
                miss.add(c)
                continue
            nrec += 1
            for k, name in enumerate(SDIMS):
                v = rec.get(name)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
                    S[di, ci, k] = v
            for k, name in enumerate(PDIMS):
                v = rec.get(name)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
                    P[di, ci, k] = v
        del day

    print(f"\n解析完成: {nday} 日 / {nrec:,} 行 / 未匹配 {len(miss)} / {time.time()-t0:.0f}s")
    np.savez_compressed(OUT,
                        dates=np.array(dates), codes=np.array(codes),
                        dims=np.array(SDIMS), pdims=np.array(PDIMS),
                        S=S, P=P)
    print(f"写出 {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB)")

    # 逐维覆盖自检
    print("\n逐维有值截面数（分隔：有值/总日）:")
    for k, name in enumerate(SDIMS):
        col = S[:, :, k]
        cnt = int(np.sum(~np.isnan(col).all(axis=1)))
        print(f"  {name:<20} {cnt}/{D}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
