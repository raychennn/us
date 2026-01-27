import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
import traceback
import io
from datetime import datetime, timedelta

# --- A. 自動獲取 NASDAQ 清單 ---
def get_nasdaq_stock_list():
    """從 NASDAQ Trader 獲取所有 NASDAQ 上市股票代碼"""
    try:
        url = "http://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
        s = requests.get(url).content
        df = pd.read_csv(io.BytesIO(s), sep="|")
        
        # 過濾掉測試代碼與 ETF (依據需求，這裡先保留主要股票)
        # 移除 'Symbol' 為 NaN 的
        df = df.dropna(subset=['Symbol'])
        # 排除測試股票 (Test Issue)
        df = df[df['Test Issue'] == 'N']
        
        full_list = df['Symbol'].tolist()
        
        # 排除特殊符號 (如 warrants, units 等，通常含 W, U, R)
        # 簡單過濾：長度過長或包含特殊字的
        clean_list = [x for x in full_list if x.isalpha() and len(x) < 5]
        
        print(f"✅ 成功獲取 {len(clean_list)} 檔 NASDAQ 清單")
        # 為了避免免費伺服器記憶體爆炸，這裡可選擇性回傳前 2000 大或全部
        return clean_list 
    except Exception as e:
        print(f"❌ 獲取 NASDAQ 清單失敗: {e}")
        # 備案：回傳 NASDAQ 100 成分股 (部分)
        return ['AAPL', 'MSFT', 'AMZN', 'NVDA', 'TSLA', 'GOOGL', 'META', 'AMD', 'NFLX', 'INTC']

# --- B. 輔助計算: RS Score ---
def calculate_performance_score(close_series):
    """
    計算 IBD 風格的加權績效分數 (RS Score Raw)
    權重: 近1季(40%) + 近2季(20%) + 近3季(20%) + 近4季(20%)
    """
    if len(close_series) < 260: return -999 # 資料不足
    
    try:
        # 使用 21交易日/月 近似
        c_now = close_series.iloc[-1]
        c_3m = close_series.iloc[-63]
        c_6m = close_series.iloc[-126]
        c_9m = close_series.iloc[-189]
        c_12m = close_series.iloc[-252]
        
        # 計算漲跌幅
        roc_3m = (c_now - c_3m) / c_3m
        roc_6m = (c_now - c_6m) / c_6m
        roc_9m = (c_now - c_9m) / c_9m
        roc_12m = (c_now - c_12m) / c_12m
        
        # 加權分數
        score = (roc_3m * 0.4) + (roc_6m * 0.2) + (roc_9m * 0.2) + (roc_12m * 0.2)
        return score
    except:
        return -999

# --- C. VCP 與 RS 判斷邏輯 ---
def check_vcp_criteria(df, qqq_score=None):
    """
    大量掃描專用函數: 回傳 True/False
    包含: 價格/成交額濾網、趨勢、VCP型態、RS 強度
    """
    # 0. 資料長度與基礎濾網
    if len(df) < 260: return False # 需一年資料算 RS
    
    close = df['Close']
    vol = df['Volume']
    current_c = close.iloc[-1]
    current_v = vol.iloc[-1]
    
    # [新增] 價格與成交金額濾網
    # 1. 股價 < 10 美金 -> 剔除
    if current_c < 10: return False
    
    # 2. 成交金額 (Dollar Volume) < 100,000 美金 -> 剔除
    # 使用 20日均量計算比較穩當，避免單日極端值
    avg_vol_20 = vol.tail(20).mean()
    avg_dollar_vol = current_c * avg_vol_20
    if avg_dollar_vol < 100000: return False

    # 3. RS 強度檢查 (對比 QQQ)
    if qqq_score is not None:
        stock_score = calculate_performance_score(close)
        # 如果個股分數 < QQQ 分數，代表表現輸給大盤 -> 剔除
        if stock_score < qqq_score: return False

    # 4. 趨勢濾網: 價格 > 50MA (美股習慣看 50MA/200MA) 且 50MA 翻揚
    sma50 = ta.sma(close, length=50)
    sma200 = ta.sma(close, length=200)
    
    if sma50 is None or sma200 is None: return False
    
    # 確保最後一筆不是 NaN
    if pd.isna(sma50.iloc[-1]) or pd.isna(sma50.iloc[-5]): return False

    if current_c < sma50.iloc[-1]: return False  # 股價要在季線(50MA)上
    if sma50.iloc[-1] < sma200.iloc[-1]: return False # 50MA 要在 200MA 上 (多頭排列)

    # ====================================================
    # 5. VCP Tightness (Close-to-Close, 10 Days, 4% - 美股波動較大放寬至 4-5%)
    # ====================================================
    recent_closes = close.tail(10) # 檢查近10天
    max_c = recent_closes.max()
    min_c = recent_closes.min()
    
    # 計算收盤價震幅百分比
    range_pct = (max_c - min_c) / current_c
    
    if range_pct > 0.05: # 美股放寬至 5%
        return False

    # 6. 成交量 VCP: 近期量縮 (10MA < 50MA)
    vol_sma10 = vol.tail(10).mean()
    vol_sma50 = vol.tail(50).mean()
    if vol_sma10 >= vol_sma50: return False
    
    return True

