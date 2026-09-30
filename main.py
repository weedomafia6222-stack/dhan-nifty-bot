import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Apna Token check karein
TELEGRAM_CHAT_ID   = "1327677831"      # Apna Chat ID check karein

EMA_PERIOD = 20
COOLDOWN_MINUTES = 10

INDICES = {
    "^NSEI": {
        "name": "NIFTY 50",
        "step": 50,
        "zone": 12,
        "sl_buf": 8,
        "opt_ratio": 0.7,
        "min_slope": 0.35   # Sideways filter: Flat EMA par trade nahi lega
    },
    "^NSEBANK": {
        "name": "BANKNIFTY",
        "step": 100,
        "zone": 35,
        "sl_buf": 25,
        "opt_ratio": 0.65,
        "min_slope": 1.2
    },
    "^BSESN": {
        "name": "SENSEX",
        "step": 100,
        "zone": 45,
        "sl_buf": 30,
        "opt_ratio": 0.65,
        "min_slope": 1.5
    }
}

history = {symbol: [] for symbol in INDICES}
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
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def fetch_live_index_spot(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code == 200:
            data = res.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            curr_price = meta.get("regularMarketPrice")
            return float(curr_price) if curr_price else None
    except Exception as e:
        print(f"Fetch Warning ({symbol}): {e}", flush=True)
    return None

def get_itm_option(name, spot_price, step, trade_type):
    atm = round(spot_price / step) * step
    if trade_type == "CALL":
        strike = atm - step
        return f"{name} {strike} CE"
    else:
        strike = atm + step
        return f"{name} {strike} PE"

def indian_market_worker():
    print("🚀 Indian Market Multi-Index Scanner Active...", flush=True)
    send_telegram_alert("🇮🇳 *INDIAN MARKETS SCANNER LIVE!*\nMonitoring Nifty 50, BankNifty & Sensex with Trend-Slope Filter.")

    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            # Mon-Fri 09:15 AM - 03:30 PM
            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    for s in INDICES:
                        trade_counts[s] = 0
                        history[s].clear()

                for symbol, cfg in INDICES.items():
                    spot = fetch_live_index_spot(symbol)
                    if spot:
                        history[symbol].append({"time": now_ist, "price": spot})
                        if len(history[symbol]) > 60:
                            history[symbol].pop(0)

                        df = pd.DataFrame(history[symbol])
                        df['ema'] = df['price'].ewm(span=EMA_PERIOD, adjust=False).mean()

                        ema_val = round(df['ema'].iloc[-1], 1)
                        recent_low = df['price'].tail(12).min()
                        recent_high = df['price'].tail(12).max()

                        # Slope calculation over last 3 ticks (Trend momentum check)
                        slope = 0.0
                        if len(df) >= 4:
                            slope = df['ema'].iloc[-1] - df['ema'].iloc[-4]

                        cooldown_passed = True
                        if last_trade_times[symbol]:
                            passed = (now_ist - last_trade_times[symbol]).total_seconds() / 60
                            if passed < COOLDOWN_MINUTES:
                                cooldown_passed = False

                        if len(df) >= EMA_PERIOD and cooldown_passed:
                            # 1. Bullish Setup: EMA rising (slope > min_slope) + Bounce
                            if slope > cfg['min_slope'] and spot >= ema_val and (spot - ema_val) <= cfg['zone'] and recent_low < ema_val:
                                contract = get_itm_option(cfg['name'], spot, cfg['step'], "CALL")
                                sl_pts = round(spot - recent_low + cfg['sl_buf'], 1)
                                tp_pts = round(sl_pts * 1.4, 1)
                                trade_counts[symbol] += 1
                                last_trade_times[symbol] = now_ist

                                msg = (
                                    f"🟢 *{cfg['name']} CALL BUY #{trade_counts[symbol]}* 🟢\n\n"
                                    f"🎯 *Option Strike:* `{contract}`\n"
                                    f"🔹 *Spot Price:* {spot:,.1f}\n"
                                    f"🛑 *Spot SL:* -{sl_pts} pts (~{round(sl_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"🎯 *Spot Target:* +{tp_pts} pts (~{round(tp_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"📈 *Trend Slope:* Bullish (+{slope:.2f})\n\n"
                                    f"⚡ *Rule:* Trail SL to cost after 15-20 option points."
                                )
                                send_telegram_alert(msg)

                            # 2. Bearish Setup: EMA falling (slope < -min_slope) + Rejection
                            elif slope < -cfg['min_slope'] and spot <= ema_val and (ema_val - spot) <= cfg['zone'] and recent_high > ema_val:
                                contract = get_itm_option(cfg['name'], spot, cfg['step'], "PUT")
                                sl_pts = round(recent_high - spot + cfg['sl_buf'], 1)
                                tp_pts = round(sl_pts * 1.4, 1)
                                trade_counts[symbol] += 1
                                last_trade_times[symbol] = now_ist

                                msg = (
                                    f"🔴 *{cfg['name']} PUT BUY #{trade_counts[symbol]}* 🔴\n\n"
                                    f"🎯 *Option Strike:* `{contract}`\n"
                                    f"🔹 *Spot Price:* {spot:,.1f}\n"
                                    f"🛑 *Spot SL:* +{sl_pts} pts (~{round(sl_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"🎯 *Spot Target:* -{tp_pts} pts (~{round(tp_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"📉 *Trend Slope:* Bearish ({slope:.2f})\n\n"
                                    f"⚡ *Rule:* Trail SL to cost after 15-20 option points."
                                )
                                send_telegram_alert(msg)

                    time.sleep(1)

                if history["^NSEI"]:
                    last_nifty = history["^NSEI"][-1]["price"]
                    last_ema = round(pd.DataFrame(history["^NSEI"])['ema'].iloc[-1], 1)
                    print(f"[{now_ist.strftime('%H:%M:%S')}] Nifty: {last_nifty:,.1f} | EMA: {last_ema:,.1f} | Scanning...", flush=True)

            time.sleep(15)

        except Exception as e:
            print(f"Scanner Exception: {e}", flush=True)
            time.sleep(10)

# Instant Port Binding Server (Prevents Render Port Scan Timeout)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nifty Bot Live & Scanning!")

    def log_message(self, format, *args):
        return

def run_server():
    # Render binds automatically to 10000 or the PORT env var
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"✅ Web Port Server bound to {port}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=indian_market_worker, daemon=True)
    t.start()
    run_server()
