import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from curl_cffi import requests as cureq
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"
TELEGRAM_CHAT_ID   = "1327677831"

EMA_PERIOD = 20           # Fast pullback scalp EMA
COOLDOWN_MINUTES = 10

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def fetch_nse_nifty_candles():
    url = "https://www.nseindia.com/api/chart-databyindex?index=NIFTY%2050&indices=true"
    session = cureq.Session(impersonate="chrome120")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "*/*"
    })
    try:
        session.get("https://www.nseindia.com", timeout=8)
        r = session.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            points = data.get("grapthData", [])
            if points:
                records = [{"timestamp": pd.to_datetime(p[0], unit='ms'), "price": float(p[1])} for p in points]
                df = pd.DataFrame(records)
                df.set_index("timestamp", inplace=True)
                df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
                
                df_5m = df['price'].resample('5min').ohlc().dropna()
                df_5m['ema'] = df_5m['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
                return df_5m
    except Exception as err:
        print(f"Fetch Warning: {err}", flush=True)
    return None

def get_itm_option(spot_price, trade_type):
    atm = round(spot_price / 50) * 50
    return f"{atm - 50} CE" if trade_type == "CALL" else f"{atm + 50} PE"

def nifty_bot_worker():
    print("🚀 Nifty 50 Pullback Scalper Engine Started...", flush=True)
    send_telegram_alert("🇮🇳 *NIFTY PULLBACK SCALPER ACTIVE!*\nMonitoring 5m 20-EMA Dip-Bounce setups.")

    last_trade_time = None
    trades_count = 0
    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    trades_count = 0

                df = fetch_nse_nifty_candles()

                if df is not None and len(df) > EMA_PERIOD:
                    curr_price = df.iloc[-1]['close']
                    last = df.iloc[-2]
                    ema_val = last['ema']
                    is_green = last['close'] > last['open']
                    is_red   = last['close'] < last['open']

                    print(f"[{now_ist.strftime('%H:%M:%S')}] Nifty: {curr_price:.1f} | 20-EMA: {ema_val:.1f} | Scanning Pullbacks...", flush=True)

                    cooldown_passed = True
                    if last_trade_time:
                        passed = (now_ist - last_trade_time).total_seconds() / 60
                        if passed < COOLDOWN_MINUTES:
                            cooldown_passed = False

                    if cooldown_passed:
                        # 1. Bullish Pullback (Dip near EMA + Green Reversal)
                        if last['close'] > ema_val and last['low'] <= (ema_val + 6) and is_green:
                            contract = get_itm_option(curr_price, "CALL")
                            sl_pts = round(curr_price - last['low'] + 5, 1)
                            tp_pts = round(sl_pts * 1.4, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🟢 *NIFTY SCALP CALL BUY #{trades_count}* 🟢\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* -{sl_pts} pts (~{round(sl_pts * 0.7)} pts in option)\n"
                                f"🎯 *Spot Target:* +{tp_pts} pts (~{round(tp_pts * 0.7)} pts in option)\n"
                                f"📈 *Pattern:* 20-EMA Support Bounce\n\n"
                                f"⚡ *Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

                        # 2. Bearish Pullback (Rally into EMA + Red Rejection)
                        elif last['close'] < ema_val and last['high'] >= (ema_val - 6) and is_red:
                            contract = get_itm_option(curr_price, "PUT")
                            sl_pts = round(last['high'] - curr_price + 5, 1)
                            tp_pts = round(sl_pts * 1.4, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🔴 *NIFTY SCALP PUT BUY #{trades_count}* 🔴\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* +{sl_pts} pts (~{round(sl_pts * 0.7)} pts in option)\n"
                                f"🎯 *Spot Target:* -{tp_pts} pts (~{round(tp_pts * 0.7)} pts in option)\n"
                                f"📉 *Pattern:* 20-EMA Resistance Rejection\n\n"
                                f"⚡ *Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

            time.sleep(25)

        except Exception as e:
            print(f"Nifty Loop Notice: {e}", flush=True)
            time.sleep(15)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nifty Pullback Bot Live!")

    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=nifty_bot_worker, daemon=True)
    t.start()
    run_server()
