import logging
import pytz
import pandas as pd
from datetime import datetime
import requests
import io

# 設定 Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def get_market_tickers():
    """
    獲取市場股票清單。
    為了穩定性，這裡組合了 S&P 500 和 Nasdaq 100 以及其他來源。
    如果要掃描全美股，建議從 NASDAQ FTP 或其他 API 定期更新 CSV，
    但為了避免 yfinance 過載，建議先從主要指數成分股開始。
    """
    tickers = set()
    
    try:
        # 1. 抓取 S&P 500
        logger.info("正在抓取 S&P 500 成分股...")
        payload = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')
        sp500 = payload[0]['Symbol'].values.tolist()
        tickers.update(sp500)
        
        # 2. 抓取 Nasdaq 100
        logger.info("正在抓取 Nasdaq 100 成分股...")
        payload_ndx = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')
        ndx100 = payload_ndx[0]['Ticker'].values.tolist()
        tickers.update(ndx100)

        # 3. 如果需要更多，可以考慮在這裡加入讀取本地 tickers.txt 的邏輯
        # with open('tickers.txt', 'r') as f:
        #     custom_tickers = [line.strip() for line in f if line.strip()]
        #     tickers.update(custom_tickers)

    except Exception as e:
        logger.error(f"獲取股票清單失敗: {e}")
        # 如果爬蟲失敗，回傳一個保底清單 (範例)
        return ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "AMZN"]

    # 清理 ticker (有些來源會有 . 替換為 -)
    clean_tickers = [t.replace('.', '-') for t in tickers]
    return list(set(clean_tickers))

def is_market_open(timezone_str):
    """檢查目前美股是否開盤 (簡單判斷週間與時間)"""
    tz = pytz.timezone(timezone_str)
    now = datetime.now(tz)
    
    # 週末
    if now.weekday() >= 5:
        return False
    
    # 時間判斷 (09:30 - 16:00)
    market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    
    return market_start <= now <= market_end

def get_current_est_time(timezone_str):
    tz = pytz.timezone(timezone_str)
    return datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')
