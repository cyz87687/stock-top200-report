#!/usr/bin/env python3
"""
A股TOP50 极简公司评分 - Stock-Scorer v2.1
模型: 消息面30% + 技术面25% + 基本面25% + 资金面20% → 20分制
数据源: 腾讯K线(技术) + iFinD(基本/消息) + 东方财富(资金)
"""

import json, sys, os, time, re, subprocess
import urllib.request
import concurrent.futures
from datetime import datetime, timedelta
from collections import Counter

# ============================================================
# Config
# ============================================================
PY = "/Users/yzreal/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
IFIND_FIN = os.path.expanduser("~/.workbuddy/skills/ifind-repilot-finance-data-search/scripts/fetch_data.py")
IFIND_NEWS = os.path.expanduser("~/.workbuddy/skills/ifind-repilot-news-search/scripts/fetch_data.py")
KLINES_N = 120

SECTOR = {
    "中际旭创":"AI/半导体","新易盛":"AI/半导体","天孚通信":"AI/半导体",
    "寒武纪":"AI/半导体","海光信息":"AI/半导体","中芯国际":"AI/半导体",
    "北方华创":"AI/半导体","中微公司":"AI/半导体","澜起科技":"AI/半导体",
    "兆易创新":"AI/半导体","佰维存储":"AI/半导体","江波龙":"AI/半导体",
    "长电科技":"AI/半导体","通富微电":"AI/半导体","拓荆科技":"AI/半导体",
    "盛合晶微":"AI/半导体","沪硅产业":"AI/半导体","源杰科技":"AI/半导体",
    "德明利":"AI/半导体","香农芯创":"AI/半导体","江丰电子":"AI/半导体",
    "长川科技":"AI/半导体","雅克科技":"AI/半导体","南大光电":"AI/半导体",
    "光迅科技":"AI/半导体","工业富联":"AI/半导体","立讯精密":"AI/半导体",
    "中兴通讯":"AI/半导体","东山精密":"AI/半导体","胜宏科技":"AI/半导体",
    "沪电股份":"AI/半导体","亨通光电":"AI/半导体","中天科技":"AI/半导体",
    "烽火通信":"AI/半导体","华工科技":"AI/半导体","信维通信":"AI/半导体",
    "风华高科":"AI/半导体","长光华芯":"AI/半导体","芯原股份":"AI/半导体",
    "华虹宏力":"AI/半导体","协创数据":"AI/半导体","三花智控":"AI/半导体",
    "炬光科技":"AI/半导体","太辰光":"AI/半导体","剑桥科技":"AI/半导体",
    "永鼎股份":"AI/半导体","兴森科技":"AI/半导体","太极实业":"AI/半导体",
    "立昂微":"AI/半导体","深科技":"AI/半导体","晶盛机电":"AI/半导体",
    "韦尔股份":"AI/半导体","卓胜微":"AI/半导体","圣邦股份":"AI/半导体",
    "思瑞浦":"AI/半导体","纳芯微":"AI/半导体","瑞芯微":"AI/半导体",
    "全志科技":"AI/半导体","晶晨股份":"AI/半导体","恒玄科技":"AI/半导体",
    "乐鑫科技":"AI/半导体","聚辰股份":"AI/半导体","恒润股份":"AI/半导体",
    "宏英智能":"AI/半导体","凌云光":"AI/半导体","罗博特科":"AI/半导体",
    "蓝思科技":"电子制造",
    "紫金矿业":"有色资源","洛阳钼业":"有色资源","厦门钨业":"有色资源",
    "赣锋锂业":"有色资源","天齐锂业":"有色资源","盐湖股份":"有色资源",
    "天赐材料":"有色资源","多氟多":"有色资源","中钨高新":"有色资源",
    "云南锗业":"有色资源","盛新锂能":"有色资源","华友钴业":"有色资源",
    "锡业股份":"有色资源","章源钨业":"有色资源","北方稀土":"有色资源",
    "铜陵有色":"有色资源","云铝股份":"有色资源","中国铝业":"有色资源",
    "山东黄金":"有色资源","中金黄金":"有色资源","银泰黄金":"有色资源",
    "宁德时代":"新能源","阳光电源":"新能源","比亚迪":"新能源",
    "恩捷股份":"新能源","星源材质":"新能源","璞泰来":"新能源",
    "容百科技":"新能源","当升科技":"新能源","振华新材":"新能源",
    "中伟股份":"新能源","格林美":"新能源","华友钴业":"新能源",
    "京东方A":"电子面板","TCL科技":"电子面板","维信诺":"电子面板",
    "深天马A":"电子面板","彩虹股份":"电子面板",
    "美的集团":"家电制造","格力电器":"家电制造","海尔智家":"家电制造",
    "小熊电器":"家电制造","新宝股份":"家电制造",
    "中国巨石":"基础材料","国瓷材料":"基础材料","泰和新材":"基础材料",
    "伟星新材":"基础材料","东方雨虹":"基础材料","科顺股份":"基础材料",
    "三棵树":"基础材料","亚士创能":"基础材料","金螳螂":"基础材料",
    "巨化股份":"化工","万华化学":"化工","华鲁恒升":"化工",
    "恒力石化":"化工","荣盛石化":"化工","恒逸石化":"化工",
    "桐昆股份":"化工","新凤鸣":"化工","东方盛虹":"化工",
    "三友化工":"化工","中泰化学":"化工","新疆天业":"化工",
    "XD昊华科":"化工","山东海化":"化工","远兴能源":"化工",
    "生益科技":"电子材料","中船特气":"特种材料","雅克科技":"特种材料",
    "凯盛新材":"特种材料","濮阳惠成":"特种材料","康强电子":"特种材料",
    "东材科技":"基础材料","金安国纪":"电子材料","国际复材":"基础材料",
    "铜冠铜箔":"电子材料","XD圣泉集":"基础材料","远东股份":"综合",
    "世纪华通":"综合","天华新能":"综合","绿的谐波":"综合",
    "贵州茅台":"消费","五粮液":"消费","泸州老窖":"消费",
    "洋河股份":"消费","山西汾酒":"消费","酒鬼酒":"消费",
    "舍得酒业":"消费","古井贡酒":"消费","口子窖":"消费",
    "同花顺":"金融","东方财富":"金融","恒生电子":"金融",
    "指南针":"金融","大智慧":"金融",
    "中信证券":"金融","中国平安":"金融","三环集团":"电子元件",
    "药明康德":"医药生物","中国卫星":"航天军工",
    "恒瑞医药":"医药生物","亿纬锂能":"新能源","迈为股份":"新能源",
    "金风科技":"新能源","深南电路":"电子制造","顺络电子":"电子元件",
    "大族激光":"AI/半导体","晶方科技":"AI/半导体","国科微":"AI/半导体",
    "中科飞测":"AI/半导体","芯源微":"AI/半导体","长芯博创":"AI/半导体",
    "三安光电":"AI/半导体","安集科技":"AI/半导体","北京君正":"AI/半导体",
    "长飞光纤":"AI/半导体","光库科技":"AI/半导体","联特科技":"AI/半导体",
    "仕佳光子":"AI/半导体","晶合集成":"AI/半导体","普冉股份":"AI/半导体",
    "汇川技术":"高端制造","埃斯顿":"高端制造","绿的谐波":"高端制造",
    "中控技术":"高端制造","麦格米特":"高端制造","英维克":"高端制造",
    "恒为科技":"高端制造","锐科激光":"高端制造","精智达":"高端制造",
    "和远气体":"化工","华特气体":"特种材料","中巨芯":"化工",
    "飞凯材料":"电子材料","神剑股份":"军工","彤程新材":"化工",
    "利通电子":"电子制造","方正科技":"电子制造","融捷股份":"有色资源",
    "中国稀土":"有色资源","盛和资源":"有色资源","盛屯矿业":"有色资源",
    "江铜有色":"有色资源","江西铜业":"有色资源","华友钴业":"有色资源",
    "红星发展":"有色资源","兴发集团":"化工","中化国际":"化工",
    "石英股份":"电子材料","火炬电子":"电子元件","宏和科技":"电子材料",
    "华正新材":"电子材料","弘信电子":"电子元件","鼎龙股份":"电子材料",
    "生益电子":"电子材料","领益智造":"电子制造","鹏鼎控股":"电子制造",
    "联讯仪器":"AI/半导体","博云新材":"新材料","西部材料":"新材料",
    "中材科技":"新材料","天通股份":"AI/半导体","华天科技":"AI/半导体",
    "通鼎互联":"AI/半导体","天华新能":"新能源","晶盛机电":"AI/半导体",
    "TCL中环":"新能源","隆基绿能":"新能源","中国卫通":"航天军工",
    "中国船舶":"航天军工","航天电子":"航天军工","江龙船艇":"航天军工",
    "中船防务":"航天军工","中国重工":"航天军工","中国神华":"煤炭",
    "长江电力":"电力","华电辽能":"电力","豫能控股":"电力","大唐发电":"电力",
    "诺德股份":"新能源","XD兴福电":"电子材料","兴业银锡":"有色资源",
    "招商银行":"金融","华泰证券":"金融","国泰海通":"金融","工商银行":"金融",
    "莲花控股":"农业","大普微":"AI/半导体","天娱数科":"传媒娱乐",
    "宏景科技":"计算机软件","英唐智控":"AI/半导体","华海清科":"AI/半导体",
    "阿石创":"电子材料","索辰科技":"计算机软件","嘉元科技":"新能源",
    "江海股份":"电子元件","杰瑞股份":"高端制造","潍柴动力":"汽车",
    "隆华科技":"新材料","京东方A":"电子面板","黄河旋风":"新材料",
    "盛美上海":"AI/半导体","德福科技":"电子材料","翔鹭钨业":"有色资源",
    "有研新材":"电子材料","奥比中光":"AI/半导体","沃格光电":"电子材料",
    "旭光电子":"电子元件","菲利华":"电子材料",
    "鼎泰高科":"电子制造",
    "泰晶科技":"电子元件",
    "远东股份":"电力设备","世纪华通":"传媒娱乐",
    "昊华科技":"化工","浪潮信息":"AI/半导体","圣泉集团":"基础材料",
    "海亮股份":"有色资源","宗申动力":"高端制造","商络电子":"电子元件",
    "士兰微":"AI/半导体","特变电工":"电力设备","华安证券":"金融",
    "金钼股份":"有色资源","杭电股份":"电力设备","中科曙光":"AI/半导体",
    "中巨芯":"电子材料","景旺电子":"电子制造","中矿资源":"有色资源",
    "德业股份":"新能源","新宙邦":"新能源","紫光股份":"AI/半导体",
    "芯碁微装":"AI/半导体","招商证券":"金融","驰宏锌锗":"有色资源",
    "航天发展":"航天军工","永杉锂业":"有色资源","润泽科技":"AI/半导体",
    "华润微":"AI/半导体","福晶科技":"AI/半导体","利和兴":"电子制造",
    "东芯股份":"AI/半导体","蓝色光标":"传媒娱乐",
    "先导智能":"新能源","中国西电":"电力设备","鹏辉能源":"新能源",
    "宏达电子":"电子元件","扬杰科技":"AI/半导体","振华科技":"电子元件",
    "长信科技":"电子面板","鼎通科技":"电子制造","博迁新材":"基础材料",
    "麦捷科技":"电子元件","欧陆通":"电子制造","中银证券":"金融",
    "洁美科技":"电子材料","四方股份":"电力设备","海康威视":"AI/半导体",
    "斯迪克":"电子材料","甬矽电子":"AI/半导体","中稀有色":"有色资源",
    "汇绿生态":"综合","中远海能":"交通运输","招商轮船":"交通运输",
    "天岳先进":"AI/半导体","盛科通信":"AI/半导体","机器人":"高端制造",
    "四方达":"基础材料","XD佰维存":"AI/半导体","XD石英股":"电子材料",
    "思源电气":"电力设备","中船特气":"特种材料","帝尔激光":"高端制造",
    "华丰科技":"AI/半导体","永鼎股份":"AI/半导体","中国长城":"AI/半导体",
    "精智达":"高端制造","楚江新材":"有色资源","中一科技":"电子材料",
    "华峰测控":"AI/半导体","中国中免":"消费","紫光国微":"AI/半导体",
    "华宏科技":"有色资源","东阳光":"电子材料","深桑达A":"AI/半导体",
    "同有科技":"AI/半导体","南亚新材":"电子材料","东岳硅材":"化工",
    "科翔股份":"电子元件","盛龙股份":"基础材料","中国能建":"电力设备",
    "德科立":"AI/半导体","光智科技":"AI/半导体","双星新材":"电子材料",
    "嘉元科技":"新能源","金安国纪":"电子材料",
}

def _normalize_name(name):
    out = []
    for ch in name:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        else:
            out.append(ch)
    return ''.join(out)

def get_sec(name):
    if name in SECTOR:
        return SECTOR[name]
    n = _normalize_name(name)
    if n in SECTOR:
        return SECTOR[n]
    for prefix in ("XD", "DR", "XR"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    base = n.rstrip("UW").rstrip("-").rstrip("U").rstrip("-")
    if base in SECTOR:
        return SECTOR[base]
    for key in SECTOR:
        if base.startswith(key) or key.startswith(base):
            return SECTOR[key]
            break
    return "综合"


# ============================================================
# Data fetching
# ============================================================
def fetch_weekly_klines(code, n=30):
    """前复权周线K线 → [{date,open,close,high,low,vol,amount}]"""
    raw = code[2:]
    market = "sh" if code.startswith("sh") else "sz"
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{raw},week,,,{n},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        key = f"{market}{raw}"
        kdata = data.get("data", {}).get(key, {})
        raw_kl = kdata.get("qfqweek") or kdata.get("week")
        if raw_kl and len(raw_kl) >= 10:
            out = []
            for k in raw_kl:
                close = float(k[2])
                vol_shares = float(k[5]) * 100
                out.append({
                    "date": k[0], "open": float(k[1]), "close": close,
                    "high": float(k[3]), "low": float(k[4]),
                    "vol": vol_shares,
                })
            return out
    except:
        pass
    return None

def fetch_klines(code):
    """前复权K线 → [{date,open,close,high,low,vol,amount}]
    v2.6: 腾讯前复权(qfqday)优先，东方财富前复权(fqt=1)备选，新浪不复权最后手段
         腾讯qfqday成交量是手(×100)，东方财富成交量已是股"""
    raw = code[2:]
    market = "sh" if code.startswith("sh") else "sz"
    
    # 方案1: 腾讯前复权K线 (qfqday, 重试3次)
    tencent_result = None
    for attempt in range(3):
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{raw},day,,,{KLINES_N},qfq"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            key = f"{market}{raw}"
            kdata = data.get("data", {}).get(key, {})
            raw_kl = kdata.get("qfqday")
            if raw_kl and len(raw_kl) >= 60:
                out = []
                for k in raw_kl:
                    close = float(k[2])
                    vol_shares = float(k[5]) * 100
                    amount = vol_shares * close
                    out.append({
                        "date": k[0], "open": float(k[1]), "close": close,
                        "high": float(k[3]), "low": float(k[4]),
                        "vol": vol_shares,
                        "amount": amount,
                    })
                return out
            elif raw_kl:
                tencent_result = raw_kl
                break
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        except:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    
    # 方案2: 东方财富前复权K线 (fqt=1, 重试2次)
    mid = 0 if code.startswith("sz") else 1
    secid = f"{mid}.{raw}"
    for attempt in range(2):
        try:
            url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                   f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
                   f"&klt=101&fqt=1&end=20500101&lmt={KLINES_N}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            klines = data.get("data", {}).get("klines", [])
            if klines:
                out = []
                for k in klines:
                    parts = k.split(",")
                    close = float(parts[2])
                    vol_shares = float(parts[5])
                    amount = float(parts[6]) if len(parts) > 6 else vol_shares * close
                    out.append({
                        "date": parts[0], "open": float(parts[1]), "close": close,
                        "high": float(parts[3]), "low": float(parts[4]),
                        "vol": vol_shares,
                        "amount": amount,
                    })
                return out
        except:
            if attempt < 1:
                time.sleep(1.0)
    
    # 方案2.5: 腾讯前复权数据不足60条时，仍优先使用(比新浪不复权好)
    if tencent_result:
        out = []
        for k in tencent_result:
            close = float(k[2])
            vol_shares = float(k[5]) * 100
            amount = vol_shares * close
            out.append({
                "date": k[0], "open": float(k[1]), "close": close,
                "high": float(k[3]), "low": float(k[4]),
                "vol": vol_shares,
                "amount": amount,
            })
        return out
    
    # 方案3: 新浪K线 (不复权，最后手段)
    sina_market = "sh" if code.startswith("sh") else "sz"
    url3 = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_market}{raw}&scale=240&ma=no&datalen={KLINES_N}"
    req3 = urllib.request.Request(url3, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    try:
        with urllib.request.urlopen(req3, timeout=10) as r:
            text = r.read().decode("utf-8")
        kl_data = json.loads(text)
        if kl_data:
            out = []
            for k in kl_data:
                close = float(k["close"])
                vol_shares = float(k["volume"])
                amount = vol_shares * close
                out.append({
                    "date": k["day"], "open": float(k["open"]), "close": close,
                    "high": float(k["high"]), "low": float(k["low"]),
                    "vol": vol_shares,
                    "amount": amount,
                })
            return out
    except:
        pass
    
    return []


def fetch_fund_flow(code):
    """东方财富资金流 - f62(方向), f140(超大单净额), f143(大单净额), f137(主力净额), f50(换手)"""
    raw = code[2:]
    mid = 1 if code.startswith("sh") else 0
    secid = f"{mid}.{raw}"
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f62,f137,f140,f143,f146,f149,f50,f116,f184,f185,f186,f187")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8")).get("data") or {}
        return d
    except:
        return {}


