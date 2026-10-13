import os
import sqlite3
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf

# ================= 參數與環境變數設定 =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_NAME = "trading_journal.db"

# 自訂追蹤股票池：設定「代碼: 中文名稱」映射
WATCHLIST = {
    "2330.TW": "台積電",
    "2317.TW": "鴻海",
    "2454.TW": "聯發科",
    "2382.TW": "廣達",
    "3231.TW": "緯創",
    "2603.TW": "長榮",
    "3037.TW": "欣興",
}


# ================= 推播工具函式 =================
def send_telegram_alert(message: str):
    """發送訊息至 Telegram Bot"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，略過推播。")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram 推播失敗: {response.text}")
    except Exception as e:
        print(f"推播發送異常: {e}")


# ================= 資料庫存取與初始化 =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            signal_type TEXT,
            price REAL,
            yesterday_low REAL,
            yesterday_high REAL,
            volume REAL,
            note TEXT
        )
    """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            entry_date TEXT,
            entry_price REAL,
            initial_stop REAL,
            status TEXT
        )
    """
    )
    conn.commit()
    conn.close()


def has_alerted_today(symbol, signal_type):
    """檢查當日是否已經推播過相同訊號，避免重複打擾"""
    conn = sqlite3.connect(DB_NAME)
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*) FROM signal_logs 
        WHERE symbol = ? AND signal_type = ? AND timestamp LIKE ?
    """,
        (symbol, signal_type, f"{today_str}%"),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def log_signal(symbol, signal_type, price, yesterday_low, yesterday_high, volume, note=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO signal_logs (timestamp, symbol, signal_type, price, yesterday_low, yesterday_high, volume, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            signal_type,
            price,
            yesterday_low,
            yesterday_high,
            volume,
            note,
        ),
    )
    conn.commit()
    conn.close()


# ================= 盤中訊號篩選 =================
def check_intraday_signals():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    try:
        active_positions = pd.read_sql(
            "SELECT * FROM positions WHERE status = 'HOLD'", conn
        )
        holding_symbols = (
            active_positions["symbol"].tolist()
            if not active_positions.empty
            else []
        )
    except Exception:
        holding_symbols = []
    finally:
        conn.close()

    now_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_time_str}] 開始執行市場訊號檢驗...")

    for symbol, stock_name in WATCHLIST.items():
        try:
            df = yf.download(
                symbol,
                period="3mo",
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if df.empty or len(df) < 25:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df["MA5"] = df["Close"].rolling(window=5).mean()
            df["MA10"] = df["Close"].rolling(window=10).mean()
            df["MA20"] = df["Close"].rolling(window=20).mean()
            df["Vol_MA5"] = df["Volume"].rolling(window=5).mean()

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            curr_price = float(latest["Close"])
            curr_vol = float(latest["Volume"])
            prev_high = float(prev["High"])
            prev_low = float(prev["Low"])
            avg_vol = float(latest["Vol_MA5"])

            # ---------------- 條件 A：既有持股出場檢查 ----------------
            if symbol in holding_symbols:
                if curr_price < prev_low:
                    if not has_alerted_today(symbol, "SELL_SIGNAL"):
                        msg = (
                            f"🚨 *【手動停損/停利警示 - 跌破昨低】*\n"
                            f"• 標的：`{stock_name} ({symbol})`\n"
                            f"• 最新成交價：`{curr_price:.2f}`\n"
                            f"• 昨日最低點：`{prev_low:.2f}`\n"
                            f"• 觸發時間：`{now_time_str}`\n"
                            f"• 處置方式：已跌破防守點，建議手動掛單出場截斷風險。"
                        )
                        send_telegram_alert(msg)
                        log_signal(
                            symbol,
                            "SELL_SIGNAL",
                            curr_price,
                            prev_low,
                            prev_high,
                            curr_vol,
                            "跌破前日低點",
                        )
                continue

            # ---------------- 條件 B：空手多頭選股進場檢查 ----------------
            is_bull_trend = (
                latest["MA5"] > latest["MA10"]
                and latest["MA10"] > latest["MA20"]
            )
            is_breakout = curr_price > prev_high
            is_volume_spike = curr_vol > (avg_vol * 1.8)

            if is_bull_trend and is_breakout and is_volume_spike:
                # 若今天已經推播過這檔買進訊號，則不再重複通知
                if not has_alerted_today(symbol, "BUY_SIGNAL"):
                    msg = (
                        f"🔥 *【手動買進訊號 - 突破起漲】*\n"
                        f"• 標的：`{stock_name} ({symbol})`\n"
                        f"• 突破價位：`{curr_price:.2f}` (突破昨高 `{prev_high:.2f}`)\n"
                        f"• 成交量量增：`{int(curr_vol):,}` (均量 `{int(avg_vol):,}`)\n"
                        f"• 預設防守線：`{prev_low:.2f}` (昨日低點)\n"
                        f"• 觸發時間：`{now_time_str}`\n"
                        f"• 處置方式：若符合個人進場標準，請手動建倉並掛出停損單。"
                    )
                    send_telegram_alert(msg)
                    log_signal(
                        symbol,
                        "BUY_SIGNAL",
                        curr_price,
                        prev_low,
                        prev_high,
                        curr_vol,
                        "帶量過昨高突破",
                    )

        except Exception as e:
            print(f"分析標的 {symbol} 失敗: {e}")


# ================= 盤後復盤日報 =================
def daily_review_and_report():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    today_str = datetime.now().strftime("%Y-%m-%d")

    query = (
        f"SELECT * FROM signal_logs WHERE timestamp LIKE '{today_str}%' ORDER BY timestamp ASC"
    )
    today_signals = pd.read_sql(query, conn)
    conn.close()

    if today_signals.empty:
        send_telegram_alert(
            f"📋 *【盤後復盤日報 - {today_str}】*\n"
            f"今日追蹤清單中無任何符合標準之觸發訊號。"
        )
        return

    buy_signals = today_signals[today_signals["signal_type"] == "BUY_SIGNAL"]
    sell_signals = today_signals[today_signals["signal_type"] == "SELL_SIGNAL"]

    review_msg = (
        f"📊 *【盤後復盤日報 - {today_str}】*\n"
        f"• 買進觸發檔數：`{len(buy_signals)}` 檔\n"
        f"• 出場觸發檔數：`{len(sell_signals)}` 檔\n"
        f"------------------------------------\n"
    )

    for _, row in today_signals.iterrows():
        sym = row["symbol"]
        name = WATCHLIST.get(sym, sym)
        action_icon = "🟢 買進" if row["signal_type"] == "BUY_SIGNAL" else "🔴 出場"
        review_msg += (
            f"{action_icon} `{name} ({sym})` | 價: `{row['price']:.2f}` | "
            f"昨低: `{row['yesterday_low']:.2f}`\n"
        )

    review_msg += "\n💡 訊號已記錄至資料庫，請核對券商手動執行狀況。"
    send_telegram_alert(review_msg)


if __name__ == "__main__":
    init_db()
    check_intraday_signals()
