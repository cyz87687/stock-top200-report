#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_klines_latest.py —— 给长价格库补最新交易日（增量，不重抓历史）

背景：data/klines_long.json 由 fetch_klines_long.py 维护，但那是**全量 35 分钟**
的重抓。日常只需把最新 1~N 个交易日追加进去，历史不动。

本脚本只做增量合并：
  1. 读现有 data/klines_long.json
  2. 对每个代码只拉最近 need 根（默认 8），**仅追加库中不存在的日期**
  3. 已有日期一律不覆盖（防止前复权因子变化导致历史被静默改写）
     ⚠️ 前复权价的固有性质：除权后历史价格会整体平移。本脚本**故意不重写历史**
        以保住已归档面板的可复现性；如需重算，显式用 --rebuild。

用法
  python append_klines_latest.py                 # 追加最新交易日
  python append_klines_latest.py --dry-run       # 只看将追加多少
  python append_klines_latest.py --need 30       # 多拉几根（补几天缺口）
  python append_klines_latest.py --until 2026-11-06   # 只补到指定日（补历史缺口/盘中补跑）

## 盘中污染保护（--until 的由来）

腾讯 `fqkline` 在**盘中**就返回当日行，且价与量都是**未收盘的瞬时值**
（实测 2026-11-09 10:50 茅台当日 vol=12532 手，而 11-06 整日 42848 手）。
本脚本「已有日期绝不覆盖」，一旦盘中缺陷跑，那条半日 K 线会被**永久写死**，
事后收盘也不会被修正 —— 污染不可逆。

因此：
  1. `--until` 硬上限：晚于该日期的 bar 一律丢弃
  2. 未显式给 `--until` 时，若当前处于**交易时段内**（工作日 09:30~15:05），
     自动把「当天」排除并打印警告（收盘后跑不受影响，保留原行为）

