#!/usr/bin/env python3
"""新浪接口获取全A成交额 TOP500 主力池 (v3.0 扩池, 观察池已删除)
v2.13 增强:
  1. 数据质量校验: 成交额全0/920段北交所异常占位数据 → 判定失败, 不覆盖已有数据
  2. 备源: 校验失败时改用 akshare 东方财富全A快照按成交额取TOP500
  3. 双源均失败 → 保留旧 top500_all_a.json 并 exit 1 (工作流重试)
v3.0 变更(2026-10-13):
  4. 池大小由 config.json pool_size 驱动(默认500), 观察池逻辑整体删除
  5. 入池资格过滤: ST/*ST/退市整理 名称判据剔除, 计数透明(dropped_eligibility)
"""
import urllib.request
import json
import time
import sys
import os
from datetime import datetime

# 清除代理环境变量: 本地代理可能不通(HTTP 502), 行情接口需直连
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
OUT = "top500_all_a.json"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_pool_config():
    """读 config.json: 池大小与抓取页数。缺失/损坏回退 TOP200 兼容缺省。"""
    cfg = {"pool_size": 500, "fetch": {"pages": 15, "page_size": 100, "total_n": 1500},
           "eligibility_filter": {"exclude_st": True, "exclude_delisting": True}}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if user.get("pool_size"):
                cfg["pool_size"] = int(user["pool_size"])
            if isinstance(user.get("fetch"), dict):
                cfg["fetch"].update(user["fetch"])
            if isinstance(user.get("eligibility_filter"), dict):
                cfg["eligibility_filter"].update(user["eligibility_filter"])
        except Exception as e:
            print(f"  ⚠️ config.json 解析失败({str(e)[:40]}), 使用缺省池大小 TOP200")
    return cfg


def eligibility_filter(stocks, cfg=None):
    """入池资格过滤: ST/*ST/退市整理 名称判据剔除。返回 (kept, dropped)。

    数据纪律: 仅按名称判据剔除(可靠、无额外接口依赖), 不做停牌/上市天数推断
    (那需要逐股接口, 成本高且易误杀)。dropped 计数透明输出。
    """
    cfg = cfg or {"exclude_st": True, "exclude_delisting": True}
    if not (cfg.get("exclude_st", True) or cfg.get("exclude_delisting", True)):
        return stocks, []
    kept, dropped = [], []
    for s in stocks:
        name = str(s.get("name", "") or "").upper().replace(" ", "")
        is_st = cfg.get("exclude_st", True) and ("ST" in name or "*ST" in name)
        is_delist = cfg.get("exclude_delisting", True) and ("退" in name or "退市" in name)
        if is_st or is_delist:
            dropped.append({"name": s.get("name"), "code": s.get("code") or s.get("symbol"),
                            "reason": "ST" if is_st else "退市"})
        else:
            kept.append(s)
    return kept, dropped


_POOL_CFG = load_pool_config()
POOL = _POOL_CFG["pool_size"]


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


def fallback_eastmoney(pool=500):
    """备源: akshare 东方财富全A快照, 按成交额降序取 TOP{pool}"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None, "东财快照为空"
        df = df.dropna(subset=["成交额"])
        df = df[df["成交额"] > 0]
        df = df.sort_values("成交额", ascending=False).head(pool)
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
    fc = _POOL_CFG.get("fetch", {})
    pages = int(fc.get("pages", 15))
    page_size = int(fc.get("page_size", 100))
    print("=" * 60)
    print(f"  新浪接口获取全A成交额 TOP{POOL} 主力池 (v3.0 扩池, 观察池已删除)")
    print("=" * 60)
    all_stocks = []
    for page in range(1, pages + 1):
        print(f"获取第{page}页...")
        data = fetch_page(page, num=page_size)
        if not data:
            continue
        all_stocks.extend(data)
        print(f"  累计 {len(all_stocks)} 只")
        time.sleep(0.8)

    all_stocks, dropped = eligibility_filter(all_stocks, _POOL_CFG.get("eligibility_filter"))
    if dropped:
        print(f"  资格过滤剔除 ST/退市 {len(dropped)} 只: {', '.join(str(d.get('name','')) for d in dropped[:5])}{'...' if len(dropped) > 5 else ''}")
    all_stocks.sort(key=lambda x: -float(x.get("amount", 0)))
    main_pool = all_stocks[:POOL]
    print(f"\n✅ 共获取 {len(all_stocks)} 只（主力池 TOP{POOL}）")

    # ===== 数据质量校验（针对主力池）=====
    ok, reason = validate_data(main_pool)
    used_fallback = False
    if not ok:
        print(f"⚠️ 新浪数据校验失败: {reason}")
        print("🔄 尝试东财备源...")
        out, fb = fallback_eastmoney(POOL)
        if out:
            ok2, reason2 = validate_data([{"symbol": s["code"], "code": s["code"],
                                           "trade": s["price"], "amount": s["amount"]} for s in out])
            if ok2:
                print(f"✅ {fb}, 校验通过 ({reason2})")
                main_norm = out
                used_fallback = True
            else:
                print(f"❌ 东财备源校验也失败: {reason2}")
                sys.exit(1)
        else:
            print(f"❌ {fb}")
            sys.exit(1)
    else:
        main_norm = normalize(main_pool, 1)

    _src = "东财备源(akshare)" if used_fallback else "新浪财经全市场行情接口"
    _today = datetime.now().strftime("%Y-%m-%d")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "top50": main_norm[:50],
            "top100": main_norm[:100],
            "top500": main_norm,
            "main_pool": main_norm,
            "pool_size": len(main_norm),
            "all_count": len(all_stocks),
            "dropped_eligibility": len(dropped),
            "date": _today,
            "source": _src,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n📊 主力池 TOP10:")
    for i, s in enumerate(main_norm[:10]):
        print(f"  {i+1}. {s['name']:8s} {s['code']} {s['price']:8.2f} {s['pct_chg']:+.2f}% 成交额{s['turnover']/10000:.0f}万")
    print(f"\n✅ 数据已保存: {OUT} (主力池{len(main_norm)}只, 无观察池)")


if __name__ == "__main__":
    main()
