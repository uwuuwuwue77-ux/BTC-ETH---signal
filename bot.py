"""
ETH/USDT Signal Bot for Telegram
---------------------------------
Pobiera świece z Binance (publiczne API, bez klucza), liczy wskaźniki
techniczne (RSI, EMA, wolumen, formacje świecowe) i wysyła podsumowanie
na Telegram — albo na żądanie (/analiza), albo automatycznie co interwał,
jeśli wykryje mocny sygnał.

WAŻNE: To są wskaźniki techniczne, nie gwarancja ani realna "szansa
matematyczna" sukcesu. Traktuj to jako pomoc do własnej analizy, nie
jako automatyczny sygnał do ślepego wejścia.
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eth_bot")

# ---------- KONFIGURACJA (z zmiennych środowiskowych) ----------
# Wczytywane leniwie (dopiero w main()), żeby moduł dało się importować/testować
# bez ustawionych zmiennych środowiskowych.
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "ETHUSDT,BTCUSDT").split(",")]
INTERVAL = os.environ.get("INTERVAL", "30m")             # świece 30-minutowe
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "900"))  # co 15 min
SIGNAL_THRESHOLD = int(os.environ.get("SIGNAL_THRESHOLD", "70"))  # od kiedy wysyłać auto-alert

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def get_config():
    """Wczytuje wymagane zmienne środowiskowe (dopiero gdy bot faktycznie startuje)."""
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    return token, chat_id, f"https://api.telegram.org/bot{token}"


# ---------------------- DANE RYNKOWE ----------------------
def get_klines(symbol, interval=INTERVAL, limit=100):
    """Pobiera świece OHLCV z Binance (publiczne, bez klucza API)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    r.raise_for_status()
    raw = r.json()
    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


# ---------------------- WSKAŹNIKI ----------------------
def ema(values, period):
    k = 2 / (period + 1)
    ema_vals = [values[0]]
    for v in values[1:]:
        ema_vals.append(v * k + ema_vals[-1] * (1 - k))
    return ema_vals


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_candle_pattern(candle, prev_candle):
    body = abs(candle["close"] - candle["open"])
    range_ = candle["high"] - candle["low"]
    if range_ == 0:
        return None
    upper_wick = candle["high"] - max(candle["close"], candle["open"])
    lower_wick = min(candle["close"], candle["open"]) - candle["low"]

    if body / range_ < 0.1:
        return "Doji (niezdecydowanie)"
    if lower_wick > body * 2 and candle["close"] > candle["open"]:
        return "Pin bar / Hammer (możliwe odbicie w górę)"
    if upper_wick > body * 2 and candle["close"] < candle["open"]:
        return "Pin bar odwrócony (możliwe odbicie w dół)"
    # Engulfing
    prev_body = abs(prev_candle["close"] - prev_candle["open"])
    if (candle["close"] > candle["open"] and prev_candle["close"] < prev_candle["open"]
            and candle["close"] > prev_candle["open"] and candle["open"] < prev_candle["close"]
            and body > prev_body):
        return "Bullish Engulfing"
    if (candle["close"] < candle["open"] and prev_candle["close"] > prev_candle["open"]
            and candle["open"] > prev_candle["close"] and candle["close"] < prev_candle["open"]
            and body > prev_body):
        return "Bearish Engulfing"
    return None


