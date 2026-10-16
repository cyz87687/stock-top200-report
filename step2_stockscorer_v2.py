#!/usr/bin/env python3
"""
A股TOP50 极简公司评分 - Stock-Scorer v2.1
模型: 消息面30% + 技术面25% + 基本面25% + 资金面20% → 20分制
数据源: 腾讯K线(技术) + iFinD(基本/消息) + 东方财富(资金)
"""

import json, sys, os, time, re, subprocess, glob
import urllib.request
import concurrent.futures
from datetime import datetime, timedelta
from collections import Counter

# 清除代理环境变量: 本地代理可能不通(HTTP 502), 行情接口需直连
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

# ============================================================
# Config
# ============================================================
PY = sys.executable  # 使用当前Python解释器
IFIND_FIN = os.path.expanduser("~/.workbuddy/skills/ifind-repilot-finance-data-search/scripts/fetch_data.py")
IFIND_NEWS = os.path.expanduser("~/.workbuddy/skills/ifind-repilot-news-search/scripts/fetch_data.py")
KLINES_N = 120

# ===== v2.41 IC 驱动权重（2026-10-13 量化审查后）=====
# 五维权重原为 v2.40 主观先验（消息0.20/技术0.20/基本面0.30/题材0.18/板块动量0.12）。
# v3.0-10dim: 十维评分（2026-10-13，依据《因子排名与10维评分模型设计_2026-10-13》）。
# 结构：保留池内 IC 已验证维度(fund/news/sector)；技术面/题材两个负 IC 维度拆解——
#   有效成分(RSI 反转)由回测 Top5 的 score_rsi_rev 覆盖，负向成分(放量脉冲/超买)由
#   score_vol_pulse/score_overbought 反向覆盖；另拆超跌60/乖离低位(回测超额前二)与
#   两个先验维度(题材内禀/关注度)。均线/MACD 结构分与动量因子退出评分(三重负证据)，
#   仅在 tech detail 展示。
# ⚠️ 新拆 7 维在 TOP200 池无历史截面快照 → 初始权重为全市场回测先验映射，
#   元数据标注 prior_pending；满 30+ 截面后由 factor_eval.py --optimize-weights 重拟合。
WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
_W_PRIOR_10D = {
    "score_fund": 0.18, "score_news": 0.18, "score_sector": 0.10,
    "score_oversold60": 0.12, "score_bias60": 0.10, "score_rsi_rev": 0.06,
    "score_vol_pulse": 0.10, "score_overbought": 0.06,
    "score_theme_intrinsic": 0.05, "score_attention": 0.05,
}
# 旧五维(v2.41-ic) → 十维映射：tech 拆 3 维、theme 拆 2 维，按十维先验比例缩放
_W_MAP_FROM_5D = {
    "score_fund": [("score_fund", 1.0)],
    "score_news": [("score_news", 1.0)],
    "score_sector": [("score_sector", 1.0)],
    "score_tech": [("score_rsi_rev", 0.06 / 0.22), ("score_vol_pulse", 0.10 / 0.22),
                   ("score_overbought", 0.06 / 0.22)],
    "score_theme": [("score_theme_intrinsic", 0.5), ("score_attention", 0.5)],
}


def load_factor_weights():
    """读 factor_weights.json → (weights10d, meta)。十维优先；旧五维按映射转换；
    缺失/非法回退十维先验(v3.0-10dim)。"""
    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        w = d.get("weights") or {}
        if all(k in w for k in _W_PRIOR_10D):            # 已是十维
            vals = {k: float(w[k]) for k in _W_PRIOR_10D}
        elif set(_W_PRIOR_10D) & set(w) and all(
                k in w for k in ("score_news", "score_tech", "score_fund", "score_theme", "score_sector")):
            vals = {k: 0.0 for k in _W_PRIOR_10D}         # 旧五维 → 映射转换
            for old, v in w.items():
                for new, s in _W_MAP_FROM_5D.get(old, []):
                    vals[new] += float(v) * s
            d = dict(d or {})
            d["_migrated_from_5d"] = True
        else:
            raise ValueError("weights 字段不全")
        if any(v < 0 for v in vals.values()) or abs(sum(vals.values()) - 1.0) > 0.02:
            raise ValueError("weights 非法: %s" % vals)
        return vals, d
    except Exception:
        return dict(_W_PRIOR_10D), None


FW, FW_META = load_factor_weights()
W_FUND, W_NEWS, W_SECTOR_W = FW["score_fund"], FW["score_news"], FW["score_sector"]
W_OVERSOLD60, W_BIAS60, W_RSI_REV = FW["score_oversold60"], FW["score_bias60"], FW["score_rsi_rev"]
W_VOL_PULSE, W_OVERBOUGHT = FW["score_vol_pulse"], FW["score_overbought"]
W_THEME_INT, W_ATTENTION = FW["score_theme_intrinsic"], FW["score_attention"]
SCORER_VER = FW_META.get("version", "v3.0-10dim") if FW_META else "v3.0-10dim"

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


def get_sub_theme_label(name):
    """根据股票名称解析细分题材标签(与 SUB_THEME 一致, 用于持续性判定等)。"""
    if not name:
        return ""
    if name in SUB_THEME:
        return SUB_THEME[name][0]
    n = _normalize_name(name)
    if n in SUB_THEME:
        return SUB_THEME[n][0]
    for prefix in ("XD", "DR", "XR"):
        if n.startswith(prefix):
            n2 = n[len(prefix):]
            if n2 in SUB_THEME:
                return SUB_THEME[n2][0]
            break
    base = n.rstrip("UW").rstrip("-").rstrip("U").rstrip("-")
    if base in SUB_THEME:
        return SUB_THEME[base][0]
    for key in SUB_THEME:
        if base.startswith(key) or key.startswith(base):
            return SUB_THEME[key][0]
    return ""


# ============================================================
# Data fetching
# ============================================================
def fetch_weekly_klines(code, n=30):
    """前复权周线K线 → [{date,open,close,high,low,vol,amount}]"""
    raw = code[2:]
    market = "sh" if code.startswith("sh") else "sz"
    for _host in ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]:
        try:
            url = f"{_host}/appstock/app/fqkline/get?param={market}{raw},week,,,{n},qfq"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = r.read().decode("utf-8")
            if txt.lstrip().startswith("<"):
                continue  # WAF 拦截页, 换域名
            data = json.loads(txt)
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
            continue
    return None

def fetch_klines(code):
    """前复权K线 → [{date,open,close,high,low,vol,amount}]
    v2.6: 腾讯前复权(qfqday)优先，东方财富前复权(fqt=1)备选，新浪不复权最后手段
         腾讯qfqday成交量是手(×100)，东方财富成交量已是股"""
    raw = code[2:]
    market = "sh" if code.startswith("sh") else "sz"
    
    # 方案1: 腾讯前复权K线 (qfqday, 多域名轮询; web.ifzq 域名会被 WAF 拦截)
    tencent_result = None
    _tq_hosts = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
    for attempt in range(3):
        try:
            raw_kl = None
            for _host in _tq_hosts:
                url = f"{_host}/appstock/app/fqkline/get?param={market}{raw},day,,,{KLINES_N},qfq"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        txt = r.read().decode("utf-8")
                except Exception:
                    continue
                if txt.lstrip().startswith("<"):
                    continue  # WAF 拦截页, 换域名
                data = json.loads(txt)
                key = f"{market}{raw}"
                kdata = data.get("data", {}).get(key, {})
                raw_kl = kdata.get("qfqday")
                if raw_kl:
                    break
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
    if os.environ.get("SKIP_IFIND"):
        return ""
    raw = code[2:]
    q = f"{name}{raw}市盈率PE扣非净利润增速同行业排名ROE"
    try:
        r = subprocess.run([PY, IFIND_FIN, q], capture_output=True, text=True, timeout=30)
        return r.stdout
    except:
        return ""


def run_ifind_news(name, code):
    if os.environ.get("SKIP_IFIND"):
        return ""
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


def fetch_announcements_map(days=7):
    """
    拉取近 days 日全市场公告(akshare stock_notice_report)，按6位代码聚合标题文本。
    作为消息面评分的通用覆盖源，弥补 iFinD 仅抓重点标的/被限流(429)的缺口。
    返回 {code6: 'title1 | title2 | ...'}
    """
    try:
        import akshare as ak
    except Exception:
        print("   ⚠️ akshare 不可用，跳过公告覆盖")
        return {}
    from datetime import datetime as _dt, timedelta as _td
    ann = {}
    d = _dt.now()
    for _ in range(days):
        ds = d.strftime("%Y%m%d")
        d -= _td(days=1)
        try:
            df = ak.stock_notice_report(symbol="全部", date=ds)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            c = str(row.get("代码", "")).strip()
            title = str(row.get("公告标题", "")).strip()
            if not c or not title:
                continue
            ann.setdefault(c, []).append(title)
    return {c: " | ".join(v) for c, v in ann.items()}


# ============================================================
# v3.0 B2: 同花顺财务摘要 —— 按代码缓存(7天TTL) + 指数退避重试
# 扩池至TOP500后调用量×2.5, 该接口偶发限流; 缓存+重试保证覆盖率
# ============================================================
FIN_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'fin_cache')
FIN_CACHE_TTL_DAYS = 7


def fetch_ths_abstract_cached(sym, indicator='按年度', ttl_days=FIN_CACHE_TTL_DAYS):
    """同花顺财务摘要(按年度): 命中缓存直接返回; 否则网络拉取+落盘缓存。
    指数退避重试最多4次(1.5s/3s/4.5s/6s), 仍失败返回 None(调用方兜底)。"""
    os.makedirs(FIN_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(FIN_CACHE_DIR, f"{sym}.json")
    if os.path.exists(cache_path):
        try:
            age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
            if age_days < ttl_days:
                import pandas as pd
                rows = json.load(open(cache_path, encoding='utf-8')).get('rows') or []
                if rows:
                    return pd.DataFrame(rows)
        except Exception:
            pass
    for attempt in range(4):
        try:
            import akshare as ak
            df = ak.stock_financial_abstract_ths(symbol=sym, indicator=indicator)
            if df is not None and len(df) > 0:
                try:
                    json.dump({'sym': sym,
                               'cached_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                               'rows': df.to_dict('records')},
                              open(cache_path, 'w', encoding='utf-8'), ensure_ascii=False, default=str)
                except Exception:
                    pass
                return df
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def _num_or_none(v):
    """财务字段清洗: None/False/'--'/''/百分号 → float 或 None"""
    if v is None or v is False or v == 'False':
        return None
    s = str(v).replace('%', '').replace(',', '').strip()
    if not s or s in ('--', '-', 'False', 'nan'):
        return None
    try:
        return float(s)
    except Exception:
        return None


# ============================================================
# v3.5.1: 东财「按报告期」财务摘要 —— 修复 growth 取数滞后半年缺陷
# ------------------------------------------------------------
# 【缺陷】原 growth 取同花顺「按年度」摘要末行 = 最近年报（如 2025 年报），
#   结构性滞后最新报告期 6~9 个月。实证（2026-10-14 全池审计）：
#     · 普冉股份(688766) 模型值 −38.67%（2025 年报口径）
#       vs 2026 中报扣非同比 +2994.08% —— 与业务方给数精确吻合；
#     · 全池 490/490 只缓存最后报告期均 = 2025；口径背离 >30pt 者 327 只(67%)、
#       增长方向相反者 174 只(36%)。
# 【处置】改取东财 stock_financial_abstract —— 列名即报告期（20260630/20250630…），
#   取「扣非净利润」行，按【累计口径】算「最新已披露报告期 ÷ 上年同期 − 1」。
#   累计口径即标准同比（东财按报告期接口为累计值，非单季）。
# 【口径自检】已用东财自带「归属母公司净利润增长率」字段逐位交叉验证 4/4 吻合
#   （688766 1929.650268↔1929.65 / 300502 90.984424 / 603986 1091.499056 / 000021 20.281728），
#   证明「累计口径 本期÷上年同期−1」与官方同比定义一致。
# 【边界】同比分母（上年同期）为负/为零时的处理 —— 口径必须与官方一致，否则方向反转：
#   · 官方口径 = (cur − prev) / |prev|（A股财报标准披露口径，负基数取绝对值做分母）
#   · 实证：佰维存储(688525) 2026中报扣非 66.33亿 / 2025中报 −2.32亿，
#     东财自带「归属母公司净利润增长率」= +3273.48；cur/prev−1 得 −3273.48（方向反转❌），
#     (cur−prev)/|prev| 得 +3273.48（与官方逐位吻合✅）。扣非口径同法得 +2962.97%。
#     另一例 300782（亏损扩大 −3.71亿 vs −1.47亿）官方 = −151.53，绝对值口径同样吻合。
#   · prev == 0 → 除零，同比数学无定义：growth=None + flag='zero_base'（不编造）
#   flag 语义：'ok'(prev>0且cur>0) / 'loss'(转亏, prev>0且cur≤0, 同比≤−100%) /
#   'turnaround'(扭亏为盈, prev<0且cur>0) / 'loss_stay'(持续亏损) / 'no_prev' / 'no_data'
# ============================================================
EM_FIN_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'fin_cache_em')
EM_FIN_CACHE_TTL_DAYS = 7


def _prev_year_period(p):
    """报告期同比基期: '20260630' → '20250630'; 非 8 位数字返回 None。"""
    p = str(p).strip()
    if len(p) != 8 or not p.isdigit():
        return None
    return "%d%s" % (int(p[:4]) - 1, p[4:])


def fetch_em_growth_cached(sym, ttl_days=EM_FIN_CACHE_TTL_DAYS):
    """东财财务摘要(按报告期) → 最新已披露报告期的扣非净利润累计同比。

    返回 dict（永不抛异常，失败以 flag 留档，保证不阻塞个股评分）：
      growth : float|None  同比(%)，None = 除零(zero_base)或数据缺失
      period : str|None    本期报告期（如 '20260630'）
      base   : str|None    基期报告期（如 '20250630'）
      cur/prev: float|None 扣非净利润原值（元）
      flag   : 'ok'|'loss'|'turnaround'|'loss_stay'|'zero_base'|'no_prev'|'no_data'
      src    : 'em'
    """
    sym = str(sym).strip()
    os.makedirs(EM_FIN_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(EM_FIN_CACHE_DIR, "%s.json" % sym)
    raw = None
    if os.path.exists(cache_path):
        try:
            if (time.time() - os.path.getmtime(cache_path)) / 86400 < ttl_days:
                raw = json.load(open(cache_path, encoding='utf-8'))
        except Exception:
            raw = None
    if not raw:
        for attempt in range(4):
            try:
                import akshare as ak
                df = ak.stock_financial_abstract(symbol=sym)
                if df is not None and len(df) > 0:
                    recs = []
                    for r in df.to_dict('records'):
                        row = {}
                        for k, v in r.items():
                            key = str(k)
                            if len(key) == 8 and key.isdigit():      # 报告期列 → float
                                try:
                                    row[key] = float(v)
                                except Exception:
                                    row[key] = None
                            else:                                     # 选项/指标 → str
                                row[key] = str(v) if v is not None else ""
                        recs.append(row)
                    raw = {'sym': sym,
                           'cached_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           'records': recs}
                    try:
                        json.dump(raw, open(cache_path, 'w', encoding='utf-8'),
                                  ensure_ascii=False)
                    except Exception:
                        pass
                    break
            except Exception:
                pass
            time.sleep(1.5 * (attempt + 1))
    empty = {'growth': None, 'period': None, 'base': None, 'cur': None,
             'prev': None, 'flag': 'no_data', 'src': 'em'}
    recs = (raw or {}).get('records') or []
    if not recs:
        return empty

    periods = sorted({k for r in recs for k in r
                      if len(str(k)) == 8 and str(k).isdigit()}, reverse=True)
    if not periods:
        return empty

    kf_row = None
    for r in recs:                                    # 优先「常用指标」口径的扣非净利润
        if str(r.get('指标')) == '扣非净利润' and str(r.get('选项')) == '常用指标':
            kf_row = r
            break
    if kf_row is None:
        for r in recs:
            if str(r.get('指标')) == '扣非净利润':
                kf_row = r
                break
    if kf_row is None:
        return empty

    def _val(p):
        v = kf_row.get(p)
        if v is None:
            return None
        try:
            f = float(v)
        except Exception:
            return None
        if f != f or f in (float('inf'), float('-inf')):   # NaN / inf 视为缺失
            return None
        return f

    # 选「最新已披露报告期」：该期扣非净利润有有效值，且存在可比的上年同期
    chosen, fallback_p = None, None
    for p in periods:
        if _val(p) is None:
            continue
        if fallback_p is None:
            fallback_p = p
        bp = _prev_year_period(p)
        if bp and _val(bp) is not None:
            chosen = (p, bp)
            break
    if chosen is None:
        return {'growth': None, 'period': fallback_p, 'base': None,
                'cur': _val(fallback_p) if fallback_p else None,
                'prev': None, 'flag': 'no_prev' if fallback_p else 'no_data', 'src': 'em'}

    p, bp = chosen
    cur, prev = _val(p), _val(bp)
    if prev > 0:
        # 正常口径：与绝对值口径数学等价
        g = round((cur / prev - 1.0) * 100.0, 2)
        flag = 'ok' if cur > 0 else 'loss'
    elif prev < 0:
        # 负基数：官方口径 (cur−prev)/|prev| —— 分母取绝对值，方向与经营实质一致
        #   （扭亏为盈为正、亏损扩大为负）。切勿用 cur/prev−1（负分母会反转符号）。
        g = round((cur - prev) / abs(prev) * 100.0, 2)
        flag = 'turnaround' if cur > 0 else 'loss_stay'
    else:
        # prev == 0：除零，同比数学无定义，不编造
        g, flag = None, 'zero_base'
    return {'growth': g, 'period': p, 'base': bp, 'cur': cur, 'prev': prev,
            'flag': flag, 'src': 'em'}


# ============================================================
# v3.5.1: Q3 预测景气加分（数据源：手工汇总表 parse_q3_forecast.py 结构化产物）
# 定位：Q3 预测**不作为全年盈利预测的直接输入**，而是「景气边际动能确认信号」，
#   以加分项形式接入 score_fund（只加分不扣分，Q3 是预测非事实）。
# 去重闸门：Q3 同比须**超出机构全年预测隐含的下半年增速(implied_h2)**才算增量信息，
#   避免与 fwd_np（同花顺机构一致预期）双重计数。
# 标定依据（2026-10-14，池内 65 只匹配样本实测）：
#   · 匹配样本 margin=q3_eff−implied_h2 呈双峰分布（≥56pt 或 <10pt），
#     +30/+15 与 +50/+25 两套阈值对当前数据**判定结果完全一致**（空区鲁棒）；
#   · implied_h2 可算 60/65（5 只缺 fwd_pe 或中报扣非≤0 → 走退化规则）；
#   · 扭亏文字样本池内 6 只（长鑫科技/国科微/芯源微/正帆科技/天岳先进/迅捷兴）。
# ============================================================
Q3_BONUS_CAP = 1.0          # score_fund(5分制) 加分上限 → 百分制 +3.6≈+4 分
Q3_STRONG_MARGIN = 30.0     # 强加速: q3_eff − implied_h2 > 30pt
Q3_MILD_MARGIN = 15.0       # 温和加速: > 15pt
Q3_BEAT_FLOOR = 10.0        # 略超预期: ≥ 10pt（implied_h2 估算噪声缓冲；实测空区 6.3→20.7）
Q3_STRONG_ABS = 50.0        # 强加速绝对增速下限(%)，防"低增速+负ih2基槛"被高估
Q3_MILD_ABS = 30.0          # 温和加速绝对增速下限(%)
Q3_TURNAROUND_BONUS = 0.4   # 扭亏确认（文字无数值，幅度保守，占上限40%）
Q3_DEGRADED_CAP = 0.3       # implied_h2 缺失(无法去重)时的加分上限
Q3_FORECAST_TTL_DAYS = 45   # Q3实际披露(约10月底)后即失效

# 显式别名表白名单（Q3表名 → 池内名）—— **严禁任何形式的前缀/模糊匹配**
#   历史教训：比亚迪电子(0285.HK) ≠ 比亚迪(sz002594)，前缀匹配会完全错配。
Q3_NAME_ALIAS = {"芯联集成-U": "芯联集成"}


def load_q3_forecast(ttl_days=Q3_FORECAST_TTL_DAYS):
    """取最新 data/q3_forecast_*.json，返回 {池内名: rec}（别名已映射）。

    rec 字段：sector_raw / a_yoy / a_kind / b_yoy / b_kind / a_rev / b_rev /
              a_np / b_np / b_np_kf / a_qoq / b_qoq / flag
    TTL 45 天（Q3 实际披露后失效）；无文件/过期返回 {}（不加分，不阻塞）。
    """
    try:
        paths = sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'data', 'q3_forecast_*.json')),
            reverse=True)
        if not paths:
            return {}
        path = paths[0]
        age = (time.time() - os.path.getmtime(path)) / 86400
        if age > ttl_days:
            print(f"   ⚠️ Q3预测数据过期({age:.0f}天>{ttl_days}天)：{os.path.basename(path)}，不加分")
            return {}
        payload = json.load(open(path, encoding='utf-8'))
        data = payload.get('data') or {}
        out = {}
        for nm, rec in data.items():
            out[Q3_NAME_ALIAS.get(nm, nm)] = rec      # 显式别名映射
        return out
    except Exception as e:
        print(f"   ⚠️ Q3预测加载失败（不加分）：{e}")
        return {}


