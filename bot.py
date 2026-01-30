import os
import io
import asyncio
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_TOKEN, ALLOWED_USER_ID, MARKET_TIMEZONE
from utils import get_current_est_time, is_market_open
from strategy import run_scanner
import logging

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ 未授權的使用者。")
        return
    await update.message.reply_text(f"🚀 美股 RS/VCP 掃描機器人已啟動！\n目前美東時間: {get_current_est_time(MARKET_TIMEZONE)}\n輸入 /now 立即掃描。")

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ALLOWED_USER_ID:
        return

    # 防呆：如果市場開盤中，數據可能不準 (yfinance 延遲)
    # if is_market_open(MARKET_TIMEZONE):
    #     await update.message.reply_text("⚠️ 警告：目前美股尚未收盤，數據可能不完整或有延遲。")
    
    status_msg = await update.message.reply_text("🔍 開始掃描全市場... 這可能需要幾分鐘，請稍候。")
    
    try:
        # 在另一個 thread 執行掃描以免卡死 Bot
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, run_scanner)
        
        if not results:
            await status_msg.edit_text("❌ 本次掃描無符合條件的股票。")
            return
            
        # 1. 製作文字報告
        msg = f"📊 **掃描結果 ({len(results)})**\n"
        msg += f"Time: {get_current_est_time(MARKET_TIMEZONE)}\n\n"
        
        # 只顯示前 15 檔以免訊息過長
        for item in results[:15]:
            msg += f"🔹 `{item['Ticker']}`: {item['Price']}$ | {item['Pattern']}\n"
            
        if len(results) > 15:
            msg += f"\n...還有 {len(results)-15} 檔，請查看檔案。"
            
        await status_msg.edit_text(msg, parse_mode='Markdown')
        
        # 2. 製作 TradingView 匯入檔 (TXT)
        # 格式: NASDAQ:AAPL,NYSE:TSLA,...
        # 簡單起見，統一加個前綴或只給 Ticker (TV 通常能自動辨識)
        tv_list = ",".join([f"{r['Ticker']}" for r in results])
        
        file_buffer = io.BytesIO(tv_list.encode('utf-8'))
        file_buffer.name = f"watchlist_{get_current_est_time(MARKET_TIMEZONE)[:10]}.txt"
        
        await context.bot.send_document(chat_id=update.effective_chat.id, document=file_buffer, caption="📂 TradingView 匯入清單")

    except Exception as e:
        logger.error(f"掃描執行錯誤: {e}")
        await status_msg.edit_text(f"❌ 發生錯誤: {str(e)}")

# 用於排程任務的包裝函式
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id=chat_id, text="⏰ 收盤自動掃描開始...")
    
    # 這裡直接呼叫邏輯，複製上面 /now 的部分邏輯比較好，或是抽取出來
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
