import os
import time
import threading
from datetime import datetime, time as dtime
import pytz
import pandas as pd
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"
TELEGRAM_CHAT_ID   = "1327677831"

COOLDOWN_MINUTES = 35

INDICES = {
    "^NSEI": {
        "name": "NIFTY 50",
        "step": 50,
        "sl_buf": 8.0,
        "rr": 2.0,
        "opt_ratio": 0.70
    },
    "^NSEBANK": {
        "name": "BANKNIFTY",
        "step": 100,
        "sl_buf": 25.0,
        "rr": 2.0,
        "opt_ratio": 0.65
    }
}

last_trade_times = {symbol: None for symbol in INDICES}
trade_counts = {symbol: 0 for symbol in INDICES}

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=8)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def fetch_5m_candles(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=2d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            res_data = data.get("chart", {}).get("result", [{}])[0]
            timestamps = res_data.get("timestamp", [])
            indicators = res_data.get("indicators", {}).get("quote", [{}])[0]

            if not timestamps or not indicators.get("close"):
                return None

            df = pd.DataFrame({
                "timestamp": timestamps,
                "open": indicators.get("open"),
                "high": indicators.get("high"),
                "low": indicators.get("low"),
                "close": indicators.get("close")
            }).dropna().reset_index(drop=True)

            # IST Timestamp convert
            ist = pytz.timezone('Asia/Kolkata')
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s').dt.tz_localize('UTC').dt.tz_convert(ist)
            return df
    except Exception as e:
        print(f"Fetch Error ({symbol}): {e}", flush=True)
    return None

def get_itm_option(name, spot_price, step, trade_type):
    atm = round(spot_price / step) * step
    strike = (atm - step) if trade_type == "CALL" else (atm + step)
    suffix = "CE" if trade_type == "CALL" else "PE"
    return f"{name} {strike} {suffix}"

def check_high_accuracy_setup(df, cfg, now_ist):
    """
    LOGIC:
    1. First 15m Range (09:15 - 09:30) Breakout Direction Filter
    2. EMA 9 & EMA 21 Trend Stack
    3. Rejection / Pullback Confirmation at EMA 9
    """
    if df is None or len(df) < 25:
        return None

    # Filter Today's Candles
    today_candles = df[df['datetime'].dt.date == now_ist.date()].copy()
    if len(today_candles) < 4:  # Pehle 15-20 min koi trade nahi
        return None

    # First 3 candles define Opening Range (09:15, 09:20, 09:25)
    first_15m = today_candles.iloc[:3]
    orb_high = first_15m['high'].max()
    orb_low  = first_15m['low'].min()

    # EMAs Calculation
    df['ema9']  = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    prev_candle = df.iloc[-2]  # Just closed candle
    curr_candle = df.iloc[-1]  # Active breakout candle

    ema9 = prev_candle['ema9']
    ema21 = prev_candle['ema21']

    # --- 1. HIGH ACCURACY CALL SETUP ---
    # Trend: 9 EMA > 21 EMA & Price > ORB High
    bullish_structure = (ema9 > ema21) and (prev_candle['close'] > orb_high)
    # Pullback to 9 EMA (Low touched near EMA9 but closed above it)
    bull_pullback = (prev_candle['low'] <= (ema9 * 1.001)) and (prev_candle['close'] > ema9)
    # Trigger: Current candle breaks previous pullback high
    if bullish_structure and bull_pullback and (curr_candle['close'] > prev_candle['high']):
        sl = round(prev_candle['low'] - cfg['sl_buf'], 1)
        risk = round(curr_candle['close'] - sl, 1)
        if 15 <= risk <= 45 if "NIFTY" in cfg['name'] else 35 <= risk <= 120:
            tp = round(curr_candle['close'] + (risk * cfg['rr']), 1)
            return {
                "side": "CALL",
                "entry": curr_candle['close'],
                "sl": sl,
                "tp": tp,
                "risk": risk,
                "reward": round(risk * cfg['rr'], 1),
                "reason": "15m ORB + 9 EMA Pullback"
            }

    # --- 2. HIGH ACCURACY PUT SETUP ---
    # Trend: 9 EMA < 21 EMA & Price < ORB Low
    bearish_structure = (ema9 < ema21) and (prev_candle['close'] < orb_low)
    # Pullback to 9 EMA (High touched near EMA9 but closed below it)
    bear_pullback = (prev_candle['high'] >= (ema9 * 0.999)) and (prev_candle['close'] < ema9)
    # Trigger: Current candle breaks previous pullback low
    if bearish_structure and bear_pullback and (curr_candle['close'] < prev_candle['low']):
        sl = round(prev_candle['high'] + cfg['sl_buf'], 1)
        risk = round(sl - curr_candle['close'], 1)
        if 15 <= risk <= 45 if "NIFTY" in cfg['name'] else 35 <= risk <= 120:
            tp = round(curr_candle['close'] - (risk * cfg['rr']), 1)
            return {
                "side": "PUT",
                "entry": curr_candle['close'],
                "sl": sl,
                "tp": tp,
                "risk": risk,
                "reward": round(risk * cfg['rr'], 1),
                "reason": "15m ORB + 9 EMA Pullback"
            }

    return None

def indian_market_worker():
    print("🚀 Indian Market High-Accuracy ORB+EMA Scanner Live...", flush=True)

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            is_weekday = now_ist.weekday() < 5
            # Trade window: 09:35 AM to 03:00 PM (No late-market noise)
            market_start = now_ist.replace(hour=9, minute=35, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=0, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                for symbol, cfg in INDICES.items():
                    if last_trade_times[symbol]:
                        passed_min = (now_ist - last_trade_times[symbol]).total_seconds() / 60
                        if passed_min < COOLDOWN_MINUTES:
                            continue

                    df = fetch_5m_candles(symbol)
                    time.sleep(0.5)

                    setup = check_high_accuracy_setup(df, cfg, now_ist)
                    if setup:
                        trade_type = setup['side']
                        contract = get_itm_option(cfg['name'], setup['entry'], cfg['step'], trade_type)

                        trade_counts[symbol] += 1
                        last_trade_times[symbol] = now_ist

                        icon = "🟢" if trade_type == "CALL" else "🔴"
                        opt_sl = round(setup['risk'] * cfg['opt_ratio'])
                        opt_tp = round(setup['reward'] * cfg['opt_ratio'])

                        msg = (
                            f"{icon} *{cfg['name']} HIGH ACCURACY {trade_type}* {icon}\n\n"
                            f"📌 *Strategy:* {setup['reason']}\n"
                            f"🎯 *Suggested Strike:* `{contract}`\n"
                            f"🔹 *Index Spot Price:* ₹{setup['entry']:,.1f}\n"
                            f"🎯 *Spot Target (1:{cfg['rr']}):* ₹{setup['tp']:,.1f} (+{setup['reward']} pts)\n"
                            f"🛑 *Spot SL:* ₹{setup['sl']:,.1f} (-{setup['risk']} pts)\n"
                            f"📊 *Approx Option SL:* -{opt_sl} pts | *Target:* +{opt_tp} pts\n\n"
                            f"⚡ *Rule:* Premium mein 20+ points aate hi SL Cost-to-Cost shift karein."
                        )
                        send_telegram_alert(msg)

            time.sleep(25)

        except Exception as e:
            print(f"Worker Error: {e}", flush=True)
            time.sleep(15)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"High Accuracy Indian Scanner Alive!")

    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=indian_market_worker, daemon=True)
    t.start()
    run_server()
