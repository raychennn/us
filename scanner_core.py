import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
import traceback
import io
import math  # 用於無條件進位計算
from datetime import datetime, timedelta

# 策略參數
import config as cfg

# --- A. 自動獲取 NASDAQ 清單 (嚴格過濾版) ---
def get_nasdaq_stock_list():
    """
    從 NASDAQ 獲取清單，並嚴格過濾 ETF, ADR, 權證, 特別股
    """
    try:
        url = "http://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
        s = requests.get(url).content
        df = pd.read_csv(io.BytesIO(s), sep="|")
        
        # 1. 基礎清洗
        df = df.dropna(subset=['Symbol'])
        df = df[df['Test Issue'] == 'N'] # 排除測試代碼
        
        # 2. 排除 ETF
        if 'ETF' in df.columns:
            df = df[df['ETF'] == 'N']
            
        # 3. 利用名稱排除 ADR, 特別股, 權證
        # 轉大寫以利比對
        df['Security Name'] = df['Security Name'].str.upper()
        
        # 定義排除關鍵字
        exclude_keywords = [
            ' ADR ', ' ADS ', ' DEPOSITARY ', # ADR 相關
            ' PREFERRED ', ' PFD ',           # 特別股
            ' WARRANT ', ' WTS ', ' UNIT ',   # 權證與單位
            ' RIGHTS ',                       # 認股權
            ' ACQUISITION '                   # SPAC 相關
        ]
        
        for kw in exclude_keywords:
            df = df[~df['Security Name'].str.contains(kw, na=False)]

        # 4. 符號長度過濾 (NASDAQ 通常 4 碼)
        full_list = df['Symbol'].tolist()
        
        # 清除包含非字母的符號
        clean_list = [x for x in full_list if x.isalpha()]
        
        print(f"✅ 成功獲取 {len(clean_list)} 檔 NASDAQ 本土股票 (已排除 ETF/ADR/權證)")
        return clean_list 
        
    except Exception as e:
        print(f"❌ 獲取 NASDAQ 清單失敗: {e}")
        # 備案：回傳大型科技股
        return ['AAPL', 'MSFT', 'AMZN', 'NVDA', 'TSLA', 'META', 'AMD', 'NFLX', 'GOOGL', 'AVGO']

# --- B. 輔助計算: RS Score ---
def calculate_performance_score(close_series):
    """計算 IBD 風格的 RS Score (40%/20%/20%/20%)"""
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
    """\
    Fallen Angel RS Gate（vs QQQ）。

    回傳:
      (pass_gate: bool, features: dict)

    features 會包含你指定的欄位：
      - leader_peak_excess（%）
      - rs_near_high_pct（%）
      - rs_dd_vs_price_dd（ratio）
      - rs_ma20_slope（%）
    """
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

    # RS line
    rs_line = close_s / close_b

    # ---------- A) Leader Peak ----------
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

    # ---------- B) Resilience ----------
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

    # 價格回撤區間
    if pd.isna(price_dd) or not (cfg.MIN_PRICE_DD <= price_dd <= cfg.MAX_PRICE_DD):
        return False, features

    resilience_ok = (pd.notna(rs_near_high_pct) and rs_near_high_pct >= cfg.MIN_RS_NEAR_HIGH_PCT) and \
                    (pd.notna(ratio) and ratio <= cfg.MAX_RS_DD_TO_PRICE_DD_RATIO)
    if not resilience_ok:
        return False, features

    # ---------- C) Turn-up ----------
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


def yf_download_with_retry(tickers, start, end, **kwargs):
    """yfinance.download 包一層 retry + exponential backoff，避免偶發網路/節流失敗。"""
    last_err = None
    for attempt in range(1, cfg.YF_MAX_RETRIES + 1):
        try:
            return yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True, **kwargs)
        except Exception as e:
            last_err = e
            wait = cfg.YF_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            print(f"⚠️ yfinance 下載失敗 (attempt {attempt}/{cfg.YF_MAX_RETRIES}): {e} ; sleep {wait:.1f}s")
            try:
                import time
                time.sleep(wait)
            except Exception:
                pass
    if last_err:
        raise last_err
    return pd.DataFrame()


