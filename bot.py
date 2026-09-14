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


def detect_candle_pattern(candles_window):
    """Rozpoznaje formację świecową na bazie ostatnich 1-3 świec.
    candles_window: lista świec, gdzie ostatnia (candles_window[-1]) to bieżąca."""
    candle = candles_window[-1]
    prev_candle = candles_window[-2]

    body = abs(candle["close"] - candle["open"])
    range_ = candle["high"] - candle["low"]
    if range_ == 0:
        return None
    upper_wick = candle["high"] - max(candle["close"], candle["open"])
    lower_wick = min(candle["close"], candle["open"]) - candle["low"]

    prev_body = abs(prev_candle["close"] - prev_candle["open"])
    prev_is_down = prev_candle["close"] < prev_candle["open"]
    prev_is_up = prev_candle["close"] > prev_candle["open"]

    # --- Formacje 3-świecowe (sprawdzane najpierw, bo są bardziej specyficzne) ---
    if len(candles_window) >= 3:
        c1, c2, c3 = candles_window[-3], candles_window[-2], candles_window[-1]
        c1_body = abs(c1["close"] - c1["open"])
        c3_body = abs(c3["close"] - c3["open"])
        # Morning Star: spadek, mała świeca (niezdecydowanie), silny wzrost
        if (c1["close"] < c1["open"] and c1_body > 0
                and abs(c2["close"] - c2["open"]) < c1_body * 0.4
                and c3["close"] > c3["open"] and c3_body > c1_body * 0.6
                and c3["close"] > (c1["open"] + c1["close"]) / 2):
            return "Morning Star (silne odbicie w górę)"
        # Evening Star: wzrost, mała świeca, silny spadek
        if (c1["close"] > c1["open"] and c1_body > 0
                and abs(c2["close"] - c2["open"]) < c1_body * 0.4
                and c3["close"] < c3["open"] and c3_body > c1_body * 0.6
                and c3["close"] < (c1["open"] + c1["close"]) / 2):
            return "Evening Star (silne odwrócenie w dół)"

    # --- Formacje 1-świecowe zależne od kontekstu (trend przed świecą) ---
    if body / range_ < 0.1:
        return "Doji (niezdecydowanie)"

    if lower_wick > body * 2 and upper_wick < body * 0.5:
        # Długi dolny knot: Hammer (po spadku) lub Hanging Man (po wzroście)
        if prev_is_down:
            return "Hammer (możliwe odbicie w górę)"
        elif prev_is_up:
            return "Hanging Man (ostrzeżenie przed spadkiem)"
        return "Pin bar / długi dolny knot"

    if upper_wick > body * 2 and lower_wick < body * 0.5:
        # Długi górny knot: Shooting Star (po wzroście) lub Inverted Hammer (po spadku)
        if prev_is_up:
            return "Shooting Star (ostrzeżenie przed spadkiem)"
        elif prev_is_down:
            return "Inverted Hammer (możliwe odbicie w górę)"
        return "Pin bar odwrócony / długi górny knot"

    # --- Engulfing ---
    if (candle["close"] > candle["open"] and prev_candle["close"] < prev_candle["open"]
            and candle["close"] > prev_candle["open"] and candle["open"] < prev_candle["close"]
            and body > prev_body):
        return "Bullish Engulfing"
    if (candle["close"] < candle["open"] and prev_candle["close"] > prev_candle["open"]
            and candle["open"] > prev_candle["close"] and candle["close"] < prev_candle["open"]
            and body > prev_body):
        return "Bearish Engulfing"
    return None


