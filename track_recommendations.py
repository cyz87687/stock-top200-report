#!/usr/bin/env python3
"""
推荐跟踪 (P0-4): 记录每日推荐/组合快照, 回填 T+1/T+5/T+20 收益, 统计评级胜率。

设计:
1. 每日从 scored json 中抽取"推荐标的"(S/A级)与"报告组合"(AI组合 + 量化组合), 打标签存档;
2. 后续运行(次日或手动)时, 用真实K线回填各标的相对推荐日的 T+N 收盘涨跌幅;
3. 汇总各评级 / 各组合的命中率与平均超额收益 → 输出 recommendation_track.json + 控制台摘要。

数据纪律: 收盘涨跌幅全部来自真实K线(腾讯前复权, 新浪兜底), 无K线则留空不编造。

用法:
  python3 track_recommendations.py                  # 用最新 scored json 记录 + 回填
  python3 track_recommendations.py <scored.json>    # 指定输入
  python3 track_recommendations.py --report         # 只打印已存档统计(不抓K线)
"""
import os
import sys
import json
import glob
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK_FILE = os.path.join(HERE, "recommendation_track.json")
SECTOR_DIR = os.path.join(HERE, "sector")

# 清除可能干扰的代理
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

TENCENT_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HORIZONS = [1, 5, 20]


def _sym(code):
    """归一化为腾讯/新浪 K线所需的符号格式 (sh600519 / sz300450 / bj430047)。

    兼容两种输入:
      - 带市场前缀: 'sz300450' / 'SH600519'  → 直接归一化前缀大小写后返回
      - 纯数字:    '300450' / '600519'      → 按首位数字推断市场
    历史 bug: scored json 的 code 已是 'sz300450' 形式, 旧实现对其 zfill 后
    用 startswith 判断数字前缀, 全部失配返回 None, 导致回填恒为 0 个收益点。
    """
    c = str(code).strip().lower()
    # 情况1: 已带前缀 (sh/sz/bj)
    if len(c) > 2 and c[:2] in ("sh", "sz", "bj") and c[2:].isdigit():
        return c
    c = str(code).strip().zfill(6)
    if c.startswith(('60', '68', '9', '51', '58')):
        return 'sh' + c
    if c.startswith(('00', '30', '20', '1', '15', '16')):
        return 'sz' + c
    if c.startswith(('8', '4', '92')):
        return 'bj' + c
    return None


def fetch_kline(code, need=60):
    """→ {date: close} 前复权日线 (腾讯多域名轮询 + 新浪兜底)

    数据纪律: 全部来自真实接口; 任一源失败则尝试下一个; 全失败返回 {} (留空不编造)。
    """
    sym = _sym(code)
    if not sym:
        return {}
    # --- 源1: 腾讯 (多域名轮询, ifzq.gtimg.cn 为准) ---
    for host in TENCENT_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode('utf-8')
            if txt.lstrip().startswith('<'):
                continue
            j = json.loads(txt)
            d = j.get('data', {}).get(sym, {})
            kl = d.get('qfqday') or d.get('day') or []
            out = {}
            for x in kl:
                if len(x) >= 3:
                    try:
                        out[str(x[0])] = float(x[2])
                    except (ValueError, TypeError):
                        continue
            if out:
                return out
        except Exception:
            continue
    # --- 源2: 新浪 (不复权, 最后手段) ---
    try:
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={max(need, 60)}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode('utf-8')
        arr = json.loads(txt)
        out = {}
        for x in (arr or []):
            if isinstance(x, dict) and x.get('day') and x.get('close') is not None:
                try:
                    out[str(x['day'])] = float(x['close'])
                except (ValueError, TypeError):
                    continue
        if out:
            return out
    except Exception:
        pass
    return {}