def merge_q3_yoy(rec):
    """A/B 机构 Q3 净利润同比合并：双方有点值取均值，一方有取该方，都没有 None。

    分歧度留档由调用方处理（a_yoy/b_yoy 均在 rec 中，报告层可查）。
    实测 23 只双方都有：中位分歧 13.7pt，>50pt 的 5 只（普冉/兆易/南亚新材/豪威/立讯），
    均为低基数情形，由 _effective_growth 饱和兜底。
    """
    a, b = rec.get('a_yoy'), rec.get('b_yoy')
    cand = [x for x in (a, b) if isinstance(x, (int, float))]
    if not cand:
        return None
    return round(sum(cand) / len(cand), 2)


def fetch_research_reports_map(days=7, codes=None):
    """近 days 日个股研报(akshare stock_research_report_em), 按6位代码聚合。
    v2.17 新增: 作为消息面评分信号源, 弥补 iFinD新闻/交易所公告未覆盖券商研报的缺口
    (此前新易盛等个股有密集研报却被判"近7日无消息")。
    返回 {code6: {"count":N, "titles":"...", "latest":"YYYY-MM-DD", "ratings":[...]}}。
    并行抓取 + 单只超时 + 失败跳过(降级不阻塞个股评分)。
    """
    try:
        import akshare as ak
    except Exception:
        print("   ⚠️ akshare 不可用，跳过研报覆盖")
        return {}
    os.environ.setdefault("TQDM_DISABLE", "1")
    from datetime import datetime as _dt, timedelta as _td
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cutoff = _dt.now() - _td(days=days)

    def _fetch_one(code):
        try:
            df = ak.stock_research_report_em(symbol=code)
        except Exception:
            return (code, None)
        if df is None or len(df) == 0:
            return (code, None)
        titles, ratings, latest = [], [], ""
        for _, row in df.iterrows():
            d = str(row.get("日期", row.get("发布日期", ""))).strip()
            if not d:
                continue
            try:
                dt = _dt.strptime(d, "%Y-%m-%d")
            except Exception:
                continue
            if dt < cutoff:
                continue
            t = str(row.get("报告名称", row.get("标题", ""))).strip()
            if t:
                titles.append(t)
            rt = str(row.get("东财评级", "")).strip()
            if rt:
                ratings.append(rt)
            if not latest or d > latest:
                latest = d
        if not titles:
            return (code, None)
        return (code, {"count": len(titles), "titles": " | ".join(titles[:8]),
                       "latest": latest, "ratings": ratings[:8]})

    if not codes:
        return {}
    out = {}
    timeout_per = 20
    try:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(_fetch_one, c): c for c in codes}
            for fut in as_completed(futs):
                try:
                    code, res = fut.result(timeout=timeout_per)
                except Exception:
                    continue
                if res:
                    out[code] = res
    except Exception as e:
        print(f"   ⚠️ 研报并行抓取异常(降级): {type(e).__name__}")
    print(f"   研报覆盖 {len(out)} 只标的 (近{days}日, 共查{len(codes)}只)")
    return out


# ============================================================
# 1. 技术面 (0-5) × 20%
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

def rsi(closes, period=14):
    """Wilder平滑 RSI(相对强弱指标), 与同花顺/东财/通达信/腾讯自选股等主流平台一致。
    v2.19: 由简单平均改为 Wilder 平滑(EMA式递归), 修复与外部平台口径不一致的问题
    (此前简单平均会把工业富联算成29.3, 主流平台口径为44.3)。返回 0-100 或 None。"""
    if closes is None or len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(closes, fast=12, slow=26, signal=9):
    """MACD(指数平滑异同移动平均): DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF, signal),
    HIST=(DIF-DEA)*2(柱). 用于判断趋势动能方向/强度。closes 为收盘价序列, 返回
    (dif, dea, hist) 或 None(数据不足)。"""
    n = len(closes)
    if closes is None or n < slow + signal:
        return None
    # EMA 递推
    def ema(vals, span):
        k = 2.0 / (span + 1)
        e = vals[0]
        out = [e]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(n)]
    dea = ema(dif, signal)
    hist = [(dif[i] - dea[i]) * 2 for i in range(n)]
    return dif[-1], dea[-1], hist[-1]


def _ema(closes, span):
    if closes is None or len(closes) < span:
        return None
    k = 2.0 / (span + 1)
    e = closes[0]
    for v in closes[1:]:
        e = v * k + e * (1 - k)
    return e


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
    rsi14 = rsi(cls, 14)
    rsi_state = None
    if rsi14 is not None:
        if rsi14 >= 70:
            rsi_state = "超买"
        elif rsi14 <= 30:
            rsi_state = "超卖"
        else:
            rsi_state = "中性"
    hi, lo = max(cls), min(cls)
    pos = (price - lo) / (hi - lo) * 100 if hi != lo else 50
    vol5 = sum(k["vol"] for k in kl[-5:]) / 5
    vol20 = sum(k["vol"] for k in kl[-20:]) / 20
    vr = vol5 / vol20 if vol20 > 0 else 1.0
    amt5 = sum(k.get("amount", k["vol"] * k["close"]) for k in kl[-5:]) / 5
    amt20 = sum(k.get("amount", k["vol"] * k["close"]) for k in kl[-20:]) / 20
    amt_ratio = amt5 / amt20 if amt20 > 0 else 1.0
    amt_today = kl[-1].get("amount", kl[-1]["vol"] * kl[-1]["close"]) / 1e8
    is_volume_up = amt_ratio > 1.5
    is_shrink = amt_ratio < 0.7

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

    # ===== 结构分四因子 (v2.20重构: 去掉BOLL/高成长, 改为均线/MACD/RSI/成交量) =====
    # 因子1: 均线(趋势) — 多空排列等级 + 金叉死叉 + MA60确认
    if bull_level == 3:
        ma_factor = 5.0
    elif bull_level == 2:
        ma_factor = 4.3
    elif bull_level == 1:
        ma_factor = 3.6
    elif trend in ("金叉", "偏多"):
        ma_factor = 3.2
    elif trend == "缠绕":
        ma_factor = 2.5
    elif trend in ("死叉", "偏空"):
        ma_factor = 1.8
    elif bear_level == 1:
        ma_factor = 1.2
    elif bear_level == 2:
        ma_factor = 0.7
    else:
        ma_factor = 0.3
    if ma60v and price > ma60v:
        ma_factor = min(5.0, ma_factor + 0.2)
    elif ma60v and price < ma60v:
        ma_factor = max(0.0, ma_factor - 0.2)

    # 因子2: MACD(动能) — DIF/DEA零轴位置 + 柱方向
    mc = macd(cls)
    if mc is None:
        macd_factor = None
    else:
        _dif, _dea, _hist = mc
        if _dif > 0:
            macd_factor = 4.6 if _hist > 0 else 3.2
        else:
            macd_factor = 1.0 if _hist < 0 else 2.3
        if abs(_hist) / price > 0.005:
            macd_factor += 0.2 if _hist > 0 else -0.2
        macd_factor = max(0.0, min(5.0, macd_factor))

    # 因子3: 成交量(资金参与) — 量比/额比 + 量价配合
    if amt_ratio >= 2.0:
        vol_factor = 5.0
    elif amt_ratio >= 1.5:
        vol_factor = 4.3
    elif amt_ratio >= 1.0:
        vol_factor = 3.8
    elif amt_ratio >= 0.7:
        vol_factor = 3.0
    else:
        vol_factor = 2.0
    if amt_ratio > 1.5 and pct >= 0:
        vol_factor = min(5.0, vol_factor + 0.3)
    elif amt_ratio > 1.5 and pct < 0:
        vol_factor = max(0.0, vol_factor - 0.3)

    # 因子4: RSI(入场时机) — v3.0 分档重构(A1)：按实证七档单调递减
    #   (factor-backtest supp.json: 20日 +1.13%→−0.75% 完美单调)。旧代码 <20 档 3.0
    #   < 20-35 档 4.8 与实证矛盾(<20 与 20-30 并列最高档)，本版修复；
    #   <20 与 20-30 同分是对日均 46 只小样本的保守处理。
    #   注: 该子因子仅影响 tech detail 展示——十维总分已由独立 score_rsi_rev 承载。
    if rsi14 is None:
        rsi_factor = 2.5
    elif rsi14 < 30:
        rsi_factor = 5.0
    elif rsi14 < 40:
        rsi_factor = 4.0
    elif rsi14 < 50:
        rsi_factor = 3.0
    elif rsi14 < 60:
        rsi_factor = 2.0
    else:
        rsi_factor = 1.0

    W_MA, W_MACD, W_VOL, W_RSI = 0.35, 0.25, 0.25, 0.15
    _macd_f = macd_factor if macd_factor is not None else 2.5
    s = W_MA * ma_factor + W_MACD * _macd_f + W_VOL * vol_factor + W_RSI * rsi_factor
    r = ""

    # ---- 动量分 (v2.17 新增) — v3.0 三档开关，默认 off(A3) ----
    # 三重负证据: ①全市场 F1/F2 趋势多头全周期负 α；②TOP200 池 tech 维度
    #   IC −0.035(T+1)/−0.030(T+20)；③因子报告§3.2「A股是反转市」。
    # off=不融入(默认)；reverse=反向(低动量高分)；normal=原逻辑(仅对照臂)。
    # 注: 十维总分已不含动量维度，该开关仅影响 tech detail 展示值；
    #   detail 的 mom/momentum_score 字段无论开关都计算（仅展示，不进总分）。
    MOM_MODE = "off"
    W_MOM = 0.15
    _mom = _stock_momentum(kl)          # detail 展示用（60 日一并返回）
    _s_mom_disp = _momentum_score(_mom)
    _s_struct = s
    if MOM_MODE != "off":
        _s_mom = _s_mom_disp
        if MOM_MODE == "reverse":
            _s_mom = 5.0 - _s_mom
        s = _s_struct * (1 - W_MOM) + _s_mom * W_MOM
        r += f"; 结构{_s_struct:.1f}/动量{_s_mom:.1f}({MOM_MODE},权重{W_MOM:.0%})"
    else:
        s = _s_struct
        r += f"; 动量off(三重负证据,detail仅展示)"

    # ---- 结构分四因子说明 ---- v3.3 量纲升级: 内部 0-5 ×2 → 0-10
    _macd_f = macd_factor if macd_factor is not None else 2.5
    r += (f"; 均线{ma_factor*2:.1f}({trend})·MACD{_macd_f*2:.1f}·量能{vol_factor*2:.1f}"
          f"·RSI时机{rsi_factor*2:.1f}(RSI{rsi14:.0f})")

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
        "macd_dif": round(mc[0], 3) if mc else None,
        "macd_dea": round(mc[1], 3) if mc else None,
        "macd_hist": round(mc[2], 3) if mc else None,
        "ma_factor": round(ma_factor, 2),
        "macd_factor": round(_macd_f, 2),
        "vol_factor": round(vol_factor, 2),
        "rsi_factor": round(rsi_factor, 2),
        "rsi": round(rsi14, 1) if rsi14 is not None else None,
        "rsi_state": rsi_state,
        "mom5": round(_mom.get(5), 1) if _mom and _mom.get(5) is not None else None,
        "mom10": round(_mom.get(10), 1) if _mom and _mom.get(10) is not None else None,
        "mom20": round(_mom.get(20), 1) if _mom and _mom.get(20) is not None else None,
        "mom60": round(_mom.get(60), 1) if _mom and _mom.get(60) is not None else None,
        "momentum_score": round(_s_mom_disp, 2),
        "momentum_mode": MOM_MODE,
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
    if pos >= 90:
        explanations.append(f"价格处于120日{pos:.0f}%分位，接近区间高点")
    elif pos <= 10:
        explanations.append(f"价格处于120日{pos:.0f}%分位，接近区间低点")
    if rsi14 is not None:
        if rsi_state == "超买":
            explanations.append(f"RSI={rsi14:.0f} 进入超买区(>70)，短线回调概率上升")
        elif rsi_state == "超卖":
            explanations.append(f"RSI={rsi14:.0f} 进入超卖区(<30)，存在技术性反弹机会")
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


# 周期性行业板块(盈利随景气大幅波动, 低PE常处盈利高峰, 需论证持续性)
CYCLICAL_SECTORS = {
    "有色资源", "化工", "电子面板", "面板", "电力", "煤炭", "航运",
    "钢铁", "石油", "建材", "基础材料",
}