def atr(candles, period=14):
    """Average True Range - miara zmienności, używana do sugerowania SL/TP."""
    if len(candles) < period + 1:
        period = len(candles) - 1
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period if trs else 0


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

    # Trend na podstawie EMA (to jest OPÓŹNIONE - może się mylić przy świeżym zwrocie)
    trend = "wzrostowy" if ema20[-1] > ema50[-1] else "spadkowy"
    trend_strength = abs(ema20[-1] - ema50[-1]) / current_price * 100

    pattern = detect_candle_pattern(candles[-3:] if len(candles) >= 3 else candles[-2:])
    current_atr = atr(candles)

    # Zmiana ceny za noc / ostatnie N świec (np. ostatnie 16 świec 30m = ~8h)
    lookback = min(16, len(closes) - 1)
    overnight_change_pct = (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100

    # ---- NOWE: świeże momentum z ostatnich 3-4 świec (żeby złapać zwrot zanim EMA go zauważy) ----
    recent_n = min(4, len(closes) - 1)
    recent_closes = closes[-(recent_n + 1):]
    recent_change_pct = (recent_closes[-1] - recent_closes[0]) / recent_closes[0] * 100
    # czy ostatnie świece konsekwentnie spadają / rosną
    recent_diffs = [recent_closes[i + 1] - recent_closes[i] for i in range(len(recent_closes) - 1)]
    falling_streak = all(d < 0 for d in recent_diffs)
    rising_streak = all(d > 0 for d in recent_diffs)

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

    # Klasyfikacja formacji świecowej: bycza / niedźwiedzia / ostrzegawcza
    bullish_patterns = ("Hammer", "Bullish Engulfing", "Morning Star", "Inverted Hammer")
    bearish_patterns = ("Bearish Engulfing", "Evening Star", "Shooting Star", "Hanging Man")
    if pattern and any(p in pattern for p in bullish_patterns):
        score += 10
        direction = "long"
    if pattern and any(p in pattern for p in bearish_patterns):
        score -= 10
        direction = "short"

    # ---- NOWE: świeże momentum ma DUŻĄ wagę - przebija opóźnione EMA ----
    momentum_override = None
    if falling_streak and abs(recent_change_pct) > 0.3:
        score -= 20
        momentum_override = "short"
    elif rising_streak and abs(recent_change_pct) > 0.3:
        score += 20
        momentum_override = "long"

    score = max(0, min(100, score))

    # Kierunek finalny: świeże momentum ma pierwszeństwo nad opóźnionym trendem EMA
    if momentum_override:
        direction = momentum_override
    elif direction == "neutralny":
        # Podniesiony próg (był >55/<45) - score blisko środka = szczerze "neutralny", nie fałszywy sygnał
        direction = "long" if score >= 65 else ("short" if score <= 35 else "neutralny")

    # ---- Sugerowane poziomy entry/SL/TP na bazie ATR (miara zmienności) ----
    # To orientacyjne poziomy, nie rekomendacja - zawsze weryfikuj samodzielnie.
    entry_zone = None
    stop_loss = None
    take_profit_1 = None
    take_profit_2 = None
    if direction == "long":
        entry_zone = (current_price - current_atr * 0.3, current_price)
        stop_loss = current_price - current_atr * 1.5
        take_profit_1 = current_price + current_atr * 1.5
        take_profit_2 = current_price + current_atr * 3
    elif direction == "short":
        entry_zone = (current_price, current_price + current_atr * 0.3)
        stop_loss = current_price + current_atr * 1.5
        take_profit_1 = current_price - current_atr * 1.5
        take_profit_2 = current_price - current_atr * 3

    return {
        "price": current_price,
        "trend": trend,
        "trend_strength": trend_strength,
        "rsi": current_rsi,
        "volume_ratio": volume_ratio,
        "pattern": pattern,
        "atr": current_atr,
        "overnight_change_pct": overnight_change_pct,
        "recent_change_pct": recent_change_pct,
        "momentum_warning": momentum_override,
        "score": score,
        "direction": direction,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "candles": candles,  # potrzebne do wygenerowania wykresu
    }


def format_report(a, symbol):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"📊 *{symbol}* — {now}",
        f"Cena: `{a['price']:.2f}`",
        f"Trend (EMA20 vs EMA50): *{a['trend']}* (siła: {a['trend_strength']:.2f}%) [wskaźnik opóźniony]",
        f"RSI(14): {a['rsi']:.1f}" if a["rsi"] else "RSI: brak danych",
        f"Wolumen vs średnia(20): {a['volume_ratio']:.2f}x",
        f"Zmiana za ostatnie ~8h: {a['overnight_change_pct']:+.2f}%",
        f"Świeże momentum (ostatnie świece): {a['recent_change_pct']:+.2f}%",
    ]
    if a.get("momentum_warning"):
        lines.append(f"⚠️ Świeże momentum ({a['momentum_warning'].upper()}) przebija opóźniony trend EMA!")
    if a["pattern"]:
        lines.append(f"Formacja świecowa: {a['pattern']}")
    lines.append("")
    lines.append(f"🎯 Signal score: *{a['score']}/100* → kierunek: *{a['direction'].upper()}*")
    if a["direction"] in ("long", "short") and a["entry_zone"]:
        lines.append("")
        lines.append(f"📍 Orientacyjne poziomy (ATR={a['atr']:.2f}):")
        lines.append(f"   Entry: `{a['entry_zone'][0]:.2f} - {a['entry_zone'][1]:.2f}`")
        lines.append(f"   SL: `{a['stop_loss']:.2f}`")
        lines.append(f"   TP1: `{a['take_profit_1']:.2f}`  TP2: `{a['take_profit_2']:.2f}`")
    lines.append("_To wskaźnik techniczny, nie gwarancja. Zawsze rób własny research._")
    return "\n".join(lines)