def load_track():
    if os.path.exists(TRACK_FILE):
        try:
            return json.load(open(TRACK_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"records": []}


def save_track(t):
    json.dump(t, open(TRACK_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _find_latest_scored(explicit=None):
    if explicit:
        return explicit
    fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
          if "_watch_" not in f and ".bak" not in f]
    return fs[0] if fs else None


def record_snapshot(scored_path):
    """从 scored json 抽取当日推荐快照"""
    data = json.load(open(scored_path, encoding="utf-8"))
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
    results = data.get("results", [])
    name_by = {r["name"]: r for r in results}

    recs = []
    # 1) S/A 级个股(评级推荐)
    for r in results:
        if r.get("rating") in ("S", "A"):
            recs.append({
                "kind": "rating", "label": r["rating"], "name": r["name"], "code": r["code"],
                "base_price": r.get("price"), "total": r.get("total"),
            })
    # 2) AI 组合
    try:
        ai = json.load(open(os.path.join(HERE, "ai_assessment.json"), encoding="utf-8"))
        if date in str(ai.get("date", "")):
            for p in (ai.get("portfolio") or {}).get("picks", []):
                nm = p.get("name") if isinstance(p, dict) else p
                r = name_by.get(nm)
                if r:
                    recs.append({"kind": "ai_portfolio", "label": "AI组合", "name": nm,
                                 "code": r["code"], "base_price": r.get("price"), "total": r.get("total")})
    except Exception:
        pass
    # 3) 量化组合(A动量/B反转)
    try:
        qf = os.path.join(SECTOR_DIR, f"portfolio_{date}.json")
        if not os.path.exists(qf):
            qf = os.path.join(SECTOR_DIR, "portfolio_latest.json")
        if os.path.exists(qf):
            qj = json.load(open(qf, encoding="utf-8"))
            for mode, pf in (qj.get("portfolios") or {}).items():
                picks = pf.get("picks") if isinstance(pf, dict) else pf
                for p in (picks or []):
                    nm = p.get("name")
                    r = name_by.get(nm)
                    if r:
                        recs.append({"kind": f"quant_{mode}", "label": f"量化·{mode}", "name": nm,
                                     "code": r["code"], "base_price": r.get("price"), "total": r.get("total")})
    except Exception:
        pass

    track = load_track()
    # 去重: 同 date + kind + code 只记一次
    exist = {(x["date"], x["kind"], x["code"]) for x in track["records"]}
    added = 0
    for rc in recs:
        key = (date, rc["kind"], rc["code"])
        if key in exist:
            continue
        rc.update({"date": date, "pick_price": rc.get("base_price"),
                   "rets": {str(h): None for h in HORIZONS}})
        track["records"].append(rc)
        added += 1
    save_track(track)
    print(f"📝 记录 {date} 推荐快照: 新增 {added} 条 (共 {len(track['records'])} 条)")
    return track


def _last_closed_trade_date():
    """返回「已收盘的最新交易日」(北京时间为准), 格式 YYYY-MM-DD。

    数据纪律要点: 腾讯/新浪 K 线在盘中会返回当日实时价(未收盘), 若直接用作 T+N
    收盘价会把盘中波动误当收盘收益。故:
      - 北京时间 15:00 前 → 当日未收盘, 截止日回退到前一日;
      - 再回退跳过周末(节假日无法在此精确判断, 由 K 线实际存在的日期兜底过滤)。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    bj = _dt.now(_tz.utc) + _td(hours=8)
    d = bj.date()
    if bj.hour < 15:
        d = d - _td(days=1)
    while d.weekday() >= 5:
        d = d - _td(days=1)
    return d.strftime('%Y-%m-%d')


def backfill(track):
    """用真实K线回填 T+N 收益 (仅使用已收盘交易日数据)"""
    # 按 code 聚合, 每 code 只抓一次K线
    codes = {x["code"] for x in track["records"]}
    kl_cache = {}
    cutoff = _last_closed_trade_date()
    print(f"📈 回填收益: 涉及 {len(codes)} 只个股 (数据截止 {cutoff}, 盘中不回填当日) ...")
    for i, c in enumerate(sorted(codes), 1):
        kl_cache[c] = fetch_kline(c)
        if i % 20 == 0:
            print(f"   ... {i}/{len(codes)}")
        time.sleep(0.05)

    filled = 0
    for rec in track["records"]:
        kl = kl_cache.get(rec["code"]) or {}
        if not kl:
            continue
        rec.setdefault("rets", {str(h): None for h in HORIZONS})
        # 关键: 剔除尚未收盘的当日K线(盘中价), 只保留 <= cutoff 的已收盘交易日
        dates = sorted(d for d in kl.keys() if d <= cutoff)
        base_d = rec["date"]
        # 找推荐日或之后首个已收盘交易日
        try:
            idx = next(i for i, d in enumerate(dates) if d >= base_d)
        except StopIteration:
            continue
        base_close = rec.get("pick_price") or kl[dates[idx]]
        for h in HORIZONS:
            j = idx + h
            if j < len(dates):
                ret = (kl[dates[j]] - base_close) / base_close * 100 if base_close else None
                rec["rets"][str(h)] = round(ret, 2) if ret is not None else None
                filled += 1
            else:
                rec["rets"][str(h)] = None
    save_track(track)
    print(f"✅ 回填完成: {filled} 个收益点")
    return track


def summarize(track):
    """输出统计: 各评级/组合的命中率与平均收益"""
    from collections import defaultdict
    agg = defaultdict(lambda: {str(h): [] for h in HORIZONS})
    for rec in track["records"]:
        for h in HORIZONS:
            v = rec["rets"].get(str(h))
            if v is not None:
                agg[rec["kind"]][str(h)].append(v)
                agg["__ALL__"][str(h)].append(v)

    lines = ["", "=" * 66, "📊 推荐跟踪统计 (T+N 收盘涨跌幅, 真实K线)", "=" * 66,
             f"{'分组':<16}{'样本':>5}{'T+1均':>9}{'T+5均':>9}{'T+20均':>9}{'T+5胜率':>9}", "-" * 66]
    order = ["__ALL__", "rating", "ai_portfolio", "quant_momentum", "quant_reversal"]
    kinds = order + [k for k in agg if k not in order]
    for k in kinds:
        if k not in agg:
            continue
        d = agg[k]
        n5 = len(d["5"])
        if not d["1"] and not d["5"] and not d["20"]:
            continue
        avg = lambda h: (sum(d[h]) / len(d[h])) if d[h] else float('nan')
        win = (sum(1 for x in d["5"] if x > 0) / n5 * 100) if n5 else float('nan')
        lbl = "全部推荐" if k == "__ALL__" else ("评级S/A" if k == "rating" else k)
        lines.append(f"{lbl:<16}{len(d['1']):>5}{avg('1'):>8.2f}%{avg('5'):>8.2f}%{avg('20'):>8.2f}%{win:>8.1f}%")
    lines.append("=" * 66)
    print("\n".join(lines))


def main():
    args = sys.argv[1:]
    report_only = "--report" in args
    explicit = next((a for a in args if not a.startswith("--")), None)

    track = load_track()
    if not report_only:
        sp = _find_latest_scored(explicit)
        if sp:
            track = record_snapshot(sp)
            track = backfill(track)
    summarize(track)


if __name__ == "__main__":
    main()
