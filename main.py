import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Token dalein
TELEGRAM_CHAT_ID   = "1327677831"      # Chat ID dalein

EMA_PERIOD = 20
COOLDOWN_MINUTES = 10

INDICES = {
    "^NSEI": {
        "name": "NIFTY 50",
        "step": 50,
        "zone": 12,
        "sl_buf": 6,
        "opt_ratio": 0.7
    },
    "^NSEBANK": {
        "name": "BANKNIFTY",
        "step": 100,
        "zone": 35,
        "sl_buf": 20,
        "opt_ratio": 0.65
    },
    "^BSESN": {
        "name": "SENSEX",
        "step": 100,
        "zone": 45,
        "sl_buf": 25,
        "opt_ratio": 0.65
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
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            curr_price = meta.get("regularMarketPrice")
            return float(curr_price) if curr_price else None
    except Exception as e:
        print(f"Fetch Error ({symbol}): {e}", flush=True)
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
    print("🚀 Indian Markets Multi-Index Engine Active...", flush=True)
    send_telegram_alert("🇮🇳 *INDIAN MARKETS TRIO ACTIVE!*\nMonitoring Live Pullbacks on:\n• Nifty 50\n• BankNifty\n• BSE Sensex")

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

                status_line = []
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

                        status_line.append(f"{cfg['name']}: {spot:,.0f}")

                        cooldown_passed = True
                        if last_trade_times[symbol]:
                            passed = (now_ist - last_trade_times[symbol]).total_seconds() / 60
                            if passed < COOLDOWN_MINUTES:
                                cooldown_passed = False

                        if len(df) >= EMA_PERIOD and cooldown_passed:
                            # 1. Bullish Dip Bounce (CALL)
                            if spot >= ema_val and (spot - ema_val) <= cfg['zone'] and recent_low < ema_val:
                                contract = get_itm_option(cfg['name'], spot, cfg['step'], "CALL")
                                sl_pts = round(spot - recent_low + cfg['sl_buf'], 1)
                                tp_pts = round(sl_pts * 1.4, 1)
                                trade_counts[symbol] += 1
                                last_trade_times[symbol] = now_ist

                                msg = (
                                    f"🟢 *{cfg['name']} SCALP CALL BUY #{trade_counts[symbol]}* 🟢\n\n"
                                    f"🎯 *Option Strike:* `{contract}`\n"
                                    f"🔹 *Spot Price:* {spot:,.1f}\n"
                                    f"🛑 *Spot SL:* -{sl_pts} pts (~{round(sl_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"🎯 *Spot Target:* +{tp_pts} pts (~{round(tp_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"📈 *Pattern:* 20-EMA Dynamic Support Rebound\n\n"
                                    f"⚡ *Rule:* Trail SL to cost after 15-25 option points."
                                )
                                send_telegram_alert(msg)

                            # 2. Bearish Pullback Rejection (PUT)
                            elif spot <= ema_val and (ema_val - spot) <= cfg['zone'] and recent_high > ema_val:
                                contract = get_itm_option(cfg['name'], spot, cfg['step'], "PUT")
                                sl_pts = round(recent_high - spot + cfg['sl_buf'], 1)
                                tp_pts = round(sl_pts * 1.4, 1)
                                trade_counts[symbol] += 1
                                last_trade_times[symbol] = now_ist

                                msg = (
                                    f"🔴 *{cfg['name']} SCALP PUT BUY #{trade_counts[symbol]}* 🔴\n\n"
                                    f"🎯 *Option Strike:* `{contract}`\n"
                                    f"🔹 *Spot Price:* {spot:,.1f}\n"
                                    f"🛑 *Spot SL:* +{sl_pts} pts (~{round(sl_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"🎯 *Spot Target:* -{tp_pts} pts (~{round(tp_pts * cfg['opt_ratio'])} pts in option)\n"
                                    f"📉 *Pattern:* 20-EMA Dynamic Resistance Rejection\n\n"
                                    f"⚡ *Rule:* Trail SL to cost after 15-25 option points."
                                )
                                send_telegram_alert(msg)

                    time.sleep(1)

                if status_line:
                    print(f"[{now_ist.strftime('%H:%M:%S')}] Live: {' | '.join(status_line)}", flush=True)

            time.sleep(15)

        except Exception as e:
            print(f"Indian Market Scanner Exception: {e}", flush=True)
            time.sleep(10)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Indian Markets Trio Live!")

    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=indian_market_worker, daemon=True)
    t.start()
    run_server()