日链正常在收盘后运行，不需要传参；**盘中手工补跑必须显式 `--until`**。
"""
import os
import sys
import json
import time
import argparse
import datetime
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import kline_cutoff      # noqa: E402  日期上限判据的单一实现

KLONG = os.path.join(HERE, "data", "klines_long.json")
TQ_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn",
            "https://proxy.finance.qq.com/ifzqgtimg"]
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")


def _fetch_sina(sym, need=8):
    """新浪个股日线备源。返回 (bars, vol_scale)。

    ⚠️ 三个必须记住的性质：
      1. **个股日线滞后 T-1**（MEMORY 已记：新浪 getKLineData 当日不出）。
         补 11-06 这类历史日期时它**天然安全**，不可能带进 11-09 半日价。
         但**不能依赖**它当保护 —— 硬保护始终是 cutoff。
      2. `volume` 单位是**股**，而库内口径是 `kline_units` 定义的
         **688=股 / 其余=手**。故这里把 (date,o,c,h,l,vol) 算成「库口径」，
         需要乘的系数由返回的 `vol_scale` 表达，**不在 fetch 内部硬编码**
         （硬编码 /100 会把 688 算错 100 倍 —— round116 血案同型）。
      3. `strict_until` 由调用方统一施加，此函数不做日期过滤。
    """
    url = SINA_URL.format(sym=sym, n=int(need))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            txt = r.read().decode("utf-8", "ignore")
    except Exception:
        return None, 1.0
    try:
        arr = json.loads(txt)
    except Exception:
        return None, 1.0
    if not isinstance(arr, list) or not arr:
        return None, 1.0
    out = []
    for k in arr:
        try:
            out.append((str(k["day"])[:10], float(k["open"]), float(k["close"]),
                        float(k["high"]), float(k["low"]), float(k["volume"])))
        except Exception:
            continue
    if not out:
        return None, 1.0
    # 新浪给的是「股」；库口径 688 要股（×1）、其余要手（÷100）
    scale = 1.0 if sym.startswith("sh688") else 0.01
    return out, scale


def fetch_ohlcv(sym, need=8):
    """返回 (bars, vol_scale)。bar = (date, open, close, high, low, vol_raw)。

    `vol_raw * vol_scale` 才是**库口径**成交量（688=股，其余=手）。
    先腾讯（当日可得），全域名失败则回退新浪。
    """
    bars, scale = _fetch_tx(sym, need)
    if bars:
        return bars, scale
    return _fetch_sina(sym, need)


def _fetch_tx(sym, need=8):
    for _host in TQ_HOSTS:
        url = f"{_host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                txt = r.read().decode("utf-8")
        except Exception:
            continue
        if txt.lstrip().startswith("<"):
            continue
        try:
            data = json.loads(txt)
            kdata = data.get("data", {}).get(sym, {}) or {}
            raw = kdata.get("qfqday") or kdata.get("day")
        except Exception:
            continue
        if not raw:
            continue
        out = []
        for k in raw:
            try:
                out.append((str(k[0])[:10], float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5])))
            except Exception:
                continue
        if out:
            # 腾讯原值**已经是库口径**（688=股、其余=手）→ scale 恒为 1
            return out, 1.0
    return None, 1.0


def sym_of(code6):
    if code6.startswith(("60", "68", "51", "58", "11", "50")):
        return "sh" + code6
    if code6.startswith(("00", "30", "12", "15", "16")):
        return "sz" + code6
    if code6.startswith(("8", "4", "92")):
        return "bj" + code6
    return "sh" + code6


def _now_cst():
    """当前北京时间（不依赖本机时区设置）。"""
    return kline_cutoff.now_cst()


def resolve_cutoff(until, now=None):
    """日期硬上限 —— 判据单一实现，见 kline_cutoff.resolve_cutoff。

    保留本函数只为兼容既有测试与调用点，逻辑不在此重复。
    """
    cut, reason = kline_cutoff.resolve_cutoff(until, now)
    if not until and cut:
        reason = reason.replace("未指定上限且当前", "未传 --until 且当前")
    elif until:
        reason = "显式 --until"
    return cut, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0,
                    help="分片起始下标（含），配合 --end 使用；默认 0 不影响现有调用")
    ap.add_argument("--end", type=int, default=0,
                    help="分片结束下标（不含）；0=到末尾。用于长任务分片前台跑，避免单条超时被 SIGTERM")
    ap.add_argument("--until", default="",
                    help="日期硬上限 YYYY-MM-DD，晚于该日的 bar 一律丢弃（盘中补跑必用）")
    args = ap.parse_args()

    cutoff, cutoff_reason = resolve_cutoff(args.until)

    print("=" * 96)
    print("append_klines_latest · 增量补最新交易日")
    print("=" * 96)
    if cutoff:
        print(f"🔒 日期上限：{cutoff}（{cutoff_reason}）→ 晚于该日的 bar 丢弃")
    else:
        print(f"ℹ️  日期上限：无（{cutoff_reason}）")

    raw = json.load(open(KLONG, encoding="utf-8"))
    codes = sorted({str(c)[-6:] for c in raw if str(c)[-6:].isdigit()})
    total_codes = len(codes)
    if args.limit:
        codes = codes[:args.limit]
    elif args.end:
        # 分片：codes[start:end]。已有日期绝不覆盖，每片写回都累积，故分片等价于全量。
        codes = codes[args.start:args.end]
    elif args.start:
        codes = codes[args.start:]
    print(f"价格库：{len(raw)} 键 → {len(codes)} 个 6 位代码"
          + (f"（分片 {args.start}:{args.end or total_codes} / 共 {total_codes}）"
             if (args.start or args.end) else ""))

    # 现有最新日期
    alld = set()
    for c, m in raw.items():
        alld.update(m.keys())
    dmax = max(alld) if alld else None
    print(f"库内最新日期：{dmax}，总日期数 {len(alld)}")

    added_dates = set()
    n_new = 0
    n_fail = 0
    n_codes_touched = 0
    n_cut = 0                      # 被日期上限丢弃的 bar 数
    cut_dates = set()
    t0 = time.time()
    for i, c6 in enumerate(codes):
        sym = sym_of(c6)
        bars, vol_scale = fetch_ohlcv(sym, args.need)
        if not bars:
            n_fail += 1
            continue
        # 找对应键（可能是 6 位或带前缀）
        key = None
        if c6 in raw:
            key = c6
        else:
            for k in raw:
                if str(k)[-6:] == c6:
                    key = k
                    break
        if key is None:
            raw[c6] = {}
            key = c6
        m = raw[key]
        touched = False
        for bar in bars:
            d, o, cl, hi, lo, vol = bar
            if not kline_cutoff.bar_allowed(d, cutoff):
                n_cut += 1                 # 盘中未收盘价 / 超出补跑范围 → 一律丢弃
                cut_dates.add(d)
                continue
            if d in m:
                continue                     # 已有日期绝不覆盖
            m[d] = [o, cl, hi, lo, vol * vol_scale]
            added_dates.add(d)
            n_new += 1
            touched = True
        if touched:
            n_codes_touched += 1
        if (i + 1) % 200 == 0:
            el = time.time() - t0
            print(f"   … {i+1}/{len(codes)}  新增 {n_new} 点  失败 {n_fail}  "
                  f"用时 {el:.0f}s", flush=True)

    print()
    print(f"完成：{n_codes_touched} 只被追加，新增 {n_new} 个价格点，失败 {n_fail} 只")
    if n_cut:
        print(f"🔒 按日期上限丢弃 {n_cut} 个 bar（日期：{sorted(cut_dates)}）")
    if added_dates:
        print(f"新增日期：{sorted(added_dates)}")
    else:
        print("无新增日期（库已是最新）")

    if args.dry_run:
        print("--dry-run：不写文件")
        return
    if n_new == 0:
        print("无变化，跳过写盘")
        return
    with open(KLONG, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    print(f"✅ 已写回 {KLONG} ({os.path.getsize(KLONG)/1e6:.2f} MB)")
    print("=" * 96)


if __name__ == "__main__":
    main()
