"""探测历史市值/估值数据源，以及 2020 起长面板的可行性测算。"""
import time

import akshare as ak


def probe_baidu_mv():
    print("=== 百度估值接口（总市值/流通市值/PE/PB） ===")
    for ind in ("总市值", "流通市值", "市盈率(TTM)", "市净率"):
        try:
            t0 = time.time()
            df = ak.stock_zh_valuation_baidu(symbol="000001", indicator=ind, period="全部")
            print(f"  {ind}: {len(df)} 行  {df.iloc[0,0]}~{df.iloc[-1,0]}  {time.time()-t0:.2f}s")
        except Exception as e:
            print(f"  {ind}: 失败 {type(e).__name__}: {str(e)[:100]}")


def probe_batch_mv():
    print("\n=== 全市场截面带市值（可逐日回补？） ===")
    try:
        t0 = time.time()
        df = ak.stock_zh_a_spot_em()
        cols = [c for c in df.columns if any(k in c for k in ("市值", "市盈", "市净", "换手"))]
        print(f"  行数 {len(df)}  {time.time()-t0:.1f}s")
        print(f"  相关列: {cols}")
    except Exception as e:
        print(f"  失败: {str(e)[:100]}")


def probe_size():
    """测算长面板体积。"""
    print("\n=== 长面板体积测算 ===")
    n_stock, n_day, n_dim = 5927, 1655, 11
    rows = n_stock * n_day
    print(f"  全A {n_stock} 只 × {n_day} 交易日 = {rows:,} 行 × {n_dim} 维")
    # 实测：现有 panel 478,650 行 11 维 = 128.5 MB
    per_row = 128.5e6 / 478650
    print(f"  现有 panel 实测 {per_row:.0f} bytes/行 → 预估 {rows*per_row/1e9:.1f} GB（JSON，不可行）")
    print(f"  npz float32: {rows*n_dim*4/1e9:.2f} GB + mask → 仍偏大")
    print(f"  仅保留 ~3000 只有效股: {3000*n_day*n_dim*4/1e9:.2f} GB（npz，可行）")


if __name__ == "__main__":
    probe_baidu_mv()
    probe_batch_mv()
    probe_size()
