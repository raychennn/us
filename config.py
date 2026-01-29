# config.py
# 統一管理策略參數（Fallen Angel RS + VCP/Trend）

# --- 資料抓取 ---
BENCH_SYMBOL = "QQQ"
HIST_CALENDAR_DAYS = 650          # 抓 650 個日曆天
YF_BATCH_SIZE = 80                # 每批抓取 ticker 數
YF_SLEEP_BETWEEN_BATCH_SEC = 0.6  # 每批間隔（避免被 Yahoo 節流）
YF_MAX_RETRIES = 3
YF_BACKOFF_BASE_SEC = 1.2         # retry 指數退避基準秒數

# --- TradingView 輸出 ---
TRADINGVIEW_PREFIX = "NASDAQ:"    # TradingView 建議帶 exchange 前綴；不需要可改成 ""
TRADINGVIEW_BLOCK_SEPARATOR = "\n\n"  # 區塊間隔（每個 symbol 中間空一行）

# --- Fallen Angel RS Gate（三段） ---
# Leader Peak（曾是 leader：曾有顯著超額報酬）
LEADER_PEAK_LOOKBACK_D = 126      # 往回看多久去找「峰值」
MIN_PEAK_EXCESS_3M = 0.15         # 3M 超額報酬峰值 >= 15%
MIN_PEAK_EXCESS_6M = 0.25         # 6M 超額報酬峰值 >= 25%

# Resilience（回調期 RS 抗跌）
RESILIENCE_LOOKBACK_D = 126
MIN_RS_NEAR_HIGH_PCT = 0.92
MAX_RS_DD_TO_PRICE_DD_RATIO = 0.60
MIN_PRICE_DD = 0.05
MAX_PRICE_DD = 0.35

# Turn-up（第二波前兆：RS 重新轉強）
RS_MA_LEN = 20
RS_SLOPE_LOOKBACK_D = 5
MIN_RS_MA20_SLOPE = 0.0  # >0 代表 RS_ma20 重新走強

# --- VCP/Trend（維持原邏輯，只把參數集中） ---
MIN_PRICE = 10
MIN_DOLLAR_VOL_20D = 20_000_000
LOW_52W_MULTIPLIER = 1.25
CONSOLIDATION_MAX_DEPTH_60D = 0.30
VDU_MAX_RATIO = 0.70

VCP_TIGHT_DAYS = 10
VCP_GAP_THRESHOLD = 0.04
VCP_DEFAULT_TIGHTNESS = 0.035