# ---------------------- ANALIZA ----------------------
def analyze(candles):
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50) if len(closes) >= 50 else ema(closes, len(closes) - 1)
    current_price = closes[-1]
    current_rsi = rsi(closes)

    avg_volume = sum(volumes[-20:]) / min(20, len(volumes))
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume if avg_volume else 1

    # Trend na podstawie EMA
    trend = "wzrostowy" if ema20[-1] > ema50[-1] else "spadkowy"
    trend_strength = abs(ema20[-1] - ema50[-1]) / current_price * 100

    pattern = detect_candle_pattern(candles[-1], candles[-2])

    # Zmiana ceny za noc / ostatnie N świec (np. ostatnie 16 świec 30m = ~8h)
    lookback = min(16, len(closes) - 1)
    overnight_change_pct = (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100

    # ---- Prosty composite "signal score" (0-100), NIE prawdziwe prawdopodobieństwo ----
    score = 50
    direction = "neutralny"

    if trend == "wzrostowy":
        score += 10
    else:
        score -= 10

    if current_rsi is not None:
        if current_rsi < 30:
            score += 15  # wyprzedanie -> możliwe odbicie w górę
            direction = "long"
        elif current_rsi > 70:
            score -= 15  # wykupienie -> możliwa korekta w dół
            direction = "short"

    if volume_ratio > 1.5:
        score += 10 if trend == "wzrostowy" else -10

    if pattern and "górę" in (pattern or ""):
        score += 10
        direction = "long"
    if pattern and "dół" in (pattern or ""):
        score -= 10
        direction = "short"
    if pattern == "Bullish Engulfing":
        score += 10
        direction = "long"
    if pattern == "Bearish Engulfing":
        score -= 10
        direction = "short"

    score = max(0, min(100, score))
    if direction == "neutralny":
        direction = "long" if score > 55 else ("short" if score < 45 else "neutralny")

    return {
        "price": current_price,
        "trend": trend,
        "trend_strength": trend_strength,
        "rsi": current_rsi,
        "volume_ratio": volume_ratio,
        "pattern": pattern,
        "overnight_change_pct": overnight_change_pct,
        "score": score,
        "direction": direction,
    }


def format_report(a, symbol):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"📊 *{symbol}* — {now}",
        f"Cena: `{a['price']:.2f}`",
        f"Trend (EMA20 vs EMA50): *{a['trend']}* (siła: {a['trend_strength']:.2f}%)",
        f"RSI(14): {a['rsi']:.1f}" if a["rsi"] else "RSI: brak danych",
        f"Wolumen vs średnia(20): {a['volume_ratio']:.2f}x",
        f"Zmiana za ostatnie ~8h: {a['overnight_change_pct']:+.2f}%",
    ]
    if a["pattern"]:
        lines.append(f"Formacja świecowa: {a['pattern']}")
    lines.append("")
    lines.append(f"🎯 Signal score: *{a['score']}/100* → kierunek: *{a['direction'].upper()}*")
    lines.append("_To wskaźnik techniczny, nie gwarancja. Zawsze rób własny research._")
    return "\n".join(lines)


# ---------------------- TELEGRAM ----------------------
def send_message(api_url, chat_id, text):
    try:
        requests.post(f"{api_url}/sendMessage", data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
    except Exception as e:
        log.error(f"Błąd wysyłki Telegram: {e}")


def get_updates(api_url, offset=None):
    params = {"timeout": 20}
    if offset:
        params["offset"] = offset
    r = requests.get(f"{api_url}/getUpdates", params=params, timeout=25)
    return r.json().get("result", [])


# ---------------------- GŁÓWNA PĘTLA ----------------------
def analyze_symbol(symbol):
    candles = get_klines(symbol)
    return analyze(candles)


def main():
    token, default_chat_id, api_url = get_config()
    log.info(f"Bot startuje... obserwowane symbole: {SYMBOLS}")
    last_update_id = None
    last_auto_alert_score = {s: None for s in SYMBOLS}

    send_message(api_url, default_chat_id,
                 f"🤖 Bot wystartował. Śledzę: {', '.join(SYMBOLS)}. "
                 f"Wpisz /analiza żeby dostać raport na żądanie.")

    while True:
        try:
            # 1) Sprawdź komendy od użytkownika
            updates = get_updates(api_url, offset=last_update_id)
            for u in updates:
                last_update_id = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and text.strip().lower() in ("/analiza", "/start", "/analysis"):
                    reports = []
                    for symbol in SYMBOLS:
                        result = analyze_symbol(symbol)
                        reports.append(format_report(result, symbol))
                    send_message(api_url, chat_id, "\n\n---\n\n".join(reports))

            # 2) Auto-alert jeśli sygnał jest mocny, osobno dla każdego symbolu
            for symbol in SYMBOLS:
                result = analyze_symbol(symbol)
                if result["score"] >= SIGNAL_THRESHOLD or result["score"] <= (100 - SIGNAL_THRESHOLD):
                    if result["score"] != last_auto_alert_score[symbol]:
                        send_message(api_url, default_chat_id,
                                     "🔥 *Mocny sygnał wykryty!*\n\n" + format_report(result, symbol))
                        last_auto_alert_score[symbol] = result["score"]

        except Exception as e:
            log.error(f"Błąd w pętli głównej: {e}")

        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