# --- D. 單一股票診斷邏輯 ---
def diagnose_single_stock(df, symbol, qqq_df=None):
    """
    對單一股票進行詳細檢查
    """
    report = []
    is_pass = True
    
    # 0. 資料基礎檢查
    df = df.dropna()
    if len(df) < 260:
        return False, f"❌ 資料不足: 有效 K 線僅 {len(df)} 根 (需 > 260 根以計算 RS)"

    close = df['Close']
    vol = df['Volume']
    c_now = close.iloc[-1]
    
    # [新增] 1. 基礎門檻檢查
    avg_vol = vol.tail(20).mean()
    dollar_vol = c_now * avg_vol
    
    report.append(f"🔹 **基礎門檻 (Basic Filters)**")
    if c_now >= 10:
        report.append(f"   ✅ 股價: ${c_now:.2f} (>= $10)")
    else:
        report.append(f"   ❌ 股價: ${c_now:.2f} (< $10)")
        is_pass = False
        
    if dollar_vol >= 100000:
        report.append(f"   ✅ 日均成交額: ${dollar_vol/1000:.0f}K (>= $100K)")
    else:
        report.append(f"   ❌ 日均成交額: ${dollar_vol/1000:.0f}K (< $100K)")
        is_pass = False

    # 2. RS 強度 (vs QQQ)
    report.append(f"\n🔹 **相對強度 (RS vs QQQ)**")
    if qqq_df is not None:
        stock_score = calculate_performance_score(close)
        qqq_score = calculate_performance_score(qqq_df['Close'])
        
        if stock_score > qqq_score:
             report.append(f"   ✅ 強於大盤 (Score: {stock_score:.2f} > QQQ: {qqq_score:.2f})")
        else:
             report.append(f"   ❌ 弱於大盤 (Score: {stock_score:.2f} < QQQ: {qqq_score:.2f})")
             is_pass = False
    else:
        report.append(f"   ⚠️ 無法比較 (缺少 QQQ 數據)")

    # 3. 趨勢
    sma50 = ta.sma(close, length=50).iloc[-1]
    sma200 = ta.sma(close, length=200).iloc[-1]
    
    report.append(f"\n🔹 **趨勢 (Trend)**")
    if c_now > sma50 > sma200:
        report.append(f"   ✅ 多頭排列 (股價 > 50MA > 200MA)")
    else:
        report.append(f"   ❌ 趨勢不符 (50MA: {sma50:.2f}, 200MA: {sma200:.2f})")
        is_pass = False

    # 4. VCP Tightness
    recent_closes = close.tail(10)
    range_pct = (recent_closes.max() - recent_closes.min()) / c_now
    
    report.append(f"\n🔹 **收斂度 (Tightness)**")
    if range_pct <= 0.05:
        report.append(f"   ✅ 10日震幅 {range_pct*100:.1f}% (<= 5%)")
    else:
        report.append(f"   ❌ 震幅過大 {range_pct*100:.1f}% (> 5%)")
        is_pass = False
        
    # 5. 量縮
    vol_sma10 = vol.tail(10).mean()
    vol_sma50 = vol.tail(50).mean()
    report.append(f"\n🔹 **成交量 (Volume)**")
    if vol_sma10 < vol_sma50:
        report.append(f"   ✅ 量縮 (10MA < 50MA)")
    else:
        report.append(f"   ❌ 未量縮")
        is_pass = False

    final_msg = "\n".join(report)
    return is_pass, final_msg

