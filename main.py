import os
import io
import asyncio
import logging
import pytz
import pandas as pd
from datetime import datetime, time as dtime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

import config as cfg
from scanner_core import scan_market, fetch_and_diagnose

load_dotenv()
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def make_tradingview_text(rows):
    symbols = []
    for r in rows:
        s = str(r.get('Symbol', '')).strip()
        if not s:
            continue
        symbols.append(f"{cfg.TRADINGVIEW_PREFIX}{s}" if cfg.TRADINGVIEW_PREFIX else s)
    return cfg.TRADINGVIEW_BLOCK_SEPARATOR.join(symbols)

def make_csv_bytes(rows, formatted_date):
    df = pd.DataFrame(rows).copy()
    bio = io.BytesIO(df.to_csv(index=False).encode('utf-8'))
    bio.name = f"NASDAQ_FallenAngel_{formatted_date.replace('-', '')}.csv"
    return bio, df

def make_txt_bytes(text, formatted_date):
    bio = io.BytesIO(text.encode('utf-8'))
    bio.name = f"NASDAQ_FallenAngel_{formatted_date.replace('-', '')}.txt"
    return bio

async def run_full_scan_background(chat_id, context, date_str, label):
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🇺🇸 正在執行 {label} NASDAQ 全市場掃描\n"
                  f"策略：VCP/Trend + Fallen Angel RS (Bench={cfg.BENCH_SYMBOL})\n"
                  f"資料尺度：{cfg.HIST_CALENDAR_DAYS} 日曆天\n"
                  f"⏳ 視 NASDAQ 檔數與 Yahoo 節流情況，可能需要幾分鐘")
        )

        rows, formatted_date = await scan_market(date_str)

        # 依 RS 轉強優先排序（第二波候選更直觀）
        try:
            rows = sorted(
                rows,
                key=lambda r: (
                    float(r.get('RS_ma20_slope') if r.get('RS_ma20_slope') is not None else -1e9),
                    float(r.get('leader_peak_excess') if r.get('leader_peak_excess') is not None else -1e9),
                ),
                reverse=True,
            )
        except Exception:
            pass

        if not rows:
            await context.bot.send_message(chat_id=chat_id, text=f"📉 {formatted_date} 掃描無符合標的。")
            return

        # 摘要訊息
        top_preview = [r.get('Symbol') for r in rows[:20] if r.get('Symbol')]
        preview_text = ", ".join(top_preview)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"✅ **{formatted_date} 掃描完成**\n"
                  f"共 {len(rows)} 檔\n"
                  f"前 20 檔預覽：\n{preview_text}"),
            parse_mode=ParseMode.MARKDOWN
        )

        # TradingView TXT（區塊間隔）
        tv_text = make_tradingview_text(rows)
        txt_bio = make_txt_bytes(tv_text, formatted_date)

        # CSV（含欄位）
        csv_bio, _df = make_csv_bytes(rows, formatted_date)

        # 依序傳送 TXT + CSV（你要：同時訊息 + txt 檔）
        await context.bot.send_document(
            chat_id=chat_id,
            document=txt_bio,
            caption=(f"📄 TradingView 匯入清單（區塊間隔）\n{formatted_date} / {len(rows)} 檔")
        )

        await context.bot.send_document(
            chat_id=chat_id,
            document=csv_bio,
            caption=("📊 指標明細（CSV）\n"

                     "欄位：leader_peak_excess, rs_near_high%, rs_dd_vs_price_dd, RS_ma20_slope")
        )

    except Exception as e:
        logger.exception("Scan failed")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 掃描失敗: {e}")

async def run_diagnostic_background(chat_id, status_message_id, date_str, symbol, context):
    try:
        is_pass, report, formatted_date = await fetch_and_diagnose(symbol, date_str)
        if len(report) > 4000:
            report = report[:4000] + "\n...(截斷)"
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message_id,
            text=report,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.exception("Diagnostic failed")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message_id,
            text=f"❌ 錯誤: {e}"
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗽 **美股 VCP / Fallen Angel 狙擊手**\n\n"
        "1. `/now`: 立即掃描 (NASDAQ)\n"
        "2. `/231225`: 回測特定日期\n"
        "3. `/231225 NVDA`: 診斷特定個股\n\n"
        "📌 掃描完成會同時傳送：\n"
        "- TradingView TXT（每檔一個區塊）\n"
        "- CSV 指標明細",
        parse_mode=ParseMode.MARKDOWN
    )

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 啟動美股掃描...")
    asyncio.create_task(run_full_scan_background(update.effective_chat.id, context, None, "Today"))

async def history_scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.replace('/', '').strip()
    await update.message.reply_text(f"⏳ 準備回測: {date_str}...")
    asyncio.create_task(run_full_scan_background(update.effective_chat.id, context, date_str, date_str))

async def diagnostic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace('/', '').strip()
    parts = text.split()
    if len(parts) < 2:
        return
    date_str, symbol = parts[0], parts[1]
    msg = await update.message.reply_text(f"👨‍⚕️ 診斷中: {symbol}...")
    asyncio.create_task(run_diagnostic_background(update.effective_chat.id, msg.message_id, date_str, symbol, context))

async def scheduled_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not TG_CHAT_ID:
        return
    await context.bot.send_message(chat_id=TG_CHAT_ID, text="🔔 美股收盤後自動掃描啟動...")
    await run_full_scan_background(TG_CHAT_ID, context, None, "Scheduled")

def main():
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN not found")

    app = ApplicationBuilder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}\s+.+$'), diagnostic_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}$'), history_scan_handler))

    tz_ny = pytz.timezone('America/New_York')
    app.job_queue.run_daily(
        scheduled_scan_job,
        time=dtime(hour=16, minute=15),
        days=(0, 1, 2, 3, 4),
        tzinfo=tz_ny
    )

    print("🤖 US Stock Bot started...")
    app.run_polling()

if __name__ == '__main__':
    main()
