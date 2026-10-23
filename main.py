import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"
TELEGRAM_CHAT_ID   = "1327677831"

COOLDOWN_MINUTES = 30

INDICES = {
    "^NSEI": {
        "name": "NIFTY",
        "step": 50,
        "sl_buf": 22.0,
        "min_risk": 15.0,
        "max_risk": 45.0,
        "min_ema_spread": 4.0,
        "rr": 1.5,              # Realistic intraday reward-to-risk ratio
        "max_opt_sl": 18.0,      # Max allowable Option SL in points
        "max_opt_tp": 30.0       # Max realistic Option Target in points
    },
    "^NSEBANK": {
        "name": "BANKNIFTY",
        "step": 100,
        "sl_buf": 60.0,
        "min_risk": 35.0,
        "max_risk": 110.0,
        "min_ema_spread": 12.0,
        "rr": 1.5,
        "max_opt_sl": 40.0,
        "max_opt_tp": 75.0
    }
}

last_trade_times = {symbol: None for symbol in INDICES}

# ----------------- TELEGRAM NOTIFIER -----------------
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, data=payload, timeout=8)
        if res.status_code != 200:
            clean_text = message.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
            payload["text"] = clean_text
            payload.pop("parse_mode", None)
            requests.post(url, data=payload, timeout=8)
    except Exception as e:
        print(f"Telegram Alert Error: {e}", flush=True)