def run_ifind_fin(name, code):
    raw = code[2:]
    q = f"{name}{raw}市盈率PE扣非净利润增速同行业排名ROE"
    try:
        r = subprocess.run([PY, IFIND_FIN, q], capture_output=True, text=True, timeout=30)
        return r.stdout
    except:
        return ""


def run_ifind_news(name, code):
    raw = code[2:]
    q = f"{name} {raw} 最新公告业绩利好利空"
    tdy = datetime.now()
    ago = tdy - timedelta(days=7)
    try:
        r = subprocess.run(
            [PY, IFIND_NEWS, q, "--start-date", ago.strftime("%Y-%m-%dT00:00:00"),
             "--end-date", tdy.strftime("%Y-%m-%dT23:59:59")],
            capture_output=True, text=True, timeout=30)
        return r.stdout
    except:
        return ""


# ============================================================
# 1. 技术面 (0-5) × 25%
# ============================================================
def ma(kl, n):
    if len(kl) < n: return None
    return sum(k["close"] for k in kl[-n:]) / n

def boll(kl, n=20, k=2):
    if len(kl) < n: return None, None, None
    closes = [k["close"] for k in kl[-n:]]
    mid = sum(closes) / n
    std = (sum((c - mid) ** 2 for c in closes) / n) ** 0.5
    return mid + k * std, mid, mid - k * std

def score_tech(kl, price, pct, cagr3=None, wkl=None):
    if len(kl) < 60:
        return 2, {"note": "K线不足60日"}

    ma5v = ma(kl, 5)
    ma10v = ma(kl, 10)
    ma20v = ma(kl, 20)
    ma60v = ma(kl, 60)
    boll_upper, boll_mid, boll_lower = boll(kl, 20, 2)
    boll_pos = None
    if boll_upper and boll_lower and boll_upper != boll_lower:
        boll_pos = (price - boll_lower) / (boll_upper - boll_lower) * 100
    cls = [k["close"] for k in kl]
    hi, lo = max(cls), min(cls)
    pos = (price - lo) / (hi - lo) * 100 if hi != lo else 50
    vol5 = sum(k["vol"] for k in kl[-5:]) / 5
    vol20 = sum(k["vol"] for k in kl[-20:]) / 20
    vr = vol5 / vol20 if vol20 > 0 else 1.0
    amt5 = sum(k.get("amount", k["vol"] * k["close"]) for k in kl[-5:]) / 5
    amt20 = sum(k.get("amount", k["vol"] * k["close"]) for k in kl[-20:]) / 20
    amt_ratio = amt5 / amt20 if amt20 > 0 else 1.0
    amt_today = kl[-1].get("amount", kl[-1]["vol"] * kl[-1]["close"]) / 1e8

    prev_ma5 = sum(k["close"] for k in kl[-6:-1]) / 5 if len(kl) >= 6 else None
    prev_ma20 = sum(k["close"] for k in kl[-21:-1]) / 20 if len(kl) >= 21 else None
    is_cross_up = (prev_ma5 is not None and prev_ma20 is not None
                   and prev_ma5 <= prev_ma20 and ma5v > ma20v
                   and (ma5v - ma20v) / ma20v > 0.002)
    is_cross_down = (prev_ma5 is not None and prev_ma20 is not None
                     and prev_ma5 >= prev_ma20 and ma5v < ma20v
                     and (ma20v - ma5v) / ma20v > 0.002)

    # 日线多头排列: MA5>MA10>MA20
    daily_bull = (ma5v and ma10v and ma20v and ma5v > ma10v > ma20v)
    daily_bear = (ma5v and ma10v and ma20v and ma5v < ma10v < ma20v)

    # 周线多头排列: MA5>MA10>MA20
    weekly_bull = False
    weekly_bear = False
    wma5 = wma10 = wma20 = None
    if wkl and len(wkl) >= 20:
        wma5 = ma(wkl, 5)
        wma10 = ma(wkl, 10)
        wma20 = ma(wkl, 20)
        if wma5 and wma10 and wma20:
            weekly_bull = wma5 > wma10 > wma20
            weekly_bear = wma5 < wma10 < wma20

    # 综合趋势判定
    if daily_bull and weekly_bull:
        trend = "双线多头"
    elif weekly_bull and not daily_bull:
        trend = "周线多头"
    elif daily_bull and not weekly_bull:
        trend = "日线多头"
    elif daily_bear and weekly_bear:
        trend = "双线空头"
    elif weekly_bear and not daily_bear:
        trend = "周线空头"
    elif daily_bear and not weekly_bear:
        trend = "日线空头"
    elif is_cross_up:
        trend = "金叉"
    elif is_cross_down:
        trend = "死叉"
    elif ma5v and ma20v and ma5v > ma20v:
        trend = "偏多"
    elif ma5v and ma20v and ma5v < ma20v:
        trend = "偏空"
    else:
        trend = "缠绕"

    is_bullish = trend in ("双线多头", "周线多头", "日线多头", "金叉", "偏多")
    is_bearish = trend in ("双线空头", "周线空头", "日线空头", "死叉", "偏空")
    is_neutral = trend in ("缠绕",)

    boll_inside = (boll_pos is not None and 0 <= boll_pos <= 100)
    boll_touch_upper = (boll_pos is not None and 80 < boll_pos <= 100)
    boll_above_upper = (boll_pos is not None and boll_pos > 100)
    boll_touch_lower = (boll_pos is not None and 0 <= boll_pos < 20)
    boll_below_lower = (boll_pos is not None and boll_pos < 0)

    # ---- 核心评分逻辑 ----
    # 多头排列等级: 双线多头(最高) > 周线多头(次高) > 日线多头(中等) > 无多头排列
    # 空头排列等级: 双线空头(最低) > 周线空头(次低) > 日线空头(中等) > 无空头排列
    bull_level = 0  # 0=无, 1=日线, 2=周线, 3=双线
    if daily_bull and weekly_bull:
        bull_level = 3
    elif weekly_bull:
        bull_level = 2
    elif daily_bull:
        bull_level = 1

    bear_level = 0  # 0=无, 1=日线, 2=周线, 3=双线
    if daily_bear and weekly_bear:
        bear_level = 3
    elif weekly_bear:
        bear_level = 2
    elif daily_bear:
        bear_level = 1

    s = 3
    r = ""

    if is_bullish and pos < 30 and (boll_inside or boll_touch_upper):
        # 低位+看多: 双线5, 周线4.5, 日线4, 其他3.5
        s = 3.5 + bull_level * 0.5
        r = f"{trend}+低位({pos:.0f}%)+BOLL通道内"
    elif is_bullish and 30 <= pos < 60 and boll_inside:
        # 中位+看多: 双线4, 周线3.5, 日线3, 其他2.5
        s = 2.5 + bull_level * 0.5
        r = f"{trend}+中位({pos:.0f}%)+BOLL通道内"
    elif is_bullish and 60 <= pos < 80 and boll_inside:
        # 相对高位+看多: 双线3, 周线2.5, 日线2, 其他1.5
        s = 1.5 + bull_level * 0.5
        r = f"{trend}+相对高位({pos:.0f}%)+BOLL通道内"
    elif is_bullish and 80 <= pos < 95 and boll_touch_upper:
        s = 1 + bull_level * 0.3
        r = f"{trend}+高位({pos:.0f}%)+BOLL触碰上轨"
    elif is_bullish and pos >= 95 and boll_above_upper:
        s = 0.5 + bull_level * 0.2
        r = f"{trend}+极值高位({pos:.0f}%)+BOLL突破上轨(严重超买)"
    elif is_bullish and pos >= 95 and boll_touch_upper:
        s = 0.5 + bull_level * 0.2
        r = f"{trend}+极值高位({pos:.0f}%)+BOLL触碰上轨(超买)"
    elif is_bullish and pos >= 80:
        s = 1 + bull_level * 0.3
        r = f"{trend}+高位({pos:.0f}%)"
    elif is_bullish and boll_below_lower:
        s = 1 + bull_level * 0.3
        r = f"趋势偏多+BOLL破下轨(异常)"
    elif is_bearish and pos < 20 and boll_below_lower:
        s = 0
        r = f"趋势走弱+低位BOLL连续破下轨"
    elif is_bearish and boll_below_lower:
        s = 0
        r = f"趋势走弱+BOLL破下轨"
    elif is_bearish and pos >= 80 and boll_above_upper:
        s = 0
        r = f"{trend}+高位+BOLL破上轨(极度危险)"
    elif is_bearish and pos >= 60:
        s = max(0, 1.5 - bear_level * 0.5)
        r = f"趋势走弱+高位({pos:.0f}%)"
    elif is_bearish and pos < 30:
        s = max(0, 2.5 - bear_level * 0.5)
        r = f"趋势走弱+低位({pos:.0f}%)可能反弹"
    elif is_bearish:
        s = max(0, 1.5 - bear_level * 0.5)
        r = f"趋势走弱+中位({pos:.0f}%)"
    elif is_neutral and boll_inside:
        s = 3
        r = f"趋势震荡+BOLL通道内+分位({pos:.0f}%)"
    elif is_neutral:
        s = 2
        r = f"趋势震荡+分位({pos:.0f}%)"
    else:
        s = 2
        r = f"趋势{trend}+分位({pos:.0f}%)"

    # 成交额放量/缩量调节
    is_volume_up = amt_ratio > 1.5
    is_shrink = amt_ratio < 0.7
    if is_volume_up and s < 5:
        s = min(5, s + 1)
        r += f"; 放量({amt_ratio:.1f}x/{amt_today:.0f}亿)"
    elif is_shrink and s > 0:
        s = max(0, s - 1)
        r += f"; 缩量({amt_ratio:.1f}x)"

    # 3年CAGR成长加分
    if cagr3 is not None:
        if cagr3 >= 50:
            s = min(5, s + 1)
            r += f"; 超高成长(CAGR3={cagr3:.0f}%)+1"
        elif cagr3 >= 25:
            s = min(5, s + 0.5)
            r += f"; 高成长(CAGR3={cagr3:.0f}%)+0.5"

    boll_label = ""
    if boll_pos is not None:
        if boll_pos > 100:
            boll_label = "突破上轨"
        elif boll_pos > 80:
            boll_label = "上轨附近"
        elif boll_pos > 50:
            boll_label = "中轨上方"
        elif boll_pos > 20:
            boll_label = "中轨下方"
        elif boll_pos > 0:
            boll_label = "下轨附近"
        else:
            boll_label = "跌破下轨"
        r += f"; BOLL:{boll_label}({boll_pos:.0f}%)"

    detail = {
        "ma5": round(ma5v, 1) if ma5v else None,
        "ma10": round(ma10v, 1) if ma10v else None,
        "ma20": round(ma20v, 1) if ma20v else None,
        "ma60": round(ma60v, 1) if ma60v else None,
        "wma5": round(wma5, 1) if wma5 else None,
        "wma10": round(wma10, 1) if wma10 else None,
        "wma20": round(wma20, 1) if wma20 else None,
        "trend": trend,
        "bull_level": bull_level,
        "position": round(pos, 0),
        "vol_ratio": round(vr, 2),
        "amount_ratio": round(amt_ratio, 2),
        "amount_today_yi": round(amt_today, 1),
        "boll_upper": round(boll_upper, 2) if boll_upper else None,
        "boll_mid": round(boll_mid, 2) if boll_mid else None,
        "boll_lower": round(boll_lower, 2) if boll_lower else None,
        "boll_pos": round(boll_pos, 1) if boll_pos is not None else None,
        "boll_label": boll_label if boll_label else None,
        "reason": r,
    }

    # ===== 技术面增强：综合判断 + 通俗解释 + 支撑压力位 + 量价分析 =====

    # 1. 综合判断
    if bull_level >= 2 and pos < 80 and not is_shrink:
        overall = "强势"
    elif bull_level >= 1 and pos < 95:
        overall = "偏强"
    elif bear_level >= 2 or (bear_level >= 1 and pos >= 60):
        overall = "弱势"
    elif bear_level >= 1:
        overall = "偏弱"
    else:
        overall = "震荡"
    detail["overall"] = overall

    # 2. 指标通俗解释
    explanations = []
    if trend == "双线多头":
        explanations.append("日线和周线均线均多头排列，中长趋势向上")
    elif trend == "周线多头":
        explanations.append("周线多头排列但日线未确认，中线偏强")
    elif trend == "日线多头":
        explanations.append("日线多头排列但周线未确认，短线偏强")
    elif trend in ("双线空头",):
        explanations.append("日线和周线均空头排列，中长趋势向下")
    elif trend == "金叉":
        explanations.append("MA5上穿MA20，短线买点信号")
    elif trend == "死叉":
        explanations.append("MA5下穿MA20，短线卖点信号")
    if boll_label:
        boll_explain = {
            "突破上轨": "价格突破BOLL上轨，短期超买注意回调",
            "上轨附近": "接近BOLL上轨压力位，上涨空间有限",
            "中轨上方": "BOLL中轨上方运行，多头占优",
            "中轨下方": "BOLL中轨下方运行，空头占优",
            "下轨附近": "接近BOLL下轨支撑位，可能反弹",
            "跌破下轨": "价格跌破BOLL下轨，短期超卖可能反弹",
        }
        if boll_label in boll_explain:
            explanations.append(boll_explain[boll_label])
    if pos >= 90:
        explanations.append(f"价格处于120日{pos:.0f}%分位，接近区间高点")
    elif pos <= 10:
        explanations.append(f"价格处于120日{pos:.0f}%分位，接近区间低点")
    detail["explanations"] = explanations

    # 3. 关键支撑位、压力位
    support_levels = []
    resistance_levels = []
    # 支撑位：BOLL下轨、MA20、MA60、120日低点
    if boll_lower:
        support_levels.append(("BOLL下轨", round(boll_lower, 2)))
    if ma20v:
        support_levels.append(("MA20均线", round(ma20v, 2)))
    if ma60v:
        support_levels.append(("MA60均线", round(ma60v, 2)))
    support_levels.append(("120日低点", round(lo, 2)))
    # 压力位：BOLL上轨、MA5（空头时）、120日高点
    if boll_upper:
        resistance_levels.append(("BOLL上轨", round(boll_upper, 2)))
    if ma5v and is_bearish and ma5v > price:
        resistance_levels.append(("MA5均线", round(ma5v, 2)))
    if ma60v and price < ma60v:
        resistance_levels.append(("MA60均线", round(ma60v, 2)))
    resistance_levels.append(("120日高点", round(hi, 2)))
    # 去重并排序
    support_levels.sort(key=lambda x: -x[1])
    resistance_levels.sort(key=lambda x: x[1])
    detail["support_levels"] = support_levels[:3]
    detail["resistance_levels"] = resistance_levels[:3]

    # 4. 量价配合分析
    vol_price_analysis = ""
    if is_volume_up and pct > 3:
        vol_price_analysis = "放量上涨，资金积极买入，多头量价配合"
    elif is_volume_up and pct < -3:
        vol_price_analysis = "放量下跌，抛压较大，空头量价配合"
    elif is_volume_up and abs(pct) < 1:
        vol_price_analysis = "放量滞涨，分歧加大，注意方向选择"
    elif is_shrink and pct > 3:
        vol_price_analysis = "缩量上涨，上涨动力不足"
    elif is_shrink and pct < -3:
        vol_price_analysis = "缩量下跌，抛压减轻，可能接近底部"
    elif is_shrink and abs(pct) < 1:
        vol_price_analysis = "缩量震荡，市场观望"
    else:
        vol_price_analysis = "量价基本平衡"
    detail["vol_price_analysis"] = vol_price_analysis

    return max(0, min(5, s)), detail


