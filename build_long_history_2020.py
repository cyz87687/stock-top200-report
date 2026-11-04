"""build_long_history_2020.py —— 构建 2020-01 至今的全 A 股长历史库。

为什么必须重建：
    现有 data/klines_long.json 只有 342 个交易日（2025-06-09~2026-11-03）。
    h=20 的 NW 校正 lag=19，218 个截面的**有效独立观测仅 ≈11**
    → t 值无信息量。要得到有信息量的 h=20 结论，样本跨度必须 ≥5 年。

数据源（全部实测可用）：
    日线   ak.stock_zh_a_hist(adjust="qfq")   -> [o,c,h,l,v,amount,turnover]
    股票列表 ak.stock_info_a_code_name()       -> 5578 只在市
    退市列表 ak.stock_info_sh_delist/sz_delist -> 补充幸存者偏差

⚠️ 关键口径（血泪）：
    1. 成交量单位：东财 stock_zh_a_hist 的「成交量」单位是**手**（×100 = 股）
       与库内 klines_long 的「688=股、其余=手」双口径**不同**。
       本脚本统一转成**股**，并在 meta 里显式声明，避免下游再踩。
    2. 市值不能直接用百度接口（实测每 15 天采样一次，非日频）。
       改用「换手率反推流通股本」：shares = 成交量(股) / (换手率/100)
       实测茅台偏差 0.1%~2.4%，宁德 41.58% 变异是真增发 → 反推可信。
    3. 前复权锚点：东财 qfq 以**最新日**为基准，全区间一次性拉取 →
       不存在「分次拉取导致锚点漂移」问题。这是相对现有库的重大改进。

输出：
    data/klines_2020.npz        (n_stock, n_day, 7) float32
                                轴2 = [o,c,h,l,v(股),amount,turnover]
    data/klines_2020_meta.json  轴/日期/代码/口径/审计

用法：
    python build_long_history_2020.py --workers 6            # 全量（约 6~25 分钟）
    python build_long_history_2020.py --limit 20 --workers 2  # 试跑
    python build_long_history_2020.py --resume                 # 断点续传
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT_NPZ = os.path.join(DATA, "klines_2020.npz")
OUT_META = os.path.join(DATA, "klines_2020_meta.json")
CACHE_DIR = os.path.join(DATA, "klines_2020_cache")

START = "20200101"
END = "20261104"

# 轴2 的字段顺序，改动必须同步 meta 与下游
FIELDS = ["open", "close", "high", "low", "vol_shares", "amount", "turnover"]

_lock = threading.Lock()


def _log(msg):
    with _lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_universe():
    """在市 + 退市 全量代码（退市股必须纳入，否则幸存者偏差）。"""
    import akshare as ak
    codes = {}
    try:
        df = ak.stock_info_a_code_name()
        for _, r in df.iterrows():
            c = str(r["code"]).zfill(6)
            codes[c] = {"name": r.get("name"), "status": "listed"}
        _log(f"在市 {len(codes)} 只")
    except Exception as e:
        _log(f"⚠️ 在市列表失败 {type(e).__name__}: {e}")

    for fn in ("stock_info_sh_delist", "stock_info_sz_delist"):
        try:
            df = getattr(ak, fn)()
            n0 = len(codes)
            for _, r in df.iterrows():
                c = str(r.iloc[0]).zfill(6)
                if c not in codes:
                    codes[c] = {"name": r.iloc[1], "status": "delisted",
                                "delist_date": str(r.iloc[3])[:10]}
            _log(f"{fn}: 新增退市 {len(codes) - n0} 只")
        except Exception as e:
            _log(f"⚠️ {fn} 失败 {type(e).__name__}: {e}")

    return codes


def fetch_one(code):
    """拉单只 qfq 日线。返回 (code, rows) 或 (code, None)。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = os.path.join(CACHE_DIR, f"{code}.json")
    # 缓存：当天拉过的直接复用（断点续传）
    if os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("start") == START and blob.get("end") == END:
                return code, blob["rows"]
        except Exception:
            pass

    import akshare as ak
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=START, end_date=END, adjust="qfq")
            if df is None or not len(df):
                return code, []
            rows = []
            for _, r in df.iterrows():
                try:
                    rows.append([
                        str(r["日期"])[:10],
                        float(r["开盘"]), float(r["收盘"]),
                        float(r["最高"]), float(r["最低"]),
                        float(r["成交量"]),      # 手，下游 ×100 转股
                        float(r["成交额"]),
                        float(r["换手率"]),
                    ])
                except (TypeError, ValueError):
                    continue
            blob = {"code": code, "start": START, "end": END,
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "rows": rows}
            tmp = cp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False)
            os.replace(tmp, cp)
            return code, rows
        except Exception as e:
            if attempt == 2:
                return code, None
            time.sleep(1.5 * (attempt + 1))
    return code, None


