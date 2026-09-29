import os
import time
import threading
from datetime import datetime
import pytz
import pandas as pd
import requests
from dhanhq import dhanhq
from http.server import HTTPServer, BaseHTTPRequestHandler

# ----------------- CONFIGURATION -----------------
DHAN_CLIENT_ID     = "1106958122"        # Apna 10-digit Dhan ID yahan dalein
DHAN_ACCESS_TOKEN  = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJ1c2VyUmVnaW9uIjoiUjEiLCJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzkwNzk1NTAyLCJpYXQiOjE3OTA3MDkxMDIsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA2OTU4MTIyIn0.FV96YlgYQsaiuJJjOy-HAVYPGOoHVztT70W8vbR9zEuY7vE9f4JT03tXFRyvdiZVOp20tO_KynqjcPPxDsMLKg"     # Jo token abhi copy kiya wo yahan dalein

TELEGRAM_BOT_TOKEN = "8608122374:AAF5OXFFo4pKrhda8RyThOCs9dN0zkd0V14"    # Wahi Telegram Bot Token
TELEGRAM_CHAT_ID   = "1327677831"      # Wahi Telegram Chat ID

SECURITY_ID        = "13"          # Nifty 50 Index on Dhan
EXCHANGE_SEGMENT   = "IDX_I"

SWING_LOOKBACK     = 12            # Recent 1-hour swings on 5m
COOLDOWN_MINUTES   = 20

dhan = dhanhq(client_id=DHAN_CLIENT_ID, access_token=DHAN_ACCESS_TOKEN)

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

def get_resampled_nifty_data():
    try:
        response = dhan.historical_minute_data(
            security_id=SECURITY_ID,
            exchange_segment=EXCHANGE_SEGMENT,
            instrument_type="INDEX"
        )
        if not response or 'data' not in response:
            return None, None
            
        raw = pd.DataFrame(response['data'])
        raw['datetime'] = pd.to_datetime(raw['start_Time']).dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
        raw.set_index('datetime', inplace=True)
        raw.sort_index(inplace=True)

        # 5-Minute Timeframe
        df_5m = raw.resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()

        # 15-Minute Timeframe + 200 EMA
        df_15m = raw.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()
        df_15m['ema200'] = df_15m['close'].ewm(span=200, adjust=False).mean()

        return df_5m, df_15m
    except Exception as e:
        print(f"Data Fetch Error: {e}")
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
    print("🇮🇳 Dhan Multi-Timeframe Nifty Scalper Started...")
    send_telegram_alert(
        "🇮🇳 *NIFTY MULTI-TIMEFRAME SCALPER ACTIVE!*\n\n"
        "📈 *Macro Filter:* 15-Minute 200 EMA\n"
        "⚡ *Trigger:* 5-Minute Liquidity Sweep\n"
        "🛡 *Anti-Decay:* Deep In-The-Money (ITM) Strike Selection"
    )

    last_trade_time = None
    trades_count = 0
    current_day = None

    while True:
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now_ist = datetime.now(ist)

            # Active Market Hours: Mon-Fri, 09:20 AM to 03:15 PM
            is_weekday = now_ist.weekday() < 5
            market_start = now_ist.replace(hour=9, minute=20, second=0, microsecond=0)
            market_end   = now_ist.replace(hour=15, minute=15, second=0, microsecond=0)

            if is_weekday and (market_start <= now_ist <= market_end):
                if current_day != now_ist.day:
                    current_day = now_ist.day
                    trades_count = 0

                df_5m, df_15m = get_resampled_nifty_data()

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

# Render Port Keep-Alive
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
