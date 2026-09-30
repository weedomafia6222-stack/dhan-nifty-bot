import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from bs4 import BeautifulSoup
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Apna Telegram Bot Token dalein
TELEGRAM_CHAT_ID   = "1327677831"      # Apna Telegram Chat ID dalein

EMA_PERIOD = 20           # 20-EMA for active dynamic pullbacks
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

def fetch_live_nifty_spot():
    """Ultra-fast live spot fetch via Google Finance - 100% open on cloud IPs"""
    try:
        url = "https://www.google.com/finance/quote/NIFTY_50:INDEXNSE"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            price_div = soup.find("div", {"class": "YMlKec fxKbKc"})
            if price_div:
                price_text = price_div.text.replace("₹", "").replace(",", "").strip()
                return float(price_text)
    except Exception as e:
        print(f"Google Feed Notice: {e}", flush=True)
    return None

def get_itm_option(spot_price, trade_type):
    atm = round(spot_price / 50) * 50
    return f"{atm - 50} CE" if trade_type == "CALL" else f"{atm + 50} PE"

def nifty_bot_worker():
    print("🚀 Nifty 50 Cloud-Safe Scalper Live...", flush=True)
    send_telegram_alert("🇮🇳 *NIFTY 50 LIVE BOT ACTIVE!*\nSwitched to cloud-safe feed. Monitoring live ticks.")

    price_history = []
    last_trade_time = None
    trades_count = 0
    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            # Active Market Hours: Mon-Fri, 09:15 AM to 03:30 PM
            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    trades_count = 0
                    price_history.clear()

                spot = fetch_live_nifty_spot()

                if spot:
                    price_history.append({"time": now_ist, "price": spot})
                    # Keep latest 60 data points (approx 15-20 mins)
                    if len(price_history) > 60:
                        price_history.pop(0)

                    df = pd.DataFrame(price_history)
                    df['ema'] = df['price'].ewm(span=EMA_PERIOD, adjust=False).mean()

                    ema_val = round(df['ema'].iloc[-1], 1)
                    recent_low = df['price'].tail(15).min()
                    recent_high = df['price'].tail(15).max()

                    print(f"[{now_ist.strftime('%H:%M:%S')}] Nifty Spot: {spot:,.1f} | EMA: {ema_val:,.1f} | Scanning...", flush=True)

                    cooldown_passed = True
                    if last_trade_time:
                        passed = (now_ist - last_trade_time).total_seconds() / 60
                        if passed < COOLDOWN_MINUTES:
                            cooldown_passed = False

                    # Need at least 20 ticks to form EMA base
                    if len(df) >= EMA_PERIOD and cooldown_passed:
                        # 1. Bullish Dip-Bounce Setup (Price near EMA and bouncing up)
                        if spot >= ema_val and (spot - ema_val) <= 10 and recent_low < ema_val:
                            contract = get_itm_option(spot, "CALL")
                            sl_pts = round(spot - recent_low + 6, 1)
                            tp_pts = round(sl_pts * 1.4, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🟢 *NIFTY SCALP CALL BUY #{trades_count}* 🟢\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {spot:,.1f}\n"
                                f"🛑 *Spot SL:* -{sl_pts} pts (~{round(sl_pts * 0.7)} pts in option)\n"
                                f"🎯 *Spot Target:* +{tp_pts} pts (~{round(tp_pts * 0.7)} pts in option)\n"
                                f"📈 *Setup:* 20-EMA Dynamic Support Rebound\n\n"
                                f"⚡ *Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

                        # 2. Bearish Pullback-Rejection Setup (Price near EMA and rejecting down)
                        elif spot <= ema_val and (ema_val - spot) <= 10 and recent_high > ema_val:
                            contract = get_itm_option(spot, "PUT")
                            sl_pts = round(recent_high - spot + 6, 1)
                            tp_pts = round(sl_pts * 1.4, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🔴 *NIFTY SCALP PUT BUY #{trades_count}* 🔴\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {spot:,.1f}\n"
                                f"🛑 *Spot SL:* +{sl_pts} pts (~{round(sl_pts * 0.7)} pts in option)\n"
                                f"🎯 *Spot Target:* -{tp_pts} pts (~{round(tp_pts * 0.7)} pts in option)\n"
                                f"📉 *Setup:* 20-EMA Dynamic Resistance Rejection\n\n"
                                f"⚡ *Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

            time.sleep(15)

        except Exception as e:
            print(f"Loop Notice: {e}", flush=True)
            time.sleep(10)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nifty Bot Live & Scanning!")

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
