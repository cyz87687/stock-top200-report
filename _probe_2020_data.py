"""探测：能否拉到 2020-01 至今的前复权日线 + 市值 + 财务。"""
import sys, time, json

import akshare as ak


def probe_kline():
    print("=== 1. 东财 stock_zh_a_hist qfq 长区间 ===")
    for sym in ("000001", "600519", "300750"):
        try:
            t0 = time.time()
            df = ak.stock_zh_a_hist(symbol=sym, period="daily",
                                    start_date="20200101", end_date="20261104",
                                    adjust="qfq")
            print(f"  {sym}: {len(df)} 行  {df.iloc[0,0]} ~ {df.iloc[-1,0]}  "
                  f"耗时 {time.time()-t0:.1f}s")
            print(f"       列: {list(df.columns)}")
        except Exception as e:
            print(f"  {sym}: 失败 {type(e).__name__}: {e}")


def probe_stock_list():
    print("\n=== 2. 全 A 股列表 ===")
    for fn in ("stock_zh_a_spot_em", "stock_info_a_code_name"):
        try:
            t0 = time.time()
            df = getattr(ak, fn)()
            print(f"  {fn}: {len(df)} 只  耗时 {time.time()-t0:.1f}s  列={list(df.columns)[:8]}")
        except Exception as e:
            print(f"  {fn}: 失败 {type(e).__name__}: {e}")


def probe_mv():
    print("\n=== 3. 市值/估值历史（circ_mv/total_mv/pe/pb） ===")
    try:
        t0 = time.time()
        df = ak.stock_zh_valuation_baidu(symbol="000001", indicator="总市值",
                                         period="全部")
        print(f"  百度总市值: {len(df)} 行  {df.iloc[0,0]} ~ {df.iloc[-1,0]}  耗时 {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"  百度总市值: 失败 {type(e).__name__}: {e}")


if __name__ == "__main__":
    probe_kline()
    probe_stock_list()
    probe_mv()
