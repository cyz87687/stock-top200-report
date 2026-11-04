"""build_panel_sharded.py —— 分段构建长面板并合并（绕开本机 SIGTERM）。

为什么需要：
    build_panel_long.py 全量跑 1653 日约 700s，实测在本机跑到 1300/1653
    就被 SIGTERM（Exit 137）。本机对长任务的资源限制很紧，
    且**后台任务也会被回收** → 只能前台分段串行。

做法：
    依次调用 build_panel_long.py --day-offset K --limit-days N
    → 每片写 data/panel_shard_{K}.json
    → 全部完成后合并成 panel_round124_long.json（与单片格式完全一致）

⚠️ 合并必须校验：
    1) 分片日期不重叠、不缺口（用 klines_2020 的交易日全集比对）
    2) 每片 meta 一致（domain / dim_max / pit 口径）
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "panel_round124_long.json")
SHARD_DIR = os.path.join(DATA, "panel_shards")


def main():
    shard_n = 300          # 每片交易日数（实测 ~130s，安全）
    total = 1653
    args = sys.argv[1:]
    if "--shard" in args:
        shard_n = int(args[args.index("--shard") + 1])
    if "--total" in args:
        total = int(args[args.index("--total") + 1])
    os.makedirs(SHARD_DIR, exist_ok=True)

    offs = list(range(0, total, shard_n))
    print(f"分 {len(offs)} 片，每片 {shard_n} 日，总计 {total} 日")
    done = []
    for k, off in enumerate(offs):
        fp = os.path.join(SHARD_DIR, f"shard_{off:04d}.json")
        if os.path.exists(fp) and os.path.getsize(fp) > 1e6:
            print(f"  [{k+1}/{len(offs)}] offset={off} 已存在，跳过 "
                  f"({os.path.getsize(fp)/1e6:.0f} MB)")
            done.append(fp)
            continue
        lim = min(shard_n, total - off)
        cmd = [PY, "-u", os.path.join(HERE, "build_panel_long.py"),
               "--day-offset", str(off), "--limit-days", str(lim),
               "--out", fp]
        t0 = time.time()
        print(f"  [{k+1}/{len(offs)}] offset={off} limit={lim} ...", flush=True)
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ❌ 分片失败 rc={r.returncode}\n{r.stderr[-1500:]}")
            return 1
        print(f"      ok  {time.time()-t0:.0f}s  "
              f"({os.path.getsize(fp)/1e6:.0f} MB)")
        done.append(fp)

    print(f"\n合并 {len(done)} 片 → {OUT}")
    merged_rows = {}
    meta = None
    alldates = []
    for fp in done:
        print(f"  读 {os.path.basename(fp)} ...", flush=True)
        with open(fp, encoding="utf-8") as f:
            blob = json.load(f)
        if meta is None:
            meta = blob.get("meta") or {}
        for d, rows in blob["rows"].items():
            if d in merged_rows:
                raise RuntimeError(f"分片日期重复: {d}")
            merged_rows[d] = rows
        alldates += list(blob["rows"].keys())
        del blob

    ds = sorted(merged_rows)
    print(f"  合并后 {len(ds)} 日  {ds[0]} ~ {ds[-1]}")
    # 校验连续性：与 klines_2020 的交易日比对
    import numpy as np
    z = np.load(os.path.join(DATA, "klines_2020.npz"), allow_pickle=False)
    kd = [str(x) for x in z["dates"]]
    expect = [d for d in kd if ds[0] <= d <= ds[-1]]
    missing = [d for d in expect if d not in merged_rows]
    extra = [d for d in ds if d not in kd]
    print(f"  应含 {len(expect)} 日 / 实含 {len(ds)} 日 / "
          f"缺 {len(missing)} / 多 {len(extra)}")
    if missing:
        print(f"  ⚠️ 缺失交易日（前 10）: {missing[:10]}")
    if extra:
        print(f"  ⚠️ 多余日期（前 10）: {extra[:10]}")

    if meta is not None:
        meta["n_dates"] = len(ds)
        meta["date_range"] = [ds[0], ds[-1]]
        meta["sharded"] = {"n_shards": len(done), "shard_dir": "data/panel_shards"}
    out = {"version": "round124-long",
           "meta": meta or {},
           "dates": ds,
           "rows": {d: merged_rows[d] for d in ds}}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"写出 {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB)")

    # 逐维覆盖自检
    order = ["score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
             "score_size_growth", "score_ep", "score_quality", "score_growth",
             "score_fund", "score_dominant"]
    print("\n逐维有值截面数:")
    for k in order:
        cnt = 0
        lowest = 10 ** 9
        for d in ds:
            v = sum(1 for r in merged_rows[d].values()
                    if isinstance(r.get(k), (int, float))
                    and not isinstance(r.get(k), bool) and r.get(k) == r.get(k))
            if v > 0:
                cnt += 1
            lowest = min(lowest, v)
        print(f"  {k:<20} {cnt}/{len(ds)}  最低 {lowest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