# ===== v3.5: growth 低基数失真治理（依据《半导体消费电子三季度预测汇总.xlsx》诊断）=====
# 问题：growth 取自「扣非净利润同比增长率」单点值，当基数极低(接近0或为负)时会出现
#   利通电子 +3906%、凯美特气 +3720% 这类失真值。原逻辑 `growth>50` 是二值闸门——
#   871% 与 55% 同拿满分，再叠加 PEG=PE/growth 因分母虚大而误判"严重低估"，构成双重奖励。
# 池内实证（2026-10-13，490只，年报口径）：growth max=3906%、30只>300%；growth>150 的 54 只中
#   34 只（63%）三年扣非 CAGR 为负或背离 3 倍以上 → 强烈提示低基数失真。
# ⚠️ v3.5.1（2026-10-14）按【中报口径】重标定：growth 改取最新已披露报告期（v3.5.1 修复滞后缺陷）
#   后分布显著右移，旧参数过严（中报口径下压 105 只/21.4%）。新标定（实测 490 只）：
#     p50=27.4  p75=110.8  p88=372.9  p95=1096.7  max=+318054.9%  min=−9555.5%
#   KNEE≈p75、CAP≈p88，与原「KNEE 覆盖 ≥75%、CAP≈p88」设计意图一致。
#   误伤检查通过（全部保留不动）：新易盛+90.9(cagr3=129.7)、中际旭创+229.3(117.8)、
#     赤峰黄金+56.3(96.8)、生益科技+111.0(30.5)、沪电股份+70.0、胜宏科技+12.5。
#   被压 74 只(15.1%)中，有锚 29 只里 20 只 g/cagr3>10 倍（江波龙138.7、香农芯创81.3、源杰47.0），
#     均为真实低基数（如天齐锂业 2025中报扣非仅132万→2026中报42亿）。
# 处置：①识别"存续期增长"(cagr3) 偏离 → 对极值做饱和压缩(不改变真实高增长的相对排序)；
#       ②基础分闸门由二值改为"越高越难"，消除 871%≥55% 的等价bug；
#       ③PEG 改用压缩后增速，切断失真→PEG虚小→再加分的二次奖励。
GROWTH_SATURATE_KNEE = 110.0   # v3.5.1: 60→110（中报口径 p75=110.8）
GROWTH_SATURATE_CAP = 370.0    # v3.5.1: 150→370（中报口径 p88=372.9）
GROWTH_ANCHOR_MULT = 2.5       # 存续期锚容忍倍数：单点增速 ≤ CAGR×倍数+拐点 视为被趋势支持


def _effective_growth(growth, cagr3):
    """v3.5: 将单点增速折算为「可采信增速」，抑制低基数极值失真（winsorize 饱和）。

    规则（保守、可审计；被存续期趋势支持的高增长不做任何改动）：
      1. growth ≤ KNEE(110%) → 原样返回（覆盖中报口径池内 ≥75% 标的，主体分布不动）。
      2. growth > KNEE：算"存续期趋势可支持的上限" ceiling——
         - cagr3(三年扣非 CAGR) 为正 → ceiling = cagr3 × 2.5 + KNEE
           （cagr3 是可信存续期锚，故不设全局硬上限；新易盛 cagr3=129.7 → 上限434，
            单点增速 91% 未被误伤）
         - cagr3 缺失或 ≤0（历史未增长/衰退）→ ceiling = CAP(370%)，视为无可信锚
      3. growth ≤ ceiling → 原样返回（被趋势支持，不得误伤；如中际旭创 229.3% < 117.8×2.5+110=404.5）。
      4. growth > ceiling → 饱和至 ceiling 并标记 was_capped（失真证据）。

    返回 (effective_growth, was_capped: bool, note: str|None)
    """
    g = growth
    if g is None or g <= GROWTH_SATURATE_KNEE:
        return g, False, None
    if isinstance(cagr3, (int, float)) and cagr3 > 0:
        ceiling = cagr3 * GROWTH_ANCHOR_MULT + GROWTH_SATURATE_KNEE
    else:
        ceiling = GROWTH_SATURATE_CAP
    ceiling = max(ceiling, GROWTH_SATURATE_KNEE + 1.0)   # 保底，避免 ceiling≤KNEE 导致倒挂
    if g <= ceiling:
        return g, False, None                            # 被趋势支持 → 不动
    eff = round(ceiling, 1)
    anchor = f"三年CAGR={cagr3:.1f}%" if isinstance(cagr3, (int, float)) else "无存续期CAGR"
    note = f"低基数失真修正: 单点增速{g:.0f}%→{eff:.0f}%(超出{anchor}可支持上限,已饱和)"
    return eff, True, note


