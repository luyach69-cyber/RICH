import os
import sqlite3
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf

# ================= 參數與環境變數設定 =================
# 從 GitHub Actions Secrets 讀取敏感資訊
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_NAME = "trading_journal.db"

# 自訂追蹤股票池 (台股上市後綴 .TW，上櫃後綴 .TWO)
WATCHLIST = [
    "2330.TW",  # 台積電
    "2317.TW",  # 鴻海
    "2454.TW",  # 聯發科
    "2382.TW",  # 廣達
    "3231.TW",  # 緯創
    "2603.TW",  # 長榮
    "3037.TW",  # 欣興
]


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
            print(f"Telegram 推播失敗，狀態碼: {response.status_code}, 回應: {response.text}")
    except Exception as e:
        print(f"推播發送異常: {e}")


# ================= 資料庫存取與初始化 =================
def init_db():
    """初始化 SQLite 資料表結構"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 訊號觸發歷史紀錄 (供後續回測與勝率分析)
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

    # 2. 目前手動持股追蹤表 (跌破昨低時發出警示)
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


def log_signal(symbol, signal_type, price, yesterday_low, yesterday_high, volume, note=""):
    """將觸發的訊號寫入資料庫"""
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
    """盤中排程執行：檢測買進起漲訊號與持股跌破昨低出場訊號"""
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

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始執行市場訊號檢驗...")

    for symbol in WATCHLIST:
        try:
            # 抓取最近 3 個月日 K 線歷史資料
            df = yf.download(
                symbol,
                period="3mo",
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if df.empty or len(df) < 25:
                continue

            # 多層次欄位扁平化處理 (相容新版 yfinance 回傳格式)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # 計算均線與成交均量
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
                # 跌破昨日低點，發送出場警示
                if curr_price < prev_low:
                    msg = (
                        f"🚨 *【手動停損/停利警示 - 跌破昨低】*\n"
                        f"• 標的：`{symbol}`\n"
                        f"• 最新成交價：`{curr_price:.2f}`\n"
                        f"• 昨日最低點：`{prev_low:.2f}`\n"
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
            # 1. 均線呈多頭排列 (5MA > 10MA > 20MA)
            is_bull_trend = (
                latest["MA5"] > latest["MA10"]
                and latest["MA10"] > latest["MA20"]
            )
            # 2. 股價突破昨日高點
            is_breakout = curr_price > prev_high
            # 3. 伴隨成交量放大 (超過 5 日均量的 1.8 倍)
            is_volume_spike = curr_vol > (avg_vol * 1.8)

            if is_bull_trend and is_breakout and is_volume_spike:
                msg = (
                    f"🔥 *【手動買進訊號 - 突破起漲】*\n"
                    f"• 標的：`{symbol}`\n"
                    f"• 突破價位：`{curr_price:.2f}` (突破昨高 `{prev_high:.2f}`)\n"
                    f"• 成交量量增：`{int(curr_vol):,}` (均量 `{int(avg_vol):,}`)\n"
                    f"• 預設防守線：`{prev_low:.2f}` (昨日低點)\n"
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


# ================= 盤後復盤與彙總日報 =================
def daily_review_and_report():
    """每日盤後排程執行：彙總當日觸發訊號並發送復盤訊息"""
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
        action_icon = "🟢 買進" if row["signal_type"] == "BUY_SIGNAL" else "🔴 出場"
        review_msg += (
            f"{action_icon} `{row['symbol']}` | 價: `{row['price']:.2f}` | "
            f"昨低: `{row['yesterday_low']:.2f}` | 備註: {row['note']}\n"
        )

    review_msg += (
        f"\n💡 訊號已記錄至 SQLite 資料庫，請核對券商手動執行狀況以確保交易紀律。"
    )
    send_telegram_alert(review_msg)


# ================= 主程式本機測試進入點 =================
if __name__ == "__main__":
    init_db()
    print("手動執行測試中...")
    check_intraday_signals()
    daily_review_and_report()
