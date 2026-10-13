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

# 預設基礎追蹤清單 (萬一網路異常抓不到證交所時的備用池)
FALLBACK_WATCHLIST = {
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
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"Telegram 推播失敗: {response.text}")
    except Exception as e:
        print(f"推播發送異常: {e}")


# ================= 資料庫存取與初始化 =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 歷史觸發訊號表
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

    # 2. 目前持股部位 (跌破昨低警示)
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

    # 3. 盤後選出之「動態觀察池」(供次日盤中監控)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_dynamic (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            added_date TEXT,
            neckline_price REAL
        )
    """
    )
    conn.commit()
    conn.close()


def has_alerted_today(symbol, signal_type):
    """防止盤中相同訊號重複轟炸"""
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


# ================= 證交所全市場股票清單抓取 =================
def fetch_twse_stock_universe() -> dict:
    """從證交所開放資料 API 取得所有上市股票代碼與名稱"""
    stock_dict = {}
    try:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for item in data:
                code = str(item.get("公司代號", "")).strip()
                name = str(item.get("公司名稱", "")).strip() or str(item.get("公司簡稱", "")).strip()
                # 僅保留一般 4 位數代號普通股 (過濾特別股、ETF 00xxx 等)
                if len(code) == 4 and code.isdigit():
                    stock_dict[f"{code}.TW"] = name
    except Exception as e:
        print(f"從證交所 API 讀取股票名冊失敗: {e}")

    # 若失敗則回傳預設池
    return stock_dict if stock_dict else FALLBACK_WATCHLIST


# ================= 盤後全市場掃描 (型態收斂 + 準備突破) =================
def scan_market_and_update_watchlist():
    """
    每日盤後 15:30 執行：
    掃描全市場股票，挑出符合「均線多頭 + 整理收斂即將過頸線」的標的，
    推播至 Telegram 並自動存入 watchlist_dynamic 供隔日監控。
    """
    init_db()
    stocks = fetch_twse_stock_universe()
    all_symbols = list(stocks.keys())
    print(f"開始全市場掃描，標的總數：{len(all_symbols)} 檔...")

    selected_candidates = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 批次下載以節省時間 (每批 60 檔)
    batch_size = 60
    for i in range(0, len(all_symbols), batch_size):
        batch = all_symbols[i : i + batch_size]
        try:
            data = yf.download(
                batch,
                period="3mo",
                interval="1d",
                group_by="ticker",
                progress=False,
                auto_adjust=True,
                threads=True,
            )

            for sym in batch:
                try:
                    df = data[sym].dropna() if len(batch) > 1 else data.dropna()
                    if len(df) < 35:
                        continue

                    # 指標計算
                    df["MA5"] = df["Close"].rolling(5).mean()
                    df["MA10"] = df["Close"].rolling(10).mean()
                    df["MA20"] = df["Close"].rolling(20).mean()
                    df["MA60"] = df["Close"].rolling(60).mean() if len(df) >= 60 else df["MA20"]
                    df["Vol_MA5"] = df["Volume"].rolling(5).mean()

                    latest = df.iloc[-1]
                    prev = df.iloc[-2]

                    curr_price = float(latest["Close"])
                    curr_vol = float(latest["Volume"])
                    vol_ma5 = float(latest["Vol_MA5"])

                    # 1. 流動性篩選：單日成交量 >= 2,000 張 (2,000,000 股)
                    if curr_vol < 2000000:
                        continue

                    # 2. 均線架構：多頭排列且站穩月季線 (5MA > 10MA > 20MA 且 Close > 20MA, 60MA)
                    is_bull = (
                        latest["MA5"] > latest["MA10"] > latest["MA20"]
                        and curr_price > latest["MA20"]
                        and curr_price >= latest["MA60"]
                    )
                    if not is_bull:
                        continue

                    # 3. 型態學整理區頸線計算：過去 20 天的高點 (不含當日)
                    recent_high = float(df["High"].iloc[-21:-1].max())

                    # 4. 突破或臨近突破 (距離頸線高點 -2% 到 +2% 之間，或量增過頸線)
                    pct_to_high = (curr_price - recent_high) / recent_high
                    is_near_breakout = -0.02 <= pct_to_high <= 0.02
                    is_breakout_today = (curr_price >= recent_high) and (curr_vol > vol_ma5 * 1.5)

                    if is_near_breakout or is_breakout_today:
                        selected_candidates.append({
                            "symbol": sym,
                            "name": stocks.get(sym, sym),
                            "price": curr_price,
                            "neckline": recent_high,
                            "volume": curr_vol,
                            "vol_ratio": curr_vol / vol_ma5 if vol_ma5 > 0 else 1.0,
                            "status": "今日已突破" if is_breakout_today else "臨近頸線蓄勢",
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"批次下載異常: {e}")

    # 排序：按量比與型態優先度排序，取前 8 檔精華
    selected_candidates.sort(key=lambda x: x["vol_ratio"], reverse=True)
    top_candidates = selected_candidates[:8]

    # 更新資料庫 watchlist_dynamic
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist_dynamic")  # 清空舊清單
    for c in top_candidates:
        cursor.execute(
            """
            INSERT OR REPLACE INTO watchlist_dynamic (symbol, name, added_date, neckline_price)
            VALUES (?, ?, ?, ?)
        """,
            (c["symbol"], c["name"], today_str, c["neckline"]),
        )
    conn.commit()
    conn.close()

    # 發送 Telegram 盤後選股名單報告
    if not top_candidates:
        send_telegram_alert(f"📋 *【盤後全市場掃描 - {today_str}】*\n今日市場無符合「均線多頭＋頸線突破」之標的。")
        return

    msg = (
        f"🎯 *【明日突破潛力觀察池 (盤後全市場篩選)】*\n"
        f"日期：`{today_str}` | 共篩選出 `{len(top_candidates)}` 檔\n"
        f"------------------------------------\n"
    )
    for c in top_candidates:
        msg += (
            f"📌 *{c['name']}* (`{c['symbol']}`)\n"
            f"• 狀態：`{c['status']}`\n"
            f"• 收盤價：`{c['price']:.2f}`\n"
            f"• 關鍵頸線：`{c['neckline']:.2f}`\n"
            f"• 今日成交量：`{int(c['volume']):,}` (量增 `{c['vol_ratio']:.1f}x`)\n\n"
        )
    msg += "💡 *實戰提示*：上述標的已自動加入次日盤中監控池。建議明早可於券商 App 針對上述個股設定「觸價買進單（過頸線觸發）」。"
    send_telegram_alert(msg)


# ================= 盤中訊號篩選 (即時監控持股與動態觀察池) =================
def check_intraday_signals():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    
    # 讀取持股
    active_positions = pd.read_sql("SELECT * FROM positions WHERE status = 'HOLD'", conn)
    holding_symbols = active_positions["symbol"].tolist() if not active_positions.empty else []

    # 讀取盤後自動篩選出的動態觀察池
    dynamic_watch = pd.read_sql("SELECT * FROM watchlist_dynamic", conn)
    conn.close()

    # 合併監控池 (動態觀察池 + 預設權值池)
    current_watchlist = dict(FALLBACK_WATCHLIST)
    if not dynamic_watch.empty:
        for _, row in dynamic_watch.iterrows():
            current_watchlist[row["symbol"]] = row["name"]

    now_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_time_str}] 執行盤中監控，監控檔數：{len(current_watchlist)} 檔...")

    for symbol, stock_name in current_watchlist.items():
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

            df["MA5"] = df["Close"].rolling(5).mean()
            df["MA10"] = df["Close"].rolling(10).mean()
            df["MA20"] = df["Close"].rolling(20).mean()
            df["Vol_MA5"] = df["Volume"].rolling(5).mean()

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            curr_price = float(latest["Close"])
            curr_vol = float(latest["Volume"])
            prev_high = float(prev["High"])
            prev_low = float(prev["Low"])
            avg_vol = float(latest["Vol_MA5"])

            # ---------------- 條件 A：持股出場檢查 ----------------
            if symbol in holding_symbols:
                if curr_price < prev_low:
                    if not has_alerted_today(symbol, "SELL_SIGNAL"):
                        msg = (
                            f"🚨 *【手動停損/停利警示 - 跌破昨低】*\n"
                            f"• 標的：`{stock_name} ({symbol})`\n"
                            f"• 最新成交價：`{curr_price:.2f}`\n"
                            f"• 昨日最低點：`{prev_low:.2f}`\n"
                            f"• 觸發時間：`{now_time_str}`\n"
                            f"• 處置方式：已跌破短線防守點，建議手動掛單退場。"
                        )
                        send_telegram_alert(msg)
                        log_signal(symbol, "SELL_SIGNAL", curr_price, prev_low, prev_high, curr_vol, "跌破前日低點")
                continue

            # ---------------- 條件 B：觀察池帶量過昨高突破 ----------------
            is_bull_trend = latest["MA5"] > latest["MA10"] > latest["MA20"]
            is_breakout = curr_price > prev_high
            is_volume_spike = curr_vol > (avg_vol * 1.5)

            if is_bull_trend and is_breakout and is_volume_spike:
                if not has_alerted_today(symbol, "BUY_SIGNAL"):
                    msg = (
                        f"🔥 *【手動買進訊號 - 帶量突破】*\n"
                        f"• 標的：`{stock_name} ({symbol})`\n"
                        f"• 現價：`{curr_price:.2f}` (突破昨高 `{prev_high:.2f}`)\n"
                        f"• 今日成交量：`{int(curr_vol):,}` (大於 5 日均量 1.5 倍)\n"
                        f"• 停損防守線：`{prev_low:.2f}` (昨日低點)\n"
                        f"• 處置建議：符合突破買點，請評估建倉並掛出停損單。"
                    )
                    send_telegram_alert(msg)
                    log_signal(symbol, "BUY_SIGNAL", curr_price, prev_low, prev_high, curr_vol, "帶量過昨高突破")

        except Exception as e:
            print(f"分析標的 {symbol} 異常: {e}")


# ================= 盤後復盤日報 =================
def daily_review_and_report():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    today_str = datetime.now().strftime("%Y-%m-%d")

    query = f"SELECT * FROM signal_logs WHERE timestamp LIKE '{today_str}%' ORDER BY timestamp ASC"
    today_signals = pd.read_sql(query, conn)
    conn.close()

    if today_signals.empty:
        return

    buy_signals = today_signals[today_signals["signal_type"] == "BUY_SIGNAL"]
    sell_signals = today_signals[today_signals["signal_type"] == "SELL_SIGNAL"]

    review_msg = (
        f"📊 *【今日觸發覆盤日報 - {today_str}】*\n"
        f"• 買進觸發檔數：`{len(buy_signals)}` 檔\n"
        f"• 出場觸發檔數：`{len(sell_signals)}` 檔\n"
        f"------------------------------------\n"
    )

    for _, row in today_signals.iterrows():
        action_icon = "🟢 買進" if row["signal_type"] == "BUY_SIGNAL" else "🔴 出場"
        review_msg += f"{action_icon} `{row['symbol']}` | 價: `{row['price']:.2f}` | 昨低: `{row['yesterday_low']:.2f}`\n"

    review_msg += "\n💡 今日數據已同步至資料庫存查。"
    send_telegram_alert(review_msg)


if __name__ == "__main__":
    init_db()
    # 測試執行盤後全市場選股
    scan_market_and_update_watchlist()