def score_fund(pe, fwd_pe, growth, code, sector, pe_ladder_data=None,
               roe=None, gross_margin=None, sub_theme_label=None,
               hist_loss_free=False, fwd_np=None, sw_l2=None, cagr3=None,
               q3_info=None):
    """基本面+竞争力评分 0-5，用估值阶梯替代单年PEG
    v2.8: 增加PEG指标、ROE、毛利率评分
    v2.10: 增加公司持续性/低PE可持续性子评分(周期股/存储链重点论证)
    v2.12: 存储链识别扩至芯片/控制器/分销; 周期股持续性采用封顶策略(陷阱3.5/中性4.5)
    v2.14: 方案C——模组/分销环节一律周期中性(封顶4.5); 芯片/控制器需穿越周期
           校验(近4年扣非净利全为正) + 预测增速不衰减(fwd_np后两年增速≥25%)才判可持续
    v2.15: 模组/分销业务模式不可持续 → 基本面封顶3分(标签+名单双识别)
    v2.16: 名单改以【财报主营构成】为依据(德明利收入100%存储/毛利14.8%=模组厂,
           非"存储控制器"标签; 名单含主营构成证据注释)
    v3.5.1: 新增 q3_info(Q3预测景气加分)——只加分不扣分；叠加在持续性封顶**之前**
           （周期股封顶不被加分突破；被封顶标的的加分可能被部分/全部吸收，审计留档）
    """
    effective_pe = fwd_pe if fwd_pe else pe
    if code in KNOWN:
        k_fwd, k_growth = KNOWN[code]
        if effective_pe is None or effective_pe <= 0:
            effective_pe = k_fwd
        if growth is None:
            growth = k_growth

    reasons = []
    _fwd_pe_out = effective_pe if (fwd_pe or code in KNOWN or effective_pe) else None
    _ladder_out = pe_ladder_data or {}
    _sustain_empty = {"score": 3.0, "note": "数据不足,未评估持续性", "is_cyclic": False, "is_storage_mod": False}

    # v3.5: 低基数失真治理——原始增速留档 (_growth_raw)，后续评分统一用压缩后的 _g
    _growth_raw = growth
    growth, _g_capped, _g_note = _effective_growth(growth, cagr3)
    # v3.5.2: _growth_out 必须是 effective 化【之后】的快照。v3.5 原快照取在压缩之前，
    #   导致返回/落库的 growth 是原始值(兆易 796.9)——评分内部虽用压缩值 370 正确计分，
    #   但报告徽章渲染成「+797%→+797%」(raw→raw 恒等)，低基数修正对使用者不可见。
    #   本修复只改展示透传字段，不动评分逻辑（total/rating 逐只不变，可回归验证）。
    _growth_out = growth if growth is not None else None

    if effective_pe is None or effective_pe <= 0:
        if growth is None:
            return 2, ["数据不足"], _fwd_pe_out, _growth_out, _ladder_out, _sustain_empty
        if growth > 100:
            return 3, [f"高增长(+{growth:.0f}%)但缺估值"], _fwd_pe_out, _growth_out, _ladder_out, _sustain_empty
        elif growth > 20:
            return 3, [f"中速增长(+{growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out, _sustain_empty
        elif growth > 0:
            return 2, [f"低增长(+{growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out, _sustain_empty
        else:
            return (1 if growth > -20 else 0), [f"负增长({growth:.0f}%)"], _fwd_pe_out, _growth_out, _ladder_out, _sustain_empty

    if growth is None:
        growth = 15

    # 低基数修正如触发，挂到 reasons 头部（数据可审计）
    if _g_capped and _g_note:
        reasons.append(_g_note)

    # 基础评分：基于FwdPE + 增速
    # v3.5: 增速已由 _effective_growth 做低基数饱和；原 `growth>50` 的"871%与55%同满分"
    #   问题已由增速压缩从根源消除（失真值 ≤150，真实高增长按其存续期趋势保留）。
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

    # ===== 公司持续性 / 低PE可持续性评估 (v2.10) =====
    # 判定: 周期股(盈利随景气波动) & 存储模组厂(模组环节薄利/价格周期敏感)
    # 论证低PE是否可持续: 用估值阶梯轨迹(收缩=周期底部错杀/扩张=顶部陷阱)+增速+ROE
    # v2.46: 周期股判定 = 主题粗分类命中 或 真实申万二级行业命中
    #   (粗分类如"周期资源"会覆盖煤炭/石油/钢铁, 但"综合"里的周期行业需靠 sw_l2 兜底)
    is_cyclic = sector in CYCLICAL_SECTORS or (bool(sw_l2) and sw_l2 in SW_CYCLICAL)
    # v2.12: 存储产业链(芯片/模组/控制器/分销)均为强价格周期环节,
    # 与有色/化工等周期股同等对待, 需论证低PE可持续性(此前仅识别"存储模组"过窄)
    STORAGE_CYCLIC_KW = ("存储芯片", "存储模组", "存储控制器", "存储分销")
    is_storage_mod = any(k in (sub_theme_label or "") for k in STORAGE_CYCLIC_KW)
    ladder_shrink = None
    if pe_ladder_data and len(pe_ladder_data) >= 2:
        ys = sorted(pe_ladder_data.keys())
        pf, pl = pe_ladder_data[ys[0]], pe_ladder_data[ys[-1]]
        if pf and pf > 0:
            ladder_shrink = (pf - pl) / pf

    sustain_score = 3.0
    if is_cyclic or is_storage_mod:
        # v2.12: 周期/存储链的景气期高增速、高ROE本身顺周期失真(盈利高峰),
        # 不能单独作为"可持续"证据; 需"高增长+估值阶梯收缩"双重确认才算可持续
        low_pe_peak = (effective_pe is not None and effective_pe < 20
                       and growth is not None and growth <= 15)
        # v2.14 方案C: 预测后两年增速衰减校验(机构预测本身判断高增长能否延续)
        decay = None
        if fwd_np and isinstance(fwd_np, dict) and len(fwd_np) >= 3:
            ys = sorted(int(k) for k in fwd_np.keys())
            gs = []
            for a, b in zip(ys[:-1], ys[1:]):
                va, vb = fwd_np.get(str(a), fwd_np.get(a)), fwd_np.get(str(b), fwd_np.get(b))
                if va and vb and va > 0:
                    gs.append((vb - va) / va)
            if gs:
                decay = sum(gs) / len(gs)
        # v2.16: 模组/分销业务识别以【财报主营构成】为准(非题材标签)。
        # 标签可能与实际收入组成不符——德明利题材标"存储控制器",但2025年报
        # 收入100%来自存储(存储产品42%+嵌入式34%+移动存储14%+内存条10%),
        # 毛利率仅14.8%,本质是模组厂而非控制器厂。以下名单均基于主营构成核验:
        STORAGE_BUSINESS_MIX = {
            "sh688525": "存储产品96%+经销56%(2025年报),模组厂",
            "sz301308": "存储产品100%(2026中报),模组厂",
            "sz300475": "电子元器件分销94%(2025年报),分销商",
            "sz001309": "存储100%、毛利率14.8%(2025年报),模组厂(收入无控制器)",
            "sz000021": "高端制造代工54%+存储26%(2025年报),代工/模组",
        }
        is_pure_price = (any(k in (sub_theme_label or "") for k in ("存储模组", "存储分销"))
                         or code in STORAGE_BUSINESS_MIX)
        if is_pure_price:
            sustain_score = 1.0
            sustain_note = "模组/分销业务模式不可持续(盈利依赖存储价格周期、无定价权),基本面封顶3分"
        elif low_pe_peak:
            sustain_score = 2.0
            sustain_note = "周期/存储链低PE或处盈利高峰,警惕低PE陷阱(盈利不可持续)"
        else:
            # v2.14 方案A: 穿越周期校验——近4年扣非净利全为正(历史盈利未在
            # 下行期亏损) + 预测增速不衰减(后两年预测净利增速均值>=25%)
            # v3.5 收紧(依据《三季度预测汇总》诊断)：原 `growth > 30` 单点即可解锁 durable，
            #   使中材科技(单点234%/三年CAGR-16.5%)、金丹科技(439%/-12.4%) 这类「高YoY但
            #   长期衰退」的峰值幻象被误判为可持续。现改为多重证据：
            #   ①用"压缩后可采信增速"取代原始单点(低基数失真不会解锁 durable)；
            #   ②须有存续期锚支持(cagr3 为正或缺失但增速在合理区间)；
            #   ③须有前瞻延续证据(decay 缺失时不再默认放行)。
            _anchor_ok = (cagr3 is None) or (isinstance(cagr3, (int, float)) and cagr3 > 0)
            _growth_ok = growth is not None and (growth > 30 or _g_capped)
            durable = (_growth_ok
                       and _anchor_ok
                       and (ladder_shrink is None or ladder_shrink >= 0.15)
                       and hist_loss_free
                       and (decay is None or decay >= 0.25))
            if durable:
                sustain_score = 4.0
                sustain_note = "高增长+估值阶梯收缩+穿越周期盈利稳定+预测增长延续,可持续性强"
            else:
                sustain_score = 3.0
                reason_parts = []
                if not hist_loss_free:
                    reason_parts.append("历史盈利未穿越周期(近4年有亏损/负净利)")
                if not _anchor_ok:
                    reason_parts.append(f"存续期CAGR为负({cagr3:.1f}%),高增速系低基数/景气峰值")
                if decay is not None and decay < 0.25:
                    reason_parts.append(f"预测后两年增速衰减至{decay*100:.0f}%")
                if not _growth_ok:
                    reason_parts.append("增速不足或已判定低基数失真")
                sustain_note = "周期性行业,盈利随景气波动,持续性中性" + (
                    ";" + ";".join(reason_parts) if reason_parts else "")
    else:
        if roe is not None and roe >= 0.15 and (growth is None or growth > 0):
            sustain_score = 4.0
            sustain_note = f"高ROE({roe*100:.0f}%)护城河,盈利可持续"
        elif growth is not None and growth < 0:
            sustain_score = 2.0
            sustain_note = "盈利下滑,持续性偏弱"
        else:
            sustain_score = 3.0
            sustain_note = "盈利持续性中性"

    # ===== v3.5.1: Q3 预测景气加速加分（只加分不扣分）=====
    # 三层机制：①Q3同比先过 _effective_growth 饱和（防低基数极值触发最高档）
    #          ②与机构全年预测隐含下半年增速 implied_h2 做去重闸门（防与 fwd_np 双重计数）
    #          ③分档映射；叠加在下方持续性封顶**之前** → 周期股封顶不被加分突破
    q3_bonus, q3_note = 0.0, ""
    q3_audit = {"yoy_raw": None, "yoy_eff": None, "accel": None,
                "implied_h2": None, "bonus": 0.0, "note": "", "src": None}
    if q3_info:
        q3_audit["src"] = q3_info.get("src") or "manual_q3_table"
        _q3_yoy_raw = merge_q3_yoy(q3_info)
        q3_audit["yoy_raw"] = _q3_yoy_raw
        _q3_flag = q3_info.get("flag") or "none"
        _q3_ih2 = q3_info.get("implied_h2")
        if isinstance(_q3_ih2, (int, float)):
            _q3_ih2 = float(_q3_ih2)
        else:
            _q3_ih2 = None
        q3_audit["implied_h2"] = _q3_ih2
        _q3_eff = None
        if isinstance(_q3_yoy_raw, (int, float)):
            _q3_eff = _effective_growth(float(_q3_yoy_raw), cagr3)[0]
        q3_audit["yoy_eff"] = _q3_eff
        # accel = Q3饱和同比 − H1饱和同比（growth 此时已是 _effective_growth 后的值）
        _q3_accel = ((_q3_eff - growth)
                     if (_q3_eff is not None and isinstance(growth, (int, float))) else None)
        q3_audit["accel"] = _q3_accel
        if _q3_flag == "turnaround":
            # 扭亏文字不赋数值：最强度数但幅度保守（占上限40%）
            q3_bonus = Q3_TURNAROUND_BONUS
            q3_note = "Q3预测扭亏为盈(文字表述,未量化)"
        elif _q3_eff is not None and _q3_eff > 0:
            if _q3_ih2 is not None:
                _margin = _q3_eff - _q3_ih2
                if (_q3_accel is not None and _q3_accel > 0
                        and _q3_eff >= Q3_STRONG_ABS and _margin > Q3_STRONG_MARGIN):
                    q3_bonus = Q3_BONUS_CAP
                    q3_note = (f"Q3预测强加速(+{_q3_eff:.0f}%,H1+{growth:.0f}%)"
                               f"且超机构隐含下半年增速({_margin:+.0f}pt)")
                elif (_q3_accel is not None and _q3_accel > 0
                      and _q3_eff >= Q3_MILD_ABS and _margin > Q3_MILD_MARGIN):
                    q3_bonus = 0.6
                    q3_note = (f"Q3预测温和加速(+{_q3_eff:.0f}%,H1+{growth:.0f}%)"
                               f"超机构隐含增速({_margin:+.0f}pt)")
                elif _margin >= Q3_BEAT_FLOOR:
                    q3_bonus = 0.3
                    q3_note = f"Q3预测略超机构隐含下半年增速({_margin:+.0f}pt)"
            elif _q3_accel is not None and _q3_accel > 0:
                # 退化规则：implied_h2 缺失(缺fwd_pe或中报扣非≤0) → 无法去重，加分上限+0.3
                q3_bonus = Q3_DEGRADED_CAP
                q3_note = f"Q3预测加速(+{_q3_eff:.0f}%,H1+{growth:.0f}%);无法计算机构隐含增速,加分降档"
        if _q3_flag == "suspect" and q3_bonus > 0:
            q3_bonus = round(q3_bonus * 0.5, 2)     # 区间颠倒样本加分减半
            q3_note += "(机构区间颠倒,加分减半)"
        q3_audit["bonus"] = q3_bonus
        q3_audit["note"] = q3_note
        if q3_bonus > 0:
            s = min(5, s + q3_bonus)
            reasons.append(f"Q3预测景气确认(+{q3_bonus:.1f}):{q3_note}")

    # v2.12 持续性修正: 非周期股用有限权重(±0.2)微调;
    # 周期/存储链采用"封顶"策略——只有"可持续"(durable)才允许满分:
    #   模组/分销业务不可持续(1.0) → 基本面封顶3.0; 低PE陷阱(2.0) → 封顶3.5;
    #   中性(3.0) → 封顶4.5; 可持续(4.0) → 不封顶
    # 防止周期股在盈利高峰以低PE虚高拿满分
    if is_cyclic or is_storage_mod:
        if is_pure_price:
            s = min(s, 3.0)
            reasons.append("模组/分销业务模式不可持续,基本面分封顶3分")
        elif sustain_score <= 2.0:
            s = min(s, 3.5)
            reasons.append("周期股低PE陷阱,基本面分封顶3.5")
        elif sustain_score == 3.0:
            s = min(s, 4.5)
            reasons.append("周期股持续性中性,基本面分封顶4.5")
    else:
        s = max(0, min(5, s + (sustain_score - 3.0) * 0.2))
    reasons.append(f"持续性:{sustain_note}")
    sustain_info = {
        "score": round(sustain_score, 1),
        "note": sustain_note,
        "is_cyclic": is_cyclic,
        "is_storage_mod": is_storage_mod,
        # v3.5: 低基数失真治理的审计信息（原始增速保留在外部 growth 字段，此处为评分实际取值）
        "growth_raw": _growth_raw,
        "growth_eff": growth,
        "growth_capped": bool(_g_capped),
        "growth_note": _g_note,
        # v3.5.1: Q3 预测景气加分审计（yoy_raw=机构合并原值 / yoy_eff=饱和后 /
        #   accel=Q3−H1饱和同比 / implied_h2=机构全年隐含下半年增速 / bonus=加分）
        "q3_yoy_raw": q3_audit.get("yoy_raw"),
        "q3_yoy_eff": q3_audit.get("yoy_eff"),
        "q3_accel": q3_audit.get("accel"),
        "q3_implied_h2": q3_audit.get("implied_h2"),
        "q3_bonus": q3_audit.get("bonus", 0.0),
        "q3_note": q3_audit.get("note", ""),
        "q3_src": q3_audit.get("src"),
    }

    return s, reasons, _fwd_pe_out, _growth_out, _ladder_out, sustain_info


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
    # ---- v2.46 补充: 静态字典原有键但此前无主题定义(全部落入"综合"模板) ----
    "电力设备": {
        "score": 3,
        "label": "电力设备/电网/风电",
        "reason": "电网投资+海风招标+特高压建设, 稳健成长赛道, 受招标节奏驱动",
        "top5": ["思源电气", "中国西电", "特变电工"],
    },
    "高端制造": {
        "score": 3,
        "label": "高端制造/机器人",
        "reason": "工业机器人/专用设备国产替代, 制造业升级长逻辑, 事件与订单驱动",
        "top5": ["汇川技术", "埃斯顿", "先导智能"],
    },
    "航天军工": {
        "score": 3,
        "label": "航天军工",
        "reason": "国防装备采购+商业航天, 事件驱动型题材, 订单节奏不透明",
        "top5": ["中国船舶", "航天电子"],
    },
    "医药生物": {
        "score": 3,
        "label": "医药生物/创新药",
        "reason": "创新药出海+医疗设备招标复苏, 防御性赛道, 受政策与临床数据驱动",
        "top5": ["恒瑞医药", "药明康德"],
    },
    "金融": {
        "score": 2,
        "label": "金融/券商保险",
        "reason": "券商保险跟涨大盘, 非当前题材主线, 弹性来自成交额与政策预期",
        "top5": ["中信证券", "东方财富"],
    },
    "计算机软件": {
        "score": 4,
        "label": "计算机/AI应用",
        "reason": "AI应用/信创/算力服务, 软件层阶段性活跃, 业绩兑现分化大",
        "top5": ["浪潮信息", "中科曙光"],
    },
    "交通运输": {
        "score": 2,
        "label": "交通运输/航运",
        "reason": "航运港口运价波动+出行链, 周期属性明显, 受运价与油价驱动",
        "top5": ["中远海能", "招商轮船"],
    },
    "传媒娱乐": {
        "score": 2,
        "label": "传媒/游戏",
        "reason": "游戏/影视/AI应用, 事件驱动轮动, 版号与内容周期主导",
        "top5": ["世纪华通"],
    },
    "汽车": {
        "score": 3,
        "label": "汽车/智能驾驶",
        "reason": "智能驾驶+机器人叙事, 整车与零部件轮动, 价格战压制盈利",
        "top5": ["比亚迪"],
    },
    "消费": {
        "score": 2,
        "label": "消费/食饮零售",
        "reason": "消费复苏偏弱, 防御性配置为主, 关注客流与提价兑现",
        "top5": ["贵州茅台", "中国中免"],
    },
    "周期资源": {
        "score": 2,
        "label": "煤炭石油钢铁",
        "reason": "强周期资源品, 受商品价格与稳增长政策驱动, 与成长风格负相关",
        "top5": ["中国神华"],
    },
    "农业": {
        "score": 2,
        "label": "农业/养殖",
        "reason": "农林牧渔, 猪周期与种植链, 防御性题材, 价格弹性主导",
        "top5": [],
    },
    "电力": {
        "score": 2,
        "label": "电力/公用事业",
        "reason": "公用事业防御属性, 电价与来水波动, 红利资金配置方向",
        "top5": ["长江电力"],
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

# ============================================================
# v2.46 行业数据有效性: 申万二级行业 → 主题大类
# ------------------------------------------------------------
# 历史问题(老大核查发现): get_sec() 仅为名称静态字典(348条), 而 TOP200 中
#   30~40% 个股不在字典内 → sector 落入"综合" → 题材热度用中性模板评分、
#   细分题材显示"综合/其他"(如大金重工实为风电设备, 并非综合)。
#   另: 观察池输入自带的 sector 是申万二级名(如"化学制药"), THEME_DB 无此键,
#   同样整体落入"综合"模板。
# 修复: 以 sector/sw_map.json(申万二级, 覆盖全市场 5218 只)为权威源, 先把
#   任意行业名归一到 THEME_DB 主题键; 静态字典降级为兜底; 细分题材展示层
#   优先用真实申万二级行业名。解析来源记入结果 sector_src 供审计。
# ============================================================
SW2THEME = {
    # --- AI/半导体 链 ---
    "半导体": "AI/半导体", "其他电子Ⅱ": "AI/半导体", "元件": "AI/半导体",
    "通信设备": "AI/半导体",
    # --- 电子 ---
    "光学光电子": "电子面板", "消费电子": "电子制造", "电子化学品Ⅱ": "电子材料",
    "照明设备Ⅱ": "家电制造",
    # --- 计算机/AI应用 ---
    "计算机设备": "计算机软件", "软件开发": "计算机软件", "IT服务Ⅱ": "计算机软件",
    # --- 新能源与电力设备 ---
    "电池": "新能源", "光伏设备": "新能源", "风电设备": "新能源",
    "电网设备": "电力设备", "电机Ⅱ": "电力设备", "其他电源设备Ⅱ": "电力设备",
    "电力": "电力", "燃气Ⅱ": "电力",
    # --- 有色资源 ---
    "贵金属": "有色资源", "工业金属": "有色资源", "小金属": "有色资源",
    "能源金属": "有色资源", "金属新材料": "有色资源",
    # --- 材料/化工 ---
    "化学原料": "化工", "化学制品": "化工", "化学纤维": "化工", "农化制品": "化工",
    "橡胶": "化工", "塑料": "化工", "炼化及贸易": "化工",
    "玻璃玻纤": "基础材料", "水泥": "基础材料", "装修建材": "基础材料",
    "非金属材料Ⅱ": "基础材料",
    # --- 家电/消费 ---
    "白色家电": "家电制造", "黑色家电": "家电制造", "小家电": "家电制造",
    "厨卫电器": "家电制造", "家电零部件Ⅱ": "家电制造", "其他家电Ⅱ": "家电制造",
    "白酒Ⅱ": "消费", "非白酒": "消费", "饮料乳品": "消费", "食品加工": "消费",
    "休闲食品": "消费", "调味发酵品Ⅱ": "消费", "一般零售": "消费", "专业连锁Ⅱ": "消费",
    "互联网电商": "消费", "旅游零售Ⅱ": "消费", "化妆品": "消费", "个护用品": "消费",
    "饰品": "消费", "服装家纺": "消费", "纺织制造": "消费", "家居用品": "消费",
    "文娱用品": "消费", "旅游及景区": "消费", "酒店餐饮": "消费", "教育": "消费",
    # --- 传媒 ---
    "游戏Ⅱ": "传媒娱乐", "影视院线": "传媒娱乐", "数字媒体": "传媒娱乐",
    "出版": "传媒娱乐", "广告营销": "传媒娱乐", "电视广播Ⅱ": "传媒娱乐",
    "体育Ⅱ": "传媒娱乐",
    # --- 汽车 ---
    "乘用车": "汽车", "商用车": "汽车", "汽车零部件": "汽车", "汽车服务": "汽车",
    "摩托车及其他": "汽车",
    # --- 医药 ---
    "化学制药": "医药生物", "生物制品": "医药生物", "医疗器械": "医药生物",
    "医疗服务": "医药生物", "医药商业": "医药生物", "中药Ⅱ": "医药生物",
    "医疗美容": "医药生物", "动物保健Ⅱ": "医药生物",
    # --- 金融 ---
    "证券Ⅱ": "金融", "国有大型银行Ⅱ": "金融", "股份制银行Ⅱ": "金融",
    "城商行Ⅱ": "金融", "农商行Ⅱ": "金融", "保险Ⅱ": "金融", "多元金融": "金融",
    # --- 军工 ---
    "航天装备Ⅱ": "航天军工", "航空装备Ⅱ": "航天军工", "航海装备Ⅱ": "航天军工",
    "地面兵装Ⅱ": "航天军工", "军工电子Ⅱ": "航天军工",
    # --- 机械/高端制造 ---
    "专用设备": "高端制造", "通用设备": "高端制造", "自动化设备": "高端制造",
    "工程机械": "高端制造", "轨交设备Ⅱ": "高端制造",
    # --- 交通运输 ---
    "航运港口": "交通运输", "航空机场": "交通运输", "物流": "交通运输",
    "铁路公路": "交通运输",
    # --- 周期资源(煤炭石油钢铁化纤造纸) ---
    "煤炭开采": "周期资源", "油气开采Ⅱ": "周期资源", "油服工程": "周期资源",
    "焦炭Ⅱ": "周期资源", "普钢": "周期资源", "特钢Ⅱ": "周期资源",
    "冶钢原料": "周期资源", "造纸": "周期资源",
    # --- 农业 ---
    "种植业": "农业", "养殖业": "农业", "饲料": "农业", "渔业": "农业",
    "林业Ⅱ": "农业", "农产品加工": "农业", "农业综合Ⅱ": "农业",
    # 未列入的申万二级(房地产开发/建筑/环保/贸易/专业服务/综合Ⅱ/包装印刷/
    # 通信服务等)归"综合"——语义上确为中性常规赛道, 不做强行归类。
}

# 静态字典旧键 → THEME_DB 主题键(静态字典降级为兜底后仍需归一)
STATIC2THEME = {
    "电子元件": "AI/半导体", "新材料": "基础材料", "煤炭": "周期资源",
    "石油": "周期资源", "航运": "交通运输", "建材": "基础材料",
    "钢铁": "周期资源", "面板": "电子面板", "农业": "农业",
}

# 申万层面的周期行业集合: 主题粗分类(如"周期资源")会丢失周期属性时,
# 用真实申万二级名补充 is_cyclic 判定(周期股低PE陷阱保护)。
SW_CYCLICAL = {
    "贵金属", "工业金属", "小金属", "能源金属", "金属新材料", "煤炭开采",
    "油气开采Ⅱ", "油服工程", "焦炭Ⅱ", "普钢", "特钢Ⅱ", "冶钢原料", "水泥",
    "玻璃玻纤", "装修建材", "航运港口", "航空机场", "电力", "燃气Ⅱ", "橡胶",
    "塑料", "化学纤维", "造纸", "种植业", "养殖业", "饲料", "渔业", "林业Ⅱ",
    "农产品加工", "农业综合Ⅱ", "房地产开发",
}

_SW_MAP_CACHE = None


def load_sw_map():
    """读取 sector/sw_map.json → (code6→申万二级名, 二级名集合, 二级→一级父行业)"""
    global _SW_MAP_CACHE
    if _SW_MAP_CACHE is not None:
        return _SW_MAP_CACHE
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector", "sw_map.json")
    code2l2, l2_names, l2_parent = {}, set(), {}
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        code2l2 = {str(c).zfill(6): n for c, n in (m.get("l2") or {}).items()}
        l2_names = set((m.get("l2_members") or {}).keys())
        l2_parent = dict(m.get("l2_parent") or {})
    except Exception as e:
        print(f"⚠️ 申万映射读取失败({path}): {e}; 行业解析降级为静态字典")
    _SW_MAP_CACHE = (code2l2, l2_names, l2_parent)
    return _SW_MAP_CACHE


def _norm_code6(code):
    """sz002487 / 002487 → 002487"""
    c = str(code or "").strip().lower()
    if len(c) > 6 and c[:2] in ("sh", "sz", "bj"):
        return c[2:]
    return c


def to_theme_key(sec):
    """任意行业名(申万二级/静态字典旧键/主题键) → THEME_DB 主题键"""
    if not sec:
        return "综合"
    sec = str(sec).strip()
    if sec in THEME_DB:
        return sec
    if sec in SW2THEME:
        return SW2THEME[sec]
    return STATIC2THEME.get(sec, "综合")


def resolve_sector(name, code=None):
    """行业主题解析(v2.46): 申万二级映射优先 → 静态字典 → 综合。
    返回 (theme_key, sw_l2, source); sw_l2 为真实申万二级行业名(可能为 None)。
    """
    code2l2, _l2_names, _ = load_sw_map()
    l2 = code2l2.get(_norm_code6(code)) if code else None
    if l2:
        th = SW2THEME.get(l2)
        if th:
            return th, l2, "sw"
    s = get_sec(name)          # 静态字典兜底(名称匹配)
    if s != "综合":
        return to_theme_key(s), l2, "static"
    return "综合", l2, "fallback"


def sw_l2_of(code):
    """code → 申万二级行业名(无映射返回 None)"""
    code2l2, _l2_names, _ = load_sw_map()
    return code2l2.get(_norm_code6(code))

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
    "周期资源": "煤炭行业",
    "农业": "农牧饲渔",
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


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _stock_momentum(kl):
    """从日K线计算个股近5/10/20/60日收益率(%)。返回 {5,10,20,60} 或 None。
    v3.0 增 60 日：供超跌60 因子(F12)使用。"""
    if not kl or len(kl) < 6:
        return None
    closes = [k["close"] for k in kl]
    def _ret(n):
        if len(closes) <= n:
            return None
        base = closes[-1 - n]
        if base <= 0:
            return None
        return (closes[-1] - base) / base * 100.0
    return {5: _ret(5), 10: _ret(10), 20: _ret(20), 60: _ret(60)}


def _agg_momentum(moms):
    """对一组个股动量求中位数与上涨占比。moms: list of {5,10,20} 或 None。"""
    out = {}
    for w in (5, 10, 20):
        vals = [m[w] for m in moms if m and m.get(w) is not None]
        if vals:
            s = sorted(vals)
            mid = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0
            up = sum(1 for v in vals if v > 0)
            out[w] = {"median": mid, "up_ratio": up / len(vals), "n": len(vals)}
    return out


def _momentum_score(mom):
    """由个股近5/10/20日收益率映射动量分(0-5)。v2.17 新增, 作为技术面动量子项。
    规则: 中性基准2.5; 20日收益为主(权重0.6), 10日(0.3), 5日(0.1); 分级加成。
    短中期均强(20日≥10%且10日≥5%)额外+0.3; 均弱额外-0.3。
    """
    if not mom:
        return 2.5
    r20 = mom.get(20)
    r10 = mom.get(10)
    r5 = mom.get(5)

    def adj(v):
        if v is None:
            return 0
        if v >= 30: return 1.5
        if v >= 20: return 1.2
        if v >= 10: return 0.8
        if v >= 5:  return 0.4
        if v >= 0:  return 0.0
        if v >= -5: return -0.4
        if v >= -10: return -0.8
        if v >= -20: return -1.2
        return -1.5

    s = 2.5
    s += adj(r20) * 0.6 + adj(r10) * 0.3 + adj(r5) * 0.1
    if r20 is not None and r10 is not None and r20 >= 10 and r10 >= 5:
        s += 0.3
    if r20 is not None and r10 is not None and r20 <= -10 and r10 <= -5:
        s -= 0.3
    return max(0.0, min(5.0, s))


# ============================================================
# v3.0-10dim: 回测 Top5 因子（2026-10-13，factor-backtest 扩展回测实证）
# 口径: 全市场 2021-01~2026-09, 5019 只, T+1 开盘进(剔除涨停), h=20 日,
#       基准=同日全市场等权, 聚类稳健 t。详见
#       《因子排名与10维评分模型设计_2026-10-13》。
# ⚠️ 这些维度在 TOP200 池无历史截面 → 初始权重为先验映射(prior_pending)，
#   池内 IC 满 30+ 截面后由 --optimize-weights 重拟合。
# ============================================================

def score_oversold60(mom60):
    """F12 超跌60（Top5#1，h20 超额 +2.57%, t=7.34, 60日 +3.85% 全场最高, 六年全正）。
    60 日收益深跌 = 长线均值回复候选。"""
    if mom60 is None:
        return 2.5
    if mom60 <= -30: return 5.0
    if mom60 <= -20: return 4.0
    if mom60 <= -10: return 3.0
    if mom60 <= 0:   return 2.0
    return 1.0


def score_bias60(price, ma60):
    """F9 乖离低位（Top5#2，h20 超额 +2.04%, t=9.40 全场最高, 六年全正）。
    (收盘−MA60)/MA60 深度负值 = 相对均线位置超跌。"""
    if not ma60 or ma60 <= 0 or not price:
        return 2.5
    bias = (price - ma60) / ma60 * 100.0
    if bias <= -15: return 5.0
    if bias <= -10: return 4.0
    if bias <= -5:  return 3.0
    if bias <= 0:   return 2.0
    return 1.0


def score_rsi_rev(rsi14):
    """F4 RSI 反转（Top5#3，h20 超额 +1.13%, t=5.21, 六年全正；实证七档单调递减）。
    替代旧技术面 RSI 子因子并修复 <20 档得分矛盾（旧代码 3.0 < 20-35 档 4.8）。"""
    if rsi14 is None:
        return 2.5
    if rsi14 < 30: return 5.0
    if rsi14 < 40: return 4.0
    if rsi14 < 50: return 3.0
    if rsi14 < 60: return 2.0
    return 1.0


def score_vol_pulse(amt_ratio):
    """F8 放量反向（Top5#4，原生 h20 超额 −1.31%, t=−12.24 全场最强负向 → 反向后 +1.31%）。
    额比 amt5/amt20 极高 = 关注度脉冲 = TOP200 池内均值回复（与 tech/theme 负 IC 互证）。"""
    if amt_ratio is None:
        return 3.0
    if amt_ratio >= 2.0: return 1.0
    if amt_ratio >= 1.5: return 2.0
    if amt_ratio >= 1.0: return 3.0
    if amt_ratio >= 0.7: return 4.0
    return 5.0


def score_overbought(rsi14):
    """F5 超买反向（Top5#5，原生 h20 超额 −0.75%, t=−5.13 → 反向后 +0.75%）。
    RSI>70 = 高位追高风险。⚠️ 2026 年原生转正(+1.74%)，制度漂移风险已登记，权重从低。"""
    if rsi14 is None:
        return 5.0
    if rsi14 >= 70: return 1.0
    if rsi14 >= 65: return 2.0
    if rsi14 >= 60: return 3.0
    return 5.0


def _sector_momentum_score(mom20, mom10, mom5):
    """题材板块动量因子 → 0-5: 由成分股近20/10/5日中位动量构造(权重6:3:1)。"""
    def _f(r):
        if r is None:
            return 2.5
        if r >= 15: return 5.0
        if r >= 8:  return 4.2
        if r >= 3:  return 3.4
        if r >= 0:  return 2.6
        if r >= -3: return 2.0
        if r >= -8: return 1.3
        return 0.6
    vals = [(mom20, 0.6), (mom10, 0.3), (mom5, 0.1)]
    avail = [(m, w) for m, w in vals if m is not None]
    if not avail:
        return 2.5
    s = sum(_f(m) * w for m, w in avail) / sum(w for _, w in avail)
    if mom20 is not None and mom10 is not None:
        if mom10 - mom20 >= 5: s += 0.2
        elif mom10 - mom20 <= -5: s -= 0.2
    return max(0.0, min(5.0, round(s, 2)))


def _turnover_rank_score(pct_rank):
    """成交额排名因子 → 0-5: pct_rank∈[0,1], 1=成交额最高(全市场关注度最强)。"""
    if pct_rank is None:
        return 2.5
    return max(0.0, min(5.0, round(pct_rank * 5, 2)))


def score_theme(name, sector, pct, turnover, all_top200_sectors,
                sector_data=None, sector_zt_counts=None,
                mom_map=None, theme_mom=None, sub_mom=None, code=None,
                turnover_pct_map=None, sw_l2=None):
    """
    题材热度评分 (0-5): 以内禀质量(THEME_DB/SUB_THEME)为先验，以成分股近
    5/10/20日真实动量(中位数+上涨占比)为主导信号。

    修复说明(v2.7):
      旧版直接将 THEME_DB/SUB_THEME 硬编码分(如光模块/存储芯片=5)作为基础分，
      而一切动态修正(板块涨跌幅/资金流/生命周期)都依赖新浪板块接口 sector_data，
      一旦该接口映射失败或返回为空，整段动态逻辑被跳过，导致"近期持续下跌"的
      题材仍得满分。新版改为用已抓取的日K线直接计算题材成分股真实动量，动量
      作为主导信号、内禀质量仅作先验，下跌题材显著降分，且不再依赖外部板块接口。
    """
    theme = THEME_DB.get(sector, THEME_DB["综合"])
    intrinsic = theme["score"]  # 内禀质量: 题材本身吸引力先验(1-5)
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
        intrinsic = sub_score
        reasons = [f"细分题材:{sub_label}(内禀{intrinsic*2})"]  # v3.3 问题2: 量纲→0-10(内禀先验×2)
    else:
        # v2.46: 无细分条目时优先展示真实申万二级行业名(如大金重工→风电设备),
        #   不再一律显示主题大类 label(旧行为导致"综合/其他"泛滥)
        sub_label = sw_l2 or theme["label"]
        reasons = [f"板块:{sub_label}(内禀{intrinsic*2})"]  # v3.3 问题2: 量纲→0-10(内禀先验×2)

    # ============================================================
    # 因子1: 题材板块动量 (0-5) —— 成分股近20/10/5日中位动量构造
    # 优先细分题材聚合, 其次板块聚合; 均来自TOP200内成分股, 不依赖外部接口
    # ============================================================
    agg = None
    if sub_label and sub_mom and sub_label in sub_mom:
        agg = sub_mom[sub_label]
    # v2.20: 仅当细分题材完全无聚合数据(agg is None)时才 fallback 到粗板块;
    # 细分题材哪怕仅1-2只成分股, 也用自身动量作为该细分板块动量, 恢复细分区分度。
    # (旧逻辑: 细分<2只即 fallback 粗板块, 导致同粗板块下消费电子/光模块等
    #  细分题材共用同一板块动量值, 丧失区分度 — 老大核查发现两者均为3.84)
    if agg is None and theme_mom and sector in theme_mom:
        agg = theme_mom[sector]
    mom20 = agg.get(20, {}).get("median") if agg else None
    mom10 = agg.get(10, {}).get("median") if agg else None
    mom5 = agg.get(5, {}).get("median") if agg else None
    up_ratio20 = agg.get(20, {}).get("up_ratio") if agg else None
    n_cons = agg.get(20, {}).get("n", 0) if agg else 0
    M = _sector_momentum_score(mom20, mom10, mom5)
    if mom20 is not None:
        reasons.append(f"板块动量因子 {M*2:.2f}/10 (近20日中位{mom20:+.1f}%, 上涨占比{up_ratio20:.0%}, {n_cons}只样本)")  # v3.3 问题2: 量纲→0-10
    else:
        reasons.append("板块动量样本不足, 因子取中性5.00")

    # ============================================================
    # 因子2: 成交额排名 (0-5) —— 个股成交额在TOP200全市场的百分位
    # ============================================================
    _tp = turnover_pct_map.get(code, 0.5) if turnover_pct_map else 0.5
    T = _turnover_rank_score(_tp)
    _rank_pct = int((1 - _tp) * 100)
    reasons.append(f"成交额排名因子 {T*2:.2f}/10 (成交额位于全市场前{_rank_pct}%)")  # v3.3 问题2: 量纲→0-10

    # ============================================================
    # 因子3: 情绪确认 (0-5) —— 个股背离 + 今日单日 + 新浪板块 + 涨停验证
    # ============================================================
    _cdelta = 0.0
    sm = mom_map.get(code) if (mom_map and code) else None
    if sm and sm.get(20) is not None:
        s20 = sm[20]
        if s20 - (mom20 or 0) >= 8:
            _cdelta += 0.25
            reasons.append(f"个股近20日{s20:+.1f}%强于题材,超额明显")
        elif s20 - (mom20 or 0) <= -8:
            _cdelta -= 0.25
            reasons.append(f"个股近20日{s20:+.1f}%弱于题材")
    if pct >= 9.9:
        _cdelta += 0.5; reasons.append("涨停确认强度")
    elif pct >= 5:
        _cdelta += 0.25; reasons.append(f"大涨{pct:+.1f}%验证")
    elif pct <= -7:
        _cdelta -= 0.5; reasons.append(f"大跌{pct:.1f}%,短期降温")
    elif pct <= -3:
        _cdelta -= 0.25; reasons.append(f"下跌{pct:.1f}%")
    sector_pct = None
    if sector_data:
        sina_name = SECTOR_SINA_MAP.get(sector, "")
        sd = sector_data.get(sina_name) if sina_name else None
        if sd:
            sector_pct = sd["pct"]
            if sector_pct <= -2:
                _cdelta -= 0.25; reasons.append(f"板块当日下跌{sector_pct:.1f}%(新浪)")
            elif sector_pct >= 3:
                _cdelta += 0.25; reasons.append(f"板块当日大涨{sector_pct:+.1f}%(新浪)")
    zt_count = up_count = sector_total = 0
    if sector_zt_counts:
        zt_count = sector_zt_counts.get(sector, {}).get("zt", 0)
        up_count = sector_zt_counts.get(sector, {}).get("up", 0)
        sector_total = sector_zt_counts.get(sector, {}).get("total", 0)
    if sector_total > 0:
        upr = up_count / sector_total
        if zt_count >= 3:
            _cdelta += 0.3; reasons.append(f"板块{zt_count}只涨停,做多情绪强")
        elif zt_count >= 1:
            _cdelta += 0.15; reasons.append(f"板块{zt_count}只涨停")
        if upr >= 0.7:
            reasons.append(f"上涨占比{upr:.0%},普涨")
        elif upr <= 0.3:
            reasons.append(f"上涨占比仅{upr:.0%},多数下跌")
    C = max(0.0, min(5.0, 2.5 + _cdelta))

    # ===== 生命周期(基于真实动量) =====
    lifecycle = "震荡期"
    if mom20 is not None:
        if mom20 >= 8 and pct >= 0:
            lifecycle = "高潮期"
            reasons.append("⚠️题材高潮期,注意追高")
        elif mom20 >= 3:
            lifecycle = "加速期"
            reasons.append("题材加速期,趋势延续")
        elif mom20 > -3 and pct >= 0:
            lifecycle = "启动期"
            reasons.append("题材启动期,可关注")
        elif mom20 <= -3:
            lifecycle = "退潮期"
            reasons.append("题材退潮期,短期回避")
    else:
        reasons.append(f"题材处于{lifecycle}(动量样本不足)")

    # 板块集中度：同板块在TOP200中的数量
    sector_count = all_top200_sectors.count(sector)
    if sector_count >= 15:
        reasons.append(f"板块高度集中({sector_count}只上榜)")
    elif sector_count >= 10:
        reasons.append(f"板块集中({sector_count}只上榜)")
    elif sector_count >= 5:
        reasons.append(f"板块有一定关注度({sector_count}只上榜)")

    # ===== 综合(透明加权): 内禀×0.30 + 板块动量×0.35 + 成交额排名×0.15 + 情绪确认×0.20 =====
    # v3.0-10dim: 合成分仅作展示；十维总分由 theme_intrinsic(内禀)与
    #   attention(成交额排名)两个独立维度承载，板块动量子项归并 score_sector。
    W_I, W_M, W_T, W_C = 0.30, 0.35, 0.15, 0.20
    score = _clamp(W_I * intrinsic + W_M * M + W_T * T + W_C * C, 0, 5)
    # v2.46: 展示层清洗申万二级名的"Ⅱ"后缀(风电设备Ⅱ→风电设备), 聚合键不受影响
    sub_label = re.sub(r"[ⅠⅡ]+$", "", sub_label) if sub_label else sub_label
    parts = {"intrinsic": round(intrinsic, 2), "board_mom": round(M, 2),
             "turnover": round(T, 2), "sentiment": round(C, 2)}
    return round(score, 2), reasons, sub_label, lifecycle, parts


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

def score_news(news_text, pct, name="", code="", growth=None, ann_text="", report_text="", report_count=0):
    """
    消息面评分 (v2.9):
    仅依据真实新闻/已验证催化剂/交易所公告评分，业绩数据不作为消息面依据
    新增 akshare 全市场公告作为通用覆盖源(弥补 iFinD 仅抓重点标的/限流缺口)
    已验证催化剂(KNOWN_CATALYSTS 非负面)不被 iFinD/公告利空词覆盖;
    有真实内容(level>=1)的标的股价反应最多压到1分, 避免与"无消息"混淆
    返回: (score, reasons, news_items)
    news_items: [{source, time, sentiment, content, url}]
    """
    reasons = []
    news_items = []  # 结构化消息列表
    ifd_bull = ann_bull = False  # v2.12 双源共振标记

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
            content_score = min(5, content_score + 0.5)
            ifd_bull = True
            reasons.append("iFinD确认:多重利好")
            content_label = "一般利好"
            has_content = True
            news_items.append({
                "source": "财经媒体(iFinD)",
                "time": "",
                "sentiment": "利好",
                "content": f"iFinD新闻检测到{bull_total}个利好关键词",
                "url": "",
            })
        elif bull_total >= 2 and content_score < 4:
            content_score = max(content_score, 3.5)
            ifd_bull = True
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
        elif bear_total >= 6 and content_score > 0 and not (code in KNOWN_CATALYSTS and "negative" not in KNOWN_CATALYSTS[code]):
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
        elif bear_total >= 2 and content_score > 0 and not (code in KNOWN_CATALYSTS and "negative" not in KNOWN_CATALYSTS[code]):
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

    # 1b2. 公告关键词分析（akshare 全市场公告，官方披露，覆盖全部标的）
    if ann_text and len(ann_text) > 2:
        al = ann_text.lower()
        a_bull = sum(1 for k in ["回购", "增持", "中标", "业绩预增", "预增", "高送转",
                                 "分红", "资产注入", "重组", "战略合作", "项目中标",
                                 "签订协议", "定增获批", "股权激励", "扭亏", "扩产",
                                 "首次公开发行", "IPO", "科创板上市", "获批", "签约",
                                 "大单", "供货", "定点", "订单", "增资", "扭亏为盈",
                                 "业绩扭亏", "并购", "中标通知书", "控股权"] if k in al)
        a_bear = sum(1 for k in ["减持", "立案", "处罚", "诉讼", "业绩预减", "预减",
                                 "亏损", "退市风险", "问询函", "监管函", "商誉减值",
                                 "计提减值", "终止重组", "风险警示"] if k in al)
        ann_verified = code in KNOWN_CATALYSTS and "negative" not in KNOWN_CATALYSTS[code]
        if a_bull >= 1 and content_score < 5:
            content_score = min(5, content_score + 0.5)
            ann_bull = True
            reasons.append("公告确认:利好事件")
            content_label = "利好"
            has_content = True
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "利好",
                               "content": f"公告检测到利好关键词×{a_bull}", "url": ""})
        elif a_bear >= 1 and (not ann_verified) and content_score > 0:
            content_score = max(1, content_score - 1)
            reasons.append("公告确认:偏利空事件")
            content_label = "一般利空"
            has_content = True
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "利空",
                               "content": f"公告检测到利空关键词×{a_bear}", "url": ""})
        elif a_bear >= 1 and content_score == 0:
            content_score = 1
            reasons.append("公告确认:偏利空事件")
            content_label = "一般利空"
            has_content = True
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "利空",
                               "content": f"公告检测到利空关键词×{a_bear}", "url": ""})

    # 1b2c. 定期报告(财报)披露识别 — v2.21 修复
    # 此前公告关键词仅含"业绩预增/预减"等预告词, 漏识别正式半年报/季报/年报披露,
    # 导致"有财报"的标的(如胜宏科技300476于2026-08-27披露半年报净利+33%)被判"近7日无消息"。
    # 现补充: 检测到定期报告披露即视为有效消息(消除"无消息"误判);
    # 若报告含增长类措辞判为利好, 含下滑/亏损判为利空, 否则中性。
    REPORT_KW = ["半年度报告", "半年报", "季度报告", "一季报", "三季报", "年报", "年度报告",
                 "财报", "定期报告", "业绩报告", "中报"]
    if ann_text and any(k in ann_text for k in REPORT_KW):
        has_content = True
        _grow = any(k in ann_text for k in ["增长", "提升", "扭亏", "增盈", "同比增", "回暖", "创新高"])
        _decl = any(k in ann_text for k in ["下降", "下滑", "减少", "亏损", "预减", "同比降", "承压"])
        if _grow and not _decl:
            if content_score < 5:
                content_score = min(5, content_score + 0.6)
            if content_label in ("无消息", "中性"):
                content_label = "利好"
            reasons.append("财报披露:业绩增长(定期报告)")
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "利好",
                               "content": "定期报告披露且业绩增长", "url": ""})
        elif _decl and not _grow:
            if content_score > 0:
                content_score = max(1, content_score - 0.5)
            reasons.append("财报披露:业绩下滑(定期报告)")
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "利空",
                               "content": "定期报告披露且业绩下滑", "url": ""})
        else:
            reasons.append("财报披露(定期报告,中性)")
            news_items.append({"source": "交易所公告(akshare)", "time": "", "sentiment": "中性",
                               "content": "定期报告披露", "url": ""})

    # 1b2b. 研报覆盖(akshare 个股研报, 近7日) — v2.17 新增信号源
    # 弥补 iFinD新闻/交易所公告未覆盖券商研报的缺口(此前新易盛有密集研报却被判"近7日无消息")
    if report_count and report_count > 0:
        rep_bonus = min(1.0, 0.5 + 0.15 * (report_count - 1))
        if content_score < 5:
            content_score = min(5, content_score + rep_bonus)
        if content_label in ("无消息", "中性"):
            content_label = "利好"
        has_content = True
        reasons.append(f"近7日研报覆盖×{report_count}篇(机构关注度高)")
        news_items.append({"source": "券商研报(东财)", "time": "", "sentiment": "机构关注",
                           "content": f"近7日研报{report_count}篇", "url": ""})

    # 1b3. 多源共振(v2.12): iFinD新闻+交易所公告双源确认利好 → 额外加分(5分的主要通道)
    # 单源(仅关键词/仅公告)自然封顶约4.5; 双源共振或已验证催化剂(level=5)才可到5
    if ifd_bull and ann_bull and content_score < 5:
        content_score = min(5, content_score + 0.5)
        reasons.append("iFinD+公告双源共振,利好确定性高")
        news_items.append({"source": "多源共振", "time": "", "sentiment": "利好",
                           "content": "财经媒体与交易所公告同时确认利好", "url": ""})

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

    # ========== Step 1d: 近7日时效判定 (v2.10, v2.17 扩展研报) ==========
    # 7日内新闻(iFinD)/公告(akshare)/研报(东财)任一存在即视为"近期信号"。
    # 已验证催化剂(KNOWN_CATALYSTS)若近7日无新增消息/公告/研报, 视为时效性减弱, 评分下调
    recent_signal = (bool(news_text and len(news_text) > 20)
                     or bool(ann_text and len(ann_text) > 2)
                     or bool(report_count and report_count > 0))
    recency_penalty = False
    if not recent_signal:
        if code in KNOWN_CATALYSTS and content_score > 0:
            content_score = max(0, round(content_score * 0.4, 1))
            reasons.append("近7日无新增消息/公告/研报,已验证催化剂时效性减弱,消息面评分下调")
            recency_penalty = True
        news_recency = "近7日无消息"
    else:
        parts = []
        if news_text and len(news_text) > 20:
            parts.append("新闻")
        if ann_text and len(ann_text) > 2:
            parts.append("公告")
        if report_count and report_count > 0:
            parts.append(f"研报×{report_count}")
        news_recency = "近7日有" + "/".join(parts) if parts else "近7日有消息/公告"
    reasons.append(f"消息时效:{news_recency}")

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
    # 有真实内容(已验证催化剂/新闻/公告)且content>=1的标的, 股价反应最多压到1分,
    # 避免与"完全无消息"混淆(修复利通电子类被跌停清零的bug)
    if has_content and content_score >= 1 and final_score < 1:
        final_score = 1

    return final_score, reasons, news_items, news_recency


