import logging
import pytz
from telegram.ext import Application, CommandHandler
from config import TELEGRAM_TOKEN, ALLOWED_USER_ID, MARKET_CLOSE_HOUR, MARKET_TIMEZONE
from bot import start, now, scheduled_job
from datetime import time

# 設定日誌格式
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 關鍵修改：將 httpx 的日誌等級調高到 WARNING ---
# 這樣可以隱藏正常的 HTTP 請求日誌 (GET/POST 200 OK)，只顯示錯誤
logging.getLogger("httpx").setLevel(logging.WARNING)

def main():
    if not TELEGRAM_TOKEN:
        logger.error("未設定 TELEGRAM_TOKEN，程式結束。")
        return

    # 建立 Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # 加入指令處理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("now", now))

    # --- 設定排程任務 ---
    job_queue = application.job_queue
    
    if ALLOWED_USER_ID:
        # 設定在美東時間收盤後執行 (例如收盤後 15 分鐘：16:15)
        est_tz = pytz.timezone(MARKET_TIMEZONE)
        target_time = time(hour=MARKET_CLOSE_HOUR, minute=15, tzinfo=est_tz)
        
        # 每天執行一次
        job_queue.run_daily(scheduled_job, target_time, chat_id=int(ALLOWED_USER_ID), name='daily_scan')
        logger.info(f"排程已設定：每天美東時間 {target_time} 執行")
    else:
        logger.warning("未設定 ALLOWED_USER_ID，自動排程無法啟動")

    # 啟動 Bot
    logger.info("Bot 正在啟動... (httpx 日誌已隱藏)")
    application.run_polling()

if __name__ == '__main__':
    main()