# ---------------------- WYKRES ----------------------
def generate_chart(a, symbol, n_candles=40):
    """Rysuje wykres świecowy (ostatnie n_candles) + wolumen + poziomy entry/SL/TP.
    Zwraca bytes PNG gotowe do wysłania na Telegram."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from io import BytesIO

    candles = a["candles"][-n_candles:]
    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
        facecolor="#0d1117"
    )
    for ax in (ax_price, ax_vol):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#30363d")

    up_color, down_color = "#26a69a", "#ef5350"
    width = 0.6

    for i, c in enumerate(candles):
        color = up_color if c["close"] >= c["open"] else down_color
        # knot
        ax_price.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1)
        # korpus
        body_low = min(c["open"], c["close"])
        body_height = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax_price.add_patch(Rectangle((i - width / 2, body_low), width, body_height,
                                      facecolor=color, edgecolor=color))
        # wolumen
        ax_vol.bar(i, c["volume"], color=color, width=width)

    # Linie entry / SL / TP
    if a["direction"] in ("long", "short") and a["entry_zone"]:
        ax_price.axhline(a["stop_loss"], color="#ef5350", linestyle="--", linewidth=1, label="SL")
        ax_price.axhline(a["take_profit_1"], color="#26a69a", linestyle="--", linewidth=1, label="TP1")
        ax_price.axhline(a["take_profit_2"], color="#26a69a", linestyle=":", linewidth=1, label="TP2")
        ax_price.axhspan(a["entry_zone"][0], a["entry_zone"][1], color="#f0b90b", alpha=0.15)
        ax_price.legend(loc="upper left", facecolor="#0d1117", labelcolor="#c9d1d9", framealpha=0.7)

    ax_price.set_title(f"{symbol} — score {a['score']}/100 ({a['direction'].upper()})",
                        color="#c9d1d9", fontsize=12)
    ax_vol.set_xlabel("Świece (najnowsza po prawej)", color="#c9d1d9")
    plt.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------- TELEGRAM ----------------------
def send_photo(api_url, chat_id, photo_bytes, caption=""):
    try:
        files = {"photo": ("chart.png", photo_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "Markdown"}
        requests.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=15)
    except Exception as e:
        log.error(f"Błąd wysyłki zdjęcia Telegram: {e}")


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
                    for symbol in SYMBOLS:
                        result = analyze_symbol(symbol)
                        caption = format_report(result, symbol)
                        try:
                            chart = generate_chart(result, symbol)
                            send_photo(api_url, chat_id, chart, caption=caption)
                        except Exception as chart_err:
                            log.error(f"Błąd generowania wykresu {symbol}: {chart_err}")
                            send_message(api_url, chat_id, caption)

            # 2) Auto-alert jeśli sygnał jest mocny, osobno dla każdego symbolu
            for symbol in SYMBOLS:
                result = analyze_symbol(symbol)
                if result["score"] >= SIGNAL_THRESHOLD or result["score"] <= (100 - SIGNAL_THRESHOLD):
                    if result["score"] != last_auto_alert_score[symbol]:
                        caption = "🔥 *Mocny sygnał wykryty!*\n\n" + format_report(result, symbol)
                        try:
                            chart = generate_chart(result, symbol)
                            send_photo(api_url, default_chat_id, chart, caption=caption)
                        except Exception as chart_err:
                            log.error(f"Błąd generowania wykresu {symbol}: {chart_err}")
                            send_message(api_url, default_chat_id, caption)
                        last_auto_alert_score[symbol] = result["score"]

        except Exception as e:
            log.error(f"Błąd w pętli głównej: {e}")

        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