# ============================================================
# 大盘赚钱效应 (Market Money-Making Effect) — v2.7 新增
# 目的: 量化全市场情绪/广度, 评估当前是否适合入场
# 数据: akshare 全A快照(涨跌家数/涨停跌停/平均涨幅/成交额) + 三大指数涨跌
# 说明: 纯量化模型, 非投资建议; 失败时降级不阻塞个股评分
# ============================================================
def fetch_market_breadth():
    """抓取全市场广度数据(多源回退)。返回 dict, 失败时返回 None。

    数据源优先级:
      1) 东方财富全A快照 stock_zh_a_spot_em — 信息最全(含平均/中位涨幅与成交额),
         生产环境(GitHub Actions)可用;
      2) 乐股全市场活跃度 stock_market_activity_legu — 提供涨跌家数/涨停跌停,
         本地沙箱回退源;
    指数统一取新浪实时 stock_zh_index_spot_sina(本地/生产均可达)。
    """
    try:
        import akshare as ak
    except Exception:
        return None

    b = {
        "total": None, "up": None, "down": None, "flat": None,
        "suspended": None, "zt": None, "dt": None, "up5": None,
        "up3": None, "down5": None, "avg_pct": None, "median_pct": None,
        "amount_yi": None, "index": {}, "index_level": {}, "source": None, "active_rate": None,
    }

    # ---- 源1: 东方财富全A快照(信息最全) ----
    if b["up"] is None:
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and len(df) and "涨跌幅" in df.columns:
                pcts = df["涨跌幅"].astype(float)
                b.update({
                    "total": len(pcts),
                    "up": int((pcts > 0).sum()),
                    "down": int((pcts < 0).sum()),
                    "flat": int((pcts == 0).sum()),
                    "zt": int((pcts >= 9.9).sum()),
                    "dt": int((pcts <= -9.9).sum()),
                    "up5": int((pcts >= 5).sum()),
                    "up3": int((pcts >= 3).sum()),
                    "down5": int((pcts <= -5).sum()),
                    "avg_pct": round(float(pcts.mean()), 2),
                    "median_pct": round(float(pcts.median()), 2),
                    "amount_yi": round(float(df["成交额"].astype(float).sum()) / 1e8, 0)
                                   if "成交额" in df.columns else None,
                    "source": "eastmoney",
                })
        except Exception as e:
            print(f"   ⚠️ 东方财富全A快照获取失败(回退乐股): {e}")

    # ---- 源2(回退): 乐股全市场活跃度 ----
    if b["up"] is None:
        try:
            df = ak.stock_market_activity_legu()
            kv = dict(zip(df["item"], df["value"]))
            def _f(k):
                v = kv.get(k)
                try:
                    return float(v)
                except Exception:
                    return None
            b.update({
                "up": int(_f("上涨") or 0),
                "down": int(_f("下跌") or 0),
                "flat": int(_f("平盘") or 0),
                "zt": int(_f("真实涨停") or _f("涨停") or 0),
                "dt": int(_f("真实跌停") or _f("跌停") or 0),
                "suspended": int(_f("停牌") or 0),
                "active_rate": _f("活跃度"),
                "source": "legu",
            })
        except Exception as e:
            print(f"   ⚠️ 乐股活跃度获取失败: {e}")

    # ---- 指数(新浪, 本地/生产均可达) — 同时抓取最新点位(用于"沪指逼近X关口") ----
    try:
        idf = ak.stock_zh_index_spot_sina()
        name_map = {"上证指数": "上证", "深证成指": "深证", "创业板指": "创业板", "科创50": "科创"}
        for _, row in idf.iterrows():
            nm = row.get("名称")
            if nm in name_map:
                try:
                    b["index"][name_map[nm]] = round(float(row["涨跌幅"]), 2)
                except Exception:
                    pass
                lvl = row.get("最新价")
                if lvl is not None:
                    try:
                        b["index_level"][name_map[nm]] = round(float(lvl), 0)
                    except Exception:
                        pass
    except Exception as e:
        print(f"   ⚠️ 指数获取失败: {e}")

    # ---- 成交额多源回退(v2.12): 东财全A(源1已试) → 新浪全A → 指数成交额近似 ----
    # 此前回退复用与源1相同的东财接口, 东财不可达时回退形同虚设, 导致成交额恒为None
    if b["amount_yi"] is None:
        try:
            df = ak.stock_zh_a_spot()  # 新浪全A快照, 含"成交额"列, 与指数源同通道(云端可达)
            if df is not None and len(df) and "成交额" in df.columns:
                amt = float(df["成交额"].astype(float).sum()) / 1e8
                if amt and amt > 0:
                    b["amount_yi"] = round(amt, 0)
                    if b["source"] is None:
                        b["source"] = "sina"
        except Exception as e:
            print(f"   ⚠️ 新浪全A成交额获取失败: {e}")
    if b["amount_yi"] is None:
        try:
            # 指数成交额近似: 上证指数(≈沪市全市场)+深证成指+创业板指 成交额合计
            idf2 = ak.stock_zh_index_spot_sina()
            amt = 0.0
            for _, row in idf2.iterrows():
                if row.get("名称") in ("上证指数", "深证成指", "创业板指"):
                    v = row.get("成交额")
                    if v is not None:
                        try:
                            amt += float(v)
                        except Exception:
                            pass
            if amt > 0:
                b["amount_yi"] = round(amt / 1e8, 0)
        except Exception as e:
            print(f"   ⚠️ 指数成交额近似获取失败: {e}")

    # 完整性校验
    if b["up"] is None or b["down"] is None:
        return None
    if b["total"] is None:
        b["total"] = (b["up"] or 0) + (b["down"] or 0) + (b["flat"] or 0)
    return b


