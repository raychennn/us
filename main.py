import os
import sys
import time
import logging
import asyncio
from datetime import datetime, timedelta, time as dtime

# 設定日誌 (強制輸出到 stdout，確保 Zeabur 看得到)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

# --- 1. 防崩潰依賴檢查 (Dependency Check) ---
try:
    import pytz
    import pandas as pd
    from dotenv import load_dotenv
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    # 嘗試匯入核心邏輯
    import config as cfg
    from scanner_core import scan_market, fetch_and_diagnose
    
    logger.info("✅ 所有 Python 套件載入成功")

except ImportError as e:
    logger.critical(f"❌ 致命錯誤: 套件載入失敗! 請檢查 requirements.txt。詳細錯誤: {e}")
    # [防崩潰模式] 進入無限休眠，讓開發者能看到 Log
    while True:
        time.sleep(60)

# 載入 .env
load_dotenv()

# --- 2. 環境變數檢查 (Env Check) ---
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# [修正] 如果沒設定 Token，不要崩潰，而是報錯並待機
if not TG_TOKEN:
    logger.critical("❌ 致命錯誤: 未偵測到 TG_TOKEN 環境變數！")
    logger.critical("   請到 Zeabur 的 [Variables] 頁面設定 TG_TOKEN。")
    logger.critical("   程式將進入待機模式 (不會重啟)，請設定後手動 Redeploy。")
    while True:
        time.sleep(60)

if not TG_CHAT_ID:
    logger.warning("⚠️ 警告: 未設定 TG_CHAT_ID，部分通知功能可能無法運作")


# -----------------------
# Output helpers
# -----------------------
def make_tradingview_text(rows):
    symbols = []
    for r in rows:
        s = str(r.get("Symbol", "")).strip()
        if not s:
            continue
        symbols.append(f"{cfg.TRADINGVIEW_PREFIX}{s}" if cfg.TRADINGVIEW_PREFIX else s)

    return cfg.TRADINGVIEW_BLOCK_SEPARATOR.join(symbols) + ("\n" if symbols else "")

import io
def make_txt_bytes(text, date_label):
    bio = io.BytesIO(text.encode("utf-8"))
    bio.name = f"tradingview_list_{date_label}.txt"
    bio.seek(0)
    return bio

def make_csv_bytes(rows, date_label):
    df = pd.DataFrame(rows)
    bio = io.BytesIO()
    df.to_csv(bio, index=False, encoding="utf-8-sig")
    bio.name = f"scan_result_{date_label}.csv"
    bio.seek(0)
    return bio, df


# -----------------------
# Helper: Determine "Latest Closed" Date
# -----------------------
def get_latest_market_date():
    tz_ny = pytz.timezone(cfg.SCHEDULE_TZ)
    now_ny = datetime.now(tz_ny)
    market_close_time = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    
    if now_ny < market_close_time:
        target_date = now_ny - timedelta(days=1)
    else:
        target_date = now_ny

    while target_date.weekday() > 4: 
        target_date -= timedelta(days=1)
        
    return target_date


# -----------------------
# Core actions
# -----------------------
async def execute_scan(bot, chat_id: str, date_str: str | None, tag: str):
    if not chat_id:
        logger.error("TG_CHAT_ID not set")
        return

    rows, formatted_date = await scan_market(date_str)

    preview_lines = []
    for r in rows[:20]:
        sym = r.get("Symbol", "")
        lp = r.get("leader_peak_excess", "")
        near = r.get("rs_near_high_pct", "")
        ratio = r.get("rs_dd_vs_price_dd", "")
        slope = r.get("RS_ma20_slope", "")
        preview_lines.append(f"- {sym} | peak_excess:{lp} | rs_near_high:{near} | dd_ratio:{ratio} | slope:{slope}")

    preview_text = "\n".join(preview_lines) if preview_lines else "(no results)"

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ **{formatted_date} 掃描完成**（{tag}）\n"
            f"共 {len(rows)} 檔\n"
            f"前 20 檔預覽：\n{preview_text}"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    tv_text = make_tradingview_text(rows)
    txt_bio = make_txt_bytes(tv_text, formatted_date)
    csv_bio, _df = make_csv_bytes(rows, formatted_date)

    await bot.send_document(
        chat_id=chat_id,
        document=txt_bio,
        caption=f"📄 TradingView 匯入清單（區塊間隔）\n{formatted_date} / {len(rows)} 檔",
    )

    await bot.send_document(
        chat_id=chat_id,
        document=csv_bio,
        caption="📊 指標明細（CSV）"
    )


async def scheduled_scan_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await execute_scan(context.bot, TG_CHAT_ID, None, "Scheduled")
    except Exception as e:
        logger.exception("Scheduled scan failed")
        if TG_CHAT_ID:
            await context.bot.send_message(chat_id=TG_CHAT_ID, text=f"⚠️ 排程掃描失敗: {e}")