# --- C. VCP 策略檢查邏輯 (含 Dynamic Gap Reset & 10天視窗) ---
def check_vcp_criteria(df, qqq_close=None):
    """
    回傳 True/False
    """
    # 0. 資料長度 (需 > 260 天算 RS 與 52週低點)
    if len(df) < 260: return False
    
    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    open_price = df['Open'] # 需獲取 Open 計算跳空
    
    current_c = close.iloc[-1]
    
    # --- 1. 基礎門檻 (Basic Filters) ---
    # 股價 > 10 美元
    if current_c < cfg.MIN_PRICE: return False
    
    # 流動性 > 2000 萬美元 (使用 20日均量計算)
    avg_vol_20 = vol.tail(20).mean()
    dollar_vol = current_c * avg_vol_20
    if dollar_vol < cfg.MIN_DOLLAR_VOL_20D: return False # 20M USD

    # --- 2. 位階控制 (Relative Position) ---
    # 股價需高於 52 週 (250天) 最低價的 25%
    low_52w = low.tail(250).min()
    if current_c < (low_52w * cfg.LOW_52W_MULTIPLIER): return False

    # --- 3. 整理期判定 (Consolidation Logic) ---
    # 過去 60 日內的高低點落差不得超過 30%
    high_60 = high.tail(60).max()
    low_60 = low.tail(60).min()
    consolidation_depth = (high_60 - low_60) / high_60
    if consolidation_depth > cfg.CONSOLIDATION_MAX_DEPTH_60D: return False

    # --- 4. 成交量 VDU (Volume Dry-Up) ---
    # 近 3 日平均成交量 < 近 20 日平均成交量 * 70%
    avg_vol_3 = vol.tail(3).mean()
    if avg_vol_3 >= (avg_vol_20 * cfg.VDU_MAX_RATIO): return False

    # --- 5. VCP Tightness (Dynamic Gap Tolerance - 10 Days) ---
    # 檢查近 10 天 (原為5天，改為10天以涵蓋完整旗型)
    check_days = cfg.VCP_TIGHT_DAYS
    recent_closes = close.tail(check_days).tolist()
    recent_opens = open_price.tail(check_days).tolist()
    
    gap_threshold = cfg.VCP_GAP_THRESHOLD # 觸發判定的跳空門檻
    valid_start_index = 0
    allowed_tightness = cfg.VCP_DEFAULT_TIGHTNESS # 預設容許震幅 3.5%
    
    for i in range(1, len(recent_closes)):
        prev_c = recent_closes[i-1]
        curr_o = recent_opens[i]
        curr_c = recent_closes[i]
        
        # A. 更新跳空判斷: Open vs Prev Close
        gap_magnitude = (curr_o - prev_c) / prev_c
        
        if gap_magnitude > gap_threshold:
            valid_start_index = i # 重置起點至跳空當天
            
            # B. 計算當日漲幅 (Close vs Prev Close)
            day_gain_magnitude = (curr_c - prev_c) / prev_c
            
            # C. 取兩者較大值
            max_magnitude = max(gap_magnitude, day_gain_magnitude)
            
            # D. 無條件進位至整數百分比 (例如 9.1% -> 10% -> 0.10)
            allowed_tightness = math.ceil(max_magnitude * 100) / 100.0
            
    adjusted_closes = recent_closes[valid_start_index:]
    
    # 只有一根K線無法算收斂，視為通過
    if len(adjusted_closes) < 2:
        pass 
    else:
        max_c = max(adjusted_closes)
        min_c = min(adjusted_closes)
        # 震幅算法：(高-低) / 最新價
        range_pct = (max_c - min_c) / current_c
        
        # 使用動態計算的 allowed_tightness 進行過濾
        if range_pct > allowed_tightness: return False 

    # --- 6. RS Fallen Angel Gate (vs QQQ) ---
    if qqq_close is not None:
        rs_pass, _ = compute_fallen_angel_rs_features(df, qqq_close)
        if not rs_pass:
            return False

    # --- 7. 趨勢濾網 (Trend) ---
    # 股價 > 50MA > 200MA
    sma50 = ta.sma(close, length=50)
    sma200 = ta.sma(close, length=200)
    if sma50 is None or sma200 is None: return False
    
    # 確保 50MA 與 200MA 趨勢正確
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
    
    # 1. 基礎與流動性
    avg_vol_20 = vol.tail(20).mean()
    dollar_vol = c_now * avg_vol_20
    
    report.append(f"🔹 **基礎門檻**")
    if c_now >= 10:
        report.append(f"   ✅ 股價: ${c_now:.2f} (>= $10)")
    else:
        report.append(f"   ❌ 股價: ${c_now:.2f} (< $10)")
        is_pass = False
        
    if dollar_vol >= 20000000:
        report.append(f"   ✅ 日均成交額: ${dollar_vol/1000000:.1f}M (>= $20M)")
    else:
        report.append(f"   ❌ 日均成交額: ${dollar_vol/1000000:.1f}M (< $20M)")
        is_pass = False

    # 2. 位階控制
    low_52w = low.tail(250).min()
    dist_low = (c_now - low_52w) / low_52w
    report.append(f"\n🔹 **位階 (vs 52W Low)**")
    if c_now >= low_52w * 1.25:
        report.append(f"   ✅ 高於年低點: +{dist_low*100:.1f}% (>= 25%)")
    else:
        report.append(f"   ❌ 離底太近: +{dist_low*100:.1f}% (< 25%)")
        is_pass = False

    # 3. 整理型態
    high_60 = high.tail(60).max()
    low_60 = low.tail(60).min()
    depth = (high_60 - low_60) / high_60
    report.append(f"\n🔹 **整理型態 (60天內)**")
    if depth <= 0.30:
        report.append(f"   ✅ 修正幅度: -{depth*100:.1f}% (<= 30%)")
    else:
        report.append(f"   ❌ 波動過大: -{depth*100:.1f}% (> 30%)")
        is_pass = False

    # 4. VDU (Volume Dry-Up)
    avg_vol_3 = vol.tail(3).mean()
    vdu_ratio = avg_vol_3 / avg_vol_20
    report.append(f"\n🔹 **成交量 VDU**")
    if vdu_ratio < 0.70:
        report.append(f"   ✅ 量縮顯著: {vdu_ratio*100:.1f}% (Target < 70%)")
    else:
        report.append(f"   ❌ 未見量縮: {vdu_ratio*100:.1f}% (> 70%)")
        is_pass = False

    # 5. VCP Tightness (Dynamic Gap Logic - 10 Days)
    check_days = cfg.VCP_TIGHT_DAYS
    recent_closes = close.tail(check_days).tolist()
    recent_opens = open_price.tail(check_days).tolist()
    
    gap_threshold = cfg.VCP_GAP_THRESHOLD
    valid_start_index = 0
    allowed_tightness = 0.035 # Default
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
            gap_msg = f"(Gap: {gap_mag*100:.1f}%, Allow: {allowed_tightness*100:.0f}%)"
            
    adjusted_closes = recent_closes[valid_start_index:]
    max_c = max(adjusted_closes)
    min_c = min(adjusted_closes)
    range_pct = (max_c - min_c) / c_now
    
    report.append(f"\n🔹 **收斂度 (Dynamic Gap, 10 Days)**")
    if valid_start_index > 0:
        report.append(f"   ℹ️ 偵測到跳空 {gap_msg}")
        
    if range_pct <= allowed_tightness:
        report.append(f"   ✅ 10日震幅: {range_pct*100:.2f}% (<= {allowed_tightness*100:.1f}%)")
    else:
        report.append(f"   ❌ 震幅過大: {range_pct*100:.2f}% (> {allowed_tightness*100:.1f}%)")
        is_pass = False

    # 6. RS (Fallen Angel) & Trend
    if qqq_df is not None and not qqq_df.empty:
        rs_pass, feat = compute_fallen_angel_rs_features(df, qqq_df['Close'])
        report.append(f"\n🔹 **Fallen Angel RS (vs QQQ)**")
        lp = feat.get('leader_peak_excess')
        nh = feat.get('rs_near_high_pct')
        ratio = feat.get('rs_dd_vs_price_dd')
        slope = feat.get('rs_ma20_slope')
        report.append(f"   • leader_peak_excess: {lp:.2f}%" if pd.notna(lp) else "   • leader_peak_excess: N/A")
        report.append(f"   • rs_near_high%: {nh:.2f}%" if pd.notna(nh) else "   • rs_near_high%: N/A")
        report.append(f"   • rs_dd_vs_price_dd: {ratio:.3f}" if pd.notna(ratio) else "   • rs_dd_vs_price_dd: N/A")
        report.append(f"   • RS_ma20_slope: {slope:.2f}%" if pd.notna(slope) else "   • RS_ma20_slope: N/A")

        if rs_pass:
            report.append("   ✅ RS Gate: PASS")
        else:
            report.append("   ❌ RS Gate: FAIL")
            is_pass = False

    sma50 = ta.sma(close, length=50).iloc[-1]
    sma200 = ta.sma(close, length=200).iloc[-1]
    
    if c_now > sma50 and sma50 > sma200:
        report.append(f"   ✅ 多頭排列 (P > 50MA > 200MA)")
    else:
        report.append(f"   ❌ 趨勢不符")
        is_pass = False

    return is_pass, "\n".join(report)