def compute_money_effect(b):
    """将广度数据映射为 0-100 赚钱效应评分 + 情绪周期 + 入场建议。"""
    if not b:
        return {"available": False, "score": None,
                "phase": "数据缺失", "advice": "无法评估",
                "position": "观望", "components": {}, "raw": None}
    total = b["up"] + b["down"]
    adv_ratio = b["up"] / total if total else 0.5
    net_zt = b["zt"] - b["dt"]
    idx_vals = list(b["index"].values())
    idx_avg = sum(idx_vals) / len(idx_vals) if idx_vals else 0.0

    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    # 分项(单位: 分) — 透明加权, 总分上限100
    # 广度: 上涨占比从30%(冰点)到70%(普涨)线性映射到0~40分
    c_breadth = _clamp((adv_ratio - 0.30) / 0.40 * 40, 0, 40)
    c_zt = _clamp(net_zt * 0.35, -12, 18)                 # 涨停净额: 情绪温度
    c_idx = _clamp(idx_avg * 6.0, -15, 25)                # 指数强度: 三大指数均值
    c_med = _clamp((b["median_pct"] or 0) * 6.0, -10, 15)  # 中位涨幅: 剔除权重股扭曲(缺失则0)

    score = _clamp(round(c_breadth + c_zt + c_idx + c_med), 0, 100)

    # 情绪周期
    if score >= 75:
        phase = "亢奋期(普涨)"
    elif score >= 60:
        phase = "回暖期(赚钱效应好)"
    elif score >= 45:
        phase = "中性偏暖(结构性行情)"
    elif score >= 30:
        phase = "偏弱(赚钱效应一般)"
    else:
        phase = "冰点/退潮(亏钱效应)"

    # 入场建议
    if score >= 70:
        advice, position = "赚钱效应强,可积极入场,把握主线", "建议仓位 60%-80%"
    elif score >= 55:
        advice, position = "情绪偏暖,可参与结构性机会", "建议仓位 40%-60%"
    elif score >= 40:
        advice, position = "情绪中性,控制仓位,只做确定性标的", "建议仓位 20%-40%"
    elif score >= 25:
        advice, position = "赚钱效应弱,减仓观望", "建议仓位 <20%"
    else:
        advice, position = "冰点/亏钱效应,空仓观望为宜", "建议空仓"

    return {
        "available": True, "score": score, "phase": phase,
        "advice": advice, "position": position,
        "components": {
            "广度(涨跌家数占比)": round(c_breadth, 1),
            "涨停净额(涨停-跌停)": round(c_zt, 1),
            "指数强度(三大指数均值)": round(c_idx, 1),
            "中位涨幅(剔除权重扭曲)": round(c_med, 1),
        },
        "active_rate": b.get("active_rate"),
        "raw": b,
    }


