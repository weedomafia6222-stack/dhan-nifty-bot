import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
import yfinance as yf
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Apna Telegram Bot Token dalein
TELEGRAM_CHAT_ID   = "1327677831"      # Apna Telegram Chat ID dalein

SYMBOL = "^NSEI"               # Yahoo Finance symbol for Nifty 50 Index
SWING_LOOKBACK = 12            # Recent 1-hour swings on 5m
COOLDOWN_MINUTES = 20

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

def get_nifty_data():
    try:
        ticker = yf.Ticker(SYMBOL)
        # Fetch 5m candles for the last 5 days
        df_5m = ticker.history(period="5d", interval="5m")
        if df_5m.empty:
            return None, None

        # Convert index to IST
        if df_5m.index.tz is None:
            df_5m.index = df_5m.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
        else:
            df_5m.index = df_5m.index.tz_convert('Asia/Kolkata')

        # Clean column names to lowercase
        df_5m.columns = [c.lower() for c in df_5m.columns]

        # Resample to 15m for macro trend filter
        df_15m = df_5m.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()
        df_15m['ema200'] = df_15m['close'].ewm(span=200, adjust=False).mean()

        return df_5m, df_15m
    except Exception as e:
        print(f"Yahoo Data Fetch Error: {e}")
        return None, None

def get_itm_option(spot_price, trade_type):
    atm = round(spot_price / 50) * 50
    if trade_type == "CALL":
        strike = atm - 100
        return f"{strike} CE"
    else:
        strike = atm + 100
        return f"{strike} PE"

def nifty_bot_worker():
    print("🇮🇳 Hands-Free Nifty 50 Multi-Timeframe Bot Started...")
    send_telegram_alert(
        "🇮🇳 *NIFTY MULTI-TIMEFRAME SCALPER ACTIVE (ZERO-TOKEN)!*\n\n"
        "📈 *Macro Filter:* 15-Minute 200 EMA\n"
        "⚡ *Trigger:* 5-Minute Liquidity Sweep\n"
        "🛡 *Anti-Decay:* Deep ITM Strike Recommendation"
    )

    last_trade_time = None
    trades_count = 0
    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            # Active Market Hours: Mon-Fri, 09:20 AM to 03:20 PM
            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=20, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    trades_count = 0

                df_5m, df_15m = get_nifty_data()

                if df_5m is not None and df_15m is not None and len(df_15m) > 10:
                    macro_close = df_15m.iloc[-2]['close']
                    macro_ema   = df_15m.iloc[-2]['ema200']

                    macro_bullish = macro_close > macro_ema
                    macro_bearish = macro_close < macro_ema

                    recent_high = df_5m['high'].iloc[-(SWING_LOOKBACK + 2):-2].max()
                    recent_low  = df_5m['low'].iloc[-(SWING_LOOKBACK + 2):-2].min()
                    trigger_5m  = df_5m.iloc[-2]
                    curr_price  = df_5m.iloc[-1]['close']

                    can_trade = True
                    if last_trade_time:
                        passed = (now_ist - last_trade_time).total_seconds() / 60
                        if passed < COOLDOWN_MINUTES:
                            can_trade = False

                    if can_trade:
                        # Bullish Setup -> Buy CALL
                        if (macro_bullish and 
                            trigger_5m['low'] < recent_low and 
                            trigger_5m['close'] > recent_low and 
                            trigger_5m['close'] > trigger_5m['open']):

                            itm_contract = get_itm_option(curr_price, "CALL")
                            spot_sl  = round(curr_price - trigger_5m['low'] + 6, 1)
                            spot_tp  = round(spot_sl * 1.3, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🟢 *NIFTY 15m+5m CALL BUY #{trades_count}* 🟢\n\n"
                                f"🎯 *Recommended Contract:* `NIFTY {itm_contract}`\n"
                                f"🔹 *Nifty Spot:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* -{spot_sl} pts (Approx ~{round(spot_sl * 0.7)} pts in Premium)\n"
                                f"🎯 *Spot Target:* +{spot_tp} pts (Approx ~{round(spot_tp * 0.7)} pts in Premium)\n"
                                f"📈 *15m Trend:* Strong Bullish (> 200 EMA)\n\n"
                                f"⚡ *Rule:* Deep ITM contract ensures maximum delta & zero theta drag."
                            )
                            send_telegram_alert(msg)

                        # Bearish Setup -> Buy PUT
                        elif (macro_bearish and 
                              trigger_5m['high'] > recent_high and 
                              trigger_5m['close'] < recent_high and 
                              trigger_5m['close'] < trigger_5m['open']):

                            itm_contract = get_itm_option(curr_price, "PUT")
                            spot_sl  = round(trigger_5m['high'] - curr_price + 6, 1)
                            spot_tp  = round(spot_sl * 1.3, 1)
                            trades_count += 1
                            last_trade_time = now_ist

                            msg = (
                                f"🔴 *NIFTY 15m+5m PUT BUY #{trades_count}* 🔴\n\n"
                                f"🎯 *Recommended Contract:* `NIFTY {itm_contract}`\n"
                                f"🔹 *Nifty Spot:* {curr_price:,.1f}\n"
                                f"🛑 *Spot SL:* +{spot_sl} pts (Approx ~{round(spot_sl * 0.7)} pts in Premium)\n"
                                f"🎯 *Spot Target:* -{spot_tp} pts (Approx ~{round(spot_tp * 0.7)} pts in Premium)\n"
                                f"📉 *15m Trend:* Strong Bearish (< 200 EMA)\n\n"
                                f"⚡ *Rule:* Deep ITM contract ensures maximum delta & zero theta drag."
                            )
                            send_telegram_alert(msg)

            time.sleep(20)

        except Exception as e:
            print(f"Worker Loop Warning: {e}")
            time.sleep(15)

# Render Keep-Alive Port Ping (No Timeout)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Nifty Bot Healthy & Running!")

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
