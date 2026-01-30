import pandas as pd
import numpy as np
import yfinance as yf
import time
import logging
from config import *
from utils import get_market_tickers  # 修正：匯入這個函式

logger = logging.getLogger(__name__)

def fetch_data(tickers):
    """
    分批下載數據，避免 yfinance 限制。
    回傳：Dictionary of DataFrames
    """
    data_map = {}
    
    # 先下載 Benchmark (SPY)
    logger.info(f"下載 Benchmark: {BENCHMARK_TICKER}")
    spy = yf.download(BENCHMARK_TICKER, period="1y", progress=False)
    if spy.empty:
        logger.error("無法下載 Benchmark 數據，策略中止。")
        return None, None
    
    # 處理 Benchmark 收盤價 (用於 RS Line)
    # yfinance 有時回傳 MultiIndex，需要正規化
    if isinstance(spy.columns, pd.MultiIndex):
        try:
            spy_close = spy['Close'][BENCHMARK_TICKER]
        except KeyError:
            spy_close = spy['Close'] # 嘗試直接取 Close
    else:
        spy_close = spy['Close']

    # 分批下載股票
    valid_tickers_data = {}
    total = len(tickers)
    
    for i in range(0, total, BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        logger.info(f"下載批次 {i}/{total}: {batch[:3]}...")
        
        try:
            # 下載 2 年數據以計算 200MA 和 RS Line (6M)
            df = yf.download(batch, period="2y", group_by='ticker', threads=True, progress=False)
            
            # 處理 yfinance 下載單一股票與多股票結構不同的問題
            if len(batch) == 1:
                ticker = batch[0]
                if not df.empty:
                    valid_tickers_data[ticker] = df
            else:
                for ticker in batch:
                    try:
                        # 檢查該 ticker 是否有數據
                        stock_df = df[ticker]
                        # 簡單檢查數據長度
                        if len(stock_df) > 150: # 至少要有半年多數據
                            valid_tickers_data[ticker] = stock_df
                    except KeyError:
                        continue
            
            # 休息一下，避免被封
            time.sleep(BATCH_DELAY)
            
        except Exception as e:
            logger.error(f"批次下載失敗: {e}")
            continue

    return valid_tickers_data, spy_close

def calculate_indicators(df, spy_close):
    """計算單一股票的所有技術指標"""
    try:
        # 清理數據：移除全 NaN 的行
        df = df.dropna(how='all')
        if df.empty: return None

        # 確保索引對齊
        df.index = pd.to_datetime(df.index)
        spy_close.index = pd.to_datetime(spy_close.index)
        
        # 取共用時間段
        common_index = df.index.intersection(spy_close.index)
        df = df.loc[common_index]
        bench_close = spy_close.loc[common_index]

        if len(df) < 200: return None # 數據不足

        close = df['Close']
        volume = df['Volume']
        
        # 0. 基礎運算
        sma50 = close.rolling(window=50).mean()
        # sma200 = close.rolling(window=200).mean() # 目前沒用到，先註解省效能
        avg_vol_50 = volume.rolling(window=50).mean()
        avg_vol_10 = volume.rolling(window=10).mean()
        avg_vol_5 = volume.rolling(window=5).mean()
        
        high_52w = close.rolling(window=252).max()
        
        # 1. RS Line (6M 概念，計算 126 日)
        rs_line = close / bench_close
        rs_max_126 = rs_line.rolling(window=RS_LOOKBACK_DAYS).max()
        rs_sma_20 = rs_line.rolling(window=20).mean()
        
        # 準備最後一天的數據做判斷
        curr_idx = -1
        
        result = {
            'close': close.iloc[curr_idx],
            'volume': volume.iloc[curr_idx],
            'sma50': sma50.iloc[curr_idx],
            'sma50_prev_10': sma50.iloc[curr_idx - 10], # 10天前的 SMA50 (判斷趨勢)
            'avg_vol_50': avg_vol_50.iloc[curr_idx],
            'avg_vol_10': avg_vol_10.iloc[curr_idx],
            'avg_vol_5': avg_vol_5.iloc[curr_idx],
            'high_52w': high_52w.iloc[curr_idx],
            'rs_line': rs_line.iloc[curr_idx],
            'rs_max_126': rs_max_126.iloc[curr_idx],
            'rs_sma_20': rs_sma_20.iloc[curr_idx],
            
            # VCP 相關數據 (最近5日區間)
            'close_last_5_max': close.iloc[-5:].max(),
            'close_last_5_min': close.iloc[-5:].min(),
            
            # Power Play 相關 (4-8週漲幅 -> 約 20-40 交易日)
            'close_4w_ago': close.iloc[-20],
            'close_8w_ago': close.iloc[-40],
            'base_high_recent': close.iloc[-25:].max(), # 近期高點 (旗桿頂)
            'base_low_recent': close.iloc[-25:].min(),  # 近期低點
            
            # 原始序列 (用於複雜邏輯)
            'history': df
        }
        
        return result
    except Exception as e:
        # logger.debug(f"指標計算錯誤: {e}") # Debug 用，平時註解掉避免洗版
        return None

def check_strategy(ticker, data):
    """
    綜合判斷邏輯
    Return: (Bool: 是否通過, String: 標籤/理由)
    """
    if data is None: return False, ""
    
    reasons = []
    
    # --- 1. Universe 基礎濾網 ---
    if data['close'] < MIN_PRICE: return False, "Price too low"
    
    # 這裡的成交額檢查是近似值，因為 yfinance 只有當日成交量
    dollar_vol = data['volume'] * data['close']
    if data['avg_vol_50'] < MIN_AVG_VOLUME_SHARES and dollar_vol < MIN_AVG_VOLUME_DOLLAR:
        return False, "Low Liquidity"

    # --- 2. 趨勢與第二段 Gate ---
    # A. RS Line 必過
    rs_condition = (data['rs_line'] > data['rs_sma_20']) and \
                   (data['rs_line'] >= 0.98 * data['rs_max_126'])
    
    if not rs_condition: return False, "Weak RS Line"
    
    # B. 價格位置
    # 50 SMA 走升 (今日 > 10日前)
    sma_trend = data['sma50'] > data['sma50_prev_10']
    price_above_sma = data['close'] >= data['sma50']
    
    if not (sma_trend and price_above_sma): return False, "Below SMA50 or Downtrend"
    
    near_high = data['close'] >= PRICE_NEAR_HIGH_THRESHOLD * data['high_52w']
    if not near_high: return False, "Too far from 52W High"

    # --- 3. 型態判斷 (VCP OR Power Play) ---
    is_vcp = False
    is_pp = False
    
    # 分支 ①：經典 VCP
    # VCP-1: 整理深度 (這裡簡化為近期 50 日內的 Drawdown)
    # 取最近 50 天的 High/Low 區間
    recent_50_high = data['history']['High'].iloc[-50:].max()
    recent_50_low = data['history']['Low'].iloc[-50:].min()
    
    # 避免除以零
    if recent_50_high > 0:
        current_dd = (recent_50_high - data['history']['Low'].iloc[-1]) / recent_50_high
        vcp_dd_ok = current_dd < VCP_MAX_DRAWDOWN
    else:
        vcp_dd_ok = False
    
    # VCP-3: Volume Dry-Up
    vol_dry_10 = data['avg_vol_10'] <= 0.8 * data['avg_vol_50']
    vol_dry_5 = data['avg_vol_5'] <= 0.7 * data['avg_vol_50']
    
    # VCP-4: 緊密收盤
    if data['close_last_5_min'] > 0:
        tight_close = (data['close_last_5_max'] - data['close_last_5_min']) / data['close_last_5_min'] <= VCP_TIGHT_CLOSE_PCT
    else:
        tight_close = False
    
    if vcp_dd_ok and vol_dry_10 and (vol_dry_5 or tight_close):
        # 註：嚴格的 VCP "幾次收斂" 難以用簡單數學完全過濾，這裡用量縮+盤整+緊密收盤做代理變數
        is_vcp = True
        reasons.append("VCP")

    # 分支 ②：Power Play
    # PP-1: 旗桿猛 (4-8週前漲幅)
    # 取 40 天前與 20 天前的較低者作為起漲點比較
    low_window = data['history']['Low'].iloc[-45:-15].min()
    high_recent = data['base_high_recent']
    
    run_up_ok = False
    if low_window > 0:
        gain = (high_recent - low_window) / low_window
        if PP_RUN_UP_PCT_MIN <= gain: # 上限可不設
            run_up_ok = True
            
    # PP-2: 旗面盤整 (High Tight)
    # 距離近期高點回撤小
    if high_recent > 0:
        pp_dd_ok = (high_recent - data['history']['Low'].iloc[-1]) / high_recent <= PP_DRAWDOWN_MAX
    else:
        pp_dd_ok = False

    # 價格保持在高位 (Close >= Low + 0.6*(High-Low))
    range_high = data['base_high_recent']
    range_low = data['base_low_recent']
    in_upper_channel = data['close'] >= (range_low + 0.6 * (range_high - range_low))
    
    # PP-3: 量能
    pp_vol_ok = vol_dry_10 and vol_dry_5
    
    # PP-4: RS 確認 (已在 Gate A 做過，但 PP 支線可再次確認)
    
    if run_up_ok and pp_dd_ok and in_upper_channel and pp_vol_ok:
        is_pp = True
        reasons.append("PowerPlay")

    # Final Decision
    if is_vcp or is_pp:
        tag = " & ".join(reasons)
        return True, tag
    
    return False, ""

def run_scanner():
    """執行完整掃描流程"""
    logger.info("開始執行掃描任務...")
    tickers = get_market_tickers()
    logger.info(f"總共獲取到 {len(tickers)} 檔股票代號")
    
    data_map, spy_close = fetch_data(tickers)
    
    if not data_map:
        return []
    
    results = []
    
    for ticker, df in data_map.items():
        indicators = calculate_indicators(df, spy_close)
        passed, reason = check_strategy(ticker, indicators)
        if passed:
            # 加入優質標記邏輯：RS 10日內新高但價格未創高
            rs_new_high = indicators['rs_line'] >= indicators['rs_max_126'] # 簡化判斷
            price_not_high = indicators['close'] < indicators['high_52w']
            
            note = reason
            if rs_new_high and price_not_high:
                note += " (★RS領先)"
            
            results.append({
                'Ticker': ticker,
                'Price': round(indicators['close'], 2),
                'Pattern': note,
                'Volume_Ratio': round(indicators['avg_vol_5'] / indicators['avg_vol_50'], 2)
            })
    
    logger.info(f"掃描完成，找到 {len(results)} 檔符合條件")
    return results