# ----------------- INTRADAY CANDLE DATA -----------------
def fetch_5m_candles(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d&includePrePost=false"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=6)
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

            ist = pytz.timezone('Asia/Kolkata')
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s').dt.tz_localize('UTC').dt.tz_convert(ist)
            for c in ["open", "high", "low", "close"]:
                df[c] = df[c].astype(float)
            return df
    except Exception as e:
        print(f"Data Fetch Error ({symbol}): {e}", flush=True)
    return None

def get_itm_option(name, spot_price, step, trade_type):
    atm = int(round(spot_price / step) * step)
    # CALL ITM: 1 Strike below ATM | PUT ITM: 1 Strike above ATM
    strike = (atm - step) if trade_type == "CALL" else (atm + step)
    suffix = "CE" if trade_type == "CALL" else "PE"
    return f"{name} {strike} {suffix}"

# ----------------- SIGNAL GENERATION LOGIC -----------------
def check_high_accuracy_setup(df, cfg, now_ist):
    if df is None or len(df) < 5:
        return None

    # Work strictly on current trading day candles
    today_candles = df[df['datetime'].dt.date == now_ist.date()].copy().reset_index(drop=True)
    if len(today_candles) < 5:
        return None

    # First 3 candles define Opening Range (09:15 to 09:30 AM IST)
    first_15m = today_candles.iloc[:3]
    orb_high = first_15m['high'].max()
    orb_low  = first_15m['low'].min()

    today_candles['ema9']  = today_candles['close'].ewm(span=9, adjust=False).mean()
    today_candles['ema21'] = today_candles['close'].ewm(span=21, adjust=False).mean()

    # Closed-candle verification:
    # iloc[-3] -> Pullback / Retest candle
    # iloc[-2] -> Confirmed Closed Breakout candle
    pullback_c = today_candles.iloc[-3]
    confirmed_c = today_candles.iloc[-2]

    ema_spread = abs(confirmed_c['ema9'] - confirmed_c['ema21'])
    if ema_spread < cfg["min_ema_spread"]:
        return None  # Flat sideways chop filtered

    # 1. HIGH-ACCURACY CALL SIGNAL
    if (confirmed_c['ema9'] > confirmed_c['ema21']) and (confirmed_c['close'] > orb_high):
        pulled_back = (pullback_c['low'] <= (pullback_c['ema9'] * 1.001)) and (pullback_c['close'] >= pullback_c['ema9'])
        breakout = (confirmed_c['close'] > pullback_c['high'])

        if pulled_back and breakout:
            sl = round(pullback_c['low'] - cfg['sl_buf'], 1)
            risk = round(confirmed_c['close'] - sl, 1)

            if cfg['min_risk'] <= risk <= cfg['max_risk']:
                reward = round(risk * cfg['rr'], 1)
                tp = round(confirmed_c['close'] + reward, 1)
                return {
                    "side": "CALL",
                    "entry": confirmed_c['close'],
                    "sl": sl,
                    "tp": tp,
                    "risk": risk,
                    "reward": reward,
                    "reason": "15m ORB High Breakout + 9 EMA Pullback"
                }

    # 2. HIGH-ACCURACY PUT SIGNAL
    if (confirmed_c['ema9'] < confirmed_c['ema21']) and (confirmed_c['close'] < orb_low):
        pulled_back = (pullback_c['high'] >= (pullback_c['ema9'] * 0.999)) and (pullback_c['close'] <= pullback_c['ema9'])
        breakdown = (confirmed_c['close'] < pullback_c['low'])

        if pulled_back and breakdown:
            sl = round(pullback_c['high'] + cfg['sl_buf'], 1)
            risk = round(sl - confirmed_c['close'], 1)

            if cfg['min_risk'] <= risk <= cfg['max_risk']:
                reward = round(risk * cfg['rr'], 1)
                tp = round(confirmed_c['close'] - reward, 1)
                return {
                    "side": "PUT",
                    "entry": confirmed_c['close'],
                    "sl": sl,
                    "tp": tp,
                    "risk": risk,
                    "reward": reward,
                    "reason": "15m ORB Low Breakdown + 9 EMA Pullback"
                }

    return None

# ----------------- SCANNER WORKER LOOP -----------------
def indian_market_worker():
    print("🚀 Indian Market High-Accuracy Realistic Target Scanner Online...", flush=True)

    send_telegram_alert(
        "🇮🇳 <b>INDIAN F&amp;O REALISTIC TARGET SCANNER LIVE!</b>\n\n"
        "🔹 <b>Indices:</b> NIFTY, BANKNIFTY\n"
        "🔹 <b>Targets:</b> Realistic 25-30 pts (Nifty) | 60-75 pts (BankNifty)\n"
        "🔹 <b>Model:</b> Closed Candle ORB + 9 EMA Pullback\n"
        "🔹 <b>Safety:</b> Noise Buffers &amp; Strict Strike Selection Active"
    )

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=35, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=14, minute=55, second=0, microsecond=0)

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
                        last_trade_times[symbol] = now_ist

                        icon = "🟢" if trade_type == "CALL" else "🔴"
                        delta = 0.55

                        # Realistic Option Points Capped
                        opt_sl = round(min(setup['risk'] * delta, cfg['max_opt_sl']), 1)
                        opt_tp = round(min(setup['reward'] * delta, cfg['max_opt_tp']), 1)

                        msg = (
                            f"{icon} <b>CONFIRMED {cfg['name']} {trade_type} SIGNAL</b> {icon}\n\n"
                            f"📌 <b>Setup:</b> {setup['reason']}\n"
                            f"🎯 <b>Recommended Option:</b> <code>{contract}</code>\n"
                            f"🔹 <b>Spot Entry:</b> ₹{setup['entry']:,.1f}\n"
                            f"🎯 <b>Spot Target:</b> ₹{setup['tp']:,.1f} (+{setup['reward']} pts)\n"
                            f"🛑 <b>Spot SL:</b> ₹{setup['sl']:,.1f} (-{setup['risk']} pts)\n"
                            f"📊 <b>Realistic Option Target:</b> +{opt_tp} pts\n"
                            f"🛑 <b>Option SL Buffer:</b> -{opt_sl} pts\n\n"
                            "🛡️ <i>Move Option SL to Entry Cost once +15 points profit is achieved.</i>"
                        )
                        send_telegram_alert(msg)

            time.sleep(20)

        except Exception as e:
            print(f"Scanner Loop Error: {e}", flush=True)
            time.sleep(10)

# ----------------- UPTIMEROBOT HEALTH CHECK SERVER -----------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Indian Signal Scanner Online")

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