def build(args):
    t0 = time.time()
    uni = get_universe()
    codes = sorted(uni)
    if args.offset:
        codes = codes[args.offset:]
    if args.limit:
        codes = codes[:args.limit]
    _log(f"目标 {len(codes)} 只（offset={args.offset}），区间 {START}~{END}，并发 {args.workers}")

    store, failed, empty = {}, [], []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for fu in as_completed(futs):
            code, rows = fu.result()
            done += 1
            if rows is None:
                failed.append(code)
            elif not rows:
                empty.append(code)
            # ⚠️ 内存纪律（本轮实测血案）：**不要把 rows 留在内存**。
            #   5945 只 × 1655 行 × 8 字段 ≈ 1.9 GB Python 对象，
            #   实测 --limit 300 即被 SIGTERM（Exit 137）。
            #   数据已经落盘在 klines_2020_cache/，组装阶段逐只流式读取即可。
            elif args.fetch_only:
                store[code] = None          # 只记「成功」这个事实，不持有数据
            else:
                store[code] = rows
            if done % 200 == 0 or done == len(codes):
                _log(f"进度 {done}/{len(codes)}  成功 {len(store)}  "
                     f"空 {len(empty)}  失败 {len(failed)}  "
                     f"耗时 {time.time()-t0:.0f}s")

    if not store:
        _log("❌ 全部失败，中止")
        return 1

    if args.fetch_only:
        _log(f"✅ --fetch-only：已写缓存 {len(store)} 只，跳过 npz 组装")
        return 0

    # ── 对齐成密集数组 ──
    alldays = sorted({r[0] for rows in store.values() for r in rows})
    didx = {d: i for i, d in enumerate(alldays)}
    codelist = sorted(store)
    cidx = {c: i for i, c in enumerate(codelist)}
    nD, nC, nK = len(alldays), len(codelist), len(FIELDS)
    _log(f"对齐 {nD} 日 × {nC} 只 × {nK} 字段 = {nD*nC*nK:,} float32 "
         f"≈ {nD*nC*nK*4/1e6:.0f} MB")

    X = np.full((nC, nD, nK), np.nan, dtype=np.float32)
    for c, rows in store.items():
        j = cidx[c]
        for r in rows:
            i = didx.get(r[0])
            if i is None:
                continue
            X[j, i, 0] = r[1]            # open
            X[j, i, 1] = r[2]            # close
            X[j, i, 2] = r[3]            # high
            X[j, i, 3] = r[4]            # low
            X[j, i, 4] = r[5] * 100.0    # 手 → 股（统一口径）
            X[j, i, 5] = r[6]            # amount
            X[j, i, 6] = r[7]            # turnover

    os.makedirs(DATA, exist_ok=True)
    np.savez_compressed(OUT_NPZ,
                        X=X,
                        dates=np.array(alldays),
                        codes=np.array(codelist),
                        fields=np.array(FIELDS))
    _log(f"写出 {OUT_NPZ}  {os.path.getsize(OUT_NPZ)/1e6:.1f} MB")

    # ── 审计 ──
    fin = np.isfinite(X[:, :, 1])
    per_day = fin.sum(axis=0)
    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "akshare stock_zh_a_hist(adjust=qfq)",
        "range": [alldays[0], alldays[-1]],
        "n_days": nD, "n_codes": nC,
        "fields": FIELDS,
        "units": {
            "open/close/high/low": "元（前复权，锚点=最新日）",
            "vol_shares": "股（已由东财『成交量』手 ×100 转换）",
            "amount": "元",
            "turnover": "%",
        },
        "universe": {
            "total": len(codes),
            "fetched_ok": len(store),
            "empty": len(empty), "failed": len(failed),
            "delisted_included": sum(1 for c in store
                                     if uni.get(c, {}).get("status") == "delisted"),
        },
        "failed_sample": failed[:30],
        "empty_sample": empty[:30],
        "coverage": {
            "median_codes_per_day": float(np.median(per_day)),
            "min_codes_per_day": int(per_day.min()),
            "max_codes_per_day": int(per_day.max()),
        },
        "notes": [
            "全区间一次性拉取 → 前复权锚点统一为最新日，无分次拉取漂移",
            "退市股已纳入（防幸存者偏差），status 见 universe.delisted_included",
            "成交量单位已统一为『股』，与旧库 klines_long 的双口径不同，禁止混用",
            "市值不落此文件（百度接口每15天采样，非日频）；需用时按 "
            "shares=vol_shares/(turnover/100) 反推",
        ],
    }
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    _log(f"写出 {OUT_META}")
    _log(f"完成：{nD} 日 × {nC} 只  每交易日股票数 中位 "
         f"{np.median(per_day):.0f} / 最少 {per_day.min()} / 最多 {per_day.max()}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--fetch-only", action="store_true",
                    help="只拉取写缓存，不组装 npz（分批场景用）")
    ap.add_argument("--from-cache", action="store_true",
                    help="直接从 klines_2020_cache 组装 npz，不再联网")
    ap.add_argument("--resume", action="store_true", help="复用 klines_2020_cache")
    args = ap.parse_args()
    if args.from_cache:
        sys.exit(assemble_from_cache())
    sys.exit(build(args))