# ============================================================
# 盘面复盘点评 (Market Review Commentary) — v2.11 新增
# 目的: 将机械的"今日核心结论"+"大盘赚钱效应"两个模块, 合并为一段
#       拟人化、带观点的复盘点评, 风格对齐人工复盘。
# 数据源(全部真实, 无幻觉):
#   1) 全市场广度 market_breadth.raw: 指数涨跌幅/点位、成交额、涨跌家数、涨停跌停
#   2) 个股动量/评级 results: 按题材(sub_theme/板块)聚合平均总分→资金进攻方向
#   3) 聚合真实新闻 results[].news_items: 消息面催化密集度(仅统计真实条数, 不虚构标题)
# 说明: 所有结论均由上述真实数据推导, 不引用任何未经核实的个股/事件。
# ============================================================
def build_market_review(market_breadth, stats, results):
    """返回 dict: {available, text, indices, index_level, amount_yi,
                   up, down, zt, dt, money_score, money_phase, hot, weak, hw_ratio}
    text 为复盘点评自然语言段落(范文结构: 大盘→资金进攻→高低切换→市场偏好→关口展望)。
    """
    if not results:
        return {"available": False, "text": "个股数据不足, 复盘点评暂不可用。"}

    mb = (market_breadth or {}).get("raw", {}) or {}
    idx = mb.get("index", {}) or {}
    idx_level = mb.get("index_level", {}) or {}
    amount_yi = mb.get("amount_yi")
    up = mb.get("up"); down = mb.get("down"); zt = mb.get("zt"); dt = mb.get("dt")
    me_score = (market_breadth or {}).get("score")
    me_phase = (market_breadth or {}).get("phase", "")

    # ---------- 1) 大盘 ----------
    idx_order = ["上证", "深证", "创业板", "科创"]
    idx_parts = [f"{k}{idx[k]:+.2f}%" for k in idx_order if k in idx]
    lead = max(idx.items(), key=lambda kv: kv[1]) if idx else (None, 0)
    if amount_yi and amount_yi >= 10000:
        amt_str = f"{amount_yi/10000:.2f}万亿"
    elif amount_yi:
        amt_str = f"{amount_yi:.0f}亿"
    else:
        amt_str = None
    if me_score is not None:
        if me_score >= 60: effect = "赚钱效应扩散"
        elif me_score >= 45: effect = "情绪偏暖"
        elif me_score >= 30: effect = "结构性分化"
        else: effect = "亏钱效应显现"
    else:
        effect = None
    s1 = ""
    if idx_parts:
        s1 += "大盘" + "、".join(idx_parts)
        if lead[0]: s1 += f"（{lead[0]}领涨）"
    if amt_str:
        s1 += f"，全市场成交{amt_str}"
    if effect:
        s1 += f"，{effect}"
    if s1: s1 += "。"

    # ---------- 板块/题材聚合(资金进攻 & 高低切换) ----------
    from collections import defaultdict
    grp = defaultdict(lambda: {"n": 0, "score": 0.0, "pct": 0.0, "sa": 0, "news": 0})
    for r in results:
        lab = (r.get("sub_theme") or r.get("sector") or "其他").strip()
        if not lab:
            lab = "其他"
        g = grp[lab]
        g["n"] += 1
        g["score"] += r.get("total", 0) or 0
        g["pct"] += r.get("pct_chg", 0) or 0
        if r.get("rating") in ("S", "A"):
            g["sa"] += 1
        g["news"] += len(r.get("news_items") or [])
    # 仅取样本>=2的方向, 避免单只个股噪声; 按平均总分排序
    ranked = sorted(
        [(k, v) for k, v in grp.items() if v["n"] >= 2],
        key=lambda kv: kv[1]["score"] / kv[1]["n"], reverse=True)
    avg_score = lambda v: v["score"] / v["n"]
    avg_pct = lambda v: v["pct"] / v["n"]
    hot = [k for k, v in ranked[:3]]
    # 弱势方向: 平均涨幅为负(冲高回落/兑现压力); 若无则取评分最低的3个
    weak = [k for k, v in ranked if avg_pct(v) < 0][:3]
    if not weak and ranked:
        weak = [k for k, _ in ranked[-3:]]

    # ---------- 2) 资金进攻方向 ----------
    if hot:
        s2 = f"资金集中进攻{'、'.join(hot)}等方向，带动产业链走强。"
    else:
        s2 = "板块轮动较快，资金方向尚不明朗。"

    # ---------- 3) 高低切换 ----------
    if weak:
        s3 = f"{'、'.join(weak)}等题材冲高回落，资金出现明显高低切换。"
    else:
        s3 = "板块内部分化，资金高低切换明显。"

    # ---------- 4) 市场偏好(硬件制造 vs 软件故事, 仅统计S/A级真实样本) ----------
    HW_KW = ["芯片", "半导体", "PCB", "光模块", "光芯片", "存储", "模组", "设备",
             "机器人", "硬件", "算力", "电子", "面板", "材料", "制造", "封测",
             "电源", "连接器", "元件", "有色", "资源", "新能源", "军工", "汽车"]
    SW_KW = ["软件", "应用", "政企", "互联网", "传媒", "游戏", "平台", "数据",
             "信创", "AI应用"]
    hw = sw = 0
    for r in results:
        if r.get("rating") not in ("S", "A"):
            continue
        blob = (r.get("sub_theme") or "") + " " + (r.get("sector") or "")
        if any(k in blob for k in HW_KW):
            hw += 1
        elif any(k in blob for k in SW_KW):
            sw += 1
    hw_ratio = (hw / (hw + sw)) if (hw + sw) > 0 else None
    if hw_ratio is not None and hw_ratio >= 0.6:
        s4 = "当前市场偏好订单可落地的硬件制造（一阶业绩确定性），远期故事类题材遭遇阶段性兑现压力。"
    elif hw_ratio is not None and hw_ratio <= 0.4:
        s4 = "当前市场更偏好具备兑现能力的软件/应用方向，纯概念题材承压。"
    else:
        s4 = "当前市场风格趋于均衡，硬件制造与景气兑现并重的方向更受资金青睐。"

    # ---------- 5) 关口展望 + 财报季动态(去除写死"等待中报", 按月份动态) ----------
    m = datetime.now().month
    if m in (7, 8):
        season = "中报密集披露期，密切跟踪业绩兑现与赛道景气度的匹配度。"
    elif m == 9:
        season = "中报已披露完毕，重点验证业绩兑现成色与赛道景气度的匹配度，关注绩优方向的持续性。"
    elif m in (10, 11):
        season = "三季报预告陆续披露，聚焦业绩边际变化与全年指引。"
    elif m in (1, 2, 3, 4):
        season = "年报与一季报密集披露，关注业绩确定性及全年预期修正。"
    else:
        season = "处于财报真空期，题材与资金博弈主导，跟踪产业政策与景气前瞻信号。"
    sh_level = idx_level.get("上证")
    if sh_level:
        gate = (int(sh_level) // 500 + 1) * 500
        if 0 < gate - sh_level <= 100:
            s5 = f"沪指报{sh_level:.0f}点，逼近{gate}点关口，后续重点观察成交量能否持续维持高位；{season}"
        else:
            s5 = f"沪指报{sh_level:.0f}点，{season}"
    else:
        s5 = season

    # 消息面催化密集度(仅统计近7日有真实消息/公告覆盖的标的, 对齐v2.10时效口径, 不虚构标题)
    total_news = sum(1 for r in results if r.get("news_recency") == "近7日有消息/公告")
    hot_news = sum(1 for r in results
                   if r.get("news_recency") == "近7日有消息/公告"
                   and (r.get("sub_theme") or r.get("sector") or "") in set(hot))
    if total_news:
        s6 = f"消息面：近7日 {total_news} 只上榜个股有公告/中报催化，其中 {hot_news} 只集中于强势方向，事件驱动仍是资金主攻线索。"
    else:
        s6 = ""

    text = "".join([s for s in (s1, s2, s3, s4, s5, s6) if s])

    return {
        "available": True,
        "text": text,
        "indices": {k: idx[k] for k in idx_order if k in idx},
        "index_level": {k: idx_level.get(k) for k in idx_order if k in idx_level},
        "amount_yi": amount_yi,
        "up": up, "down": down, "zt": zt, "dt": dt,
        "money_score": me_score, "money_phase": me_phase,
        "hot": hot, "weak": weak, "hw_ratio": hw_ratio,
        "total_news": total_news, "hot_news": hot_news,
    }


# ============================================================
# Main
# ============================================================
def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "top500_all_a.json"
    out = sys.argv[2] if len(sys.argv) > 2 else f"top200_scored_{datetime.now().strftime('%Y-%m-%d')}.json"

    with open(inp) as f:
        raw = json.load(f)
    # v3.0: 优先 main_pool/top500(扩池口径), 兼容旧 top200/top100/top50
    if "main_pool" in raw:
        stocks = raw["main_pool"]
    elif "top500" in raw:
        stocks = raw["top500"]
    elif "top200" in raw:
        stocks = raw["top200"]
    elif "top100" in raw:
        stocks = raw["top100"]
    else:
        stocks = raw["top50"]
    
    # 剔除金融股（项目初始设计：金融股无「题材热度」属性，与十维模型口径不符）
    #   v3.2 P1-3: 记入 _fin_removed，随 stats 输出供报告层透明披露「池 N → 实际评分 M」
    FINANCE_SECTORS = {"金融"}
    before = len(stocks)
    stocks = [s for s in stocks if get_sec(s["name"]) not in FINANCE_SECTORS]
    _fin_removed = before - len(stocks)
    if _fin_removed:
        print(f"   🚫 剔除金融股 {_fin_removed}只（无题材热度属性，不计入评分）")

    # v3.2 P0-1/P0-3: 资格/流动性标记映射（fetch 池 → 逐股透传，供报告层与组合层）
    _elig_map = {}
    for _s in (raw.get("main_pool") or raw.get("top500") or []):
        _c = _s.get("code")
        if _c:
            _elig_map[_c] = {
                "turnover_ratio": _s.get("turnover_ratio"),
                "circ_mv": _s.get("circ_mv"),
                "liquid_ok": _s.get("liquid_ok", True),
                "suspended": _s.get("suspended", False),
                "is_new": _s.get("is_new", False),
            }
    _em = raw.get("eligible_marks") or {}
    if _em:
        print(f"   🏷️ 资格标记: 次新 {_em.get('new_stock', 0)} / 停牌 {_em.get('suspended', 0)}"
              f" / 流动性不达标 {_em.get('illiquid', 0)} / 低换手 {_em.get('low_turnover', 0)}")
    
    print(f"📊 Stock-Scorer v2.7 | {len(stocks)}只标的")
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
    all_top200_sectors = [resolve_sector(s["name"], s["code"])[0] for s in stocks]
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
        try:
            fund_map[s["code"]] = run_ifind_fin(s["name"], s["code"])
        except Exception as e:
            fund_map[s["code"]] = ""
            print(f"⚠️err={e}", end=" ")
        print("✓" if fund_map[s["code"]] else "✗")
    
    # --- Step 4.5: akshare 财务增速 + 机构预测 + 腾讯行情PE ---
    #   v3.0 B2: 摘要接口改走缓存+退避重试(fetch_ths_abstract_cached);
    #   扩容解析 ROE/资产负债率/每股经营现金流(OCFG YoY) → ths_extra_map
    print(f"\n📊 Step 4.5: akshare财务数据 ({len(stocks)}只, 摘要缓存+退避重试)")
    import akshare as ak
    growth_map = {}
    fwd_pe_map = {}
    cagr3_map = {}
    hist_loss_free_map = {}  # v2.14: 近4年扣非净利全为正(穿越周期盈利稳定)
    ths_extra_map = {}       # v3.0 B2: {code: {roe, debt_ratio, ocf_ps, ocfg}}
    for i, s in enumerate(stocks):
        code = s["code"]
        sym = code[2:]
        name = s["name"]
        try:
            df = fetch_ths_abstract_cached(sym)
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
                        # v2.14: 穿越周期校验——近4年扣非净利是否全为正(含2023存储下行期)
                        if len(vals) >= 4:
                            hist_loss_free_map[code] = all(
                                v is not None and v > 0 for v in vals[-4:])
                    except:
                        pass
                # v3.0 B2: ROE/资产负债率/每股经营现金流(OCFG YoY) 扩容解析
                #   —— ROE 原仅 iFinD 前20只有, 扩池后全池可得
                _ex = ths_extra_map.setdefault(code, {})
                _roe = _num_or_none(latest.get('净资产收益率'))
                if _roe is not None:
                    _ex['roe'] = _roe
                _dr = _num_or_none(latest.get('资产负债率'))
                if _dr is not None:
                    _ex['debt_ratio'] = _dr
                _ocf_now = _num_or_none(latest.get('每股经营现金流'))
                if _ocf_now is not None:
                    _ex['ocf_ps'] = _ocf_now
                    if len(df) >= 2:
                        _ocf_prev = _num_or_none(df.iloc[-2].get('每股经营现金流'))
                        if _ocf_prev and _ocf_prev > 0:
                            _ex['ocfg'] = round((_ocf_now / _ocf_prev - 1) * 100, 1)
        except:
            pass
        try:
            df2 = None
            for _try in range(2):   # v3.5.1: THS预测偶发限流/瞬断，重试1次（影响 pe_ladder 与 implied_h2 完整度）
                try:
                    df2 = ak.stock_profit_forecast_ths(symbol=sym, indicator='预测年报净利润')
                    if df2 is not None and len(df2) > 0:
                        break
                except Exception:
                    df2 = None
                    time.sleep(1.0)
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
            print(f"   [{i+1}/{len(stocks)}] 增速={len(growth_map)} FwdPE预测={len(fwd_pe_map)} CAGR3={len(cagr3_map)} ROE等={len(ths_extra_map)}")
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

    # --- Step 4.7: v3.5.1 growth口径修复(东财按报告期) + Q3预测加载 + implied_h2 ---    #   原 growth_map 走同花顺「按年度」摘要，末行=最近年报(2025) → 全池结构性滞后半年以上
    #   （老大核查铁证：普冉股份年报口径 −38.7% vs 2026中报实际 +2994.08%；全池 490/490 缓存
    #    最后报告期均为 2025）。现改为东财按报告期「各股最新已披露报告期」累计同比
    #   （官方口径 (cur−prev)/|prev|，见 fetch_em_growth_cached 注释）；
    #    同花顺年报数据降级为兜底（growth_src 留档，不静默混用）。
    print(f"\n📊 Step 4.7: growth口径修复(东财按报告期) + Q3预测 ({len(stocks)}只)")
    import concurrent.futures as _cf
    em_growth_map = {}

    def _em_one(s):
        try:
            return s["code"], fetch_em_growth_cached(s["code"][2:])
        except Exception:
            return s["code"], {'growth': None, 'period': None, 'base': None,
                               'cur': None, 'prev': None, 'flag': 'exc', 'src': 'em'}

    with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
        for _code, _res in _ex.map(_em_one, stocks):
            em_growth_map[_code] = _res
    _em_ok = sum(1 for v in em_growth_map.values() if v.get('growth') is not None)
    _em_per = {}
    for v in em_growth_map.values():
        _em_per[v.get('period')] = _em_per.get(v.get('period'), 0) + 1
    print(f"   ✅ 东财报告期增速: {_em_ok}/{len(stocks)} 只  报告期分布: "
          f"{dict(sorted(((k or '无'), v) for k, v in _em_per.items()))}")

    q3_forecast_map = load_q3_forecast()
    q3_map = {}
    if q3_forecast_map:
        _q3_hit = 0
        for s in stocks:
            code = s["code"]
            rec = q3_forecast_map.get(s["name"])
            if not rec:
                continue
            _q3_hit += 1
            # implied_h2 = 机构2026预测净利润(亿元) / (H1扣非实际×2) − 1（粗略年化基准，
            #   作为"已被定价的乐观度"去重闸门；H1扣非≤0 时数学无意义 → None 走退化规则）
            ih2 = None
            _cur = (em_growth_map.get(code) or {}).get('cur')
            _fwd = fwd_pe_map.get(code) or {}
            _np26 = _fwd.get(2026, _fwd.get('2026'))
            if (isinstance(_cur, (int, float)) and _cur > 0
                    and isinstance(_np26, (int, float)) and _np26 > 0):
                ih2 = round((_np26 * 1e8) / (_cur * 2) * 100 - 100, 1)
            q3_map[code] = dict(rec, src='manual_q3_table', implied_h2=ih2)
        _ih2_ok = sum(1 for v in q3_map.values() if v.get('implied_h2') is not None)
        print(f"   ✅ Q3预测匹配: {_q3_hit}/{len(stocks)} 只；implied_h2可算 {_ih2_ok}/{_q3_hit}")
    else:
        print("   ⚠️ 无Q3预测数据（不加分，不影响其余维度）")


    # --- Step 4.8: 题材动量预计算（基于已抓取的日K线，不依赖外部板块接口） ---
    print(f"\n🔥 Step 4.8: 题材动量预计算 (成分股真实近5/10/20日收益)")
    mom_map = {}
    for s in stocks:
        mom_map[s["code"]] = _stock_momentum(klines.get(s["code"], []))
    sub_members = {}
    sector_members = {}
    for s in stocks:
        sec, _sw_l2, _src = resolve_sector(s["name"], s["code"])
        sector_members.setdefault(sec, []).append(mom_map.get(s["code"]))
        lab = None
        sm = SUB_THEME.get(s["name"])
        if not sm:
            nn = _normalize_name(s["name"])
            for prefix in ("XD", "DR", "XR"):
                if nn.startswith(prefix):
                    nn = nn[len(prefix):]
                    break
            sm = SUB_THEME.get(nn)
            if not sm:
                base_n = nn.rstrip("UW").rstrip("-").rstrip("U").rstrip("-")
                sm = SUB_THEME.get(base_n)
                if not sm:
                    for key in SUB_THEME:
                        if base_n.startswith(key) or key.startswith(base_n):
                            sm = SUB_THEME[key]
                            break
        if sm:
            lab = sm[0]
            sub_members.setdefault(lab, []).append(mom_map.get(s["code"]))
        elif _sw_l2:
            # v2.46: 无细分条目的个股按真实申万二级行业聚合,
            #   使风电设备/化学制药等真实行业也有板块动量样本(此前完全缺席)
            sub_members.setdefault(_sw_l2, []).append(mom_map.get(s["code"]))
    theme_mom = {sec: _agg_momentum(ms) for sec, ms in sector_members.items()}
    sub_mom = {lab: _agg_momentum(ms) for lab, ms in sub_members.items()}
    for lab in ("光模块", "光模块/CPO", "存储芯片", "内存接口", "AI芯片/CPU", "AI芯片/GPU", "半导体设备", "PCB", "覆铜板/PCB"):
        if lab in sub_mom:
            a = sub_mom[lab].get(20, {})
            print(f"   题材[{lab}] 近20日中位{a.get('median', 0):+.1f}% 上涨占比{a.get('up_ratio', 0):.0%} ({a.get('n', 0)}只)")

    # v2.21 诊断: 短期(10日) vs 中期(20日) 题材动量对比 — 验证"动能衰减"是否普遍/异常
    _ns = _nd = 0; _dex = []
    for _lab, _agg in sub_mom.items():
        _m10 = _agg.get(10, {}).get("median"); _m20 = _agg.get(20, {}).get("median")
        if _m10 is not None and _m20 is not None:
            _ns += 1
            if _m10 < _m20:
                _nd += 1
                if len(_dex) < 10:
                    _dex.append(f"{_lab}:10日{_m10:+.1f}% < 20日{_m20:+.1f}%")
    print(f"   ⚠️ 题材动量诊断: {_nd}/{_ns} 个细分题材 近10日中位 < 近20日中位 (动能衰减占比 {_nd/max(1,_ns):.0%})")
    for _e in _dex: print("     ", _e)

    # --- Step 4.8: 全市场公告覆盖 (近7日, akshare) ---
    print(f"\n📢 Step 4.8: 全市场公告覆盖 (近7日)")
    ann_map = fetch_announcements_map(days=7)
    print(f"   公告覆盖 {len(ann_map)} 只标的")

    # --- Step 4.8b: 近7日研报覆盖 (akshare 个股研报, v2.17 新增信号源) ---
    print(f"\n📑 Step 4.8b: 近7日研报覆盖 (akshare)")
    _codes6 = [s["code"][2:] for s in stocks if len(s.get("code", "")) >= 6]
    report_map = fetch_research_reports_map(days=7, codes=_codes6)

    # --- Step 5: Score all ---
    print(f"\n🎯 Step 5: 四维评分(题材30%+基本面30%(含行业前景)+消息20%+技术面20%)")
    # 成交额排名百分位(TOP200全市场口径): 最高成交额=1.0
    _tns = [st.get("turnover", 0) or 0 for st in stocks]
    _order = sorted(range(len(stocks)), key=lambda i: _tns[i], reverse=True)
    turnover_pct_map = {}
    _N = max(1, len(stocks) - 1)
    for _rank, _idx in enumerate(_order):
        turnover_pct_map[stocks[_idx]["code"]] = 1.0 - _rank / _N
    results = []
    for i, s in enumerate(stocks):
        code = s["code"]
        name = s["name"]
        pct = s.get("pct_chg", 0) or 0
        pe = pe_map.get(code[2:], 0) or s.get("pe", 0) or 0
        price = s.get("price", 0) or 0
        turnover = s.get("turnover", 0)
        
        # v2.46 行业解析: 输入 sector 可能是申万二级名(观察池注入) → 归一到主题键;
        #   无输入则按"申万映射优先 → 静态字典 → 综合"解析; 来源记入 sector_src 供审计
        sw_l2 = sw_l2_of(code)
        _in_sec = (s.get("sector") or "").strip()
        if _in_sec and _in_sec != "综合":
            sector, _src = to_theme_key(_in_sec), "input"
        else:
            sector, _l2, _src = resolve_sector(name, code)
            sw_l2 = sw_l2 or _l2
        kl = klines.get(code, [])
        
        # Fundamental (传入估值阶梯数据)
        ftext = fund_map.get(code, "")
        fparsed = parse_fundamentals(ftext)
        pe_input = pe_map.get(code[2:]) or (pe if pe > 0 else None)
        fwd_pe_input = fparsed.get("fwd_pe") or computed_fwd_pe.get(code)
        # v3.5.1: growth 取数优先级（口径必须单一可审计，禁止静默混用）：
        #   ① 东财按报告期(各股最新已披露报告期, 官方累计同比) —— 主源
        #   ② iFinD 文本(扣非净利润增长率, 口径不可验) —— 次选
        #   ③ 同花顺「按年度」摘要(最近年报, 滞后半年+) —— 仅兜底
        #   ④ KNOWN 硬编码 —— 最终兜底
        _em_g = em_growth_map.get(code) or {}
        if _em_g.get('growth') is not None:
            growth_input = _em_g.get('growth')
            growth_src = 'em_report_period'
        elif fparsed.get("growth") is not None:
            growth_input = fparsed.get("growth")
            growth_src = 'ifind_text'
        elif growth_map.get(code) is not None:
            growth_input = growth_map.get(code)
            growth_src = 'ths_annual_fallback'   # 年报口径(滞后)，仅兜底并留档
        else:
            growth_input = None
            growth_src = 'none'
        if code in KNOWN:
            _, k_growth = KNOWN[code]
            if growth_input is None:
                growth_input = k_growth
                growth_src = 'known_fallback'
        ladder_input = pe_ladder.get(code)
        _ths_ex = ths_extra_map.get(code, {})
        roe_input = fparsed.get("roe") or _ths_ex.get("roe")   # v3.0 B2: ROE 兜底扩容至全池
        gm_input = fparsed.get("gross_margin")
        sub_theme_label = get_sub_theme_label(name)
        # v3.5: cagr3(三年扣非CAGR) 作为存续期锚传入, 用于低基数失真校验与周期股 durable 收紧
        cagr3_input = cagr3_map.get(code)
        fs, fr, fwd_pe_resolved, growth_resolved, ladder_resolved, sustain_info = score_fund(
            pe_input, fwd_pe_input,
            growth_input, code, sector, pe_ladder_data=ladder_input,
            roe=roe_input, gross_margin=gm_input, sub_theme_label=sub_theme_label,
            hist_loss_free=hist_loss_free_map.get(code, False),
            fwd_np=fwd_pe_map.get(code), sw_l2=sw_l2, cagr3=cagr3_input,
            q3_info=q3_map.get(code))
        
        # Tech (传入CAGR3用于成长加分, 传入周线用于双线多头判断)
        wkl = wklines.get(code)
        ts, td = score_tech(kl, price, pct, cagr3=cagr3_input, wkl=wkl)

        # Theme Heat (题材热度替代原资金面)
        # v3.0: 返回 parts（内禀/板块动量/成交额排名/情绪确认），十维总分拆用
        ths, thr, sub_theme_label, lifecycle, theme_parts = score_theme(
            name, sector, pct, turnover, all_top200_sectors,
            sector_data=sector_data, sector_zt_counts=sector_zt_counts,
            mom_map=mom_map, theme_mom=theme_mom, sub_mom=sub_mom, code=code,
            turnover_pct_map=turnover_pct_map, sw_l2=sw_l2)
        s_theme_int = theme_parts.get("intrinsic") or 2.5
        s_attn = theme_parts.get("turnover") or 2.5

        # News (传入增速用于动态评估; ann_text 为近7日公告覆盖; 研报为近7日券商研报覆盖)
        ntext = news_map.get(code, "")
        ann_text = ann_map.get(code[2:], "")
        rep = report_map.get(code[2:], {}) or {}
        report_count = rep.get("count", 0) if rep else 0
        report_text = rep.get("titles", "") if rep else ""
        ns, nr, news_items, news_recency = score_news(
            ntext, pct, name, code, growth=growth_resolved,
            ann_text=ann_text, report_text=report_text, report_count=report_count)

        # 近一周(约5交易日)涨幅, 由日K线计算
        _mom_tmp = _stock_momentum(kl)
        week_chg = round(_mom_tmp.get(5), 2) if _mom_tmp and _mom_tmp.get(5) is not None else None

        # v3.0-10dim: 回测 Top5 因子分（从 score_tech detail 取 rsi/ma60/amount_ratio，避免重算）
        _rsi14_v = td.get("rsi") if isinstance(td, dict) else None
        _ma60_v = td.get("ma60") if isinstance(td, dict) else None
        _amt_ratio_v = td.get("amount_ratio") if isinstance(td, dict) else None
        _mom60_v = _mom_tmp.get(60) if _mom_tmp else None
        s_oversold60 = score_oversold60(_mom60_v)
        s_bias60 = score_bias60(price, _ma60_v)
        s_rsi_rev = score_rsi_rev(_rsi14_v)
        s_vol_pulse = score_vol_pulse(_amt_ratio_v)
        s_overbought = score_overbought(_rsi14_v)

        # Weighted total: 题材30% + 基本面30% + 消息20% + 技术面20%
        # 行业前景加分叠加到技术面,上限5分
        industry_bonus = INDUSTRY_PROSPECT.get(code, 0)
        ts_adj = max(0, min(5, ts + industry_bonus))
        if industry_bonus != 0:
            label = INDUSTRY_PROSPECT_LABELS.get(industry_bonus, "")
            if isinstance(td, dict):
                td = dict(td)
                td["industry_prospect"] = f"行业前景{'加分' if industry_bonus > 0 else '减分'}{industry_bonus:+.1f}({label})"
        # v3.0-10dim: 十维中间总分（step2 无板块动量维度，按 (1−w_sector) 归一化
        #   保持 0-20 量纲；最终分由 apply_sector_momentum.py 按十权重重算）。
        #   权重来自 factor_weights.json（十维优先；旧五维自动映射；缺失回退十维先验）。
        _w9 = (fs * W_FUND + ns * W_NEWS
               + s_oversold60 * W_OVERSOLD60 + s_bias60 * W_BIAS60 + s_rsi_rev * W_RSI_REV
               + s_vol_pulse * W_VOL_PULSE + s_overbought * W_OVERBOUGHT
               + s_theme_int * W_THEME_INT + s_attn * W_ATTENTION)
        _scale = 1.0 / max(0.5, 1.0 - W_SECTOR_W)
        total = round(_w9 * _scale * 20, 2)  # v3.3 问题2: 量纲升级 每维满分10/总分满分100 (×20 替代 ×4)

        if total >= 85: rating, advice = "S", "优先买入，可重仓"
        elif total >= 65: rating, advice = "A", "逢低加仓，重点关注"
        elif total >= 45: rating, advice = "B", "波段操作，轻仓参与"
        elif total >= 25: rating, advice = "C", "观望，不新开仓"
        else: rating, advice = "D", "坚决回避，立即卖出"
        
        em = {"S":"🌟","A":"🔥","B":"📊","C":"👀","D":"⚠️"}[rating]
        
        results.append({
            "name": name, "code": code, "sector": sector,
            "sector_src": _src,
            "price": price, "pct_chg": pct, "pe": pe,
            "turnover": turnover,
            "score_news": round(ns, 2), "score_tech": round(ts_adj, 2), "score_fund": round(fs, 2),
            "score_theme": round(ths, 2), "total": total, "rating": rating,
            "advice": advice,
            "tech": td, "fund_reasons": fr, "theme_reasons": thr,
            "news_reasons": nr,
            "news_items": news_items,
            "news_recency": news_recency,
            "report_count": report_count,
            "sub_theme": sub_theme_label,
            "lifecycle": lifecycle,
            # v3.0-10dim: 回测 Top5 因子分 + 题材拆解两维（factor_eval 走步拟合登记用）
            "score_oversold60": round(s_oversold60, 2),
            "score_bias60": round(s_bias60, 2),
            "score_rsi_rev": round(s_rsi_rev, 2),
            "score_vol_pulse": round(s_vol_pulse, 2),
            "score_overbought": round(s_overbought, 2),
            "score_theme_intrinsic": round(s_theme_int, 2),
            "score_attention": round(s_attn, 2),
            "score_sustain": round(sustain_info["score"], 2),
            "sustain_note": sustain_info["note"],
            "is_cyclic": sustain_info["is_cyclic"],
            "is_storage_mod": sustain_info["is_storage_mod"],
            "fwd_pe": round(fwd_pe_resolved, 1) if fwd_pe_resolved else None,
            "growth": round(growth_resolved, 1) if growth_resolved else None,
            "pe_ladder": ladder_resolved if ladder_resolved else None,
            "cagr3": cagr3_input,
            # v3.5: 低基数失真审计——原始增速 vs 评分实际采用增速（触发修正时才有差异）
            "growth_raw": sustain_info.get("growth_raw"),
            "growth_capped": sustain_info.get("growth_capped", False),
            "growth_note": sustain_info.get("growth_note"),
            # v3.5.1: growth 取数口径修复审计（东财按报告期 vs 同花顺年报兜底）
            "growth_period": _em_g.get("period"),
            "growth_src": growth_src,
            "growth_annual_ths": growth_map.get(code),   # 留档：同花顺年报口径旧值（滞后半年+）
            # v3.5.1: Q3 预测景气加分审计（手工汇总表数据源，仅匹配个股有值）
            "q3_yoy": sustain_info.get("q3_yoy_eff"),
            "q3_yoy_raw": sustain_info.get("q3_yoy_raw"),
            "q3_accel": sustain_info.get("q3_accel"),
            "q3_implied_h2": sustain_info.get("q3_implied_h2"),
            "q3_bonus": sustain_info.get("q3_bonus", 0.0),
            "q3_note": sustain_info.get("q3_note"),
            # v3.0 B2: 同花顺摘要扩容字段(全池可得, 供报告层/护栏/后续因子用)
            "roe": _ths_ex.get("roe"),
            "debt_ratio": _ths_ex.get("debt_ratio"),
            "ocfg": _ths_ex.get("ocfg"),
            "week_chg": week_chg,
            # v3.2 P0-1/P0-3: 资格/流动性标记透传（来自 fetch 池，供报告层与组合层消费）
            "turnover_ratio": _elig_map.get(code, {}).get("turnover_ratio"),
            "circ_mv": _elig_map.get(code, {}).get("circ_mv"),
            "liquid_ok": _elig_map.get(code, {}).get("liquid_ok", True),
            "suspended": _elig_map.get(code, {}).get("suspended", False),
            "is_new": _elig_map.get(code, {}).get("is_new", False),
        })

        print(f"   {i+1:2d}. {name:6s} {em}{rating} {total:4.1f}  "
              f"消息{ns}/技术{ts_adj}/基本{fs}/热度{ths} | 反转{s_oversold60}/乖离{s_bias60}/RSI{s_rsi_rev}"
              f"/量能{s_vol_pulse}/超买{s_overbought} | {sector}({lifecycle})")
    
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
    
    # --- Step 4.9: 大盘赚钱效应 (全市场广度, 评估入场时机) ---
    print(f"\n🌊 Step 4.9: 大盘赚钱效应 (全市场广度)")
    breadth = fetch_market_breadth()
    money_effect = compute_money_effect(breadth)
    if money_effect["available"]:
        b = money_effect["raw"]
        src = b.get("source", "?")
        print(f"   数据源:{src} 上涨{b['up']}/下跌{b['down']}/平{b['flat']} 涨停{b['zt']}/跌停{b['dt']} "
              f"涨>5%有{b['up5']}只 跌>5%有{b['down5']}只")
        avg_s = f"{b['avg_pct']:+.2f}%" if b.get("avg_pct") is not None else "—"
        med_s = f"{b['median_pct']:+.2f}%" if b.get("median_pct") is not None else "—"
        amt_s = f"{b['amount_yi']:.0f}亿" if b.get("amount_yi") else "—"
        print(f"   平均涨幅{avg_s} 中位{med_s} 成交{amt_s} 活跃度{b.get('active_rate')}")
        if b["index"]:
            print(f"   三大指数: " + ", ".join(f"{k}{v:+.2f}%" for k, v in b["index"].items()))
        print(f"   💡 赚钱效应评分 {money_effect['score']}/100 | {money_effect['phase']} | {money_effect['position']}")
    else:
        print(f"   ⚠️ 赚钱效应数据获取失败, 跳过(不影响个股评分)")

    # Stats
    rc = Counter(r["rating"] for r in results)
    sec_avg = {}
    for r in results:
        s = r["sector"]
        sec_avg.setdefault(s, []).append(r["total"])
    
    # v3.0 A3: 因子覆盖度(A3) —— 逐股票标记各维数据是否真实(非兜底), 供报告层披露
    for r in results:
        td = r.get("tech") or {}
        r["factor_coverage"] = {
            "kline": bool(td.get("rsi") is not None or td.get("trend")),
            "fund": (r.get("fwd_pe") is not None or r.get("growth") is not None
                     or r.get("pe") is not None),
            "news": (r.get("score_news", 0) > 1 or bool(r.get("news_items"))),
            "theme": bool(r.get("theme_reasons")),
            "amount": (r.get("turnover") or 0) > 0,
        }
    _cov_dims = ("kline", "fund", "news", "theme", "amount")
    _cov_summary = {d: round(sum(1 for r in results if r["factor_coverage"][d]) / max(1, len(results)), 3)
                    for d in _cov_dims}

    out_data = {
        "date": raw.get("date", ""),
        "model": f"stock-scorer {SCORER_VER} (10-dim + AI + sector-momentum[evidence])",
        "pool_size": raw.get("pool_size") or len(stocks),
        "weights": "题材30%(内禀0.30+板块动量0.35+成交额排名0.15+情绪确认0.20) 消息20% 技术面20%(结构60%[均线趋势35%/MACD动能25%/量能资金25%/RSI入场时机15%]+动量40%,RSI为Wilder平滑口径) 基本面30%(含行业前景)",
        "results": results,
        "stats": {
            "rating_dist": {r: rc.get(r, 0) for r in "SABCD"},
            "sector_avg": {s: {"count": len(v), "avg": round(sum(v)/len(v), 1)}
                          for s, v in sorted(sec_avg.items(), key=lambda x: -sum(x[1])/len(x[1]))},
            "factor_coverage": _cov_summary,
            "eligible_marks": raw.get("eligible_marks") or {},
            "finance_removed": _fin_removed,
            "liquidity_floor": raw.get("liquidity_floor") or {},
            "market_breadth": money_effect,
        },
        # v2.11: 盘面复盘点评(数据驱动, 无幻觉) — 替代原"今日核心结论"+"大盘赚钱效应"模块
        "market_review": build_market_review(money_effect, None, results),
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
