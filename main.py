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
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Apna Bot Token dalein
TELEGRAM_CHAT_ID   = "1327677831"      # Apna Chat ID dalein

EMA_PERIOD = 50        # Intraday dynamic trend filter
SWING_LOOKBACK = 10    # Lookback candles
COOLDOWN_MINUTES = 15

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
        print(f"Telegram Error: {e}")

def fetch_nse_nifty_candles():
    """Fetches intraday 5m data using browser session bypassing Render block"""
    url = "https://www.nseindia.com/api/chart-databyindex?index=NIFTY%2050&indices=true"
    session = cureq.Session(impersonate="chrome120")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "*/*"
    })
    
    # Cookie warmup
    session.get("https://www.nseindia.com", timeout=10)
    r = session.get(url, timeout=10)
    if r.status_code == 200:
        data = r.json()
        points = data.get("grapthData", [])
        if points:
            records = []
            for p in points:
                # [timestamp_ms, price]
                records.append({
                    "timestamp": pd.to_datetime(p[0], unit='ms'),
                    "price": float(p[1])
                })
            df = pd.DataFrame(records)
            df.set_index("timestamp", inplace=True)
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
            
            # Resample tick data to 5-minute OHLC
            df_5m = df['price'].resample('5min').ohlc().dropna()
            df_5m['ema'] = df_5m['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
            return df_5m
    return None

def get_itm_option(spot_price, trade_type):
    atm = round(spot_price / 50) * 50
    if trade_type == "CALL":
        return f"{atm - 50} CE"
    else:
        return f"{atm + 50} PE"

def nifty_bot_worker():
    print("🚀 Nifty 50 NSE Feed Engine Started...")
    send_telegram_alert("🇮🇳 *NIFTY 50 LIVE BOT ENGAGED!*\nBypassed cloud rate-limits. Monitoring live NSE candles.")

    last_trade_time = None
    trades_count = 0
    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            # Market Hours: 09:15 AM to 03:30 PM (Mon-Fri)
            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    trades_count = 0

                df_5m = fetch_nse_nifty_candles()

                if df_5m is not None and len(df_5m) > 15:
                    curr_price = df_5m.iloc[-1]['close']
                    last_closed = df_5m.iloc[-2]
                    ema_val = last_closed['ema']

                    recent_high = df_5m['high'].iloc[-(SWING_LOOKBACK + 2):-2].max()
                    recent_low  = df_5m['low'].iloc[-(SWING_LOOKBACK + 2):-2].min()

                    print(f"[{now_ist.strftime('%H:%M:%S')}] Nifty: {curr_price:.1f} | EMA: {ema_val:.1f} | Scanning...")

                    cooldown_passed = True
                    if last_trade_time:
                        passed = (now_ist - last_trade_time).total_seconds() / 60
                        if passed < COOLDOWN_MINUTES:
                            cooldown_passed = False

                    if cooldown_passed:
                        # CALL Setup (Bullish Sweep)
                        if (last_closed['close'] > ema_val and 
                            last_closed['low'] < recent_low and 
                            last_closed['close'] > recent_low):

                            contract = get_itm_option(curr_price, "CALL")
                            sl_pts = round(curr_price - last_closed['low'] + 5, 1)
                            tp_pts = round(sl_pts * 1.3, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🟢 *NIFTY 5m CALL BUY #{trades_count}* 🟢\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* -{sl_pts} pts\n"
                                f"🎯 *Spot Target:* +{tp_pts} pts\n"
                                f"📈 *Trend:* Bullish (> EMA {EMA_PERIOD})\n\n"
                                f"⚡ *Scalp Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

                        # PUT Setup (Bearish Sweep)
                        elif (last_closed['close'] < ema_val and 
                              last_closed['high'] > recent_high and 
                              last_closed['close'] < recent_high):

                            contract = get_itm_option(curr_price, "PUT")
                            sl_pts = round(last_closed['high'] - curr_price + 5, 1)
                            tp_pts = round(sl_pts * 1.3, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🔴 *NIFTY 5m PUT BUY #{trades_count}* 🔴\n\n"
                                f"🎯 *Option Strike:* `NIFTY {contract}`\n"
                                f"🔹 *Spot Price:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* +{sl_pts} pts\n"
                                f"🎯 *Spot Target:* -{tp_pts} pts\n"
                                f"📉 *Trend:* Bearish (< EMA {EMA_PERIOD})\n\n"
                                f"⚡ *Scalp Rule:* Trail SL to cost after 15 premium points."
                            )
                            send_telegram_alert(msg)

            time.sleep(30)

        except Exception as e:
            print(f"Data Loop Notice: {e}")
            time.sleep(20)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nifty Bot Healthy!")

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