def assemble_from_cache():
    """从缓存目录组装 npz（不再联网）。

    为什么需要这条路径：本机的长时后台进程会被系统回收，
    无法一次跑完 5945 只。改为「分批 --fetch-only」+「最后 --from-cache 组装」。
    """
    import glob
    t0 = time.time()
    fs = sorted(glob.glob(os.path.join(CACHE_DIR, "*.json")))
    _log(f"缓存文件 {len(fs)} 个，开始组装（流式，不整体载入）")

    # 第一遍：只扫日期与代码，构建索引（不保留行数据）
    codeset, dayset = set(), set()
    valid = []
    for fp in fs:
        try:
            with open(fp, encoding="utf-8") as f:
                blob = json.load(f)
            rows = blob.get("rows") or []
            if not rows:
                continue
            code = os.path.basename(fp)[:-5]
            valid.append(fp)
            codeset.add(code)
            for r in rows:
                dayset.add(r[0])
        except Exception:
            continue
    if not valid:
        _log("❌ 缓存为空")
        return 1

    alldays = sorted(dayset)
    didx = {d: i for i, d in enumerate(alldays)}
    codelist = sorted(codeset)
    cidx = {c: i for i, c in enumerate(codelist)}
    nD, nC, nK = len(alldays), len(codelist), len(FIELDS)
    _log(f"对齐 {nD} 日 × {nC} 只 × {nK} 字段 ≈ {nD*nC*nK*4/1e6:.0f} MB")

    X = np.full((nC, nD, nK), np.nan, dtype=np.float32)
    # 第二遍：逐只读、逐只填、读完即丢（峰值内存 = 单只）
    for k, fp in enumerate(valid):
        try:
            with open(fp, encoding="utf-8") as f:
                blob = json.load(f)
            rows = blob.get("rows") or []
            j = cidx[os.path.basename(fp)[:-5]]
            for r in rows:
                i = didx.get(r[0])
                if i is None:
                    continue
                X[j, i, 0] = r[1]; X[j, i, 1] = r[2]; X[j, i, 2] = r[3]
                X[j, i, 3] = r[4]; X[j, i, 4] = r[5] * 100.0
                X[j, i, 5] = r[6]; X[j, i, 6] = r[7]
            del blob, rows
        except Exception:
            continue
        if (k + 1) % 500 == 0:
            _log(f"  组装 {k+1}/{len(valid)}  耗时 {time.time()-t0:.0f}s")

    np.savez_compressed(OUT_NPZ, X=X,
                        dates=np.array(alldays), codes=np.array(codelist),
                        fields=np.array(FIELDS))
    _log(f"写出 {OUT_NPZ}  {os.path.getsize(OUT_NPZ)/1e6:.1f} MB")

    fin = np.isfinite(X[:, :, 1])
    per_day = fin.sum(axis=0)
    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "akshare stock_zh_a_hist(adjust=qfq)",
        "range": [alldays[0], alldays[-1]],
        "n_days": nD, "n_codes": nC, "fields": FIELDS,
        "units": {"open/close/high/low": "元（前复权，锚点=最新日）",
                  "vol_shares": "股（东财成交量手 ×100）",
                  "amount": "元", "turnover": "%"},
        "coverage": {"median_codes_per_day": float(np.median(per_day)),
                     "min_codes_per_day": int(per_day.min()),
                     "max_codes_per_day": int(per_day.max())},
        "notes": ["全区间一次性拉取 → 前复权锚点统一为最新日",
                  "退市股已纳入（防幸存者偏差）",
                  "成交量单位统一为『股』，与旧库双口径不同，禁止混用",
                  "市值需按 shares=vol_shares/(turnover/100) 反推"],
    }
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    _log(f"完成组装：{nD} 日 × {nC} 只  每交易日中位 {np.median(per_day):.0f} 只  耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    main()
