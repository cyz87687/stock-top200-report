#!/usr/bin/env python3
"""新浪接口获取全A成交额TOP200 - 东方财富被限流时的备用方案"""
import urllib.request
import json
import time
from datetime import datetime

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

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

def main():
    print("=" * 60)
    print("  新浪接口获取全A成交额TOP200")
    print("=" * 60)
    all_stocks = []
    for page in range(1, 5):
        print(f"获取第{page}页...")
        data = fetch_page(page, num=80)
        if not data:
            continue
        all_stocks.extend(data)
        print(f"  累计 {len(all_stocks)} 只")
        time.sleep(1)

    all_stocks.sort(key=lambda x: -float(x.get("amount", 0)))
    top200 = all_stocks[:200]
    print(f"\n✅ 共获取 {len(top200)} 只TOP股票")

    out = []
    for s in top200:
        code_raw = s.get("code", "")
        symbol = s.get("symbol", "")
        if symbol.startswith("sh"):
            code = symbol
        elif symbol.startswith("sz"):
            code = symbol
        else:
            market = "sh" if code_raw.startswith("6") else "sz"
            code = f"{market}{code_raw}"

        price = float(s.get("trade", 0) or 0)
        pct = float(s.get("changepercent", 0) or 0)
        amount = float(s.get("amount", 0) or 0)
        turnover = amount
        name = s.get("name", "")

        out.append({
            "name": name,
            "code": code,
            "price": price,
            "pct_chg": round(pct, 2),
            "turnover": turnover,
            "amount": amount,
        })

    with open("top200_all_a.json", "w", encoding="utf-8") as f:
        json.dump({
            "top50": out[:50],
            "top100": out[:100],
            "top200": out,
            "all_count": len(all_stocks),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source": "新浪财经全市场行情接口"
        }, f, ensure_ascii=False, indent=2)

    print(f"\n📊 TOP10:")
    for i, s in enumerate(out[:10]):
        print(f"  {i+1}. {s['name']:8s} {s['code']} {s['price']:8.2f} {s['pct_chg']:+.2f}% 成交额{s['turnover']/10000:.0f}万")
    print(f"\n✅ 数据已保存: top200_all_a.json")

if __name__ == "__main__":
    main()
