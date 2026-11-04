#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_klines_sina_fallback.py —— K线库增量补齐（新浪主源 + 腾讯备源）

背景（2026-11-02 事故）：
    append_klines_latest.py 依赖腾讯 ifzq 接口，腾讯侧 WAF 对该 IP 返回
    HTTP 501（waf.tencent.com/501page.html），三个 host 全部失效。
    而该脚本的判定逻辑把「全部抓取失败（n_fail=2138, n_new=0）」
    与「库已是最新」走同一分支 → 打印「无新增日期（库已是最新）」
    exit 0 → 日链静默跳过，K线库停在 2026-10-30，技术面维度用旧数据。

    这正是项目铁律里「静默失败」类事故：**告警必须与成功可区分**。

本脚本设计：
  1. 主源新浪（scale=240 日线，返回 open/high/low/close/volume）
     备源腾讯（若恢复可用）
  2. 已有日期一律不覆盖（前复权平移风险，同 append_klines_latest.py）
  3. **失败即非 0 退出**：抓取失败率 > 阈值（默认 20%）时 exit 2，
     日志明确写 FAILED，绝不再伪装成「已是最新」
  4. --dry-run / --limit 支持小样本验证

用法：
    python append_klines_sina_fallback.py --need 8
    python append_klines_sina_fallback.py --dry-run --limit 5
"""
import os
import sys
import json
import time
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
KLONG = os.path.join(HERE, "data", "klines_long.json")


def _curl(url, timeout=20):
    r = subprocess.run(
        ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout),
         "-A", "Mozilla/5.0", url],
        capture_output=True, text=True)
    return r.stdout or ""


def sym_of(code6):
    if code6.startswith(("60", "68", "51", "58", "11", "50")):
        return "sh" + code6
    if code6.startswith(("00", "30", "12", "15", "16")):
        return "sz" + code6
    if code6.startswith(("8", "4", "92")):
        return "bj" + code6
    return "sh" + code6


def fetch_sina(sym, need=8):
    """新浪日线 → [(date, open, close, high, low, vol)] 或 None"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={need}")
    txt = _curl(url)
    if not txt or not txt.lstrip().startswith("["):
        return None
    try:
        rows = json.loads(txt)
    except Exception:
        return None
    out = []
    for k in rows:
        try:
            out.append((str(k["day"])[:10], float(k["open"]), float(k["close"]),
                        float(k["high"]), float(k["low"]), float(k["volume"])))
        except Exception:
            continue
    return out or None


def fetch_tencent(sym, need=8):
    """腾讯备源（WAF 恢复时可用）"""
    for host in ("https://web.ifzq.gtimg.cn", "https://ifzq.gtimg.cn"):
        url = f"{host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        txt = _curl(url)
        if not txt or txt.lstrip().startswith("<"):
            continue
        try:
            d = json.loads(txt)
            kd = d.get("data", {}).get(sym, {}) or {}
            raw = kd.get("qfqday") or kd.get("day")
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
            return out
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fail-threshold", type=float, default=0.20,
                    help="抓取失败率超过此值 → exit 2（默认 0.20）")
    args = ap.parse_args()

    print("=" * 96)
    print("append_klines_sina_fallback · K线增量（新浪主源）")
    print("=" * 96, flush=True)

    raw = json.load(open(KLONG, encoding="utf-8"))
    codes = sorted({str(c)[-6:] for c in raw if str(c)[-6:].isdigit()})
    if args.limit:
        codes = codes[:args.limit]
    print(f"价格库：{len(raw)} 键 → {len(codes)} 个 6 位代码")

    alld = set()
    for c, m in raw.items():
        alld.update(m.keys())
    dmax = max(alld) if alld else None
    print(f"库内最新日期：{dmax}，总日期数 {len(alld)}", flush=True)

    added_dates = set()
    n_new = 0
    n_fail = 0
    n_touched = 0
    t0 = time.time()

    for i, c6 in enumerate(codes):
        sym = sym_of(c6)
        bars = fetch_sina(sym, args.need)
        src = "sina"
        if not bars:
            bars = fetch_tencent(sym, args.need)
            src = "tencent"
        if not bars:
            n_fail += 1
            continue

        key = c6 if c6 in raw else None
        if key is None:
            for k in raw:
                if str(k)[-6:] == c6:
                    key = k
                    break
        if key is None:
            raw[c6] = {}
            key = c6
        m = raw[key]
        touched = False
        for d, o, cl, hi, lo, vol in bars:
            if d in m:
                continue
            m[d] = [o, cl, hi, lo, vol]
            added_dates.add(d)
            n_new += 1
            touched = True
        if touched:
            n_touched += 1

        if (i + 1) % 200 == 0:
            print(f"   … {i+1}/{len(codes)}  新增 {n_new} 点  失败 {n_fail}  "
                  f"用时 {time.time()-t0:.0f}s", flush=True)

    total = len(codes)
    fail_rate = n_fail / total if total else 0.0
    print()
    print(f"完成：{n_touched} 只被追加，新增 {n_new} 个价格点，失败 {n_fail} 只 "
          f"({fail_rate*100:.1f}%)")
    if added_dates:
        ds = sorted(added_dates)
        print(f"新增日期：{ds if len(ds) <= 10 else ds[:5] + ['...'] + ds[-5:]}")

    # ⚠️ 关键：失败与"已是最新"必须可区分
    if fail_rate > args.fail_threshold:
        print(f"❌ FAILED: 抓取失败率 {fail_rate*100:.1f}% > 阈值 "
              f"{args.fail_threshold*100:.0f}% —— 数据源不可用，K线库未更新")
        sys.exit(2)

    if n_new == 0:
        # 真·最新：抽样验证最新日期确为预期
        print("无新增价格点（失败率在阈值内，判定库已是最新）")
        if args.dry_run:
            print("--dry-run：不写文件")
        sys.exit(0)

    if args.dry_run:
        print("--dry-run：不写文件")
        sys.exit(0)

    if added_dates and dmax:
        latest = max(added_dates)
        if latest <= dmax:
            print(f"⚠️ 新增日期 {latest} 未超过库内最新 {dmax}，疑似历史缺口回补")

    tmp = KLONG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    os.replace(tmp, KLONG)
    print(f"✅ 已写回 {KLONG} ({os.path.getsize(KLONG)/1e6:.2f} MB)")
    print("=" * 96)


if __name__ == "__main__":
    main()