# -----------------------
# Manual scheduler fallback
# -----------------------
def _next_run_ny(now_ny: datetime) -> datetime:
    run_dt = now_ny.replace(
        hour=cfg.SCHEDULE_HOUR,
        minute=cfg.SCHEDULE_MINUTE,
        second=0,
        microsecond=0,
    )
    if run_dt <= now_ny:
        run_dt = run_dt + timedelta(days=1)
    while run_dt.weekday() not in cfg.SCHEDULE_WEEKDAYS:
        run_dt = run_dt + timedelta(days=1)
    return run_dt


async def manual_scheduler_loop(app):
    tz_ny = pytz.timezone(cfg.SCHEDULE_TZ)
    logger.warning("JobQueue unavailable; using manual scheduler loop.")
    while True:
        try:
            now_ny = datetime.now(tz_ny)
            nxt = _next_run_ny(now_ny)
            sleep_sec = max(1, int((nxt - now_ny).total_seconds()))
            logger.info("Next scheduled scan at %s (sleep %ss)", nxt.isoformat(), sleep_sec)
            await asyncio.sleep(sleep_sec)
            if TG_CHAT_ID:
                await execute_scan(app.bot, TG_CHAT_ID, None, "Scheduled(manual)")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("manual_scheduler_loop error")
            await asyncio.sleep(30)


def schedule_daily_scan(app):
    tz_ny = pytz.timezone(cfg.SCHEDULE_TZ)
    if getattr(app, "job_queue", None) is not None:
        try:
            app.job_queue.run_daily(
                scheduled_scan_job,
                time=dtime(hour=cfg.SCHEDULE_HOUR, minute=cfg.SCHEDULE_MINUTE),
                days=cfg.SCHEDULE_WEEKDAYS,
                tzinfo=tz_ny,
            )
            logger.info("Scheduled scan registered via JobQueue (%s)", cfg.SCHEDULE_TZ)
            return
        except Exception:
            logger.exception("Failed to register JobQueue. Falling back to manual.")
    app.create_task(manual_scheduler_loop(app))


# -----------------------
# Telegram handlers
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 US Stock Bot\n"
        "/now 立即掃描 (只取已收盤資料)\n"
        "/yymmdd 回測日期掃描（例：/240101）\n"
        "/yymmdd SYMBOL 做診斷（例：/240101 AAPL）"
    )


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    target_date = get_latest_market_date()
    date_str = target_date.strftime("%y%m%d")
    formatted_date_display = target_date.strftime("%Y-%m-%d")
    
    await context.bot.send_message(chat_id=chat_id, text=f"🚀 收到 /now 指令\n鎖定最近收盤日: {formatted_date_display}\n開始掃描...")

    try:
        await execute_scan(context.bot, chat_id, date_str, f"Manual({date_str})")
    except Exception as e:
        logger.exception("Manual /now failed")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 掃描失敗: {e}")


async def history_scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    date_str = update.message.text.strip().lstrip("/").strip()
    try:
        await execute_scan(context.bot, chat_id, date_str, f"History({date_str})")
    except Exception as e:
        logger.exception("History scan failed")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 歷史掃描失敗: {e}")


async def diagnostic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    raw = update.message.text.strip().lstrip("/")
    parts = raw.split()
    if len(parts) < 2:
        await context.bot.send_message(chat_id=chat_id, text="用法：/yymmdd SYMBOL（例：/240101 AAPL）")
        return

    date_str, symbol = parts[0], parts[1].upper()
    msg = await context.bot.send_message(chat_id=chat_id, text=f"🔎 診斷中：{symbol} @ {date_str} ...")
    try:
        is_pass, report, formatted_date = await fetch_and_diagnose(symbol, date_str)
        status = "✅ PASS" if is_pass else "❌ FAIL"
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text=f"{status} {symbol} @ {formatted_date}\n\n{report}",
        )
    except Exception as e:
        logger.exception("Diagnostic failed")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text=f"⚠️ 診斷失敗: {e}",
        )


async def post_init(app):
    schedule_daily_scan(app)


def main():
    try:
        app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("now", now_command))
        app.add_handler(MessageHandler(filters.Regex(r"^\/\d{6}\s+.+$"), diagnostic_handler))
        app.add_handler(MessageHandler(filters.Regex(r"^\/\d{6}$"), history_scan_handler))

        logger.info("🤖 US Stock Bot started... (Polling Mode)")
        app.run_polling()
    except Exception as e:
        logger.critical(f"Main Loop Crash: {e}")
        # 這裡也加一個防崩潰，確保我們看得到 main crash 的原因
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