# ============================================================
# 2. 基本面 (0-5) × 25%
# ============================================================
def parse_fundamentals(text):
    """从iFinD输出提取: pe_ttm, pb, growth%, fwd_pe, roe, gross_margin"""
    d = {"pe_ttm": None, "pb": None, "growth": None, "fwd_pe": None, "roe": None, "gross_margin": None}
    if not text:
        return d

    # Table-style extraction
    for line in text.split("\n"):
        line = line.strip()

        # PE TTM: find numeric value in PE rows
        if any(kw in line for kw in ["市盈率(TTM)", "静态市盈率", "最新市盈率ttm"]):
            nums = re.findall(r'(?:^|\|)\s*([\d\.]+)\s*(?:\||$)', line)
            for n in nums:
                try:
                    v = float(n)
                    if 1 < v < 5000 and d["pe_ttm"] is None:
                        d["pe_ttm"] = v
                except: pass

        # Forward PE
        if "市盈率(预测)" in line:
            parts = [p.strip() for p in line.split("|")]
            for i, p in enumerate(parts):
                if "市盈率(预测)" in p and i + 1 < len(parts):
                    try:
                        v = float(parts[i + 1])
                        if 0 < v < 5000:
                            d["fwd_pe"] = v
                    except: pass

        # Growth
        if "扣非净利润增长率" in line:
            m = re.search(r'([\-\d\.]+)\s*%', line)
            if m:
                try: d["growth"] = float(m.group(1))
                except: pass

        # PB
        if "市净率" in line and "MRQ" in line:
            m = re.search(r'市净率[^|]*\|\s*([\d\.]+)', line)
            if m:
                try: d["pb"] = float(m.group(1))
                except: pass

        # ROE
        if "ROE" in line:
            nums = re.findall(r'([\d\.]+)', line)
            for n in nums:
                try:
                    v = float(n)
                    if 0.01 < v < 2:
                        d["roe"] = v
                        break
                except: pass

        # 毛利率
        if "毛利率" in line:
            m = re.search(r'([\d\.]+)\s*%?', line)
            if m:
                try:
                    v = float(m.group(1))
                    if 0 < v < 100:
                        d["gross_margin"] = v
                except: pass

    return d


# Known data backup (机构一致预期, 2026-06)
KNOWN = {
    "sz000725": (28.5, 45),  # 京东方A
    "sh601899": (8.9, 57),   # 紫金矿业
    "sz300750": (18.0, 35),  # 宁德时代
    "sh601138": (22.3, 42),  # 工业富联
    "sh688525": (19.0, 843), # 佰维存储
    "sz000063": (15.8, 28),  # 中兴通讯
    "sh603993": (11.3, 52),  # 洛阳钼业
    "sh688820": (256, 32),   # 盛合晶微 - 机构均值12.12亿/市值3104亿
    "sz000792": (13.9, 120), # 盐湖股份
    "sz000333": (13.8, 18),  # 美的集团
    "sh600160": (22.5, 55),  # 巨化股份
    "sz002709": (14.3, 408), # 天赐材料
    "sz002460": (20.0, 250), # 赣锋锂业
    "sz002466": (15.1, 1399),# 天齐锂业
    "sh600176": (15.5, 38),  # 中国巨石
    "sh600183": (25.0, 60),  # 生益科技
    "sh603986": (55.0, 80),  # 兆易创新
    "sz002475": (22.0, 35),  # 立讯精密
    "sz300502": (40.0, 55),  # 新易盛
    "sz300308": (45.0, 50),  # 中际旭创
    "sh600522": (18.0, 109),  # 中天科技 - 机构预测2026净利60.65亿 vs 2025年29.02亿
    "sh688256": (134.5, 177),# 寒武纪
    "sh688041": (149.4, 74), # 海光信息
    "sh688012": (96, 47),   # 中微公司 - 18家机构均值31.06亿/市值2989亿
    "sz300274": (18.0, 25),  # 阳光电源
    "sh600584": (30.0, 29),  # 长电科技
    "sh600487": (28.0, 40),  # 亨通光电
    "sz301308": (22.0, 180), # 江波龙
    "sh688008": (45.0, 65),  # 澜起科技
    "sz300394": (45.0, 50),  # 天孚通信
    "sz300666": (45.0, 40),  # 江丰电子
    "sh600498": (35.0, 35),  # 烽火通信
    "sz300476": (25.0, 40),  # 胜宏科技
    "sh600549": (15.0, 30),  # 厦门钨业
    "sh688981": (55.0, 45),  # 中芯国际
    "sh688072": (80, 80),   # 拓荆科技 - 11家机构均值16.99亿/市值1356亿
    "sz002407": (35.0, 35),  # 多氟多
    "sz300433": (25.0, 35),  # 蓝思科技
    "sz002371": (35.0, 50),  # 北方华创
    "sz001309": (18.0, 200), # 德明利
    "sz300285": (30.0, 30),  # 国瓷材料
    "sz000988": (35.0, 40),  # 华工科技
    "sz002463": (28.0, 40),  # 沪电股份
    "sz300136": (35.0, 30),  # 信维通信
    "sz300346": (40.0, 45),  # 南大光电
    "sz300475": (20.0, 60),  # 香农芯创
    "sz002409": (28.0, 40),  # 雅克科技
    "sz002384": (35.0, 40),  # 东山精密
    "sz300604": (35.0, 45),  # 长川科技
    "sz002281": (40.0, 45),  # 光迅科技
    "sz002156": (25.0, 35),  # 通富微电
    "sh688498": (55.0, 80),  # 源杰科技
    "sz000657": (30.0, 35),  # 中钨高新
    "sz002428": (50.0, 40),  # 云南锗业
    "sh688126": (80.0, 50),  # 沪硅产业
    "sh688146": (50.0, 40),  # 中船特气
    "sh603738": (25.0, 1200), # 泰晶科技 - 机构预测2026净利6.5-8.5亿 vs 2025年0.52亿
    "sz000021": (40.6, 47),   # 深科技 - 国投证券预测2026净利16.64亿/市值675亿,增速46.5%
}

INDUSTRY_PROSPECT = {
    "sz300750": 1,  # 宁德时代 - 新能源车电池全球龙头
    "sh601138": 1,  # 工业富联 - AI服务器全球龙头
    "sh688525": 1,  # 佰维存储 - AI存储芯片龙头
    "sh688256": 1,  # 寒武纪 - AI芯片国产龙头
    "sh688041": 1,  # 海光信息 - 国产CPU/GPU龙头
    "sz300308": 1,  # 中际旭创 - 光模块全球龙头
    "sz300502": 1,  # 新易盛 - 光模块一线
    "sz002475": 1,  # 立讯精密 - 消费电子精密制造龙头
    "sh603738": 1,  # 泰晶科技 - 光模块晶振国内唯一
    "sz002371": 1,  # 北方华创 - 半导体设备龙头
    "sh688981": 1,  # 中芯国际 - 晶圆代工龙头
    "sh688012": 1,  # 中微公司 - 刻蚀设备龙头
    "sh688072": 1,  # 拓荆科技 - 半导体薄膜设备龙头
    "sz300394": 1,  # 天孚通信 - 光通信器件龙头
    "sh600522": 1,  # 中天科技 - 海缆+光通信龙头
    "sz002594": 1,  # 比亚迪 - 新能源车龙头
    "sh601899": 1,  # 紫金矿业 - 有色资源全球龙头
    "sz000792": 1,  # 盐湖股份 - 锂资源龙头
    "sz002460": 1,  # 赣锋锂业 - 锂电资源龙头
    "sz002466": 1,  # 天齐锂业 - 锂资源龙头
    "sz301377": 1,  # 鼎泰高科 - PCB钻针全球龙头
    "sh688008": 1,  # 澜起科技 - 内存接口芯片龙头
    "sz002428": 1,  # 云南锗业 - 稀有锗资源龙头
    "sh600183": 1,  # 生益科技 - 覆铜板龙头
    "sz300476": 1,  # 胜宏科技 - PCB/HDI龙头
    "sz002463": 1,  # 沪电股份 - 高端PCB龙头
    "sh688820": 1,  # 盛合晶微 - 先进封装龙头
    "sz000063": 0.5,  # 中兴通讯 - 通信设备一线
    "sh600487": 0.5,  # 亨通光电 - 光通信一线
    "sh600498": 0.5,  # 烽火通信 - 光通信一线
    "sz002384": 0.5,  # 东山精密 - 电子制造一线
    "sz300433": 0.5,  # 蓝思科技 - 消费电子一线
    "sh600584": 0.5,  # 长电科技 - 封装测试一线
    "sz002156": 0.5,  # 通富微电 - 封装测试一线
    "sh603986": 0.5,  # 兆易创新 - 存储芯片一线
    "sz300666": 0.5,  # 江丰电子 - 靶材一线
    "sz300346": 0.5,  # 南大光电 - 光刻胶一线
    "sz300604": 0.5,  # 长川科技 - 半导体测试一线
    "sh688808": 0.5,  # 联讯仪器 - 半导体测试一线
    "sh688498": 0.5,  # 源杰科技 - 光芯片一线
    "sz000988": 0.5,  # 华工科技 - 激光+光模块一线
    "sz002281": 0.5,  # 光迅科技 - 光通信器件一线
    "sz002409": 0.5,  # 雅克科技 - 半导体材料一线
    "sz002407": 0.5,  # 多氟多 - 氟化工+电子材料一线
    "sz000725": 0.5,  # 京东方A - 面板一线
    "sh600160": 0.5,  # 巨化股份 - 氟化工一线
    "sz002709": 0.5,  # 天赐材料 - 电解液一线
    "sz300274": 0.5,  # 阳光电源 - 逆变器一线
    "sh603993": 0.5,  # 洛阳钼业 - 有色资源一线
    "sh600176": 0.5,  # 中国巨石 - 玻纤一线
    "sz301308": 0.5,  # 江波龙 - 存储模组一线
    "sz300136": 0.5,  # 信维通信 - 天线一线
    "sz422003": -0.5, # 某衰退行业示例
}

INDUSTRY_PROSPECT_LABELS = {
    1: "顶级赛道+绝对龙头",
    0.5: "优质赛道+一线标的",
    0: "成熟赛道+普通标的",
    -0.5: "衰退/受限赛道+弱势标的",
}


