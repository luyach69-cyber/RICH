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

# 備用觀察池
FALLBACK_WATCHLIST = {
    "2330.TW": "台積電",
    "2317.TW": "鴻海",
    "2454.TW": "聯發科",
    "2382.TW": "廣達",
    "3231.TW": "緯創",
    "2603.TW": "長榮",
    "3037.TW": "欣興",
}


# ================= 升降單位 (Tick) 與交易價格計算 =================
def get_tw_tick_size(price: float) -> float:
    """計算台股最小跳動檔位 (Tick Size)"""
    if price < 10:
        return 0.01
    elif price < 50:
        return 0.05
    elif price < 100:
        return 0.1
    elif price < 500:
        return 0.5
    elif price < 1000:
        return 1.0
    else:
        return 5.0


def calculate_trade_levels(entry_price: float, stop_price: float):
    """
    計算建議防守停損與兩階段停利目標
    """
    # 限制單筆最大止損不超過 5.5% (避免昨低過深)
    max_risk_stop = entry_price * 0.945
    actual_stop = max(stop_price, max_risk_stop)

    risk = entry_price - actual_stop
    risk_pct = (risk / entry_price) * 100

    # 目標一 (1.5 倍盈虧比)、目標二 (3.0 倍盈虧比)
    tp1 = entry_price + (risk * 1.5)
    tp2 = entry_price + (risk * 3.0)

    return {
        "entry": round(entry_price, 2),
        "stop": round(actual_stop, 2),
        "risk_pct": round(risk_pct, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
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

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_dynamic (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            added_date TEXT,
            neckline_price REAL,
            suggested_entry REAL,
            suggested_stop REAL,
            tp1 REAL,
            tp2 REAL
        )
    """
    )
    conn.commit()
    conn.close()


def has_alerted_today(symbol, signal_type):
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


# ================= 證交所全市場名冊抓取 =================
def fetch_twse_stock_universe() -> dict:
    stock_dict = {}
    try:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for item in data:
                code = str(item.get("公司代號", "")).strip()
                name = str(item.get("公司名稱", "")).strip() or str(item.get("公司簡稱", "")).strip()
                if len(code) == 4 and code.isdigit():
                    stock_dict[f"{code}.TW"] = name
    except Exception as e:
        print(f"證交所 API 讀取異常: {e}")

    return stock_dict if stock_dict else FALLBACK_WATCHLIST


# ================= 盤後全市場掃描 (附精確買賣與停損停利點) =================
def scan_market_and_update_watchlist():
    init_db()
    stocks = fetch_twse_stock_universe()
    all_symbols = list(stocks.keys())
    print(f"開始全市場掃描，標的總數：{len(all_symbols)} 檔...")

    selected_candidates = []
    today_str = datetime.now().strftime("%Y-%m-%d")

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
                    curr_low = float(latest["Low"])

                    # 1. 流動性過濾 (>= 2,000 張)
                    if curr_vol < 2000000:
                        continue

                    # 2. 均線多頭且站上月季線
                    is_bull = (
                        latest["MA5"] > latest["MA10"] > latest["MA20"]
                        and curr_price > latest["MA20"]
                        and curr_price >= latest["MA60"]
                    )
                    if not is_bull:
                        continue

                    # 3. 整理區頸線計算 (前 20 日高點)
                    recent_high = float(df["High"].iloc[-21:-1].max())

                    # 4. 突破或臨近突破
                    pct_to_high = (curr_price - recent_high) / recent_high
                    is_near_breakout = -0.02 <= pct_to_high <= 0.02
                    is_breakout_today = (curr_price >= recent_high) and (curr_vol > vol_ma5 * 1.5)

                    if is_near_breakout or is_breakout_today:
                        # 計算隔日建議突破價與各防守目標點位
                        tick = get_tw_tick_size(recent_high)
                        suggested_entry = recent_high + tick if not is_breakout_today else curr_price
                        trade_plan = calculate_trade_levels(suggested_entry, curr_low)

                        selected_candidates.append({
                            "symbol": sym,
                            "name": stocks.get(sym, sym),
                            "price": curr_price,
                            "neckline": recent_high,
                            "volume": curr_vol,
                            "vol_ratio": curr_vol / vol_ma5 if vol_ma5 > 0 else 1.0,
                            "status": "今日已突破" if is_breakout_today else "臨近頸線蓄勢",
                            "plan": trade_plan,
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"批次掃描異常: {e}")

    # 取量比最大的前 6 檔標的
    selected_candidates.sort(key=lambda x: x["vol_ratio"], reverse=True)
    top_candidates = selected_candidates[:6]

    # 存入資料庫
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist_dynamic")
    for c in top_candidates:
        p = c["plan"]
        cursor.execute(
            """
            INSERT OR REPLACE INTO watchlist_dynamic 
            (symbol, name, added_date, neckline_price, suggested_entry, suggested_stop, tp1, tp2)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (c["symbol"], c["name"], today_str, c["neckline"], p["entry"], p["stop"], p["tp1"], p["tp2"]),
        )
    conn.commit()
    conn.close()

    if not top_candidates:
        send_telegram_alert(f"📋 *【盤後全市場掃描 - {today_str}】*\n今日市場無符合標準之標的。")
        return

    # 發送包含停損停利價的完整報告
    msg = (
        f"🎯 *【明日突破潛力觀察池 (附進出場價格)】*\n"
        f"日期：`{today_str}` | 共精選 `{len(top_candidates)}` 檔\n"
        f"------------------------------------\n"
    )
    for c in top_candidates:
        p = c["plan"]
        msg += (
            f"📌 *{c['name']}* (`{c['symbol']}`)\n"
            f"• 型態狀態：`{c['status']}`\n"
            f"• 今日收盤：`{c['price']:.2f}`\n"
            f"• 🟢 **建議進場價**：`{p['entry']:.2f}` (過頸線觸發)\n"
            f"• 🔴 **建議停損價**：`{p['stop']:.2f}` (風險約 `-{p['risk_pct']}%`)\n"
            f"• 🎯 **停利目標一**：`{p['tp1']:.2f}` (盈虧比 1.5x)\n"
            f"• 🚀 **波段目標二**：`{p['tp2']:.2f}` (盈虧比 3.0x)\n"
            f"• 今日成交量：`{int(c['volume']):,}` (量增 `{c['vol_ratio']:.1f}x`)\n\n"
        )
    msg += "💡 *操作紀律*：明早可於券商 App 針對上述標的設定「觸價買進單（價格 >= 建議進場價）」，成交後請立即掛出防守停損單！"
    send_telegram_alert(msg)


# ================= 盤中即時訊號 (推播精確進場與停損停利) =================
def check_intraday_signals():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    active_positions = pd.read_sql("SELECT * FROM positions WHERE status = 'HOLD'", conn)
    holding_symbols = active_positions["symbol"].tolist() if not active_positions.empty else []

    dynamic_watch = pd.read_sql("SELECT * FROM watchlist_dynamic", conn)
    conn.close()

    current_watchlist = dict(FALLBACK_WATCHLIST)
    if not dynamic_watch.empty:
        for _, row in dynamic_watch.iterrows():
            current_watchlist[row["symbol"]] = row["name"]

    now_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_time_str}] 盤中監控檢查中...")

    for symbol, stock_name in current_watchlist.items():
        try:
            df = yf.download(symbol, period="3mo", interval="1d", progress=False, auto_adjust=True)
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
                            f"• 昨低防守線：`{prev_low:.2f}`\n"
                            f"• 觸發時間：`{now_time_str}`\n"
                            f"• 處置方式：已跌破防守線，建議手動掛單賣出，截斷虧損[cite: 4]！"
                        )
                        send_telegram_alert(msg)
                        log_signal(symbol, "SELL_SIGNAL", curr_price, prev_low, prev_high, curr_vol, "跌破前日低點")
                continue

            # ---------------- 條件 B：空手帶量突破買進 ----------------
            is_bull_trend = latest["MA5"] > latest["MA10"] > latest["MA20"]
            is_breakout = curr_price > prev_high
            is_volume_spike = curr_vol > (avg_vol * 1.5)

            if is_bull_trend and is_breakout and is_volume_spike:
                if not has_alerted_today(symbol, "BUY_SIGNAL"):
                    trade_plan = calculate_trade_levels(curr_price, prev_low)
                    msg = (
                        f"🔥 *【手動買進訊號 - 帶量突破】*\n"
                        f"• 標的：`{stock_name} ({symbol})`\n"
                        f"• 🟢 **建議進場價**：`{trade_plan['entry']:.2f}` (現價過昨高 `{prev_high:.2f}`)\n"
                        f"• 🔴 **嚴格停損價**：`{trade_plan['stop']:.2f}` (昨低防守，風險 `-{trade_plan['risk_pct']}%`)[cite: 4]\n"
                        f"• 🎯 **停利目標一**：`{trade_plan['tp1']:.2f}` (盈虧比 1.5x)\n"
                        f"• 🚀 **波段目標二**：`{trade_plan['tp2']:.2f}` (盈虧比 3.0x)\n"
                        f"• 今日成交量：`{int(curr_vol):,}` (均量 `{int(avg_vol):,}`)\n"
                        f"• 處置方式：若手動進場，請同步掛出 `{trade_plan['stop']:.2f}` 停損單[cite: 4]！"
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
    scan_market_and_update_watchlist()
