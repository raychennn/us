import os
import io
import asyncio
import logging
import pytz
from datetime import datetime

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

# 引入更新後的核心邏輯
from scanner_core import scan_market, fetch_and_diagnose

load_dotenv()
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 背景任務 (共用) ---
async def run_full_scan_background(chat_id, context, date_str, formatted_date_msg):
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"🇺🇸 正在執行 {formatted_date_msg} NASDAQ 全市場掃描 (VCP + RS強於QQQ)...\n⏳ 約需 3-5 分鐘")
        
        results, formatted_date = await scan_market(date_str)
        
        if not results:
            await context.bot.send_message(chat_id=chat_id, text=f"📉 {formatted_date} 掃描無符合標的。")
            return

        file_content = "\n".join(results)
        bio = io.BytesIO(file_content.encode('utf-8'))
        bio.name = f"NASDAQ_VCP_{formatted_date.replace('-','')}.txt"
        
        caption = (f"✅ **{formatted_date} 美股掃描完成**\n"
                   f"🎯 篩選標準: VCP + RS > QQQ + Price>$10\n"
                   f"共篩選出 {len(results)} 檔標的")

        await context.bot.send_document(
            chat_id=chat_id,
            document=bio,
            caption=caption,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 掃描失敗: {e}")

async def run_diagnostic_background(chat_id, status_message_id, date_str, symbol, context):
    try:
        is_pass, report, formatted_date = await fetch_and_diagnose(symbol, date_str)
        
        if len(report) > 4000: report = report[:4000] + "\n...(截斷)"
        
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_message_id, 
            text=report, 
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Diagnostic failed: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_message_id, 
            text=f"❌ 錯誤: {e}"
        )

# --- 指令處理 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗽 **美股 VCP 狙擊手**\n\n"
        "1. `/now`: 立即掃描 (NASDAQ)\n"
        "2. `/231225`: 回測特定日期\n"
        "3. `/231225 NVDA`: 診斷特定個股"
    )

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🚀 啟動美股掃描...")
    asyncio.create_task(run_scan_task_wrapper(update.effective_chat.id, msg.message_id, None, context))

async def history_scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.replace('/', '').strip()
    msg = await update.message.reply_text(f"⏳ 準備回測: {date_str}...")
    asyncio.create_task(run_scan_task_wrapper(update.effective_chat.id, msg.message_id, date_str, context))

async def diagnostic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace('/', '').strip() 
    parts = text.split()
    if len(parts) < 2: return
    
    date_str, symbol = parts[0], parts[1]
    msg = await update.message.reply_text(f"👨‍⚕️ 診斷中: {symbol}...")
    
    asyncio.create_task(
        run_diagnostic_background(update.effective_chat.id, msg.message_id, date_str, symbol, context)
    )

async def run_scan_task_wrapper(chat_id, msg_id, date_str, context):
    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    await run_full_scan_background(chat_id, context, date_str, date_str if date_str else "Today")

# --- 排程任務 (美股收盤時間) ---
async def scheduled_daily_scan(app):
    while True:
        # 設定為美東時間
        tz_ny = pytz.timezone('America/New_York')
        now_ny = datetime.now(tz_ny)
        
        # 美股收盤通常是 16:00, 設定 16:15 執行
        if now_ny.hour == 16 and now_ny.minute == 15:
            if TG_CHAT_ID:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text="🔔 美股收盤，自動掃描啟動...")
                # 傳入 None 代表掃描今日
                results, formatted_date = await scan_market(None)
                if results:
                    file_content = "\n".join(results)
                    bio = io.BytesIO(file_content.encode('utf-8'))
                    bio.name = f"NASDAQ_Daily_{formatted_date}.txt"
                    await app.bot.send_document(chat_id=TG_CHAT_ID, document=bio, caption=f"🇺🇸 今日符合清單 ({len(results)}檔)")
                else:
                    await app.bot.send_message(chat_id=TG_CHAT_ID, text="今日無符合標的。")
            
            # 避免重複觸發，休息 65 分鐘
            await asyncio.sleep(3900)
        
        await asyncio.sleep(60)

if __name__ == '__main__':
    if not TG_TOKEN:
        print("❌ Error: TG_TOKEN not found")
        exit(1)

    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}\s+.+$'), diagnostic_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}$'), history_scan_handler))

    print("🤖 US Stock Bot started...")
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_daily_scan(app))
    app.run_polling()
