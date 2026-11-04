#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_bench.py —— 拉取基准指数日线，落盘 data/bench/<code>.json

用途：组合层回测需要「超额收益」基准。此前项目只有 IC 层、没有净值层，
      更没有基准序列，故无从计算超额夏普/超额回撤。

数据源：东方财富 push2his（主源，前复权）；**新浪 getKLineData 为备源**。
  · 000300 沪深300  —— 主基准（超额口径）
  · 000905 中证500  —— 对照（判断是否只是小盘暴露）
  · 932000 中证2000 —— 对照（本池偏小盘，需确认不是纯 size beta）

== 2026-11-04 实测事故（备源与退出码的来源）==
当日东财 push2his **四个指数全部 RemoteDisconnected**，而 main() 无论成败
都退出 0，日链因此打印「✅ 指数基准已更新」，实际一只都没更新：
  · data/bench/*.json 停在 11-03
  · regime_model.py 的日期硬门禁查到「指数最新 11-03 ≠ 数据日期 11-04」
    → exit 2 → 市场状态/风险预算整段缺失，页面却照常出（典型假成功）
同刻实测三源对指数的最新日期：东财 push2his ❌ 断连 / 腾讯 ifzq ❌ 501 /
新浪 getKLineData ✅ 2026-11-04。故加新浪备源，且**全失败必须非零退出**。
⚠️ 注意与个股行情相反：个股是腾讯可用、新浪滞后；指数是新浪可用、腾讯 501。
   两边都不能只认单一源。
"""
import os
import json
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "data", "bench")

BENCH = {
    "000300": ("1.000300", "沪深300"),
    "000905": ("1.000905", "中证500"),
    "000852": ("1.000852", "中证1000"),
    "000985": ("1.000985", "中证全指"),
}
# ⚠️ 中证2000（932000）在 push2his 取不到（rc=100 / data=None），已放弃。
#    改用中证1000 + 中证全指做小盘对照：本池偏小盘，必须证明超额不是纯 size beta，
#    若超额在「中证1000 之上」仍成立，才说明不是简单的小盘暴露。


def fetch(secid, beg="20150101", end="20261231"):
    """拉取日线；beg 默认回溯至 2015-01-01（round109b 扩样本）。

    ⚠️ 为什么扩到 2015：原 440 交易日（≈1.75 年）只有单一 regime，
       导致「方向不可预测」的结论无法区分「真的不可预测」与「样本太少」。
       扩到 2015 后覆盖 2015 股灾 / 2016 熔断 / 2018 熊市 / 2019-2021 结构牛 /
       2022 熊市 / 2024-2025 反弹，约 11 年、2700+ 交易日、多个完整涨跌周期。
    """

    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt=101&fqt=1&beg={beg}&end={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=25).read().decode()
    d = json.loads(raw)
    if not d.get("data") or not d["data"].get("klines"):
        raise RuntimeError(f"空数据: {d}")
    out = {}
    for line in d["data"]["klines"]:
        p = line.split(",")
        # date,open,close,high,low,vol,amount,amp
        out[p[0]] = [float(p[1]), float(p[2]), float(p[3]), float(p[4])]
    return out


SINA_KL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d")


def fetch_sina(code, datalen=3000):
    """新浪指数日线 → {date: [open, close, high, low]}。

    ⚠️ 与东财口径的两点差异，必须保留：
      1) 新浪 `volume` 单位是**股**（本项目 kline_units 口径里的「股」，无需再乘）；
         但本函数只取 OHLC 四价，volume 不参与，故不受双口径影响。
      2) 新浪给的是**不复权**价。指数不像个股有拆股/分红除权，指数点位本身连续，
         不存在复权问题 → 与 fqt=1 口径实质等价。
    datalen 上限实测可到 3000（≈12 年），够覆盖 fetch() 的 2015 起回溯。
    """
    sym = ("sh" if code.startswith("000") or code.startswith("9") else "sz") + code
    url = SINA_KL % (sym, int(datalen))
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": "https://finance.sina.com.cn/"})
    raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    d = json.loads(raw)
    if not isinstance(d, list) or not d:
        raise RuntimeError(f"新浪空数据: {str(raw)[:120]}")
    out = {}
    for k in d:
        try:
            out[str(k["day"])[:10]] = [float(k["open"]), float(k["close"]),
                                       float(k["high"]), float(k["low"])]
        except Exception:
            continue
    if not out:
        raise RuntimeError("新浪数据无可解析行")
    return out


def fetch_any(code, secid, old_path=None):
    """多源取优：东财 → 新浪。取「最新日期更晚」的那份；并列优先东财。

    ⚠️ **防止烂源覆盖好源**（2026-11-04 实测）：新浪的 000985 中证全指是**死数据**，
       最后一根停在 2016-06-13（datalen=50/300/3000 都如此），而旧缓存
       （东财源）有 2015-01-05~2026-11-03 共 2874 根。
       若只按「源之间取最新」比较，某天东财也挂了就会让新浪的 2016 死数据
       覆盖掉盘上已有的 2874 根 —— 数据不但没更新，反而**倒退十年**。
       ⇒ 必须再和「盘上旧文件」比一次：候选源的最新日 < 旧文件最新日 → 拒用该源。
    """
    errs = []
    got = []
    for nm, fn in (("eastmoney", lambda: fetch(secid)),
                   ("sina", lambda: fetch_sina(code))):
        try:
            px = fn()
            if px:
                got.append((nm, px))
        except Exception as e:
            errs.append(f"{nm}:{type(e).__name__}")
    if not got:
        raise RuntimeError("全部源失败（%s）" % ", ".join(errs))

    # 与盘上旧数据比对，剔除「比现状还旧」的源
    old_last = None
    if old_path and os.path.exists(old_path):
        try:
            _o = json.load(open(old_path, encoding="utf-8")).get("px") or {}
            if _o:
                old_last = max(_o)
        except Exception:
            old_last = None
    if old_last:
        _keep = []
        for nm, px in got:
            if max(px) < old_last:
                print(f"   ⛔ 拒用 {nm} 源 {code}：最新 {max(px)} 早于盘上 {old_last}"
                      f"（该源已停止维护）")
            else:
                _keep.append((nm, px))
        if _keep:
            got = _keep
        elif old_last:
            # 所有源都比盘上旧 → 保留旧文件不动，明确报错交给上层
            raise RuntimeError(f"全部源均早于盘上最新 {old_last}（疑似源失效），保留旧数据")

    if len(got) == 1:
        return got[0][1]
    got.sort(key=lambda t: max(t[1]))
    if max(got[0][1]) != max(got[1][1]):
        print(f"   ⚠️ 指数源不同步 {code}: "
              + " / ".join(f"{n}={max(p)}" for n, p in got)
              + f" → 取最新 {max(got[-1][1])}")
    return got[-1][1]


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    summary = {}
    _n_ok = 0
    for code, (secid, name) in BENCH.items():
        try:
            path = os.path.join(OUTDIR, f"{code}.json")
            px = fetch_any(code, secid, old_path=path)
            ds = sorted(px)
            json.dump({"code": code, "name": name, "px": px},
                      open(path, "w", encoding="utf-8"), ensure_ascii=False)
            summary[code] = {"name": name, "n": len(px),
                             "range": [ds[0], ds[-1]] if ds else None,
                             "path": os.path.relpath(path, HERE)}
            print(f"✅ {code} {name}  {len(px)} 交易日  {ds[0]} ~ {ds[-1]}")
            _n_ok += 1
        except Exception as e:
            print(f"❌ {code} {name}  {type(e).__name__}: {e}")
            summary[code] = {"error": str(e)}
        time.sleep(0.4)
    json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "bench": summary},
              open(os.path.join(OUTDIR, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {OUTDIR}/manifest.json")

    # ⚠️ 退出码必须反映真实结果（2026-11-04 事故）：原实现无论成败恒 exit 0，
    #   四个指数全断连时主流程照样打「✅ 指数基准已更新」，使 regime_model
    #   在陈旧指数上被日期门禁拦掉，而无人察觉基准根本没更新。
    _n = len(BENCH)
    print(f"   基准更新 {_n_ok}/{_n}")
    if _n_ok == 0:
        print("❌ 全部指数源失败 —— 基准未更新，下游 regime_model 将被日期门禁拦截")
        return 3
    if _n_ok < _n:
        print(f"⚠️ 仅 {_n_ok}/{_n} 更新，其余保留旧值（请核对 manifest.json）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
