import os

# --- Telegram 設定 ---
# 請在 Zeabur 的 Environment Variables 設定這些值，不要直接寫死在程式碼上傳 Github
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "YOUR_USER_ID_HERE")  # 限制只有你能用，填入你的 ID (數字)

# --- 基礎濾網 (Universe) ---
MIN_PRICE = 15.0
MIN_AVG_VOLUME_SHARES = 500000        # 50日均量 (股)
MIN_AVG_VOLUME_DOLLAR = 20000000      # 50日均成交額 (美元)
EXCLUDE_ETFS = True                   # 盡量排除 ETF (依賴 yfinance quoteType)

# --- RS Line 設定 ---
BENCHMARK_TICKER = "SPY"
RS_LOOKBACK_DAYS = 126                # 6個月 (約126交易日)
RS_NEAR_HIGH_THRESHOLD = 0.98         # 距離 RS 新高 2% 內

# --- 價格位置設定 ---
PRICE_NEAR_HIGH_THRESHOLD = 0.90      # 價格距離 52週新高 10% 內
PP_PRICE_NEAR_HIGH_THRESHOLD = 0.93   # Power Play 模式下更嚴格

# --- VCP 策略參數 ---
VCP_MAX_DRAWDOWN = 0.25               # 最大回撤 < 25%
VCP_VOL_DRY_UP_10 = 0.8               # 10日均量 < 50日均量 * 0.8
VCP_VOL_DRY_UP_5 = 0.7                # 5日均量 < 50日均量 * 0.7
VCP_TIGHT_CLOSE_PCT = 0.05            # 最近5日收盤區間 < 5%

# --- Power Play (High Tight Flag) 參數 ---
PP_RUN_UP_WEEKS_MIN = 4
PP_RUN_UP_WEEKS_MAX = 8
PP_RUN_UP_PCT_MIN = 0.40              # 4-8週內漲幅至少 40%
PP_RUN_UP_PCT_MAX = 0.70              # (選用) 漲幅上限，根據定義有些不設上限
PP_CONSOLIDATION_WEEKS_MIN = 2
PP_CONSOLIDATION_WEEKS_MAX = 5
PP_DRAWDOWN_MAX = 0.25                # 旗面回撤 <= 25%

# --- 系統運行設定 ---
BATCH_SIZE = 50                       # 每次向 yfinance 請求的股票數量 (太高會被封，太低太慢)
BATCH_DELAY = 1.5                     # 每批次間隔秒數 (防封鎖)
MARKET_CLOSE_HOUR = 16                # 美股收盤時間 (24小時制)
MARKET_TIMEZONE = "America/New_York"  # 關鍵：自動處理冬夏令
