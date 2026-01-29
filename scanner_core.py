import requests
import pandas as pd
# [FIX] 移除 pandas_ta 引用，避免依賴衝突
# import pandas_ta as ta 
import yfinance as yf
import asyncio
import traceback
import io
import math
import gc
from datetime import datetime, timedelta

# 策略參數
import config as cfg

# --- A. 自動獲取美股全市場清單 (NASDAQ + NYSE) ---
def get_all_us_stocks():
    """
    從 NASDAQ Trader 獲取 NASDAQ 與 NYSE 清單
    並執行嚴格過濾 (排除 ETF, ADR, 權證, 特別股)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # 定義通用的排除關鍵字
    exclude_keywords = [
        ' ADR ', ' ADS ', ' DEPOSITARY ', # ADR 相關
        ' PREFERRED ', ' PFD ',           # 特別股
        ' WARRANT ', ' WTS ', ' UNIT ',   # 權證與單位
        ' RIGHTS ',                       # 認股權
        ' ACQUISITION '                   # SPAC 相關
    ]

    candidates = set()

    # --- 1. 獲取 NASDAQ 清單 ---
    try:
        url_nasdaq = "http://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
        res = requests.get(url_nasdaq, headers=headers, timeout=15)
        res.raise_for_status()
        
        df = pd.read_csv(io.BytesIO(res.content), sep="|")
        
        # 基礎過濾
        df = df.dropna(subset=['Symbol'])
        df = df[df['Test Issue'] == 'N']
        if 'ETF' in df.columns:
            df = df[df['ETF'] == 'N']
            
        # 名稱過濾
        df['Security Name'] = df['Security Name'].str.upper()
        for kw in exclude_keywords:
            df = df[~df['Security Name'].str.contains(kw, na=False)]
            
        # 符號過濾 (只留純字母，去除有後綴的)
        nasdaq_list = [x for x in df['Symbol'].tolist() if str(x).isalpha()]
        candidates.update(nasdaq_list)
        print(f"✅ NASDAQ 篩選後數量: {len(nasdaq_list)}")
        
    except Exception as e:
        print(f"⚠️ NASDAQ 清單獲取失敗: {e}")

    # --- 2. 獲取 NYSE 清單 (從 otherlisted.txt) ---
    try:
        url_nyse = "http://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
        res = requests.get(url_nyse, headers=headers, timeout=15)
        res.raise_for_status()
        
        df = pd.read_csv(io.BytesIO(res.content), sep="|")
        
        # 基礎過濾
        # 注意: otherlisted 的代碼欄位叫做 'ACT Symbol'
        df = df.dropna(subset=['ACT Symbol'])
        df = df[df['Test Issue'] == 'N']
        if 'ETF' in df.columns:
            df = df[df['ETF'] == 'N']
            
        # [關鍵] 鎖定 NYSE (Exchange Code = 'N')
        # A = NYSE American, N = NYSE, P = NYSE Arca, Z = BATS
        df = df[df['Exchange'] == 'N']
        
        # 名稱過濾
        df['Security Name'] = df['Security Name'].str.upper()
        for kw in exclude_keywords:
            df = df[~df['Security Name'].str.contains(kw, na=False)]
            
        # 符號過濾
        nyse_list = [x for x in df['ACT Symbol'].tolist() if str(x).isalpha()]
        candidates.update(nyse_list)
        print(f"✅ NYSE 篩選後數量: {len(nyse_list)}")
        
    except Exception as e:
        print(f"⚠️ NYSE 清單獲取失敗: {e}")

    final_list = sorted(list(candidates))
    
    # 如果兩邊都掛了，回傳備用清單
    if not final_list:
        print("❌ 無法獲取任何清單，使用備用大型股")
        return ['AAPL', 'MSFT', 'AMZN', 'NVDA', 'TSLA', 'META', 'AMD', 'NFLX', 'GOOGL', 'AVGO']
        
    print(f"🚀 全市場 (NASDAQ + NYSE) 總掃描檔數: {len(final_list)}")
    return final_list

# --- B. 輔助計算: RS Score ---
def calculate_performance_score(close_series):
    if len(close_series) < 260: return -999
    try:
        c_now = close_series.iloc[-1]
        c_3m = close_series.iloc[-63]
        c_6m = close_series.iloc[-126]
        c_9m = close_series.iloc[-189]
        c_12m = close_series.iloc[-252]
        
        roc_3m = (c_now - c_3m) / c_3m
        roc_6m = (c_now - c_6m) / c_6m
        roc_9m = (c_now - c_9m) / c_9m
        roc_12m = (c_now - c_12m) / c_12m
        
        score = (roc_3m * 0.4) + (roc_6m * 0.2) + (roc_9m * 0.2) + (roc_12m * 0.2)
        return score
    except:
        return -999

# --- B2. Fallen Angel RS：指標計算與 Gate ---
def _safe_div(a, b, default=float("nan")):
    try:
        if b == 0 or pd.isna(b):
            return default
        return a / b
    except Exception:
        return default

def compute_fallen_angel_rs_features(df_stock: pd.DataFrame, qqq_close: pd.Series):
    features = {
        "leader_peak_excess": float("nan"),
        "rs_near_high_pct": float("nan"),
        "rs_dd_vs_price_dd": float("nan"),
        "rs_ma20_slope": float("nan"),
    }

    if df_stock is None or df_stock.empty or qqq_close is None or qqq_close.empty:
        return False, features

    close = df_stock["Close"].copy()
    bench = qqq_close.reindex(close.index).ffill()

    aligned = pd.concat([close, bench], axis=1).dropna()
    if aligned.shape[0] < 260:
        return False, features

    close_s = aligned.iloc[:, 0]
    close_b = aligned.iloc[:, 1]
    rs_line = close_s / close_b

    # Leader Peak
    ex3 = (close_s.pct_change(63) - close_b.pct_change(63)).tail(cfg.LEADER_PEAK_LOOKBACK_D)
    ex6 = (close_s.pct_change(126) - close_b.pct_change(126)).tail(cfg.LEADER_PEAK_LOOKBACK_D)

    max_ex3 = ex3.max(skipna=True)
    max_ex6 = ex6.max(skipna=True)
    leader_peak_excess = max(max_ex3 if pd.notna(max_ex3) else -999,
                             max_ex6 if pd.notna(max_ex6) else -999)

    if leader_peak_excess != -999:
        features["leader_peak_excess"] = float(leader_peak_excess * 100.0)

    leader_ok = ((pd.notna(max_ex3) and max_ex3 >= cfg.MIN_PEAK_EXCESS_3M) or
                 (pd.notna(max_ex6) and max_ex6 >= cfg.MIN_PEAK_EXCESS_6M))
    if not leader_ok:
        return False, features

    # Resilience
    lb = cfg.RESILIENCE_LOOKBACK_D
    if aligned.shape[0] < lb + 5:
        return False, features

    price_high = close_s.rolling(lb).max()
    rs_high = rs_line.rolling(lb).max()

    c_now = close_s.iloc[-1]
    rs_now = rs_line.iloc[-1]
    ph_now = price_high.iloc[-1]
    rsh_now = rs_high.iloc[-1]

    price_dd = 1.0 - _safe_div(c_now, ph_now, default=float("nan"))
    rs_dd = 1.0 - _safe_div(rs_now, rsh_now, default=float("nan"))

    rs_near_high_pct = _safe_div(rs_now, rsh_now, default=float("nan"))
    if pd.notna(rs_near_high_pct):
        features["rs_near_high_pct"] = float(rs_near_high_pct * 100.0)

    ratio = _safe_div(rs_dd, price_dd, default=float("inf"))
    if pd.notna(ratio):
        features["rs_dd_vs_price_dd"] = float(ratio)

    if pd.isna(price_dd) or not (cfg.MIN_PRICE_DD <= price_dd <= cfg.MAX_PRICE_DD):
        return False, features

    resilience_ok = (pd.notna(rs_near_high_pct) and rs_near_high_pct >= cfg.MIN_RS_NEAR_HIGH_PCT) and \
                    (pd.notna(ratio) and ratio <= cfg.MAX_RS_DD_TO_PRICE_DD_RATIO)
    if not resilience_ok:
        return False, features

    # Turn-up
    rs_ma20 = rs_line.rolling(cfg.RS_MA_LEN).mean()
    if rs_ma20.isna().iloc[-1]:
        return False, features

    rs_ma_now = rs_ma20.iloc[-1]
    rs_ma_prev = rs_ma20.shift(cfg.RS_SLOPE_LOOKBACK_D).iloc[-1]
    rs_ma_slope = _safe_div(rs_ma_now, rs_ma_prev, default=float("nan")) - 1.0

    if pd.notna(rs_ma_slope):
        features["rs_ma20_slope"] = float(rs_ma_slope * 100.0)

    turnup_ok = (rs_now > rs_ma_now) and (pd.notna(rs_ma_slope) and rs_ma_slope > cfg.MIN_RS_MA20_SLOPE)
    if not turnup_ok:
        return False, features

    return True, features

def yf_download_sync_wrapper(tickers, start, end):
    last_err = None
    for attempt in range(1, cfg.YF_MAX_RETRIES + 1):
        try:
            return yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
        except Exception as e:
            last_err = e
            import time
            time.sleep(cfg.YF_BACKOFF_BASE_SEC * attempt)
    if last_err:
        print(f"⚠️ yfinance download failed: {last_err}")
    return pd.DataFrame()

async def yf_download_with_retry(tickers, start, end):
    loop = asyncio.get_running_loop()
    df = await loop.run_in_executor(None, yf_download_sync_wrapper, tickers, start, end)
    return df

# --- C. VCP 策略檢查邏輯 ---
def check_vcp_criteria(df, qqq_close=None):
    if len(df) < 260: return False
    
    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    open_price = df['Open']
    
    current_c = close.iloc[-1]
    
    # 1. 基礎門檻
    if current_c < cfg.MIN_PRICE: return False
    
    avg_vol_20 = vol.tail(20).mean()
    dollar_vol = current_c * avg_vol_20
    if dollar_vol < cfg.MIN_DOLLAR_VOL_20D: return False

    # 2. 位階控制
    low_52w = low.tail(250).min()
    if current_c < (low_52w * cfg.LOW_52W_MULTIPLIER): return False

    # 3. 整理期判定
    high_60 = high.tail(60).max()
    low_60 = low.tail(60).min()
    consolidation_depth = (high_60 - low_60) / high_60
    if consolidation_depth > cfg.CONSOLIDATION_MAX_DEPTH_60D: return False

    # 4. 成交量 VDU
    avg_vol_3 = vol.tail(3).mean()
    if avg_vol_3 >= (avg_vol_20 * cfg.VDU_MAX_RATIO): return False

    # 5. VCP Tightness (Dynamic Gap) - 確保使用收盤價計算震幅
    check_days = cfg.VCP_TIGHT_DAYS
    recent_closes = close.tail(check_days).tolist()
    recent_opens = open_price.tail(check_days).tolist()
    
    gap_threshold = cfg.VCP_GAP_THRESHOLD
    valid_start_index = 0
    allowed_tightness = cfg.VCP_DEFAULT_TIGHTNESS
    
    for i in range(1, len(recent_closes)):
        prev_c = recent_closes[i-1]
        curr_o = recent_opens[i]
        curr_c = recent_closes[i]
        gap_magnitude = (curr_o - prev_c) / prev_c
        
        if gap_magnitude > gap_threshold:
            valid_start_index = i
            day_gain_magnitude = (curr_c - prev_c) / prev_c
            max_magnitude = max(gap_magnitude, day_gain_magnitude)
            # 有跳空時，動態放寬容許震幅
            allowed_tightness = math.ceil(max_magnitude * 100) / 100.0
            
    # 取出有效區間的「收盤價」序列
    adjusted_closes = recent_closes[valid_start_index:]
    
    if len(adjusted_closes) >= 2:
        # 震幅計算：(最高收盤價 - 最低收盤價) / 現價
        max_c = max(adjusted_closes)
        min_c = min(adjusted_closes)
        range_pct = (max_c - min_c) / current_c
        
        if range_pct > allowed_tightness: return False 

    # 6. RS Fallen Angel Gate
    if qqq_close is not None:
        rs_pass, _ = compute_fallen_angel_rs_features(df, qqq_close)
        if not rs_pass:
            return False

    # 7. 趨勢濾網 (Trend)
    sma50 = close.rolling(window=50).mean()
    sma200 = close.rolling(window=200).mean()
    
    if sma50.isna().iloc[-1] or sma200.isna().iloc[-1]: return False
    
    if current_c < sma50.iloc[-1]: return False
    if sma50.iloc[-1] < sma200.iloc[-1]: return False

    return True

# --- D. 單一股票診斷 (詳細報告) ---
def diagnose_single_stock(df, symbol, qqq_df=None):
    report = []
    is_pass = True
    df = df.dropna()
    
    if len(df) < 260:
        return False, f"❌ 資料不足 (< 260 days)"

    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    open_price = df['Open']
    c_now = close.iloc[-1]
    
    # 1. 基礎
    avg_vol_20 = vol.tail(20).mean()
    dollar_vol = c_now * avg_vol_20
    report.append(f"🔹 **基礎門檻**")
    if c_now >= 10: report.append(f"   ✅ 股價: ${c_now:.2f}")
    else:
        report.append(f"   ❌ 股價: ${c_now:.2f}")
        is_pass = False
    if dollar_vol >= 20000000: report.append(f"   ✅ 日均成交: ${dollar_vol/1000000:.1f}M")
    else:
        report.append(f"   ❌ 日均成交: ${dollar_vol/1000000:.1f}M")
        is_pass = False

    # 2. 位階
    low_52w = low.tail(250).min()
    dist_low = (c_now - low_52w) / low_52w
    report.append(f"\n🔹 **位階**")
    if c_now >= low_52w * 1.25: report.append(f"   ✅ >年低點: +{dist_low*100:.1f}%")
    else:
        report.append(f"   ❌ 離底太近: +{dist_low*100:.1f}%")
        is_pass = False

    # 3. 整理
    high_60 = high.tail(60).max()
    low_60 = low.tail(60).min()
    depth = (high_60 - low_60) / high_60
    report.append(f"\n🔹 **整理 (60d)**")
    if depth <= 0.30: report.append(f"   ✅ 深度: -{depth*100:.1f}%")
    else:
        report.append(f"   ❌ 過深: -{depth*100:.1f}%")
        is_pass = False

    # 4. VDU
    avg_vol_3 = vol.tail(3).mean()
    vdu_ratio = avg_vol_3 / avg_vol_20
    report.append(f"\n🔹 **VDU**")
    if vdu_ratio < 0.70: report.append(f"   ✅ 量縮: {vdu_ratio*100:.1f}%")
    else:
        report.append(f"   ❌ 未量縮: {vdu_ratio*100:.1f}%")
        is_pass = False

    # 5. VCP Tightness
    check_days = cfg.VCP_TIGHT_DAYS
    recent_closes = close.tail(check_days).tolist()
    recent_opens = open_price.tail(check_days).tolist()
    gap_threshold = cfg.VCP_GAP_THRESHOLD
    valid_start_index = 0
    allowed_tightness = 0.035
    gap_msg = ""
    
    for i in range(1, len(recent_closes)):
        prev_c = recent_closes[i-1]
        curr_o = recent_opens[i]
        curr_c = recent_closes[i]
        gap_mag = (curr_o - prev_c) / prev_c
        if gap_mag > gap_threshold:
            valid_start_index = i
            day_gain_mag = (curr_c - prev_c) / prev_c
            max_mag = max(gap_mag, day_gain_mag)
            allowed_tightness = math.ceil(max_mag * 100) / 100.0
            gap_msg = f"(Gap:{gap_mag*100:.1f}%)"
            
    adjusted_closes = recent_closes[valid_start_index:]
    
    report.append(f"\n🔹 **收斂 ({check_days}d)**")
    if valid_start_index>0: report.append(f"   ℹ️ 跳空 {gap_msg}")
    
    if len(adjusted_closes) >= 2:
        max_c = max(adjusted_closes)
        min_c = min(adjusted_closes)
        range_pct = (max_c - min_c) / c_now
        
        if range_pct <= allowed_tightness:
            report.append(f"   ✅ 10日震幅(收盤): {range_pct*100:.2f}% (Limit: {allowed_tightness*100:.1f}%)")
        else:
            report.append(f"   ❌ 10日震幅(收盤): {range_pct*100:.2f}%")
            is_pass = False

    # 6. RS
    if qqq_df is not None:
        rs_pass, feat = compute_fallen_angel_rs_features(df, qqq_df['Close'])
        report.append(f"\n🔹 **RS Gate**")
        lp = feat.get('leader_peak_excess')
        nh = feat.get('rs_near_high_pct')
        ratio = feat.get('rs_dd_vs_price_dd')
        slope = feat.get('rs_ma20_slope')
        report.append(f"   • Peak Exc: {lp:.2f}%" if pd.notna(lp) else "   • Peak Exc: N/A")
        report.append(f"   • Near High: {nh:.2f}%" if pd.notna(nh) else "   • Near High: N/A")
        report.append(f"   • DD Ratio: {ratio:.2f}" if pd.notna(ratio) else "   • DD Ratio: N/A")
        
        if rs_pass: report.append("   ✅ PASS")
        else:
            report.append("   ❌ FAIL")
            is_pass = False

    # 7. Trend
    sma50 = close.rolling(window=50).mean().iloc[-1]
    sma200 = close.rolling(window=200).mean().iloc[-1]
    
    if c_now > sma50 and sma50 > sma200:
        report.append(f"   ✅ 多頭 (P > 50 > 200)")
    else:
        report.append(f"   ❌ 趨勢不符")
        is_pass = False

    return is_pass, "\n".join(report)

# --- E. 掃描執行 ---
async def scan_market(target_date_str):
    try:
        # 如果沒有傳入 target_date_str，預設使用現在時間
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
        # 回測設定
        start_date = target_date - timedelta(days=cfg.HIST_CALENDAR_DAYS)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        print(f"🚀 開始掃描: {formatted_date}")

        qqq_data = await yf_download_with_retry(cfg.BENCH_SYMBOL, start=start_date, end=end_date)
        if qqq_data.empty:
            print("❌ 無法取得 QQQ 資料")
            return [], formatted_date

        qqq_close = qqq_data['Close'] if not isinstance(qqq_data.columns, pd.MultiIndex) else qqq_data['Close'][cfg.BENCH_SYMBOL]
        qqq_close = qqq_close.dropna()

        # [UPDATED] 使用新的函式獲取 全市場 (NASDAQ + NYSE) 清單
        tickers = get_all_us_stocks()
        
        batch_size = cfg.YF_BATCH_SIZE 
        rows = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                data = await yf_download_with_retry(batch, start=start_date, end=end_date)
                if data.empty: continue

                for symbol in batch:
                    try:
                        if symbol not in data.columns.levels[0]: continue
                        
                        df = data[symbol].copy()
                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        # 日期檢查
                        last_dt = df.index[-1].date()
                        if abs((last_dt - target_date.date()).days) > 1: continue
                        
                        if check_vcp_criteria(df, qqq_close):
                            rs_pass, feat = compute_fallen_angel_rs_features(df, qqq_close)
                            rows.append({
                                'Symbol': symbol,
                                'leader_peak_excess': feat.get('leader_peak_excess'),
                                'rs_near_high%': feat.get('rs_near_high_pct'),
                                'rs_dd_vs_price_dd': feat.get('rs_dd_vs_price_dd'),
                                'RS_ma20_slope': feat.get('rs_ma20_slope'),
                            })
                    except: continue
                
                del data
                gc.collect()
                await asyncio.sleep(cfg.YF_SLEEP_BETWEEN_BATCH_SEC)
            except Exception as e:
                print(f"Batch Error: {e}")
                continue

        return rows, formatted_date

    except Exception as e:
        traceback.print_exc()
        return [], target_date_str

# --- F. 單一診斷入口 ---
async def fetch_and_diagnose(symbol_input, date_str):
    try:
        target_date = datetime.strptime(date_str, "%y%m%d")
        start_date = target_date - timedelta(days=cfg.HIST_CALENDAR_DAYS)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        symbol = symbol_input.upper().strip().replace(".", "-")

        data = await yf_download_with_retry([symbol, cfg.BENCH_SYMBOL], start=start_date, end=end_date)
        
        if symbol not in data.columns.levels[0]:
            return False, f"❌ 找不到: {symbol}", formatted_date
            
        df_stock = data[symbol].dropna()
        df_qqq = data[cfg.BENCH_SYMBOL].dropna()

        is_pass, report = diagnose_single_stock(df_stock, symbol, df_qqq)
        header = f"🔍 **診斷報告: {symbol}**\n📅 {formatted_date}\n" + "-"*20 + "\n"
        return is_pass, header + report, formatted_date

    except Exception as e:
        return False, f"Error: {e}", date_str