# --- E. 掃描執行 ---
async def scan_market(target_date_str):
    try:
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
        start_date = target_date - timedelta(days=cfg.HIST_CALENDAR_DAYS)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        print(f"🚀 開始掃描: {formatted_date}")

        # 1. 基準 QQQ（Bench）
        qqq_data = yf_download_with_retry(cfg.BENCH_SYMBOL, start=start_date, end=end_date)
        if qqq_data.empty:
            print("❌ 無法取得 QQQ 資料")
            return [], formatted_date

        qqq_close = qqq_data['Close'] if not isinstance(qqq_data.columns, pd.MultiIndex) else qqq_data['Close'][cfg.BENCH_SYMBOL]
        qqq_close = qqq_close.dropna()

        # 2. 獲取並過濾清單
        tickers = get_nasdaq_stock_list()
        
        batch_size = cfg.YF_BATCH_SIZE 
        rows = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                data = yf_download_with_retry(batch, start=start_date, end=end_date, group_by='ticker', threads=True)
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

        data = yf_download_with_retry([symbol, cfg.BENCH_SYMBOL], start=start_date, end=end_date, group_by='ticker')
        
        if symbol not in data.columns.levels[0]:
            return False, f"❌ 找不到: {symbol}", formatted_date
            
        df_stock = data[symbol].dropna()
        df_qqq = data[cfg.BENCH_SYMBOL].dropna()

        is_pass, report = diagnose_single_stock(df_stock, symbol, df_qqq)
        header = f"🔍 **診斷報告: {symbol}**\n📅 {formatted_date}\n" + "-"*20 + "\n"
        return is_pass, header + report, formatted_date

    except Exception as e:
        return False, f"Error: {e}", date_str