# --- E. 執行掃描主程式 ---
async def scan_market(target_date_str):
    try:
        # 日期處理
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
        # 下載區間 (美股需要較長資料算 200MA 與 RS)
        start_date = target_date - timedelta(days=400)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        print(f"🚀 開始美股掃描: {formatted_date}")

        # 1. 先下載基準 QQQ 數據
        print("📊 下載 QQQ 基準數據...")
        qqq_data = yf.download("QQQ", start=start_date, end=end_date, progress=False, auto_adjust=True)
        qqq_score = -999
        if not qqq_data.empty:
            if isinstance(qqq_data.columns, pd.MultiIndex):
                qqq_series = qqq_data['Close']['QQQ']
            else:
                qqq_series = qqq_data['Close']
            qqq_score = calculate_performance_score(qqq_series)
            print(f"ℹ️ QQQ 當日 RS Score: {qqq_score:.4f}")

        # 2. 獲取 NASDAQ 清單
        tickers = get_nasdaq_stock_list()
        
        # 分批處理 (Zeabur 記憶體優化：batch_size 調小至 50)
        batch_size = 50
        valid_symbols = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                # auto_adjust=True 確保拿到還原股價
                data = yf.download(batch, start=start_date, end=end_date, group_by='ticker', progress=False, threads=True, auto_adjust=True)
                
                if data.empty: continue

                for symbol in batch:
                    try:
                        if symbol not in data.columns.levels[0]: continue # 沒下載到

                        df = data[symbol].copy()
                        
                        # 欄位處理
                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        # 日期檢核
                        last_dt = df.index[-1].date()
                        # 美股可能有時差問題，允許誤差1天
                        if abs((last_dt - target_date.date()).days) > 1: continue
                        
                        # 執行 VCP + RS 檢查
                        if check_vcp_criteria(df, qqq_score):
                            valid_symbols.append(symbol)
                    except Exception:
                        continue
                
                await asyncio.sleep(1.0) # 休息久一點避免被擋
                
            except Exception as e:
                print(f"⚠️ Batch error: {e}")
                continue

        return valid_symbols, formatted_date

    except Exception as e:
        print(f"❌ Scan fatal error: {e}")
        traceback.print_exc()
        return [], target_date_str

# --- F. 執行單一股票診斷 ---
async def fetch_and_diagnose(symbol_input, date_str):
    try:
        target_date = datetime.strptime(date_str, "%y%m%d")
        start_date = target_date - timedelta(days=400)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')

        symbol = symbol_input.upper().strip().replace(".", "-") # 美股格式修正 BRK.B -> BRK-B

        # 下載 QQQ 與 個股
        print(f"Debug: Diagnosing {symbol} vs QQQ...")
        data = yf.download([symbol, "QQQ"], start=start_date, end=end_date, group_by='ticker', progress=False, auto_adjust=True)
        
        if symbol not in data.columns.levels[0]:
            return False, f"❌ 找不到美股數據: {symbol}", formatted_date
            
        df_stock = data[symbol].dropna()
        df_qqq = data["QQQ"].dropna()

        if df_stock.empty: return False, "❌ 無有效數據", formatted_date

        # 執行診斷
        is_pass, report = diagnose_single_stock(df_stock, symbol, df_qqq)
        
        header = f"🔍 **美股診斷報告: {symbol}**\n📅 日期: {formatted_date}\n" + "-"*20 + "\n"
        full_report = header + report
        
        return is_pass, full_report, formatted_date

    except Exception as e:
        traceback.print_exc()
        return False, f"❌ 程式錯誤: {str(e)}", date_str
