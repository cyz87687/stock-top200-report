#!/usr/bin/env python3
"""新浪接口获取全A成交额TOP200 - 东方财富被限流时的备用方案
v2.13 增强:
  1. 数据质量校验: 成交额全0/920段北交所异常占位数据 → 判定失败, 不覆盖已有数据
  2. 备源: 校验失败时改用 akshare 东方财富全A快照按成交额取TOP200
  3. 双源均失败 → 保留旧 top200_all_a.json 并 exit 1 (工作流重试)
"""
import urllib.request
import json
import time
import sys
import os
from datetime import datetime

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
OUT = "top200_all_a.json"


def fetch_page(page, num=80):
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"Market_Center.getHQNodeData?page={page}&num={num}&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=sort")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  第{page}页失败(尝试{attempt+1}/3): {e}")
            time.sleep(2)
    return []


def validate_data(stocks):
    """数据质量校验: 返回 (ok, reason)"""
    if not stocks:
        return False, "空数据"
    n = len(stocks)
    # 成交额有效(>0) 数量
    valid_amt = sum(1 for s in stocks if float(s.get("amount", 0) or 0) > 0)
    amt_ratio = valid_amt / n
    if amt_ratio < 0.8:
        return False, f"成交额有效占比过低 {valid_amt}/{n} ({amt_ratio:.0%}), 疑似接口占位/异常数据"
    # 920段(北交所新代码)占比过高且价格全0 → 异常
    bj = sum(1 for s in stocks if (s.get("symbol", "") or "").startswith("sz92") or s.get("code", "").startswith("920"))
    zero_price = sum(1 for s in stocks if float(s.get("trade", 0) or 0) <= 0)
    if bj > n * 0.5 and zero_price > n * 0.8:
        return False, f"920段北交所占位数据 {bj}/{n} 且价格全0, 判定异常"
    if zero_price > n * 0.8:
        return False, f"价格全0占位数据 {zero_price}/{n}, 判定异常"
    return True, f"通过 (有效成交额 {valid_amt}/{n})"


def fallback_eastmoney():
    """备源: akshare 东方财富全A快照, 按成交额降序取TOP200"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None, "东财快照为空"
        df = df.dropna(subset=["成交额"])
        df = df[df["成交额"] > 0]
        df = df.sort_values("成交额", ascending=False).head(200)
        out = []
        for _, r in df.iterrows():
            code_raw = str(r["代码"])
            if code_raw.startswith("6"):
                code = "sh" + code_raw
            elif code_raw.startswith(("0", "3")):
                code = "sz" + code_raw
            else:
                code = "sz" + code_raw  # 北交所备源兜底(极少)
            price = float(r.get("最新价", 0) or 0)
            pct = float(r.get("涨跌幅", 0) or 0)
            amount = float(r.get("成交额", 0) or 0)
            out.append({
                "name": str(r.get("名称", "")),
                "code": code,
                "price": round(price, 2),
                "pct_chg": round(pct, 2),
                "turnover": amount,
                "amount": amount,
            })
        return out, f"东财备源 {len(out)} 只"
    except Exception as e:
        return None, f"东财备源失败: {e}"


def normalize(stocks, start_rank=1):
    """新浪行情条目 → 标准化 dict 列表（含 rank 排名）"""
    out = []
    for i, s in enumerate(stocks, start_rank):
        code_raw = s.get("code", "")
        symbol = s.get("symbol", "")
        if symbol.startswith("sh"):
            code = symbol
        elif symbol.startswith("sz"):
            code = symbol
        else:
            market = "sh" if code_raw.startswith("6") else "sz"
            code = f"{market}{code_raw}"
        out.append({
            "rank": i,
            "name": s.get("name", ""),
            "code": code,
            "price": float(s.get("trade", 0) or 0),
            "pct_chg": round(float(s.get("changepercent", 0) or 0), 2),
            "turnover": float(s.get("amount", 0) or 0),
            "amount": float(s.get("amount", 0) or 0),
        })
    return out


def main():
    print("=" * 60)
    print("  新浪接口获取全A成交额TOP1000 (主力池TOP200 + 观察池200~1000)")
    print("=" * 60)
    all_stocks = []
    for page in range(1, 11):
        print(f"获取第{page}页...")
        data = fetch_page(page, num=100)
        if not data:
            continue
        all_stocks.extend(data)
        print(f"  累计 {len(all_stocks)} 只")
        time.sleep(0.8)

    all_stocks.sort(key=lambda x: -float(x.get("amount", 0)))
    top1000 = all_stocks[:1000]
    top200 = top1000[:200]
    print(f"\n✅ 共获取 {len(top1000)} 只（含 TOP200 主力池）")

    # ===== 数据质量校验（针对主力池 TOP200）=====
    ok, reason = validate_data(top200)
    if not ok:
        print(f"⚠️ 新浪数据校验失败: {reason}")
        print("🔄 尝试东财备源...")
        out, fb = fallback_eastmoney()
        if out:
            ok2, reason2 = validate_data([{"symbol": s["code"], "code": s["code"],
                                           "trade": s["price"], "amount": s["amount"]} for s in out])
            if ok2:
                print(f"✅ {fb}, 校验通过 ({reason2})")
                # 东财备源只给 200 只，观察池暂缺（保底）
                top200_norm = out
                top1000_norm = out
            else:
                print(f"❌ 东财备源校验也失败: {reason2}")
                sys.exit(1)
        else:
            print(f"❌ {fb}")
            sys.exit(1)
    else:
        top200_norm = normalize(top200, 1)
        top1000_norm = normalize(top1000, 1)
    # 观察池 = 成交额 200~1000 名（800 只）
    watch_pool = top1000_norm[200:]

    _src = "新浪财经全市场行情接口" if "sh" in (top200_norm[0]["code"] if top200_norm else "") or "sz" in (top200_norm[0]["code"] if top200_norm else "") else "东财备源"
    _today = datetime.now().strftime("%Y-%m-%d")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "top50": top200_norm[:50],
            "top100": top200_norm[:100],
            "top200": top200_norm,
            "all_count": len(all_stocks),
            "date": _today,
            "source": _src,
        }, f, ensure_ascii=False, indent=2)

    OUT1000 = "top1000_all_a.json"
    with open(OUT1000, "w", encoding="utf-8") as f:
        json.dump({
            "date": _today,
            "source": _src,
            "main_pool": top200_norm,          # 主力池 TOP200
            "watch_pool": watch_pool,          # 观察池 200~1000
            "stocks": top1000_norm,            # 全量 1000
            "count": len(top1000_norm),
        }, f, ensure_ascii=False, indent=2)

    print(f"\n📊 主力池 TOP10:")
    for i, s in enumerate(top200_norm[:10]):
        print(f"  {i+1}. {s['name']:8s} {s['code']} {s['price']:8.2f} {s['pct_chg']:+.2f}% 成交额{s['turnover']/10000:.0f}万")
    print(f"\n✅ 数据已保存: {OUT} (主力池{len(top200_norm)}) + {OUT1000} (观察池{len(watch_pool)})")


if __name__ == "__main__":
    main()