def score_fund(pe, fwd_pe, growth, code, sector, pe_ladder_data=None,
               roe=None, gross_margin=None):
    """基本面+竞争力评分 0-5，用估值阶梯替代单年PEG
    v2.8: 增加PEG指标、ROE、毛利率评分"""
    effective_pe = fwd_pe if fwd_pe else pe
    if code in KNOWN:
        k_fwd, k_growth = KNOWN[code]
        if effective_pe is None or effective_pe <= 0:
            effective_pe = k_fwd
        if growth is None:
            growth = k_growth

    reasons = []
    _fwd_pe_out = effective_pe if (fwd_pe or code in KNOWN or effective_pe) else None
    _growth_out = growth if growth is not None else None
    _ladder_out = pe_ladder_data or {}
    if effective_pe is None or effective_pe <= 0:
        if growth is None:
            return 2, ["数据不足"], _fwd_pe_out, _growth_out, _ladder_out
        if growth > 100:
            return 3, [f"高增长(+{growth:.0f}%)但缺估值"], _fwd_pe_out, _growth_out, _ladder_out
        elif growth > 20:
            return 3, [f"中速增长(+{growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out
        elif growth > 0:
            return 2, [f"低增长(+{growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out
        else:
            return (1 if growth > -20 else 0), [f"负增长({growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out

    if growth is None:
        growth = 15

    # 基础评分：基于FwdPE + 增速
    if effective_pe < 15 and growth > 50:
        s = 5
        reasons.append(f"低估值(FwdPE={effective_pe:.1f})+高增长(+{growth:.0f}%)")
    elif effective_pe < 20 and growth > 30:
        s = 4
        reasons.append(f"合理估值(FwdPE={effective_pe:.1f})+高增长(+{growth:.0f}%)")
    elif effective_pe < 30 and growth > 20:
        s = 4
        reasons.append(f"估值合理(FwdPE={effective_pe:.1f})+增速(+{growth:.0f}%)")
    elif effective_pe < 40 and growth > 15:
        s = 3
        reasons.append(f"估值中等(FwdPE={effective_pe:.1f})+增速(+{growth:.0f}%)")
    elif effective_pe < 60 and growth > 10:
        s = 3
        reasons.append(f"估值偏高(FwdPE={effective_pe:.1f})+适度增长(+{growth:.0f}%)")
    elif effective_pe < 100 and growth > 20:
        s = 2
        reasons.append(f"高估值(FwdPE={effective_pe:.1f})但有增长(+{growth:.0f}%)")
    elif effective_pe < 80:
        s = 2
        reasons.append(f"估值偏高(FwdPE={effective_pe:.1f})+增长一般(+{growth:.0f}%)")
    elif growth < 0:
        s = 1 if growth > -20 else 0
        reasons.append(f"高估值(FwdPE={effective_pe:.1f})+负增长({growth:.0f}%)")
    else:
        s = 1
        reasons.append(f"高估值(FwdPE={effective_pe:.1f})")

    # 估值阶梯评分（替代单年PEG，高PE起始打折）
    if pe_ladder_data and len(pe_ladder_data) >= 2:
        years_sorted = sorted(pe_ladder_data.keys())
        pe_first = pe_ladder_data[years_sorted[0]]
        pe_last = pe_ladder_data[years_sorted[-1]]
        if pe_first > 0:
            shrink_rate = (pe_first - pe_last) / pe_first
            ladder_str = "→".join(f"{pe_ladder_data[y]}" for y in years_sorted)
            if pe_first >= 40:
                ladder_bonus_cap = 1
                pe_note = "高PE起始,阶梯加分受限"
            elif pe_first >= 25:
                ladder_bonus_cap = 1
                pe_note = "中PE起始,阶梯加分适度"
            else:
                ladder_bonus_cap = 2
                pe_note = ""
            if shrink_rate >= 0.5:
                bonus = min(ladder_bonus_cap, 2)
                s = min(5, s + bonus)
                reasons.append(f"估值阶梯强收缩({ladder_str}),3年收缩{shrink_rate:.0%}" + (f",{pe_note}" if pe_note else ""))
            elif shrink_rate >= 0.3:
                bonus = min(ladder_bonus_cap, 1)
                s = min(5, s + bonus)
                reasons.append(f"估值阶梯明显收缩({ladder_str}),3年收缩{shrink_rate:.0%}" + (f",{pe_note}" if pe_note else ""))
            elif shrink_rate >= 0.15:
                reasons.append(f"估值阶梯合理收缩({ladder_str}),3年收缩{shrink_rate:.0%}")
            elif shrink_rate >= 0:
                s = max(0, s - 1)
                reasons.append(f"估值阶梯收缩不足({ladder_str}),3年仅收缩{shrink_rate:.0%}")
            else:
                s = max(0, s - 2)
                reasons.append(f"估值阶梯扩张({ladder_str}),估值轨道恶化")
    elif effective_pe < 15:
        reasons.append(f"低估值(FwdPE={effective_pe:.1f})无阶梯数据,保守加分")
        s = min(5, s + 1)

    # ===== PEG指标评分 (PE/增速比) =====
    peg = None
    if effective_pe and growth and growth > 0:
        peg = effective_pe / growth
        if peg < 0.5:
            s = min(5, s + 0.5)
            reasons.append(f"PEG={peg:.2f}(严重低估,增速远超估值)")
        elif peg < 1:
            reasons.append(f"PEG={peg:.2f}(低估,增速覆盖估值)")
        elif peg < 1.5:
            reasons.append(f"PEG={peg:.2f}(合理估值)")
        elif peg < 2:
            reasons.append(f"PEG={peg:.2f}(轻度高估)")
        elif peg < 3:
            reasons.append(f"PEG={peg:.2f}(偏高估值)")
            s = max(0, s - 0.5)
        else:
            reasons.append(f"PEG={peg:.2f}(严重高估)")
            s = max(0, s - 1)

    # ===== ROE评分 =====
    if roe is not None and roe > 0:
        if roe >= 0.3:
            s = min(5, s + 0.5)
            reasons.append(f"ROE={roe*100:.1f}%(优秀盈利能力)")
        elif roe >= 0.15:
            reasons.append(f"ROE={roe*100:.1f}%(良好盈利能力)")
        elif roe >= 0.08:
            reasons.append(f"ROE={roe*100:.1f}%(一般盈利能力)")
        elif roe < 0.03:
            s = max(0, s - 0.5)
            reasons.append(f"ROE={roe*100:.1f}%(盈利能力弱)")

    # ===== 毛利率评分 =====
    if gross_margin is not None and gross_margin > 0:
        if gross_margin >= 50:
            reasons.append(f"毛利率{gross_margin:.0f}%(高毛利护城河)")
        elif gross_margin >= 30:
            reasons.append(f"毛利率{gross_margin:.0f}%(中等毛利)")
        elif gross_margin < 15:
            reasons.append(f"毛利率{gross_margin:.0f}%(低毛利竞争激烈)")

    return s, reasons, _fwd_pe_out, _growth_out, _ladder_out


# ============================================================
# 3. 题材热度 (0-5) × 20%
# 评分标准: 板块轮动强度 + 主线地位 + 个股龙头性 + 产业催化持续性
# 5: 绝对主线板块(近10日成交占比前3,连续领涨)+板块核心龙头+持续催化
# 4: 主线或强支线赛道+重要标的+阶段性催化
# 3: 中性常规赛道,非主线也非冷门
# 2: 冷门支线题材,仅单日脉冲行情
# 1: 长期边缘赛道,极少进入涨幅榜
# 0: 利空衰退赛道,持续资金净流出
# ============================================================

# 题材热度知识库（基于当日成交额TOP50出现频次+主线判断+持续催化）
THEME_DB = {
    # ---- 当日绝对主线 ----
    "AI/半导体": {
        "score": 5,
        "label": "AI算力/半导体",
        "reason": "全市场绝对主线：近10日成交占比前1,AI服务器/光模块/存储/设备连续领涨",
        "top5": ["中际旭创","新易盛","天孚通信","海光信息","寒武纪"],
    },
    "有色资源": {
        "score": 3,
        "label": "有色金属/矿业资源",
        "reason": "铜金价格高位震荡+稀土/战略金属阶段性轮动,非当前市场绝对主线",
        "top5": ["紫金矿业","洛阳钼业","云南锗业","厦门钨业","天齐锂业"],
    },
    # ---- 主线赛道 ----
    "新能源": {
        "score": 4,
        "label": "新能源/储能",
        "reason": "新能源底部企稳反弹,锂电+储能+光伏逆变器有阶段性催化,宁德时代+3.3%",
        "top5": ["宁德时代","阳光电源"],
    },
    "电子材料": {
        "score": 4,
        "label": "电子材料/PCB",
        "reason": "AI服务器带动PCB/覆铜板/电子材料强劲需求,为AI算力的强支线赛道",
        "top5": ["生益科技","沪电股份","胜宏科技","东山精密"],
    },
    "电子制造": {
        "score": 4,
        "label": "消费电子/代工制造",
        "reason": "AI终端+苹果链催化,立讯精密/蓝思科技受益于新品发布",
        "top5": ["立讯精密","蓝思科技"],
    },
    "特种材料": {
        "score": 4,
        "label": "特种化工/电子气体",
        "reason": "六氟化钨/特种气体国产替代+半导体材料国产化加速,中船特气为行业龙头",
        "top5": ["中船特气"],
    },
    # ---- 中性赛道 ----
    "化工": {
        "score": 3,
        "label": "基础化工",
        "reason": "氟化工/磷化工随大宗商品波动,无持续主线催化,偶尔跟随有色板块轮动",
        "top5": ["巨化股份","多氟多"],
    },
    "基础材料": {
        "score": 3,
        "label": "基础建材/功能材料",
        "reason": "国瓷材料(MLCC)+中国巨石(玻纤),为中性功能材料赛道,无持续主线催化",
        "top5": ["国瓷材料","中国巨石"],
    },
    "家电制造": {
        "score": 3,
        "label": "家电/消费品",
        "reason": "美的集团稳健运营,消费复苏逻辑但非当前市场主线题材",
        "top5": ["美的集团"],
    },
    "电子面板": {
        "score": 2,
        "label": "面板/显示",
        "reason": "面板行业供给宽松+价格弱势,当前为冷门支线题材",
        "top5": ["京东方A","TCL科技"],
    },
    "综合": {
        "score": 3,
        "label": "综合/其他",
        "reason": "所属板块为中性常规赛道,非主线也非冷门",
        "top5": [],
    },
}

# 细分题材标签（解决同板块内区分度问题）
SUB_THEME = {
    "中际旭创": ("光模块", 5), "新易盛": ("光模块", 5), "天孚通信": ("光模块/CPO", 5),
    "光库科技": ("光通信", 4), "长飞光纤": ("光通信/光纤", 4), "亨通光电": ("光通信/海缆", 4),
    "中天科技": ("光通信/海缆", 4), "烽火通信": ("光通信设备", 4),
    "海光信息": ("AI芯片/CPU", 5), "寒武纪": ("AI芯片/GPU", 5), "龙芯中科": ("国产CPU", 4),
    "兆易创新": ("存储芯片", 5), "佰维存储": ("存储芯片", 5), "澜起科技": ("内存接口", 5),
    "北方华创": ("半导体设备", 5), "中微公司": ("半导体设备", 5), "盛美上海": ("半导体设备", 4),
    "芯源微": ("半导体设备", 4), "中科飞测": ("半导体量测", 4),
    "沪硅产业": ("硅片", 4), "立昂微": ("硅片", 3),
    "中芯国际": ("晶圆代工", 5), "晶合集成": ("晶圆代工", 4),
    "江丰电子": ("溅射靶材", 4), "有研新材": ("半导体材料", 3),
    "安集科技": ("CMP抛光液", 4), "华海清科": ("CMP设备", 4),
    "生益科技": ("覆铜板/PCB", 4), "沪电股份": ("PCB", 4), "胜宏科技": ("PCB", 4),
    "东山精密": ("PCB/封装", 4), "深南电路": ("PCB/封装", 4),
    "紫金矿业": ("铜金矿", 4), "洛阳钼业": ("铜钴矿", 4), "云南锗业": ("稀有小金属", 4),
    "锡业股份": ("锡矿", 3), "江西铜业": ("铜矿", 3), "铜陵有色": ("铜冶炼", 2),
    "宁德时代": ("锂电池", 5), "阳光电源": ("逆变器", 4), "天赐材料": ("电解液", 4),
    "立讯精密": ("消费电子/代工", 4), "蓝思科技": ("消费电子/玻璃", 3),
    "京东方Ａ": ("玻璃基板/TGV", 4), "京东方A": ("玻璃基板/TGV", 4),
    "中船特气": ("电子特气", 4), "华特气体": ("电子特气", 4),
    "国瓷材料": ("MLCC/陶瓷", 3), "中国巨石": ("玻纤", 3),
    "仕佳光子": ("光芯片/PLC", 5), "源杰科技": ("光芯片/激光器", 5),
    "长光华芯": ("激光芯片", 4), "联特科技": ("光模块", 4),
    "圣邦股份": ("模拟芯片", 4), "晶方科技": ("封测", 3),
    "紫光国微": ("FPGA/安全芯片", 4), "复旦微电": ("FPGA", 4),
    "通富微电": ("封测", 3), "长电科技": ("封测", 3),
    "飞凯材料": ("光刻胶", 3), "南大光电": ("光刻胶/MO源", 4),
    "风华高科": ("MLCC/被动元件", 3), "火炬电子": ("MLCC/电容", 3),
    "三安光电": ("LED/化合物半导体", 3),
    "万华化学": ("MDI/化工", 3), "巨化股份": ("氟化工", 3),
    # ---- 有色资源 ----
    "盛和资源": ("稀土", 4), "北方稀土": ("稀土", 4), "中国稀土": ("稀土", 3),
    "赣锋锂业": ("锂矿", 3), "天齐锂业": ("锂矿", 3), "盐湖股份": ("锂盐湖", 3),
    "华友钴业": ("钴镍", 4), "盛屯矿业": ("铜钴矿", 3), "厦门钨业": ("钨矿", 3),
    "中钨高新": ("硬质合金/钨", 3), "翔鹭钨业": ("钨矿", 2), "章源钨业": ("钨矿", 2),
    "中国铝业": ("铝", 2), "兴业银锡": ("锡银矿", 2), "红星发展": ("锰/钡", 2),
    "融捷股份": ("锂矿", 3), "盛新锂能": ("锂盐", 3),
    # ---- 新能源 ----
    "亿纬锂能": ("锂电池", 5), "天华新能": ("锂电材料", 4), "嘉元科技": ("铜箔", 4),
    "诺德股份": ("铜箔", 3), "铜冠铜箔": ("铜箔", 3), "德福科技": ("铜箔", 3),
    "金风科技": ("风电", 3), "迈为股份": ("光伏设备", 4), "英维克": ("温控/液冷", 4),
    "三花智控": ("热管理", 4),
    # ---- 半导体/AI ----
    "江波龙": ("存储模组", 5), "德明利": ("存储控制器", 4), "协创数据": ("存储/云服务", 4),
    "长川科技": ("半导体测试", 4), "华天科技": ("封测", 3),
    "奥比中光": ("3D视觉/AI", 4), "北京君正": ("存储/处理器", 4),
    "拓荆科技": ("半导体设备/CVD", 5), "盛合晶微": ("先进封装", 4),
    "长芯博创": ("光模块/AOC", 5), "华虹宏力": ("晶圆代工", 4),
    "芯原股份": ("芯片IP", 4), "普冉股份": ("存储芯片", 4), "国科微": ("SSD/视频芯片", 4),
    "太辰光": ("光连接器", 4), "光迅科技": ("光模块/器件", 5), "剑桥科技": ("光模块", 5),
    "华工科技": ("激光/光模块", 5), "信维通信": ("天线/射频", 3),
    "香农芯创": ("存储分销", 3), "炬光科技": ("激光器", 4),
    "大族激光": ("激光设备", 4), "锐科激光": ("光纤激光器", 4),
    "兴森科技": ("PCB/IC载板", 4), "生益电子": ("PCB", 4),
    "深科技": ("存储模组/代工", 4), "太极实业": ("半导体工程", 3),
    "康强电子": ("引线框架", 3), "领益智造": ("消费电子/精密件", 3),
    "雅克科技": ("光刻胶/前驱体", 4), "鼎龙股份": ("CMP材料", 4),
    "彤程新材": ("光刻胶", 4), "南大光电": ("光刻胶/MO源", 4),
    "菲利华": ("石英玻璃/半导体", 4), "石英股份": ("石英材料", 3),
    "中巨芯": ("电子湿化学品", 3), "XD兴福电": ("电子特气", 3),
    "和远气体": ("电子特气", 3), "阿石创": ("PVD靶材", 3),
    "XD昊华科": ("氟材料/特种气体", 3),
    # ---- 消费电子/制造 ----
    "工业富联": ("AI服务器", 5), "鹏鼎控股": ("PCB/FPC", 4),
    "TCL科技": ("面板/半导体", 3), "利通电子": ("液晶模组", 3),
    "联讯仪器": ("半导体测试", 4), "精智达": ("半导体检测", 4),
    "鼎泰高科": ("PCB钻针", 5),
    "泰晶科技": ("光模块晶振", 5),
    "罗博特科": ("光伏设备", 3), "沃格光电": ("玻璃基板", 3),
    "英唐智控": ("电子分销", 2), "宏和科技": ("电子布/玻纤", 3),
    "华正新材": ("覆铜板", 3), "金安国纪": ("覆铜板", 3),
    "东材科技": ("绝缘材料/光学膜", 3),
    # ---- 通信/算力 ----
    "中兴通讯": ("5G/算力设备", 4), "永鼎股份": ("光缆/超导", 3),
    "通鼎互联": ("光缆/电缆", 3), "宏景科技": ("算力服务", 4),
    "莲花控股": ("算力租赁", 4), "天娱数科": ("AI/数字人", 3),
    # ---- 高端制造/军工 ----
    "中国船舶": ("造船", 4), "中国卫通": ("卫星通信", 3), "中国卫星": ("卫星制造", 3),
    "航天电子": ("航天电子", 3), "埃斯顿": ("工业机器人", 3),
    "绿的谐波": ("谐波减速器", 4), "汇川技术": ("工控/伺服", 4),
    "中控技术": ("DCS/工控", 4), "博云新材": ("碳纤维/航空", 3),
    "大普微": ("SSD控制器", 4), "神剑股份": ("军工电子", 3),
    "索辰科技": ("CAE仿真", 3),
    # ---- 化工/材料 ----
    "多氟多": ("氟化工/电解液", 4), "兴发集团": ("磷化工/有机硅", 3),
    "中化国际": ("化工/橡胶", 3), "隆华科技": ("传热节能/新材料", 3),
    "国际复材": ("玻纤", 3), "中材科技": ("玻纤/风电叶片", 3),
    "黄河旋风": ("超硬材料/金刚石", 3),
    # ---- 电力/能源 ----
    "大唐发电": ("火电", 2), "中国神华": ("煤炭", 2), "长江电力": ("水电", 2),
    "豫能控股": ("火电", 2), "华电辽能": ("火电", 2),
    # ---- 医药 ----
    "药明康德": ("CXO", 3), "恒瑞医药": ("创新药", 3),
    # ---- 消费/家电 ----
    "美的集团": ("家电", 3), "比亚迪": ("新能源车", 4),
    "贵州茅台": ("白酒", 2),
    # ---- 其他 ----
    "世纪华通": ("游戏/算力", 4), "杰瑞股份": ("油气设备", 3),
    "顺络电子": ("电感/被动元件", 3), "三环集团": ("陶瓷/MLCC", 3),
    "江海股份": ("电容", 3), "麦格米特": ("电源管理", 3),
    "远东股份": ("电缆/锂电铜箔", 3), "方正科技": ("PCB", 3),
    "弘信电子": ("FPC", 3), "TCL中环": ("光伏硅片", 3),
    "XD圣泉集": ("酚醛树脂/电子材料", 3), "西部材料": ("稀有金属", 3),
    "天通股份": ("蓝宝石/压电晶体", 3), "旭光电子": ("电真空器件", 2),
    "长芯博创": ("光模块/AOC", 5),
    "浪潮信息": ("AI服务器", 5), "中科曙光": ("AI服务器/算力", 5),
    "士兰微": ("功率半导体/IGBT", 4), "华润微": ("功率半导体/MCU", 4),
    "特变电工": ("变压器/新能源", 3), "德业股份": ("微型逆变器", 4),
    "新宙邦": ("电解液/氟化工", 4), "景旺电子": ("PCB/HDI", 4),
    "中矿资源": ("锂铯矿", 3), "福晶科技": ("非线性光学晶体", 4),
    "芯碁微装": ("直写光刻设备", 5), "紫光股份": ("服务器/网络设备", 4),
    "润泽科技": ("IDC/液冷算力", 4), "昊华科技": ("氟材料/特种气体", 3),
    "圣泉集团": ("酚醛树脂/电子材料", 3), "海亮股份": ("铜加工", 3),
    "宗申动力": ("航空发动机/低空经济", 4), "商络电子": ("被动元件分销", 3),
    "金钼股份": ("钼矿", 3), "杭电股份": ("电缆/光缆", 3),
    "驰宏锌锗": ("锌锗矿", 3), "永杉锂业": ("锂盐", 3),
    "利和兴": ("自动化设备/华为链", 3), "东芯股份": ("存储芯片", 4),
    "航天发展": ("数字蓝军/电子对抗", 3),
    "先导智能": ("锂电设备", 4), "中国西电": ("输变电设备", 3),
    "鹏辉能源": ("锂电池/储能", 3), "宏达电子": ("钽电容/军工电子", 4),
    "扬杰科技": ("功率半导体/SiC", 4), "振华科技": ("军工电子/MLCC", 3),
    "长信科技": ("触控屏/车载显示", 3), "鼎通科技": ("连接器", 4),
    "博迁新材": ("镍粉/MLCC材料", 3), "麦捷科技": ("SAW滤波器/电感", 4),
    "欧陆通": ("服务器电源", 4), "洁美科技": ("载带/封装材料", 3),
    "四方股份": ("继电保护/储能", 3), "海康威视": ("AI安防/机器人", 4),
    "斯迪克": ("光学膜/OCA", 3), "甬矽电子": ("封测", 4),
    "中稀有色": ("稀土", 3), "中远海能": ("油运", 3),
    "招商轮船": ("航运", 3), "天岳先进": ("SiC衬底", 4),
    "盛科通信": ("以太网交换芯片", 4), "机器人": ("工业机器人", 3),
    "四方达": ("超硬材料/金刚石", 3),
    "思源电气": ("电力设备/开关", 4), "帝尔激光": ("激光设备/光伏", 4),
    "华丰科技": ("光连接器/射频", 4), "中国长城": ("信创/国产PC", 3),
    "楚江新材": ("铜加工/碳纤维", 3), "中一科技": ("铜箔/电子材料", 3),
    "华峰测控": ("半导体测试", 4), "中国中免": ("免税/旅游", 2),
    "华宏科技": ("稀土/再生资源", 3), "东阳光": ("铝电解电容/氟化工", 3),
    "深桑达A": ("信创/云计算", 3), "同有科技": ("存储系统", 3),
    "南亚新材": ("覆铜板/FR-4", 3), "东岳硅材": ("有机硅/工业硅", 3),
    "科翔股份": ("PCB/HDI", 3), "盛龙股份": ("基础材料", 2),
    "中国能建": ("电力工程/新能源", 3), "德科立": ("光传输/DCI", 4),
    "光智科技": ("红外光学/核材料", 3), "双星新材": ("BOPET/光学膜", 3),
}

# 板块核心龙头（市值/人气前5，出现在榜可+1分）
SECTOR_LEADER = {
    "中际旭创", "新易盛", "天孚通信", "寒武纪", "海光信息",  # AI算力
    "紫金矿业", "洛阳钼业", "云南锗业", "天赐材料",           # 有色
    "宁德时代", "阳光电源",                                   # 新能源
    "生益科技", "沪电股份",                                   # PCB/材料
    "立讯精密", "中芯国际", "北方华创",                       # 制造/半导体
    "中船特气",                                               # 特种材料
}

# 自定义板块 → 新浪行业板块名称映射
SECTOR_SINA_MAP = {
    "AI/半导体": "电子器件",
    "有色资源": "有色金属",
    "新能源": "电器行业",
    "电子材料": "电子器件",
    "电子制造": "电子器件",
    "特种材料": "化工行业",
    "化工": "化工行业",
    "基础材料": "建材行业",
    "家电制造": "家电行业",
    "电子面板": "电子器件",
    "电力设备": "电器行业",
    "高端制造": "机械行业",
    "电子元件": "电子器件",
    "新材料": "化工行业",
    "计算机软件": "电子信息",
    "电力": "电力行业",
    "综合": "综合行业",
    "交通运输": "交通运输",
    "传媒娱乐": "传媒娱乐",
    "消费": "商业百货",
    "汽车": "汽车制造",
    "医药生物": "医药行业",
    "金融": "银行",
    "航天军工": "航天军工",
}


def fetch_sector_data():
    """通过新浪行业板块接口获取板块行情数据
    返回: {板块名称: {count, pct, amount, leader_code, leader_name}}
    """
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("gbk")
        json_str = text.split("=", 1)[1].strip().rstrip(";")
        data = json.loads(json_str)
        result = {}
        for code, info in data.items():
            parts = info.split(",")
            if len(parts) < 13:
                continue
            name = parts[1]
            result[name] = {
                "count": int(parts[2]),
                "pct": float(parts[4]),
                "amount": float(parts[6]),
                "leader_code": parts[8],
                "leader_name": parts[12],
            }
        return result
    except Exception as e:
        print(f"   ⚠️ 新浪板块接口失败: {e}")
        return {}


def score_theme(name, sector, pct, turnover, all_top200_sectors,
                sector_data=None, sector_zt_counts=None):
    """
    题材热度评分 (0-5): 基础分 + 板块涨幅 + 涨停验证 + 资金流入 + 生命周期
    去掉龙头加分，改为基于板块真实行情数据评分
    """
    theme = THEME_DB.get(sector, THEME_DB["综合"])
    base_score = theme["score"]
    reasons = [theme["reason"]]

    # 细分题材覆盖（优先使用细分题材评分）
    sub_label = None
    sub_match = SUB_THEME.get(name)
    if not sub_match:
        nn = _normalize_name(name)
        for prefix in ("XD", "DR", "XR"):
            if nn.startswith(prefix):
                nn = nn[len(prefix):]
                break
        sub_match = SUB_THEME.get(nn)
        if not sub_match:
            base_n = nn.rstrip("UW").rstrip("-").rstrip("U").rstrip("-")
            sub_match = SUB_THEME.get(base_n)
            if not sub_match:
                for key in SUB_THEME:
                    if base_n.startswith(key) or key.startswith(base_n):
                        sub_match = SUB_THEME[key]
                        break
    if sub_match:
        sub_label, sub_score = sub_match
        base_score = sub_score
        reasons = [f"细分题材:{sub_label}"]
    else:
        sub_label = theme["label"]
        reasons = [f"板块:{sub_label}"]

    # ===== 1. 板块近期涨跌幅 (0-1分) =====
    sector_pct = None
    if sector_data:
        sina_name = SECTOR_SINA_MAP.get(sector, "")
        sd = sector_data.get(sina_name) if sina_name else None
        if sd:
            sector_pct = sd["pct"]
            if sector_pct >= 3:
                base_score = min(5, base_score + 1)
                reasons.append(f"板块大涨{sector_pct:+.1f}%")
            elif sector_pct >= 1.5:
                base_score = min(5, base_score + 0.5)
                reasons.append(f"板块上涨{sector_pct:+.1f}%")
            elif sector_pct <= -2:
                base_score = max(0, base_score - 0.5)
                reasons.append(f"板块下跌{sector_pct:.1f}%")
            else:
                reasons.append(f"板块涨跌{sector_pct:+.1f}%")

    # ===== 2. 板块内涨停家数/上涨家数 (0-0.5分) =====
    zt_count = 0
    up_count = 0
    sector_total = 0
    if sector_zt_counts:
        zt_count = sector_zt_counts.get(sector, {}).get("zt", 0)
        up_count = sector_zt_counts.get(sector, {}).get("up", 0)
        sector_total = sector_zt_counts.get(sector, {}).get("total", 0)
    if sector_total > 0:
        up_ratio = up_count / sector_total
        if zt_count >= 3:
            base_score = min(5, base_score + 0.5)
            reasons.append(f"板块{zt_count}只涨停,做多情绪强")
        elif zt_count >= 1:
            base_score = min(5, base_score + 0.3)
            reasons.append(f"板块{zt_count}只涨停")
        if up_ratio >= 0.7:
            reasons.append(f"上涨占比{up_ratio:.0%},普涨格局")
        elif up_ratio <= 0.3:
            reasons.append(f"上涨占比仅{up_ratio:.0%},多数下跌")

    # ===== 3. 板块资金净流入 (0-0.5分) =====
    # 基于板块成交额变化推断（新浪接口无直接资金流向，用成交额+涨跌幅推断）
    if sector_data and sector_pct is not None:
        sina_name = SECTOR_SINA_MAP.get(sector, "")
        sd = sector_data.get(sina_name) if sina_name else None
        if sd and sd["amount"] > 0:
            # 成交额放大+上涨 = 资金流入信号
            amt_yi = sd["amount"] / 1e8
            if sector_pct > 1 and amt_yi > 500:
                base_score = min(5, base_score + 0.5)
                reasons.append(f"板块放量上涨(成交{amt_yi:.0f}亿),资金流入")
            elif sector_pct > 0.5 and amt_yi > 200:
                base_score = min(5, base_score + 0.2)
                reasons.append(f"板块温和放量(成交{amt_yi:.0f}亿)")

    # ===== 4. 题材生命周期判断 =====
    lifecycle = "震荡期"
    if sector_pct is not None and sector_total > 0:
        up_ratio = up_count / sector_total if sector_total > 0 else 0
        if sector_pct >= 3 and zt_count >= 3 and up_ratio >= 0.7:
            lifecycle = "高潮期"
            reasons.append(f"⚠️题材处于高潮期,注意追高风险")
        elif sector_pct >= 1.5 and zt_count >= 1 and up_ratio >= 0.5:
            lifecycle = "加速期"
            reasons.append(f"题材处于加速期,趋势延续中")
        elif sector_pct >= 0 and zt_count >= 1:
            lifecycle = "启动期"
            reasons.append(f"题材处于启动期,可关注")
        elif sector_pct < -1:
            lifecycle = "退潮期"
            base_score = max(0, base_score - 0.5)
            reasons.append(f"题材处于退潮期,短期回避")
        else:
            reasons.append(f"题材处于{lifecycle}")
    else:
        reasons.append(f"题材处于{lifecycle}(无板块数据)")

    # 板块集中度：同板块在TOP200中的数量
    sector_count = all_top200_sectors.count(sector)
    if sector_count >= 15:
        reasons.append(f"板块高度集中({sector_count}只上榜)")
    elif sector_count >= 10:
        reasons.append(f"板块集中({sector_count}只上榜)")
    elif sector_count >= 5:
        reasons.append(f"板块有一定关注度({sector_count}只上榜)")

    # 今日个股涨幅加成
    if pct >= 9.9:
        reasons.append(f"涨停确认主线强度")
    elif pct >= 5 and base_score >= 4:
        reasons.append(f"大涨{pct:+.1f}%验证主线")
    elif pct < -7 and base_score >= 4:
        reasons.append(f"获利回吐{pct:.1f}%,短期降温")
        base_score = max(3, base_score - 1)

    return max(0, min(5, round(base_score * 2) / 2)), reasons, sub_label, lifecycle


# ============================================================
# 4. 消息面 (0-5) × 30%
# 模型规则:
#   5: 重大利好（业绩超50%/百亿BD/国家级政策）+ 大涨>7%
#   4: 一般利好（业绩超10%-50%/行业政策）+ 上涨3%-7%
#   3: 中性消息（业绩符合预期）+ 涨跌<3%
#   2: 小幅利空（业绩略低/高管减持）+ 下跌1%-3%
#   1: 一般利空（业绩低20%+/监管警示）+ 下跌3%-7%
#   0: 重大利空（暴雷/立案/退市）+ 跌停/大跌>7%
# ============================================================

# 已知重大催化事件（来自财经情报局/公开来源核验）
KNOWN_CATALYSTS = {
    "sh600498": {  # 烽火通信 - 光通信+算力网络
        "level": 4,
        "reason": "光通信龙头+算力网络建设提速+AI光模块需求增长+海外市场拓展",
        "risk": "行业竞争加剧+原材料价格波动"
    },
    "sh600522": {  # 中天科技 - 海缆+光通信
        "level": 4,
        "reason": "海缆业务爆发+光通信全产业链布局+新能源协同发展",
        "risk": "海缆价格战+新能源投资进度"
    },
    "sh600183": {  # 生益科技 - 覆铜板龙头
        "level": 4,
        "reason": "覆铜板龙头+高端材料国产替代+AI服务器需求增长",
        "risk": "原材料价格波动+行业竞争加剧"
    },
    "sh603986": {  # 兆易创新 - 存储芯片
        "level": 4,
        "reason": "存储芯片龙头+NOR Flash全球前三+MCU国产替代加速",
        "risk": "存储芯片价格周期波动"
    },
    "sh600487": {  # 亨通光电 - 海缆+光通信
        "level": 4,
        "reason": "海缆业务高速增长+光通信全产业链布局+海外市场突破",
        "risk": "海缆行业竞争加剧+原材料价格波动"
    },
    "sz300666": {  # 江丰电子 - 溅射靶材
        "level": 4,
        "reason": "溅射靶材国产替代龙头+半导体原材料需求增长+进入台积电供应链",
        "risk": "技术迭代风险+客户集中度高"
    },
    "sz002463": {  # 沪电股份 - PCB龙头
        "level": 4,
        "reason": "AI服务器PCB核心供应商+高端通信板放量+汽车电子增长",
        "risk": "行业竞争加剧+原材料价格波动"
    },
    "sz002409": {  # 雅克科技 - 半导体材料
        "level": 4,
        "reason": "前驱体国产化龙头+电子特气布局+半导体材料多品类扩张",
        "risk": "产品验证周期长+技术迭代风险"
    },
    "sz002709": {  # 天赐材料 - 电解液龙头
        "level": 4,
        "reason": "电解液龙头地位稳固+新型锂盐放量+海外客户拓展",
        "risk": "锂电材料价格战+产能过剩风险"
    },
    "sz000063": {  # 中兴通讯 - 通信设备龙头
        "level": 4,
        "reason": "5G/6G通信设备龙头+算力网络布局+数字经济受益者",
        "risk": "海外地缘政治风险+运营商资本开支波动"
    },
    "sh688525": {  # 佰维存储 - 存储芯片
        "level": 4,
        "reason": "存储模组龙头+NAND Flash布局+AI算力存储需求增长",
        "risk": "存储芯片价格波动+行业竞争加剧"
    },
    "sz300502": {  # 新易盛 - 光模块
        "level": 4,
        "reason": "800G/1.6T光模块放量+海外客户拓展+AI算力需求驱动",
        "risk": "光模块价格战+客户集中度高"
    },
    "sh688498": {  # 源杰科技 - 光芯片
        "level": 4,
        "reason": "光芯片国产替代龙头+高速率产品突破+光模块上游核心供应商",
        "risk": "技术迭代快+研发投入大"
    },
    "sz000657": {  # 中钨高新 - 钨钼龙头
        "level": 3,
        "reason": "钨钼资源龙头+硬质合金需求增长+高端材料布局",
        "risk": "大宗商品价格波动"
    },
    "sz002466": {  # 天齐锂业 - 锂矿龙头
        "level": 4,
        "reason": "全球锂矿资源龙头+产能释放+新能源汽车需求增长",
        "risk": "锂价周期波动+海外政策风险"
    },
    "sz002460": {  # 赣锋锂业 - 锂矿龙头
        "level": 4,
        "reason": "全球锂盐龙头+垂直一体化布局+固态电池研发",
        "risk": "锂价周期波动+海外政策风险"
    },
    "sh600549": {  # 厦门钨业 - 钨钼稀土
        "level": 3,
        "reason": "钨钼稀土龙头+高端材料布局+新能源电池材料拓展",
        "risk": "大宗商品价格波动"
    },
    "sh600869": {  # 远东股份 - 电线电缆+新能源
        "level": 3,
        "reason": "电线电缆龙头+海缆业务拓展+新能源布局",
        "risk": "行业竞争加剧+应收账款风险"
    },
    "sh600176": {  # 中国巨石 - 玻纤龙头
        "level": 4,
        "reason": "玻纤龙头+新能源新材料布局+全球市占率提升",
        "risk": "玻纤价格周期波动+产能扩张压力"
    },
    "sh603773": {  # 沃格光电 - 玻璃基板
        "level": 4,
        "reason": "玻璃基板龙头+TGV玻璃通孔技术突破+先进封装基板国产替代",
        "risk": "技术验证周期+行业竞争加剧"
    },
    "sz002384": {  # 东山精密 - PCB+精密制造
        "level": 4,
        "reason": "消费电子/汽车PCB龙头+AI服务器板放量+苹果链核心供应商",
        "risk": "苹果链依赖+行业竞争加剧"
    },
    "sz300476": {  # 胜宏科技 - PCB
        "level": 3,
        "reason": "高端PCB供应商+AI服务器板放量+新能源汽车电子增长",
        "risk": "行业竞争加剧+原材料价格波动"
    },
    "sz301308": {  # 江波龙 - 存储
        "level": 3,
        "reason": "存储模组龙头+自有品牌+企业级存储拓展",
        "risk": "存储芯片价格周期波动"
    },
    "sz300433": {  # 蓝思科技 - 消费电子结构件
        "level": 3,
        "reason": "消费电子结构件龙头+苹果链核心供应商+汽车电子布局",
        "risk": "苹果链依赖+消费电子需求疲软"
    },
    "sz001309": {  # 德明利 - 存储控制芯片
        "level": 3,
        "reason": "存储控制芯片龙头+国产替代+移动存储扩展",
        "risk": "技术迭代快+市场竞争加剧"
    },
    "sz300475": {  # 香农芯创 - 存储
        "level": 3,
        "reason": "存储模组龙头+消费电子存储+企业级存储拓展",
        "risk": "存储芯片价格波动+行业竞争"
    },
    "sz002475": {  # 立讯精密 - 精密制造
        "level": 4,
        "reason": "苹果链龙头+精密制造平台型公司+汽车电子/VR/AR布局",
        "risk": "苹果链依赖+大客户议价能力强"
    },
    "sz301526": {  # 国际复材 - 玻纤
        "level": 3,
        "reason": "玻纤龙头+新能源材料拓展+产能扩张",
        "risk": "玻纤价格周期波动+市场竞争"
    },
    "sz301217": {  # 铜冠铜箔 - 铜箔
        "level": 3,
        "reason": "锂电铜箔+PCB铜箔龙头+新能源车需求增长",
        "risk": "铜箔价格波动+产能扩张压力"
    },
    "sz300033": {  # 同花顺 - 金融信息
        "level": 4,
        "reason": "金融信息服务龙头+AI赋能+数据要素价值重估",
        "risk": "监管政策风险+市场竞争加剧"
    },
    "sz000725": {  # 京东方A - 面板龙头
        "level": 3,
        "reason": "面板龙头+产能整合优化+VR/AR/车载显示增长",
        "risk": "面板价格周期波动+行业竞争激烈"
    },
    "sz002378": {  # 章源钨业 - 钨
        "level": 3,
        "reason": "钨资源龙头+高端硬质合金+新能源材料布局",
        "risk": "钨价周期波动+产能建设周期长"
    },
    "sh688008": {  # 澜起科技 - 内存接口芯片
        "level": 4,
        "reason": "内存接口芯片全球龙头+DDR5渗透+AI算力需求增长",
        "risk": "技术迭代快+行业周期性强"
    },
    "sz000636": {  # 风华高科 - MLCC龙头
        "level": 4,
        "reason": "MLCC国产替代+涨价周期+汽车电子增长+电子元器件龙头",
        "risk": "MLCC价格周期波动+行业竞争"
    },
    "sh688012": {  # 中微公司 - 刻蚀设备
        "level": 4,
        "reason": "刻蚀设备龙头+国产替代加速+MOCVD设备布局",
        "risk": "技术迭代快+研发投入大"
    },
    "sz002428": {  # 云南锗业 - 锗
        "level": 4,
        "reason": "锗资源龙头+光电子材料+稀缺资源价值重估",
        "risk": "资源政策风险+价格波动"
    },
    "sh688072": {  # 拓荆科技 - 薄膜沉积设备
        "level": 4,
        "reason": "薄膜沉积设备龙头+国产替代+半导体设备平台化",
        "risk": "技术迭代快+研发投入高"
    },
    "sh601138": {  # 工业富联 - 智能制造
        "level": 4,
        "reason": "全球制造龙头+AI服务器代工+数字经济+新能源车布局",
        "risk": "苹果链依赖+劳动力成本上升"
    },
    "sz002407": {  # 多氟多 - 氟化工+锂盐
        "level": 3,
        "reason": "氟化工龙头+六氟磷酸锂+新型锂盐布局",
        "risk": "锂盐价格波动+产能扩张压力"
    },
    "sz002371": {  # 北方华创 - 半导体设备
        "level": 4,
        "reason": "半导体设备平台龙头+多品类布局+国产替代加速",
        "risk": "研发投入大+技术迭代快"
    },
    "sz300136": {  # 信维通信 - 天线+无线充电
        "level": 3,
        "reason": "消费电子天线龙头+无线充电增长+汽车电子布局",
        "risk": "苹果链依赖+消费电子需求疲软"
    },
    "sz002281": {  # 光迅科技 - 光通信
        "level": 3,
        "reason": "光通信全产业链布局+光芯片光器件+AI光模块需求",
        "risk": "行业竞争加剧+技术迭代"
    },
    "sz300390": {  # 天华新能 - 锂电材料
        "level": 3,
        "reason": "锂电材料供应商+氢氧化锂产能释放+新能源需求增长",
        "risk": "锂盐价格波动+行业竞争加剧"
    },
    "sh688347": {  # 华虹宏力 - 晶圆代工
        "level": 3,
        "reason": "特色工艺晶圆代工龙头+汽车电子/MCU/功率半导体",
        "risk": "晶圆代工业竞争激烈+技术迭代"
    },
    "sh688146": {  # 中船特气 - 六氟化钨
        "level": 5,
        "reason": "六氟化钨全球缺口3300吨+价格暴涨232%+产能2000吨/年订单饱和",
        "risk": "公司公告股价过热+无大额实质订单+年内涨7.3倍"
    },
    "sz300308": {  # 中际旭创 - 光模块AI需求
        "level": 4,
        "reason": "800G光模块放量+北美AI资本开支超预期+市占率领先",
        "risk": "PE=85x高位+放量成交400亿存分歧",
        "negative": {
            "score": 2,
            "reasons": ["美国CMC清单制裁风险(实体清单)", "控股股东大规模减持28.7亿压制情绪", "高估值PE85x对利空敏感"]
        }
    },
    "sh601899": {  # 紫金矿业 - 铜金上涨
        "level": 5,
        "reason": "铜价新高+金价强势+并购扩张+海外矿山增产",
        "risk": "大宗商品波动+短期涨幅较大"
    },
    "sh603993": {  # 洛阳钼业 - 铜钼双驱动
        "level": 5,
        "reason": "铜钼量价齐升+TFM铜钴矿达产+黄金资产注入预期",
        "risk": "涨停后获利回吐+矿产品价格波动",
        "source": "公司公告+券商研报",
        "time": "2026-06",
        "negative": {
            "score": 2,
            "reasons": ["美银研报预计美联储加息3次，强美元压制大宗商品价格", "铜钴价格受宏观利空压制"],
            "source": "美银证券研报",
            "time": "2026-06",
        }
    },
    "sz300750": {  # 宁德时代
        "level": 4,
        "reason": "锂电龙头地位稳固+储能放量+海外市占率提升",
        "risk": "行业竞争加剧+锂价波动"
    },
    "sz300274": {  # 阳光电源
        "level": 4,
        "reason": "光伏逆变器全球第一+储能出货高增+海外毛利率提升",
        "risk": "光伏行业产能过剩+贸易摩擦"
    },
    "sz300346": {  # 南大光电 - MO源+光刻胶
        "level": 3,
        "reason": "光刻胶国产替代+MO源龙头+半导体材料需求增长",
        "risk": "股价高位回调+产品验证周期长"
    },
    "sz300285": {  # 国瓷材料
        "level": 3,
        "reason": "陶瓷材料国产替代+MLCC需求增长+齿科材料放量",
        "risk": "短期涨幅过大回调"
    },
    "sz300059": {  # 东方财富 - 互联网券商
        "level": 4,
        "reason": "互联网券商龙头+基金销售领先+AI赋能金融科技",
        "risk": "股市波动影响+监管政策风险"
    },
    "sh600030": {  # 中信证券 - 券商龙头
        "level": 4,
        "reason": "券商行业龙头+投行领先+财富管理转型",
        "risk": "股市周期波动+行业竞争加剧"
    },
    "sh601318": {  # 中国平安 - 保险龙头
        "level": 4,
        "reason": "保险行业龙头+综合金融布局+科技赋能",
        "risk": "利率波动+监管政策风险"
    },
    "sz300408": {  # 三环集团 - 电子元件
        "level": 3,
        "reason": "电子元件龙头+MLCC/电阻/电感布局+新能源汽车需求增长",
        "risk": "行业周期波动+竞争加剧"
    },
    # ===== 补充消息面0分公司 =====
    # AI/半导体
    "sz000021": {"level": 5, "reason": "存储封测龙头+华为合作+DDR5量产+长鑫存储IPO催化", "risk": "封测行业竞争+客户集中"},
    "sz300661": {"level": 4, "reason": "模拟芯片国产替代龙头+信号链+电源管理双平台", "risk": "模拟芯片竞争加剧+研发投入大"},
    "sh688249": {"level": 3, "reason": "晶圆代工特色工艺+DDIC/CIS/MCU代工", "risk": "晶圆代工竞争激烈+产能利用率波动"},
    "sh688361": {"level": 4, "reason": "半导体量测设备国产替代+检测设备突破", "risk": "设备验证周期长+技术迭代快"},
    "sh688167": {"level": 3, "reason": "激光芯片+光束整形+半导体上游核心", "risk": "技术路线不确定+客户集中"},
    "sh688521": {"level": 3, "reason": "芯片设计IP平台+SoC定制服务", "risk": "IP授权竞争+盈利周期长"},
    "sh605358": {"level": 3, "reason": "硅片+化合物半导体双布局", "risk": "硅片价格波动+产能释放压力"},
    "sz002185": {"level": 3, "reason": "先进封装龙头+Chiplet封装+汽车电子封装", "risk": "封测行业竞争+资本开支大"},
    "sz300672": {"level": 3, "reason": "存储控制器芯片+固态硬盘+视频解码", "risk": "存储行业周期波动+竞争加剧"},
    "sh688037": {"level": 3, "reason": "涂胶显影设备国产替代+光刻工序设备", "risk": "设备验证周期长+技术迭代快"},
    "sh600584": {"level": 4, "reason": "封测龙头+先进封装+Chiplet+汽车电子", "risk": "封测行业竞争+资本开支大"},
    "sh600667": {"level": 2, "reason": "封测配套+洁净室工程+DRAM模组", "risk": "业务分散+盈利能力弱"},
    "sh688048": {"level": 3, "reason": "激光芯片龙头+高功率半导体激光器", "risk": "技术路线不确定+下游需求波动"},
    "sz300757": {"level": 3, "reason": "光伏设备+半导体封装设备+铜电镀", "risk": "光伏行业周期+设备验收周期长"},
    "sh688126": {"level": 4, "reason": "大硅片国产替代龙头+300mm硅片量产", "risk": "硅片价格周期+产能释放压力"},
    "sz002491": {"level": 2, "reason": "光纤光缆+通信设备", "risk": "行业增长放缓+竞争激烈"},
    "sh600330": {"level": 3, "reason": "蓝宝石+压电晶体+电子材料平台", "risk": "行业周期波动+竞争加剧"},
    "sz300131": {"level": 2, "reason": "MEMS+电子分销", "risk": "分销业务毛利低+技术壁垒有限"},
    "sh600703": {"level": 4, "reason": "化合物半导体龙头+Mini/Micro LED+碳化硅", "risk": "化合物半导体投入大+盈利周期长"},
    # 电子材料/元件/制造
    "sz300657": {"level": 3, "reason": "FPC柔性板+AI服务器PCB+新能源车电子", "risk": "FPC竞争加剧+客户集中"},
    "sz300706": {"level": 3, "reason": "PVD镀膜材料+半导体靶材国产替代", "risk": "材料验证周期长+客户集中"},
    "sz002636": {"level": 3, "reason": "覆铜板龙头+电子树脂+PCB上游", "risk": "覆铜板价格周期+原材料波动"},
    "sh603688": {"level": 3, "reason": "高纯石英砂龙头+光伏坩埚+半导体石英", "risk": "石英砂价格波动+产能扩张"},
    "sh688545": {"level": 3, "reason": "电子湿化学品+半导体级硫酸/双氧水", "risk": "化学品运输风险+客户验证周期"},
    "sh600353": {"level": 2, "reason": "真空灭弧室+电子元器件", "risk": "行业增长有限+竞争加剧"},
    "sz002600": {"level": 4, "reason": "消费电子精密件龙头+AI终端结构件+汽车电子", "risk": "苹果链依赖+消费电子周期"},
    # 化工/特种材料
    "sh688549": {"level": 3, "reason": "电子湿化学品+半导体级氢氟酸", "risk": "化学品安全风险+客户集中"},
    "sh600309": {"level": 4, "reason": "MDI全球龙头+新材料+高端化学品", "risk": "化工周期波动+环保政策"},
    "sh600141": {"level": 3, "reason": "磷化工龙头+有机硅+电子化学品", "risk": "磷化工周期+环保政策"},
    "sh601208": {"level": 3, "reason": "绝缘材料+光学膜+电子材料平台", "risk": "材料行业竞争+原材料波动"},
    "sh688268": {"level": 4, "reason": "电子特气国产替代龙头+半导体上游核心", "risk": "特气安全风险+客户验证周期长"},
    "sz002119": {"level": 2, "reason": "引线框架+半导体封装材料", "risk": "行业竞争+毛利偏低"},
    # 有色资源
    "sh600367": {"level": 2, "reason": "锰/钡资源+电池材料", "risk": "小金属价格波动+需求不确定"},
    # 新能源
    "sh600110": {"level": 3, "reason": "锂电铜箔龙头+新能源车+储能铜箔", "risk": "铜箔加工费下行+产能过剩"},
    # 新材料
    "sz002297": {"level": 2, "reason": "碳纤维刹车+航空复合材料", "risk": "航空认证周期长+市场空间有限"},
    "sh600172": {"level": 2, "reason": "人造金刚石+超硬材料", "risk": "行业竞争+需求疲软"},
    "sz002149": {"level": 3, "reason": "稀有金属复合材料+航空航天+核电", "risk": "军工订单波动+原材料价格"},
    # 基础材料
    # 航天军工
    "sh600118": {"level": 3, "reason": "卫星制造龙头+低轨卫星星座+北斗", "risk": "军工订单周期+技术迭代"},
    "sh600879": {"level": 3, "reason": "航天电子设备+测控通信+军用芯片", "risk": "军工订单波动+竞争加剧"},
    # 电力
    "sh601991": {"level": 2, "reason": "火电+新能源转型", "risk": "煤价波动+电价政策"},
    "sh600396": {"level": 2, "reason": "电力+热力供应", "risk": "煤价波动+盈利能力弱"},
    "sz001896": {"level": 2, "reason": "电力+新能源转型", "risk": "煤价波动+负债率高"},
    "sh600900": {"level": 2, "reason": "水电龙头+稳定分红+防御性资产", "risk": "来水波动+增长有限"},
    # 传媒
    "sz002354": {"level": 2, "reason": "数字营销+AI应用", "risk": "行业竞争+盈利不稳定"},
    # 汽车
    "sz000338": {"level": 3, "reason": "重卡发动机龙头+燃料电池+新能源动力", "risk": "重卡周期波动+新能源转型"},
    # 家电
    "sz000333": {"level": 3, "reason": "家电龙头+机器人+智能家居+海外扩张", "risk": "地产周期+消费疲软"},
    # 煤炭
    "sh601088": {"level": 2, "reason": "煤炭龙头+稳定分红+煤化工", "risk": "煤价波动+能源转型压力"},
    # 消费
    "sh600519": {"level": 2, "reason": "白酒龙头+品牌护城河+稳定分红", "risk": "消费降级+反腐政策"},
    # ===== 第二批补充 =====
    "sh600111": {"level": 4, "reason": "稀土龙头+北方稀土集团+新能源车磁材需求", "risk": "稀土价格波动+政策调控"},
    "sz300548": {"level": 3, "reason": "光通信器件+AWG芯片+数据中心光模块", "risk": "光通信竞争加剧+技术迭代"},
    "sz002602": {"level": 3, "reason": "游戏+AI算力+数据中心", "risk": "游戏监管+AI变现不确定"},
    "sh601869": {"level": 4, "reason": "光纤光缆龙头+预制棒全产业链+海洋通信", "risk": "光纤价格上行+行业竞争缓解"},
    "sz002916": {"level": 4, "reason": "PCB龙头+AI服务器板+封装基板", "risk": "PCB行业周期+原材料波动"},
    "sh603650": {"level": 3, "reason": "光刻胶国产替代+酚醛树脂+电子材料", "risk": "光刻胶验证周期+行业竞争"},
    "sz300394": {"level": 4, "reason": "光器件龙头+光模块上游+AI算力光互联", "risk": "光器件价格竞争+客户集中"},
    "sh688183": {"level": 3, "reason": "PCB+高频高速板+汽车电子板", "risk": "PCB行业竞争+原材料波动"},
    "sz300857": {"level": 3, "reason": "数据存储+AI服务器+算力租赁", "risk": "行业竞争+盈利模式待验证"},
    "sz000426": {"level": 3, "reason": "银锡资源+有色金属采选", "risk": "金属价格波动+资源品位"},
    "sh603083": {"level": 3, "reason": "光模块+CPO+AI算力光互联", "risk": "光模块竞争+技术迭代快"},
    "sz300014": {"level": 4, "reason": "锂电龙头+动力电池+储能+大圆柱量产", "risk": "锂电行业竞争+原材料波动"},
    "sz300604": {"level": 4, "reason": "半导体测试设备国产替代+分选机突破", "risk": "设备验证周期+技术迭代快"},
    "sh688017": {"level": 3, "reason": "谐波减速器龙头+机器人核心零部件", "risk": "机器人量产节奏+行业竞争"},
    "sz300223": {"level": 3, "reason": "存储芯片+处理器+AIoT", "risk": "存储行业周期+竞争加剧"},
    "sz002240": {"level": 3, "reason": "锂矿+锂盐+新能源材料", "risk": "锂价波动+产能释放压力"},
    "sz000792": {"level": 3, "reason": "盐湖提锂龙头+钾肥+锂资源", "risk": "锂价波动+提锂技术风险"},
    "sz002837": {"level": 3, "reason": "精密温控+液冷散热+AI算力基础设施", "risk": "温控行业竞争+技术迭代"},
    "sh600150": {"level": 3, "reason": "造船龙头+LNG船+海军装备", "risk": "造船周期波动+原材料价格"},
    "sh688019": {"level": 4, "reason": "CMP抛光液国产替代+半导体材料龙头", "risk": "材料验证周期+客户集中"},
    "sh600378": {"level": 3, "reason": "氟化工+含氟电子化学品+PVDF", "risk": "氟化工周期+环保政策"},
    "sh688820": {"level": 3, "reason": "先进封装+玻璃基板+AI芯片封装", "risk": "封装技术验证+竞争加剧"},
    "sh603005": {"level": 3, "reason": "晶圆级封装+TSV+摄像头芯片封装", "risk": "封测行业竞争+客户集中"},
    "sz002202": {"level": 2, "reason": "风电整机龙头+海上风电", "risk": "风电行业周期+招标波动"},
    "sz002138": {"level": 3, "reason": "电感龙头+汽车电子+5G通信", "risk": "电子元件周期+竞争加剧"},
    "sh688313": {"level": 3, "reason": "光芯片+AWG+数据中心光通信", "risk": "光芯片竞争+技术迭代"},
    "sh688766": {"level": 3, "reason": "NOR Flash+MCU+存储芯片", "risk": "存储行业周期+竞争加剧"},
    "sh603186": {"level": 3, "reason": "覆铜板+高频高速材料+PCB上游", "risk": "覆铜板价格周期+原材料波动"},
    "sh688041": {"level": 4, "reason": "国产CPU龙头+服务器芯片+AI算力", "risk": "技术迭代+生态建设周期"},
    "sh688981": {"level": 4, "reason": "晶圆代工龙头+先进制程+国产替代", "risk": "设备限制+技术差距"},
    "sz300054": {"level": 3, "reason": "CMP抛光垫国产替代+半导体材料", "risk": "材料验证周期+客户集中"},
    "sz300570": {"level": 3, "reason": "光无源器件+光纤连接器+数据中心", "risk": "光通信竞争+行业周期"},
    "sz000831": {"level": 3, "reason": "稀土+五矿稀土集团+磁材需求", "risk": "稀土价格波动+政策调控"},
    "sz002008": {"level": 3, "reason": "激光装备龙头+PCB钻孔+新能源加工", "risk": "激光行业竞争+下游需求波动"},
    "sz002938": {"level": 4, "reason": "PCB全球龙头+FPC+AI服务器板", "risk": "消费电子周期+苹果链依赖"},
    "sh688322": {"level": 3, "reason": "3D视觉+机器人感知+AI应用", "risk": "技术商业化不确定+竞争"},
    "sh688808": {"level": 4, "reason": "半导体测试设备龙头+射频/SoC测试+国产替代加速", "risk": "设备验证周期+客户集中"},
    "sz301377": {"level": 4, "reason": "PCB钻针全球龙头+AI服务器高密度PCB拉动+微钻放量+涂层钻针毛利率超50%", "risk": "PCB行业周期+股东询价转让"},
    "sh603738": {"level": 5, "reason": "石英光刻晶振国内唯一+312.5M/625M超高频晶振国产替代+AI光模块时钟核心+日本出口管制断供+车规晶振市占60%", "risk": "高端产能爬坡+消费电子基本盘下行"},
    "sz301396": {"level": 2, "reason": "智慧城市+AI应用+数据服务", "risk": "项目制业务+盈利不稳定"},
    "sz300751": {"level": 3, "reason": "光伏电池设备龙头+HJT整线方案", "risk": "光伏行业周期+技术路线不确定"},
    "sh688120": {"level": 4, "reason": "CMP设备国产替代+半导体量测", "risk": "设备验证周期长+技术迭代"},
    "sz002594": {"level": 4, "reason": "新能源车龙头+刀片电池+智能化", "risk": "新能源车竞争+补贴退坡"},
    "sh605589": {"level": 3, "reason": "酚醛树脂+电子材料+光刻胶树脂", "risk": "材料行业竞争+原材料波动"},
    "sh600206": {"level": 3, "reason": "半导体材料+靶材+稀土功能材料", "risk": "材料验证周期+行业竞争"},
    "sh600276": {"level": 3, "reason": "创新药龙头+PD-1+国际化", "risk": "集采压力+研发失败风险"},
    "sz301666": {"level": 3, "reason": "存储控制器+企业级SSD+PCIe", "risk": "存储行业周期+竞争加剧"},
    "sz002484": {"level": 3, "reason": "铝电解电容+薄膜电容+新能源车", "risk": "电容行业竞争+原材料波动"},
    "sz002353": {"level": 3, "reason": "油服装备龙头+压裂设备+海外拓展", "risk": "油气资本开支波动+地缘风险"},
    "sz002851": {"level": 3, "reason": "电源管理+智能制造+新能源", "risk": "行业竞争+下游需求波动"},
    "sz300398": {"level": 3, "reason": "光纤光缆+紫外固化材料+半导体材料", "risk": "行业竞争+原材料波动"},
    "sh601698": {"level": 3, "reason": "卫星通信龙头+低轨卫星+国防通信", "risk": "卫星建设周期+政策依赖"},
    "sh603678": {"level": 3, "reason": "陶瓷电容+军工电子+新材料", "risk": "军工订单波动+行业竞争"},
    "sz300747": {"level": 3, "reason": "光纤激光器龙头+高功率激光+切割焊接", "risk": "激光器价格战+下游需求波动"},
    "sh601600": {"level": 3, "reason": "铝业龙头+氧化铝+电解铝+海外资源", "risk": "铝价波动+能源成本"},
    "sh603256": {"level": 2, "reason": "电子纱+玻纤布+PCB上游", "risk": "玻纤价格周期+竞争加剧"},
    "sh688256": {"level": 3, "reason": "AI芯片龙头+云端训练+国产算力", "risk": "技术差距+生态建设周期"},
    "sz000988": {"level": 3, "reason": "激光装备+传感器+汽车电子", "risk": "激光行业竞争+下游需求波动"},
    "sz002436": {"level": 3, "reason": "PCB+IC载板+半导体封装基板", "risk": "PCB行业竞争+载板验证周期"},
    "sz300395": {"level": 4, "reason": "石英玻璃龙头+半导体石英+光掩模基板", "risk": "石英材料验证周期+客户集中"},
    "sz002050": {"level": 3, "reason": "热管理龙头+新能源车热泵+机器人", "risk": "热管理竞争+下游需求波动"},
    "sz300620": {"level": 3, "reason": "铌酸锂调制器+光器件+光纤激光", "risk": "光器件竞争+技术迭代"},
    "sz300124": {"level": 4, "reason": "工控龙头+伺服+新能源车电驱+机器人", "risk": "工控行业周期+新能源车竞争"},
    "sh688627": {"level": 3, "reason": "半导体检测设备+显示检测", "risk": "设备验证周期+客户集中"},
    "sz002156": {"level": 3, "reason": "封测+先进封装+AMD合作", "risk": "封测行业竞争+客户集中"},
    "sh600105": {"level": 2, "reason": "光纤光缆+超导材料", "risk": "行业竞争+超导商业化不确定"},
    "sh603259": {"level": 3, "reason": "CXO龙头+创新药研发外包+全球化", "risk": "地缘政治+行业周期"},
    "sz301511": {"level": 3, "reason": "锂电铜箔+电子铜箔+新能源", "risk": "铜箔加工费下行+产能过剩"},
    "sh600160": {"level": 3, "reason": "氟化工龙头+制冷剂+含氟聚合物", "risk": "氟化工周期+环保政策"},
    "sz002842": {"level": 2, "reason": "钨资源+硬质合金", "risk": "钨价波动+行业竞争"},
    "sz002080": {"level": 3, "reason": "风电叶片+玻璃纤维+新材料", "risk": "风电行业周期+原材料波动"},
    "sz002361": {"level": 2, "reason": "特种电缆+军工+新能源", "risk": "订单波动+行业竞争"},
    "sz002747": {"level": 3, "reason": "工业机器人龙头+伺服+自动化", "risk": "机器人行业竞争+下游需求波动"},
    "sz301205": {"level": 3, "reason": "光模块+数据中心+AI算力光互联", "risk": "光模块竞争+技术迭代快"},
    # ===== 第三批补充 =====
    "sh603799": {"level": 4, "reason": "钴镍锂资源+三元前驱体+新能源材料", "risk": "钴镍价格波动+产能扩张压力"},
    "sh600392": {"level": 3, "reason": "稀土龙头+轻稀土资源+磁材需求", "risk": "稀土价格波动+政策调控"},
    "sz000100": {"level": 3, "reason": "面板龙头+大尺寸LCD+印刷OLED", "risk": "面板价格周期+行业竞争"},
    "sz002129": {"level": 3, "reason": "光伏硅片龙头+N型大尺寸+半导体硅片", "risk": "光伏行业产能过剩+硅片价格下行"},
    "sh600186": {"level": 3, "reason": "调味品+算力租赁+IDC数据中心", "risk": "调味品竞争+算力业务待验证"},
    # ===== 第四批补充(消息面0分覆盖) =====
    "sh600711": {"level": 3, "reason": "铜钴矿资源+新能源金属+海外矿山布局", "risk": "钴铜价格波动+海外政策风险"},
    "sh600362": {"level": 3, "reason": "铜矿龙头+铜冶炼+稀土资源", "risk": "铜价波动+冶炼加工费下行"},
    "sh688388": {"level": 3, "reason": "锂电铜箔龙头+极薄铜箔+宁德时代供应链", "risk": "铜箔加工费下行+产能过剩"},
    "sz000630": {"level": 3, "reason": "铜冶炼龙头+铜矿资源+硫酸化工", "risk": "铜价波动+冶炼利润压缩"},
    "sz000960": {"level": 3, "reason": "锡矿龙头+锡价上行+半导体焊料需求", "risk": "锡价波动+资源枯竭风险"},
    "sz002192": {"level": 3, "reason": "锂矿资源+锂盐加工+比亚迪供应链", "risk": "锂价波动+产能释放压力"},
    "sh600500": {"level": 2, "reason": "化工新材料+农药中间体+橡胶化学品", "risk": "化工周期波动+环保政策"},
    "sz300263": {"level": 3, "reason": "传热节能材料+电子新材料+军工材料", "risk": "新材料验证周期+订单波动"},
    "sh603629": {"level": 2, "reason": "液晶模组+金属结构件+消费电子", "risk": "消费电子周期+客户集中"},
    "sh688082": {"level": 3, "reason": "半导体清洗设备国产替代+单片清洗设备", "risk": "设备验证周期长+技术迭代"},
    "sh688777": {"level": 3, "reason": "工业自动化+DCS系统+智能制造", "risk": "工控行业周期+下游需求波动"},
    "sh600601": {"level": 2, "reason": "PCB制造+宽带接入+智慧城市", "risk": "PCB行业竞争+业务转型慢"},
    "sh688507": {"level": 3, "reason": "CAE仿真软件国产替代+工业软件", "risk": "软件商业化周期长+客户验证慢"},
    "sz002971": {"level": 3, "reason": "电子特气+高纯气体+半导体材料", "risk": "特气安全风险+客户验证周期长"},
    # ===== 第五批补充(score_news偏低) =====
    "sz000977": {"level": 4, "reason": "AI服务器龙头+国产算力+互联网大厂核心供应商", "risk": "AI服务器竞争加剧+供应链波动"},
    "sh601958": {"level": 3, "reason": "钼矿龙头+钼价上行+军工高温合金需求增长", "risk": "钼价波动+资源枯竭风险"},
}

def score_news(news_text, pct, name="", code="", growth=None):
    """
    消息面评分 (v2.8):
    仅依据真实新闻/已验证催化剂评分，业绩数据不作为消息面依据
    增强来源标注、时间、情感标签、原文链接
    返回: (score, reasons, news_items)
    news_items: [{source, time, sentiment, content, url}]
    """
    reasons = []
    news_items = []  # 结构化消息列表

    # ========== Step 1: 消息内容评估 (0-5) ==========
    content_score = 0
    content_label = "无消息"
    has_content = False

    # 1a. 已知催化剂（已验证的真实事件/市场预期）
    if code in KNOWN_CATALYSTS:
        cat = KNOWN_CATALYSTS[code]
        content_score = cat["level"]
        reasons.append(cat["reason"])
        if "risk" in cat:
            reasons.append(f"⚠️{cat['risk']}")
        content_label = {5:"重大利好",4:"一般利好",3:"中性",2:"小幅利空",1:"一般利空",0:"重大利空"}[content_score]
        has_content = True
        # 结构化消息
        news_items.append({
            "source": cat.get("source", "券商研报"),
            "time": cat.get("time", ""),
            "sentiment": content_label,
            "content": cat["reason"],
            "url": cat.get("url", ""),
        })

    # 1b. iFinD新闻关键词分析（仅限非业绩类事件）
    if news_text and len(news_text) > 20:
        nl = news_text.lower()

        bull_strong = sum(1 for k in ["超预期","突破","政策利好","中标","回购","增持",
                                       "量价齐升","订单饱和","供不应求","涨价","短缺","缺口",
                                       "资产注入","产能达产","达产","玻璃基板","国产替代",
                                       "技术突破","量产","首发"] if k in nl)
        bull_weak = sum(1 for k in ["合作","获批","扩产","分红","放量"] if k in nl)
        bear_strong = sum(1 for k in ["立案","暴雷","退市","监管函","处罚","问询",
                                       "诉讼","减持","爆仓","跌停"] if k in nl)
        bear_weak = sum(1 for k in ["下调","降价","产能过剩","库存积压"] if k in nl)

        bull_total = bull_strong * 3 + bull_weak
        bear_total = bear_strong * 3 + bear_weak

        if bull_total >= 6 and content_score < 5:
            content_score = min(5, content_score + 1)
            reasons.append("iFinD确认:多重利好")
            content_label = {5:"重大利好",4:"一般利好",3:"中性",2:"小幅利空",1:"一般利空"}.get(content_score,"中性")
            has_content = True
            news_items.append({
                "source": "财经媒体(iFinD)",
                "time": "",
                "sentiment": "利好",
                "content": f"iFinD新闻检测到{bull_total}个利好关键词",
                "url": "",
            })
        elif bull_total >= 2 and content_score < 4:
            content_score = max(content_score, 4)
            reasons.append("iFinD确认:偏利好")
            content_label = "一般利好"
            has_content = True
            news_items.append({
                "source": "财经媒体(iFinD)",
                "time": "",
                "sentiment": "利好",
                "content": f"iFinD新闻偏利好({bull_total}个关键词)",
                "url": "",
            })
        elif bear_total >= 6 and content_score > 0:
            content_score = max(0, content_score - 2)
            reasons.append("iFinD确认:多重利空")
            content_label = {5:"重大利好",4:"一般利好",3:"中性",2:"小幅利空",1:"一般利空",0:"重大利空"}.get(content_score,"中性")
            has_content = True
            news_items.append({
                "source": "财经媒体(iFinD)",
                "time": "",
                "sentiment": "利空",
                "content": f"iFinD新闻检测到{bear_total}个利空关键词",
                "url": "",
            })
        elif bear_total >= 2 and content_score > 0:
            content_score = max(1, content_score - 1)
            reasons.append("iFinD确认:偏利空")
            content_label = "一般利空"
            has_content = True
            news_items.append({
                "source": "财经媒体(iFinD)",
                "time": "",
                "sentiment": "利空",
                "content": f"iFinD新闻偏利空({bear_total}个关键词)",
                "url": "",
            })

    # ========== Step 1c: 已知利空（已验证的负面事件）==========
    if code in KNOWN_CATALYSTS and "negative" in KNOWN_CATALYSTS[code]:
        neg = KNOWN_CATALYSTS[code]["negative"]
        # 利空直接覆盖利好评分，取利空score
        content_score = neg["score"]
        neg_label = {5:"重大利好",4:"一般利好",3:"中性",2:"小幅利空",1:"一般利空",0:"重大利空"}.get(content_score,"利空")
        for nr in neg["reasons"]:
            reasons.append(f"⚠️利空:{nr}")
        has_content = True
        news_items.append({
            "source": neg.get("source", "券商研报"),
            "time": neg.get("time", ""),
            "sentiment": neg_label,
            "content": "; ".join(neg["reasons"]),
            "url": neg.get("url", ""),
        })

    if not has_content:
        reasons.append("无真实新闻/催化剂")
    
    # ========== Step 2: 股价反应调节 (次要) ==========
    price_mod = 0
    if pct >= 9.9:
        reasons.append(f"涨停{pct:+.1f}%(股价确认利好)")
        price_mod = +1
    elif pct > 7:
        reasons.append(f"大涨{pct:+.1f}%")
        price_mod = +1
    elif pct > 3:
        reasons.append(f"上涨{pct:+.1f}%")
        price_mod = +0
    elif pct < -9.9:
        if content_score >= 4:
            reasons.append(f"跌停{pct:.1f}%(获利回吐,消息面仍偏正面)")
            price_mod = -1
        elif content_score <= 1:
            reasons.append(f"跌停{pct:.1f}%(价量确认利空)")
            price_mod = -1
        else:
            reasons.append(f"跌停{pct:.1f}%(需关注是否有利空未覆盖)")
            price_mod = -1
    elif pct < -7:
        if content_score >= 4:
            reasons.append(f"大跌{pct:.1f}%(短期回调,基本面未变)")
            price_mod = -1
        elif content_score <= 1:
            reasons.append(f"大跌{pct:.1f}%(价格确认利空信号)")
            price_mod = -2
        else:
            reasons.append(f"大跌{pct:.1f}%")
            price_mod = -1
    elif pct < -3:
        reasons.append(f"下跌{pct:.1f}%")
        price_mod = -1
    elif pct < -1:
        reasons.append(f"小跌{pct:.1f}%")
        price_mod = 0
    else:
        reasons.append(f"横盘({pct:+.1f}%)")
        price_mod = 0

    final_score = max(0, min(5, content_score + price_mod))

    return final_score, reasons, news_items


# ============================================================
# Main
# ============================================================
def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "top200_all_a.json"
    out = sys.argv[2] if len(sys.argv) > 2 else f"top200_scored_{datetime.now().strftime('%Y-%m-%d')}.json"
    
    with open(inp) as f:
        raw = json.load(f)
    # 优先使用 top200，如果不存在则降级到 top100 或 top50
    if "top200" in raw:
        stocks = raw["top200"]
    elif "top100" in raw:
        stocks = raw["top100"]
    else:
        stocks = raw["top50"]
    
    # 剔除金融股
    FINANCE_SECTORS = {"金融"}
    before = len(stocks)
    stocks = [s for s in stocks if get_sec(s["name"]) not in FINANCE_SECTORS]
    removed = before - len(stocks)
    if removed:
        print(f"   🚫 剔除金融股 {removed}只")
    
    print(f"📊 Stock-Scorer v2.6 | {len(stocks)}只标的")
    print(f"   权重: 题材热度30% 基本面30%(含行业前景) 消息20% 技术面20%")
    
    # --- Step 1: K-lines (serial with delay to avoid API throttling) ---
    print(f"\n📈 Step 1: K线获取 ({len(stocks)}只, 腾讯前复权优先)")
    klines = {}
    for i, s in enumerate(stocks):
        try:
            klines[s["code"]] = fetch_klines(s["code"])
        except:
            klines[s["code"]] = []
        if (i + 1) % 10 == 0:
            print(f"   [{i+1}/{len(stocks)}] 已获取...")
            time.sleep(3)
        else:
            time.sleep(0.5)
    ok = sum(1 for v in klines.values() if v)
    print(f"   ✅ {ok}/{len(stocks)}只K线获取成功")
    
    # Fallback: 串行获取失败的K线
    failed = [s for s in stocks if not klines.get(s["code"])]
    if failed:
        print(f"   🔄 串行补获取 {len(failed)} 只K线...")
        for i, s in enumerate(failed):
            try:
                klines[s["code"]] = fetch_klines(s["code"])
            except:
                klines[s["code"]] = []
            time.sleep(0.5)
        ok2 = sum(1 for v in klines.values() if v)
        print(f"   ✅ 最终 {ok2}/{len(stocks)}只K线获取成功")
    
    # --- Step 1.5: 周线K线获取 ---
    print(f"\n📈 Step 1.5: 周线K线获取 ({len(stocks)}只)")
    wklines = {}
    for i, s in enumerate(stocks):
        try:
            wklines[s["code"]] = fetch_weekly_klines(s["code"])
        except:
            wklines[s["code"]] = None
        if (i + 1) % 10 == 0:
            print(f"   [{i+1}/{len(stocks)}] 已获取...")
            time.sleep(3)
        else:
            time.sleep(0.3)
    wok = sum(1 for v in wklines.values() if v)
    print(f"   ✅ {wok}/{len(stocks)}只周线获取成功")
    
    # --- Step 2: 板块分布预计算 + 板块行情数据获取 ---
    print(f"\n🔥 Step 2: 板块分布分析 + 板块行情获取")
    all_top200_sectors = [get_sec(s["name"]) for s in stocks]
    sector_cnt = Counter(all_top200_sectors)
    print(f"   主线板块: " + " | ".join(f"{s}({n}只)" for s, n in sector_cnt.most_common(5)))

    # 获取新浪行业板块行情数据
    sector_data = fetch_sector_data()
    if sector_data:
        print(f"   ✅ 新浪板块行情获取成功({len(sector_data)}个板块)")
    else:
        print(f"   ⚠️ 新浪板块行情获取失败,将使用基础评分")

    # 统计各板块涨停家数和上涨家数（基于TOP200个股数据）
    sector_zt_counts = {}
    for sec_name in set(all_top200_sectors):
        sec_stocks = [s for s in stocks if get_sec(s["name"]) == sec_name]
        zt = sum(1 for s in sec_stocks if (s.get("pct_chg", 0) or 0) >= 9.9)
        up = sum(1 for s in sec_stocks if (s.get("pct_chg", 0) or 0) > 0)
        sector_zt_counts[sec_name] = {"zt": zt, "up": up, "total": len(sec_stocks)}
    zt_summary = ", ".join(f"{s}({v['zt']}涨停/{v['up']}涨/{v['total']}只)" for s, v in sorted(sector_zt_counts.items(), key=lambda x: -x[1]["zt"])[:5] if v["zt"] > 0)
    if zt_summary:
        print(f"   涨停分布: {zt_summary}")
    
    # --- Step 3: iFinD News (priority stocks) --- (原Step3不变)
    print(f"\n📰 Step 3: iFinD新闻 (重点标的)")
    priority = []
    for s in stocks[:15]:
        priority.append(s)
    for s in stocks:
        pct = s.get("pct_chg", 0) or 0
        if abs(pct) > 5 and s not in priority:
            priority.append(s)
    
    news_map = {}
    for i, s in enumerate(priority):
        print(f"   [{i+1}/{len(priority)}] {s['name']}", end=" ", flush=True)
        news_map[s["code"]] = run_ifind_news(s["name"], s["code"])
        print("✓" if news_map[s["code"]] else "✗")
    
    # --- Step 4: iFinD Fundamentals (top 20) --- (原Step4不变)
    print(f"\n📋 Step 4: iFinD基本面 (TOP20)")
    fund_map = {}
    for i, s in enumerate(stocks[:20]):
        print(f"   [{i+1}/20] {s['name']}", end=" ", flush=True)
        fund_map[s["code"]] = run_ifind_fin(s["name"], s["code"])
        print("✓" if fund_map[s["code"]] else "✗")
    
    # --- Step 4.5: akshare 财务增速 + 机构预测 + 腾讯行情PE ---
    print(f"\n📊 Step 4.5: akshare财务数据 (200只)")
    import akshare as ak
    growth_map = {}
    fwd_pe_map = {}
    cagr3_map = {}
    for i, s in enumerate(stocks):
        code = s["code"]
        sym = code[2:]
        name = s["name"]
        try:
            df = ak.stock_financial_abstract_ths(symbol=sym, indicator='按年度')
            if df is not None and len(df) > 0:
                latest = df.iloc[-1]
                pg = latest.get('扣非净利润同比增长率', None)
                if pg and pg != False and pg != 'False':
                    try:
                        pg_str = str(pg).replace('%', '').strip()
                        growth_map[code] = float(pg_str)
                    except:
                        pass
                np_col = df.get('扣非净利润', None)
                if np_col is not None and len(df) >= 4:
                    try:
                        def parse_np(v):
                            if v is None or v is False:
                                return None
                            s = str(v).replace('亿','').replace('万','').strip()
                            val = float(s)
                            if '万' in str(v):
                                val /= 10000
                            return val
                        vals = []
                        for idx_r in range(max(0, len(df)-4), len(df)):
                            v = parse_np(df.iloc[idx_r]['扣非净利润'])
                            vals.append(v)
                        if len(vals) >= 3 and vals[0] and vals[-1] and vals[0] > 0 and vals[-1] > 0:
                            n_years = len(vals) - 1
                            cagr = (vals[-1] / vals[0]) ** (1.0 / n_years) - 1
                            cagr3_map[code] = round(cagr * 100, 1)
                    except:
                        pass
        except:
            pass
        try:
            df2 = ak.stock_profit_forecast_ths(symbol=sym, indicator='预测年报净利润')
            if df2 is not None and len(df2) > 0:
                multi_year = {}
                for _, row in df2.iterrows():
                    yr = int(row['年度'])
                    if yr in (2026, 2027, 2028):
                        try:
                            multi_year[yr] = float(row['均值'])
                        except:
                            pass
                if multi_year:
                    fwd_pe_map[code] = multi_year
        except:
            pass
        if (i + 1) % 50 == 0:
            print(f"   [{i+1}/200] 增速={len(growth_map)} FwdPE预测={len(fwd_pe_map)} CAGR3={len(cagr3_map)}")
        if (i + 1) % 10 == 0:
            time.sleep(0.3)
    
    # 用腾讯行情获取市值+PE-TTM（合并为一次请求）
    print(f"   获取市值+PE-TTM数据...")
    mktcap_map = {}
    pe_map = {}
    batch_size = 30
    for batch_start in range(0, len(stocks), batch_size):
        batch = stocks[batch_start:batch_start + batch_size]
        codes_str = ",".join(s["code"] for s in batch)
        url = f"https://qt.gtimg.cn/q={codes_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("gbk")
            for line in text.strip().split(";"):
                line = line.strip()
                if '~' not in line:
                    continue
                parts = line.split('~')
                code_raw = parts[2] if len(parts) > 2 else ""
                if not code_raw:
                    continue
                if len(parts) > 39:
                    try:
                        pe_val = float(parts[39])
                        if pe_val > 0:
                            pe_map[code_raw] = pe_val
                    except:
                        pass
                if len(parts) > 45:
                    try:
                        mktcap_map[code_raw] = float(parts[45])
                    except:
                        pass
        except:
            pass
        time.sleep(0.3)
    
    # 计算 Fwd PE = 总市值(亿) / 预测净利润(亿)，形成估值阶梯
    computed_fwd_pe = {}
    pe_ladder = {}
    for s in stocks:
        code = s["code"]
        sym = code[2:]
        if code in fwd_pe_map and sym in mktcap_map:
            mktcap = mktcap_map[sym]
            forecasts = fwd_pe_map[code]
            if isinstance(forecasts, dict):
                ladder = {}
                for yr, fc in sorted(forecasts.items()):
                    if mktcap > 0 and fc > 0:
                        ladder[yr] = round(mktcap / fc, 1)
                if ladder:
                    pe_ladder[code] = ladder
                    computed_fwd_pe[code] = ladder.get(2026) or ladder.get(min(ladder.keys()))
            elif isinstance(forecasts, (int, float)) and mktcap > 0 and forecasts > 0:
                computed_fwd_pe[code] = round(mktcap / forecasts, 1)
    
    print(f"   ✅ 增速={len(growth_map)} FwdPE={len(computed_fwd_pe)} PE阶梯={len(pe_ladder)} PE-TTM={len(pe_map)} MktCap={len(mktcap_map)}")
    
    # --- Step 5: Score all ---
    print(f"\n🎯 Step 5: 四维评分(题材30%+基本面30%(含行业前景)+消息20%+技术面20%)")
    results = []
    for i, s in enumerate(stocks):
        code = s["code"]
        name = s["name"]
        pct = s.get("pct_chg", 0) or 0
        pe = pe_map.get(code[2:], 0) or s.get("pe", 0) or 0
        price = s.get("price", 0) or 0
        turnover = s.get("turnover", 0)
        
        sector = get_sec(name)
        kl = klines.get(code, [])
        
        # Fundamental (传入估值阶梯数据)
        ftext = fund_map.get(code, "")
        fparsed = parse_fundamentals(ftext)
        pe_input = pe_map.get(code[2:]) or (pe if pe > 0 else None)
        fwd_pe_input = fparsed.get("fwd_pe") or computed_fwd_pe.get(code)
        growth_input = fparsed.get("growth") or growth_map.get(code)
        if code in KNOWN:
            _, k_growth = KNOWN[code]
            if growth_input is None:
                growth_input = k_growth
        ladder_input = pe_ladder.get(code)
        roe_input = fparsed.get("roe")
        gm_input = fparsed.get("gross_margin")
        fs, fr, fwd_pe_resolved, growth_resolved, ladder_resolved = score_fund(
            pe_input, fwd_pe_input,
            growth_input, code, sector, pe_ladder_data=ladder_input,
            roe=roe_input, gross_margin=gm_input)
        
        # Tech (传入CAGR3用于成长加分, 传入周线用于双线多头判断)
        cagr3_input = cagr3_map.get(code)
        wkl = wklines.get(code)
        ts, td = score_tech(kl, price, pct, cagr3=cagr3_input, wkl=wkl)

        # Theme Heat (题材热度替代原资金面)
        ths, thr, sub_theme_label, lifecycle = score_theme(
            name, sector, pct, turnover, all_top200_sectors,
            sector_data=sector_data, sector_zt_counts=sector_zt_counts)
        
        # News (传入增速用于动态评估)
        ntext = news_map.get(code, "")
        ns, nr, news_items = score_news(ntext, pct, name, code, growth=growth_resolved)
        
        # Weighted total: 题材30% + 基本面30% + 消息20% + 技术面20%
        # 行业前景加分叠加到技术面,上限5分
        industry_bonus = INDUSTRY_PROSPECT.get(code, 0)
        ts_adj = max(0, min(5, ts + industry_bonus))
        if industry_bonus != 0:
            label = INDUSTRY_PROSPECT_LABELS.get(industry_bonus, "")
            if isinstance(td, dict):
                td = dict(td)
                td["industry_prospect"] = f"行业前景{'加分' if industry_bonus > 0 else '减分'}{industry_bonus:+.1f}({label})"
        w = ns * 0.20 + ts_adj * 0.20 + fs * 0.30 + ths * 0.30
        total = w * 4
        
        if total >= 17: rating, advice = "S", "优先买入，可重仓"
        elif total >= 14: rating, advice = "A", "逢低加仓，重点关注"
        elif total >= 10: rating, advice = "B", "波段操作，轻仓参与"
        else: rating, advice = "C", "观望，不新开仓"
        
        em = {"S":"🌟","A":"🔥","B":"📊","C":"👀"}[rating]
        
        results.append({
            "name": name, "code": code, "sector": sector,
            "price": price, "pct_chg": pct, "pe": pe,
            "turnover": turnover,
            "score_news": ns, "score_tech": ts_adj, "score_fund": fs,
            "score_theme": ths, "total": round(total, 1), "rating": rating,
            "advice": advice,
            "tech": td, "fund_reasons": fr, "theme_reasons": thr,
            "news_reasons": nr,
            "news_items": news_items,
            "sub_theme": sub_theme_label,
            "lifecycle": lifecycle,
            "fwd_pe": round(fwd_pe_resolved, 1) if fwd_pe_resolved else None,
            "growth": round(growth_resolved, 1) if growth_resolved else None,
            "pe_ladder": ladder_resolved if ladder_resolved else None,
            "cagr3": cagr3_input,
        })

        print(f"   {i+1:2d}. {name:6s} {em}{rating} {total:4.1f}  "
              f"消息{ns}/技术{ts_adj}/基本{fs}/热度{ths} | {sector}({lifecycle})")
    
    results.sort(key=lambda x: (-x["total"], -(x["turnover"] or 0)))
    
    # Data completeness check
    missing_fund = [r for r in results if r["score_fund"] == 0]
    missing_fwd = [r for r in results if r.get("fwd_pe") is None]
    missing_growth = [r for r in results if r.get("growth") is None]
    missing_news = [r for r in results if r["score_news"] <= 1 and r["code"] not in KNOWN_CATALYSTS]
    if missing_fund or missing_fwd or missing_growth or missing_news:
        print(f"\n⚠️ 数据完整性检查:")
        if missing_fund:
            print(f"   基本面=0: {', '.join(r['name'] for r in missing_fund[:10])}")
        if missing_fwd:
            print(f"   缺FwdPE: {', '.join(r['name'] for r in missing_fwd[:10])}")
        if missing_growth:
            print(f"   缺Growth: {', '.join(r['name'] for r in missing_growth[:10])}")
        if missing_news:
            print(f"   消息面<=1(未配置催化剂): {', '.join(r['name'] for r in missing_news[:10])}")
    else:
        print(f"\n✅ 数据完整性检查: 全部通过")
    
    # Stats
    rc = Counter(r["rating"] for r in results)
    sec_avg = {}
    for r in results:
        s = r["sector"]
        sec_avg.setdefault(s, []).append(r["total"])
    
    out_data = {
        "date": raw.get("date", ""),
        "model": "stock-scorer v2.5",
        "weights": "题材30% 基本面30%(含行业前景) 消息20% 技术面20%",
        "results": results,
        "stats": {
            "rating_dist": {r: rc.get(r, 0) for r in "SABCD"},
            "sector_avg": {s: {"count": len(v), "avg": round(sum(v)/len(v), 1)}
                          for s, v in sorted(sec_avg.items(), key=lambda x: -sum(x[1])/len(x[1]))},
        },
    }
    
    with open(out, "w") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"📊 评级分布")
    for r in "SABCD":
        c = rc.get(r, 0)
        print(f"   {r}: {'█'*c} {c}只")
    print(f"\n📊 板块均分")
    for s, v in sorted(sec_avg.items(), key=lambda x: -sum(x[1])/len(x[1])):
        print(f"   {s}: {len(v)}只 均分{sum(v)/len(v):.1f}")
    print(f"\n✅ 结果: {out}")
    return out_data


if __name__ == "__main__":
    main()
