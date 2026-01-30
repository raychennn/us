import os
import io
import asyncio
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_TOKEN, ALLOWED_USER_ID, MARKET_TIMEZONE
from utils import get_current_est_time, is_market_open
from strategy import run_scanner
import logging

# --- 關鍵修正：強制將 httpx 的日誌等級調高到 WARNING ---
# 這會隱藏所有 HTTP 200 OK 的連線紀錄，只顯示錯誤
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logger.info(f"收到 /start 指令，來自 User ID: {user_id}")
    
    if user_id != ALLOWED_USER_ID:
        await update.message.reply_text(f"⛔ 未授權的使用者 (ID: {user_id})。請確認 config 設定。")
        return
    await update.message.reply_text(f"🚀 美股 RS/VCP 掃描機器人已啟動！\n目前美東時間: {get_current_est_time(MARKET_TIMEZONE)}\n輸入 /now 立即掃描。")

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logger.info(f"收到 /now 指令，來自 User ID: {user_id}")
    
    # 1. 權限檢查與回饋
    if user_id != ALLOWED_USER_ID:
        await update.message.reply_text(f"⛔ 抱歉，您沒有權限執行此操作 (您的 ID: {user_id})。")
        return

    # 2. 立即發送「收到指令」訊息
    status_msg = await update.message.reply_text("🤖 指令已接收，正在啟動掃描程序...\n(掃描全市場約需數分鐘，請勿重複點擊)")
    
    try:
        # 3. 執行掃描 (在背景執行緒)
        loop = asyncio.get_running_loop()
        # 更新訊息狀態
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text="🔍 正在下載數據與計算 VCP 型態...\n進度：0% (初始化)")
        
        results = await loop.run_in_executor(None, run_scanner)
        
        if not results:
            await status_msg.edit_text("❌ 本次掃描無符合條件的股票。")
            return
            
        # 4. 製作文字報告
        msg = f"📊 **掃描結果 ({len(results)})**\n"
        msg += f"Time: {get_current_est_time(MARKET_TIMEZONE)}\n\n"
        
        # 只顯示前 15 檔
        for item in results[:15]:
            msg += f"🔹 `{item['Ticker']}`: {item['Price']}$ | {item['Pattern']}\n"
            
        if len(results) > 15:
            msg += f"\n...還有 {len(results)-15} 檔，請查看檔案。"
            
        await status_msg.edit_text(msg, parse_mode='Markdown')
        
        # 5. 傳送 TradingView 檔案
        tv_list = ",".join([f"{r['Ticker']}" for r in results])
        file_buffer = io.BytesIO(tv_list.encode('utf-8'))
        file_buffer.name = f"watchlist_{get_current_est_time(MARKET_TIMEZONE)[:10]}.txt"
        
        await context.bot.send_document(chat_id=update.effective_chat.id, document=file_buffer, caption="📂 TradingView 匯入清單")

    except Exception as e:
        logger.error(f"掃描執行錯誤: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ 發生內部錯誤: {str(e)}")

# 排程任務
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id=chat_id, text="⏰ 收盤自動掃描開始...")
    
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, run_scanner)
        
        if results:
            tv_list = ",".join([f"{r['Ticker']}" for r in results])
            file_buffer = io.BytesIO(tv_list.encode('utf-8'))
            file_buffer.name = f"watchlist_daily.txt"
            
            await context.bot.send_message(chat_id=chat_id, text=f"📊 自動掃描完成，共 {len(results)} 檔。")
            await context.bot.send_document(chat_id=chat_id, document=file_buffer)
        else:
            await context.bot.send_message(chat_id=chat_id, text="📊 自動掃描完成，無標的。")
            
    except Exception as e:
        logger.error(f"排程錯誤: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 排程執行失敗: {e}")
