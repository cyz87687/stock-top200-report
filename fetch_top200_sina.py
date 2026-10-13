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
    """读 config.json: 池大小、抓取页数、资格过滤、流动性地板。缺失/损坏回退缺省。

    v3.2 修复: 原实现只挑 pool_size/fetch/eligibility_filter 三键, 漏载
      liquidity_filter → 下游 normalize 拿到空地板(阈值 0), 流动性过滤空转。
    """
    cfg = {"pool_size": 500, "fetch": {"pages": 15, "page_size": 100, "total_n": 1500},
           "eligibility_filter": {"exclude_st": True, "exclude_delisting": True},
           "liquidity_filter": {"enabled": True, "min_avg_amount_20d": 5e7,
                                "min_avg_turnover_rate": 0.01}}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if user.get("pool_size"):
                cfg["pool_size"] = int(user["pool_size"])
            for _sec in ("fetch", "eligibility_filter", "liquidity_filter"):
                if isinstance(user.get(_sec), dict):
                    cfg[_sec].update(user[_sec])
        except Exception as e:
            print(f"  ⚠️ config.json 解析失败({str(e)[:40]}), 使用缺省池大小 TOP200")
    return cfg


def eligibility_filter(stocks, cfg=None):
    """入池资格过滤: ST/*ST/退市整理 名称判据剔除。返回 (kept, dropped)。

    数据纪律: 名称判据剔除 ST/退市(可靠、无额外接口依赖)。
    v3.2 起停牌/次新改为「标记不剔除」(见 normalize 的 suspended/is_new/liquid_ok),
      因二者需逐股判据且误杀代价高(停牌一日≠该股不可投), 保留在池内透明披露,
      由组合层排除。
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
            _name = str(r.get("名称", ""))
            out.append({
                "name": _name,
                "code": code,
                "price": round(price, 2),
                "pct_chg": round(pct, 2),
                "turnover": amount,
                "amount": amount,
                # v3.2 与 normalize 保持字段一致（备源路径）
                "liquid_ok": True,          # 备源已按成交额>0 过滤；地板判据交由 step2 校验
                "suspended": (round(pct, 2) == 0 and amount == 0),
                "is_new": _name.upper().replace(" ", "").startswith("N"),
            })
        return out, f"东财备源 {len(out)} 只"
    except Exception as e:
        return None, f"东财备源失败: {e}"


def normalize(stocks, start_rank=1, cfg=None):
    """新浪行情条目 → 标准化 dict 列表（含 rank 排名）。

    v3.2 新增（P0-1 流动性地板接线 + P0-3 资格标记）：
      - turnover_ratio：换手率(%)，新浪 `turnoverratio` 原生字段，零新增接口。
      - circ_mv：流通市值(元)，新浪 `nmc`（万元 ×1e4）。
      - liquid_ok：**仅由当日成交额** 判据（≥ min_avg_amount_20d）。
        ⚠️ 实测修正（2026-10-13）：原清单 D1 设想的「换手率地板」会**误杀大盘蓝筹**——
        TOP500 内换手率<1% 的 61 只全是大市值股（农业银行 0.10%、工商银行 0.12%、
        贵州茅台 0.21%），其成交额 10~34 亿、流通市值数千亿，流动性实为池内最好。
        换手率低恰因盘子大，故**不作硬剔除**，仅采集供组合层容量估算。
        另实测 TOP500 池最低成交额 7.3 亿 ≫ 5000 万地板 → 成交额地板对 TOP500
        亦不构成有效筛选；此为实现真实接线而非拍阈值，如实披露（不虚报过滤数）。
        口径说明：严格「20 日均额」需 K 线；此处用当日快照近似，属保守判据。
      - suspended：当日涨跌幅与成交额同时为 0（快照可判的停牌近似）
      - is_new：名称含 N 前缀（上市首日/次新标记，行情源惯例）
    """
    cfg = cfg or {}
    lf = cfg.get("liquidity_filter", {}) or {}
    _lf_on = bool(lf.get("enabled", True))
    min_amt = float(lf.get("min_avg_amount_20d", 0) or 0) if _lf_on else 0.0
    # config 换手率阈值单位是小数(0.01)，接口为百分数(%) → ×100；仅作参考判据字段
    min_tr = float(lf.get("min_avg_turnover_rate", 0) or 0) * 100.0 if _lf_on else 0.0
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
        name = s.get("name", "") or ""
        amt = float(s.get("amount", 0) or 0)
        pct = round(float(s.get("changepercent", 0) or 0), 2)
        try:
            tr = round(float(s.get("turnoverratio", 0) or 0), 3)
        except Exception:
            tr = 0.0
        try:
            cmv = round(float(s.get("nmc", 0) or 0) * 1e4, 0)   # 万元 → 元
        except Exception:
            cmv = 0.0
        out.append({
            "rank": i,
            "name": name,
            "code": code,
            "price": float(s.get("trade", 0) or 0),
            "pct_chg": pct,
            "turnover": amt,
            "amount": amt,
            "turnover_ratio": tr,                 # 采集供容量分析，不作硬剔除
            "circ_mv": cmv,                       # 流通市值，供容量/分层
            "liquid_ok": bool((amt >= min_amt) if min_amt > 0 else True),
            "turnover_ok": bool((tr >= min_tr) if min_tr > 0 else True),  # 仅参考披露
            "suspended": (pct == 0 and amt == 0),
            "is_new": name.upper().replace(" ", "").startswith("N"),
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

    # ===== v3.2 P0-3 资格标记（次新/停牌）：标记不剔除，保留透明 =====
    # 次新股（N 前缀 / 上市不足 60 日）成交额因首日爆量虚高，且无 60 日 K 线 →
    # 超跌60/乖离60/RSI 等因子退化兜底分；停牌股价格不更新 → 收益序列失真。
    # 主池保留（不改变池径，避免与历史口径不可比），组合层排除。
    _norm_preview = normalize(all_stocks, 1, _POOL_CFG)
    _n_new = sum(1 for s in _norm_preview[:POOL] if s.get("is_new"))
    _n_susp = sum(1 for s in _norm_preview[:POOL] if s.get("suspended"))
    _n_illiq = sum(1 for s in _norm_preview[:POOL] if not s.get("liquid_ok", True))
    if _n_new or _n_susp or _n_illiq:
        print(f"  ⚠️ 资格标记(主池内): 次新 {_n_new} 只 / 停牌 {_n_susp} 只 / 流动性不达标 {_n_illiq} 只（标记不剔除，组合层排除）")
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
        main_norm = normalize(main_pool, 1, _POOL_CFG)

    _src = "东财备源(akshare)" if used_fallback else "新浪财经全市场行情接口"
    _today = datetime.now().strftime("%Y-%m-%d")

    # v3.2: 资格标记统计随池分发（供 step2/组合层/报告层消费）
    _elig_marks = {
        "new_stock": sum(1 for s in main_norm if s.get("is_new")),
        "suspended": sum(1 for s in main_norm if s.get("suspended")),
        "illiquid": sum(1 for s in main_norm if not s.get("liquid_ok", True)),
        "low_turnover": sum(1 for s in main_norm if not s.get("turnover_ok", True)),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "top50": main_norm[:50],
            "top100": main_norm[:100],
            "top500": main_norm,
            "main_pool": main_norm,
            "pool_size": len(main_norm),
            "all_count": len(all_stocks),
            "dropped_eligibility": len(dropped),
            "eligible_marks": _elig_marks,
            "liquidity_floor": _POOL_CFG.get("liquidity_filter", {}) or {},
            "date": _today,
            "source": _src,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n📊 主力池 TOP10:")
    for i, s in enumerate(main_norm[:10]):
        print(f"  {i+1}. {s['name']:8s} {s['code']} {s['price']:8.2f} {s['pct_chg']:+.2f}% 成交额{s['turnover']/10000:.0f}万")
    print(f"\n✅ 数据已保存: {OUT} (主力池{len(main_norm)}只, 无观察池)")


if __name__ == "__main__":
    main()
