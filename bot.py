"""
ETH/BTC Signal Bot for Telegram
---------------------------------
Pobiera świece z Binance (publiczne API, bez klucza), analizuje wiele
interwałów naraz, wykrywa Fair Value Gaps, sweepy płynności, strukturę
rynku (HH/HL/LH/LL), poziomy 24h high/low, oraz proxy dla pozycjonowania
dużych graczy (funding rate + open interest z rynku futures).

WAŻNE: To są wskaźniki techniczne, nie gwarancja ani realna "szansa
matematyczna" sukcesu. Bot NIE wie co realnie robią konkretne duże firmy —
funding rate i open interest to tylko pośrednie wskaźniki pozycjonowania
całego rynku futures, nie insider info. Traktuj to jako pomoc do własnej
analizy, nie jako automatyczny sygnał do ślepego wejścia.
"""

import os
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eth_bot")

# ---------- SESJA HTTP z automatycznym retry/backoff ----------
# Chroni przed przejściowymi błędami sieci i rate-limitami (429) z Binance/Telegrama -
# zamiast wywalać cały cykl, próbuje ponownie z rosnącym opóźnieniem.
_session = requests.Session()
_retry = Retry(
    total=3, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))

# ---------- KONFIGURACJA ----------
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "ETHUSDT,BTCUSDT").split(",")]
TIMEFRAMES = [s.strip() for s in os.environ.get("TIMEFRAMES", "15m,1h,4h").split(",")]
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "900"))
# Auto-alert wysyłany gdy liczba zgodnych timeframe'ów >= próg (np. 2 z 3)
CONFLUENCE_THRESHOLD = int(os.environ.get("CONFLUENCE_THRESHOLD", "2"))
# Krótka pauza między requestami do Binance - dodatkowa ochrona przed rate-limitem
REQUEST_PAUSE_SECONDS = float(os.environ.get("REQUEST_PAUSE_SECONDS", "0.3"))

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"
FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FUTURES_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"


def get_config():
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    return token, chat_id, f"https://api.telegram.org/bot{token}"


# ---------------------- DANE RYNKOWE ----------------------
def get_klines(symbol, interval, limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = _session.get(BINANCE_KLINES_URL, params=params, timeout=10)
    r.raise_for_status()
    raw = r.json()
    return [{
        "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
    } for k in raw]


def get_24h_stats(symbol):
    """Wysoki/niski poziom z ostatnich 24h - naturalne wsparcie/opór."""
    r = _session.get(BINANCE_24H_URL, params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {
        "high": float(d["highPrice"]),
        "low": float(d["lowPrice"]),
        "change_pct": float(d["priceChangePercent"]),
        "volume": float(d["volume"]),
    }


def get_futures_flow(symbol):
    """Proxy dla pozycjonowania dużych graczy: funding rate + zmiana open interest.
    UWAGA: to nie jest wgląd w konkretne transakcje firm, tylko zagregowany
    wskaźnik z rynku kontraktów futures. Zwraca None jeśli dane niedostępne
    (np. para nie ma kontraktów perpetual)."""
    try:
        r1 = _session.get(FUTURES_FUNDING_URL, params={"symbol": symbol, "limit": 1}, timeout=10)
        r1.raise_for_status()
        funding = float(r1.json()[-1]["fundingRate"]) * 100  # w %

        r2 = _session.get(FUTURES_OI_URL,
                           params={"symbol": symbol, "period": "1h", "limit": 8}, timeout=10)
        r2.raise_for_status()
        oi_data = r2.json()
        if len(oi_data) >= 2:
            oi_change_pct = ((float(oi_data[-1]["sumOpenInterest"]) - float(oi_data[0]["sumOpenInterest"]))
                              / float(oi_data[0]["sumOpenInterest"]) * 100)
        else:
            oi_change_pct = None

        return {"funding_rate_pct": funding, "oi_change_pct": oi_change_pct}
    except Exception as e:
        log.warning(f"Brak danych futures dla {symbol}: {e}")
        return None


# ---------------------- WSKAŹNIKI PODSTAWOWE ----------------------
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


def atr(candles, period=14):
    if len(candles) < period + 1:
        period = len(candles) - 1
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period if trs else 0


def detect_candle_pattern(candles_window):
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

    if len(candles_window) >= 3:
        c1, c2, c3 = candles_window[-3], candles_window[-2], candles_window[-1]
        c1_body = abs(c1["close"] - c1["open"])
        c3_body = abs(c3["close"] - c3["open"])
        if (c1["close"] < c1["open"] and c1_body > 0
                and abs(c2["close"] - c2["open"]) < c1_body * 0.4
                and c3["close"] > c3["open"] and c3_body > c1_body * 0.6
                and c3["close"] > (c1["open"] + c1["close"]) / 2):
            return "Morning Star"
        if (c1["close"] > c1["open"] and c1_body > 0
                and abs(c2["close"] - c2["open"]) < c1_body * 0.4
                and c3["close"] < c3["open"] and c3_body > c1_body * 0.6
                and c3["close"] < (c1["open"] + c1["close"]) / 2):
            return "Evening Star"

    if body / range_ < 0.1:
        return "Doji"
    if lower_wick > body * 2 and upper_wick < body * 0.5:
        if prev_is_down:
            return "Hammer"
        elif prev_is_up:
            return "Hanging Man"
        return "Pin bar (dolny knot)"
    if upper_wick > body * 2 and lower_wick < body * 0.5:
        if prev_is_up:
            return "Shooting Star"
        elif prev_is_down:
            return "Inverted Hammer"
        return "Pin bar (górny knot)"
    if (candle["close"] > candle["open"] and prev_candle["close"] < prev_candle["open"]
            and candle["close"] > prev_candle["open"] and candle["open"] < prev_candle["close"]
            and body > prev_body):
        return "Bullish Engulfing"
    if (candle["close"] < candle["open"] and prev_candle["close"] > prev_candle["open"]
            and candle["open"] > prev_candle["close"] and candle["close"] < prev_candle["open"]
            and body > prev_body):
        return "Bearish Engulfing"
    return None


BULLISH_PATTERNS = ("Hammer", "Bullish Engulfing", "Morning Star", "Inverted Hammer")
BEARISH_PATTERNS = ("Bearish Engulfing", "Evening Star", "Shooting Star", "Hanging Man")


# ---------------------- SMART MONEY CONCEPTS ----------------------
def detect_fvg(candles, lookback=20):
    """Fair Value Gap: luka między świecą 1 a świecą 3 (świeca 2 jej nie wypełnia).
    Zwraca listę niedawnych, jeszcze niewypełnionych FVG."""
    gaps = []
    start = max(2, len(candles) - lookback)
    for i in range(start, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c1["high"] < c3["low"]:
            gap = {"type": "bullish", "top": c3["low"], "bottom": c1["high"], "index": i}
        elif c1["low"] > c3["high"]:
            gap = {"type": "bearish", "top": c1["low"], "bottom": c3["high"], "index": i}
        else:
            continue
        filled = False
        for later in candles[i + 1:]:
            if gap["bottom"] <= later["close"] <= gap["top"]:
                filled = True
                break
        if not filled:
            gaps.append(gap)
    return gaps[-3:]


def detect_liquidity_sweep(candles, swing_lookback=15):
    """Wykrywa sweep płynności: cena robi nowy ekstremum (wybija poprzedni
    swing high/low knotem) po czym zamyka się z powrotem wewnątrz zakresu -
    klasyczny 'stop hunt' zanim ruch odwróci się w drugą stronę."""
    if len(candles) < swing_lookback + 2:
        return None
    last = candles[-1]
    prior = candles[-(swing_lookback + 1):-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)

    if last["high"] > prior_high and last["close"] < prior_high:
        return {"type": "sweep_high", "level": prior_high}
    if last["low"] < prior_low and last["close"] > prior_low:
        return {"type": "sweep_low", "level": prior_low}
    return None


def find_swing_points(candles, window=3):
    highs, lows = [], []
    for i in range(window, len(candles) - window):
        segment = candles[i - window:i + window + 1]
        if candles[i]["high"] == max(c["high"] for c in segment):
            highs.append((i, candles[i]["high"]))
        if candles[i]["low"] == min(c["low"] for c in segment):
            lows.append((i, candles[i]["low"]))
    return highs, lows


def market_structure(candles):
    """HH+HL = struktura wzrostowa, LH+LL = struktura spadkowa, inaczej = mieszana."""
    highs, lows = find_swing_points(candles)
    if len(highs) < 2 or len(lows) < 2:
        return {"structure": "brak wystarczających danych", "detail": "",
                "hh": False, "hl": False, "lh": False, "ll": False}

    last_two_highs = highs[-2:]
    last_two_lows = lows[-2:]
    hh = last_two_highs[1][1] > last_two_highs[0][1]
    hl = last_two_lows[1][1] > last_two_lows[0][1]
    lh = last_two_highs[1][1] < last_two_highs[0][1]
    ll = last_two_lows[1][1] < last_two_lows[0][1]

    if hh and hl:
        structure = "wzrostowa (Higher High + Higher Low)"
    elif lh and ll:
        structure = "spadkowa (Lower High + Lower Low)"
    elif hh and ll:
        structure = "rozszerzająca się zmienność (Higher High + Lower Low)"
    elif lh and hl:
        structure = "zwężający się range (Lower High + Higher Low)"
    else:
        structure = "mieszana / bez wyraźnego kierunku"

    detail = (f"Ostatnie swingi — High: {last_two_highs[0][1]:.2f} → {last_two_highs[1][1]:.2f}, "
              f"Low: {last_two_lows[0][1]:.2f} → {last_two_lows[1][1]:.2f}")
    return {"structure": structure, "detail": detail, "hh": hh, "hl": hl, "lh": lh, "ll": ll}


# ---------------------- ANALIZA JEDNEGO TIMEFRAME ----------------------
def analyze_timeframe(candles):
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50) if len(closes) >= 50 else ema(closes, len(closes) - 1)
    current_price = closes[-1]
    current_rsi = rsi(closes)
    current_atr = atr(candles)

    avg_volume = sum(volumes[-20:]) / min(20, len(volumes))
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 1

    trend = "wzrostowy" if ema20[-1] > ema50[-1] else "spadkowy"
    pattern = detect_candle_pattern(candles[-3:] if len(candles) >= 3 else candles[-2:])

    recent_n = min(4, len(closes) - 1)
    recent_closes = closes[-(recent_n + 1):]
    recent_change_pct = (recent_closes[-1] - recent_closes[0]) / recent_closes[0] * 100
    recent_diffs = [recent_closes[i + 1] - recent_closes[i] for i in range(len(recent_closes) - 1)]
    falling_streak = all(d < 0 for d in recent_diffs)
    rising_streak = all(d > 0 for d in recent_diffs)

    structure = market_structure(candles)
    fvgs = detect_fvg(candles)
    sweep = detect_liquidity_sweep(candles)

    bullish_votes, bearish_votes = [], []

    if trend == "wzrostowy":
        bullish_votes.append("EMA20>EMA50")
    else:
        bearish_votes.append("EMA20<EMA50")

    if current_rsi is not None:
        if current_rsi < 30:
            bullish_votes.append(f"RSI wyprzedany ({current_rsi:.0f})")
        elif current_rsi > 70:
            bearish_votes.append(f"RSI wykupiony ({current_rsi:.0f})")

    if pattern in BULLISH_PATTERNS:
        bullish_votes.append(f"formacja {pattern}")
    elif pattern in BEARISH_PATTERNS:
        bearish_votes.append(f"formacja {pattern}")

    if falling_streak and abs(recent_change_pct) > 0.3:
        bearish_votes.append("świeże momentum spadkowe")
    elif rising_streak and abs(recent_change_pct) > 0.3:
        bullish_votes.append("świeże momentum wzrostowe")

    if structure.get("hh") and structure.get("hl"):
        bullish_votes.append("struktura HH+HL")
    elif structure.get("lh") and structure.get("ll"):
        bearish_votes.append("struktura LH+LL")

    if sweep:
        if sweep["type"] == "sweep_low":
            bullish_votes.append(f"sweep dołu @ {sweep['level']:.2f} (możliwe odbicie)")
        else:
            bearish_votes.append(f"sweep szczytu @ {sweep['level']:.2f} (możliwa korekta)")

    for gap in fvgs:
        if gap["type"] == "bullish":
            bullish_votes.append(f"niewypełniony bullish FVG {gap['bottom']:.2f}-{gap['top']:.2f}")
        else:
            bearish_votes.append(f"niewypełniony bearish FVG {gap['bottom']:.2f}-{gap['top']:.2f}")

    if len(bullish_votes) > len(bearish_votes):
        bias = "long"
    elif len(bearish_votes) > len(bullish_votes):
        bias = "short"
    else:
        bias = "neutralny"

    return {
        "price": current_price, "atr": current_atr, "rsi": current_rsi,
        "trend": trend, "volume_ratio": volume_ratio, "pattern": pattern,
        "structure": structure, "fvgs": fvgs, "sweep": sweep,
        "bullish_votes": bullish_votes, "bearish_votes": bearish_votes,
        "bias": bias, "candles": candles,
    }


# ---------------------- ANALIZA WIELO-INTERWAŁOWA ----------------------
def analyze_symbol(symbol):
    per_tf = {}
    for tf in TIMEFRAMES:
        try:
            candles = get_klines(symbol, tf)
            per_tf[tf] = analyze_timeframe(candles)
        except Exception as e:
            log.warning(f"Pominięto timeframe {tf} dla {symbol} (błąd: {e})")
        time.sleep(REQUEST_PAUSE_SECONDS)

    if not per_tf:
        raise RuntimeError(f"Nie udało się pobrać żadnego timeframe'u dla {symbol}")

    active_timeframes = list(per_tf.keys())

    stats_24h = None
    try:
        stats_24h = get_24h_stats(symbol)
    except Exception as e:
        log.warning(f"Brak danych 24h dla {symbol}: {e}")

    flow = get_futures_flow(symbol)

    biases = [per_tf[tf]["bias"] for tf in active_timeframes]
    long_count = biases.count("long")
    short_count = biases.count("short")
    if long_count >= CONFLUENCE_THRESHOLD and long_count > short_count:
        overall_bias = "long"
    elif short_count >= CONFLUENCE_THRESHOLD and short_count > long_count:
        overall_bias = "short"
    else:
        overall_bias = "mieszany / brak zgodności"

    base_tf_name = active_timeframes[0]
    base_tf = per_tf[base_tf_name]
    entry_zone = stop_loss = tp1 = tp2 = None
    if overall_bias == "long":
        p, a_ = base_tf["price"], base_tf["atr"]
        entry_zone = (p - a_ * 0.3, p)
        stop_loss = p - a_ * 1.5
        tp1, tp2 = p + a_ * 1.5, p + a_ * 3
    elif overall_bias == "short":
        p, a_ = base_tf["price"], base_tf["atr"]
        entry_zone = (p, p + a_ * 0.3)
        stop_loss = p + a_ * 1.5
        tp1, tp2 = p - a_ * 1.5, p - a_ * 3

    return {
        "symbol": symbol, "per_tf": per_tf, "active_timeframes": active_timeframes,
        "stats_24h": stats_24h, "flow": flow,
        "overall_bias": overall_bias, "long_count": long_count, "short_count": short_count,
        "entry_zone": entry_zone, "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2,
        "base_tf": base_tf_name,
    }


# ---------------------- RAPORT TEKSTOWY ----------------------
def format_report(r):
    symbol = r["symbol"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📊 *{symbol}* — {now}", ""]

    if r["stats_24h"]:
        s = r["stats_24h"]
        lines.append(f"24h High: `{s['high']:.2f}`  |  24h Low: `{s['low']:.2f}`  ({s['change_pct']:+.2f}%)")

    if r["flow"]:
        f = r["flow"]
        oi_txt = f"  |  OI (8h): {f['oi_change_pct']:+.2f}%" if f["oi_change_pct"] is not None else ""
        lines.append(f"Funding rate: {f['funding_rate_pct']:+.4f}%{oi_txt}")
        if f["funding_rate_pct"] > 0.03:
            lines.append("  → wysoki dodatni funding: dużo pozycji long na dźwigni, ryzyko korekty")
        elif f["funding_rate_pct"] < -0.03:
            lines.append("  → wysoki ujemny funding: dużo pozycji short, ryzyko short squeeze")
        if f["oi_change_pct"] is not None and abs(f["oi_change_pct"]) > 5:
            hint = "napływa nowy kapitał" if f["oi_change_pct"] > 0 else "pozycje są zamykane"
            lines.append(f"  → open interest zmienił się o {f['oi_change_pct']:+.1f}% — {hint}")

    lines.append("")
    for tf in r["active_timeframes"]:
        d = r["per_tf"][tf]
        rsi_txt = f"{d['rsi']:.0f}" if d["rsi"] else "brak"
        lines.append(f"— *{tf}* — cena {d['price']:.2f} | trend {d['trend']} | RSI {rsi_txt}")
        lines.append(f"   Struktura: {d['structure']['structure']}")
        if d["sweep"]:
            sweep_txt = "sweep dołu" if d["sweep"]["type"] == "sweep_low" else "sweep szczytu"
            lines.append(f"   ⚡ {sweep_txt} @ {d['sweep']['level']:.2f}")
        if d["fvgs"]:
            for g in d["fvgs"]:
                lines.append(f"   FVG {g['type']}: {g['bottom']:.2f}-{g['top']:.2f} (niewypełniony)")
        if d["pattern"]:
            lines.append(f"   Formacja: {d['pattern']}")
        lines.append(f"   Bias {tf}: *{d['bias'].upper()}*")

    lines.append("")
    lines.append(f"🧭 Zgodność timeframe'ów: {r['long_count']} long / {r['short_count']} short "
                  f"(z {len(r['active_timeframes'])})")
    lines.append(f"➡️ Ogólny kierunek: *{r['overall_bias'].upper()}*")

    if r["entry_zone"]:
        lines.append("")
        lines.append(f"📍 Orientacyjne poziomy (na bazie {r['base_tf']}):")
        lines.append(f"   Entry: `{r['entry_zone'][0]:.2f} - {r['entry_zone'][1]:.2f}`")
        lines.append(f"   SL: `{r['stop_loss']:.2f}`")
        lines.append(f"   TP1: `{r['tp1']:.2f}`  TP2: `{r['tp2']:.2f}`")

    lines.append("")
    lines.append("_Wskaźniki techniczne, nie gwarancja. Funding/OI to zagregowane dane rynku "
                  "futures, nie wgląd w konkretne transakcje firm. Zawsze rób własny research._")
    return "\n".join(lines)


# ---------------------- WYKRES ----------------------
def generate_chart(r, n_candles=50):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from io import BytesIO

    symbol = r["symbol"]
    base_tf = r["base_tf"]
    d = r["per_tf"][base_tf]
    candles = d["candles"][-n_candles:]
    offset = len(d["candles"]) - len(candles)

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}, facecolor="#0d1117"
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
        ax_price.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1)
        body_low = min(c["open"], c["close"])
        body_height = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax_price.add_patch(Rectangle((i - width / 2, body_low), width, body_height,
                                      facecolor=color, edgecolor=color))
        ax_vol.bar(i, c["volume"], color=color, width=width)

    for gap in d["fvgs"]:
        idx = gap["index"] - offset
        if idx < 0:
            continue
        color = "#26a69a" if gap["type"] == "bullish" else "#ef5350"
        ax_price.axhspan(gap["bottom"], gap["top"], xmin=max(0, idx - 2) / len(candles),
                          color=color, alpha=0.12)

    if r["stats_24h"]:
        ax_price.axhline(r["stats_24h"]["high"], color="#f0b90b", linestyle="-",
                          linewidth=1, alpha=0.6, label="24h High")
        ax_price.axhline(r["stats_24h"]["low"], color="#f0b90b", linestyle="-",
                          linewidth=1, alpha=0.6, label="24h Low")

    if r["entry_zone"]:
        ax_price.axhline(r["stop_loss"], color="#ef5350", linestyle="--", linewidth=1, label="SL")
        ax_price.axhline(r["tp1"], color="#26a69a", linestyle="--", linewidth=1, label="TP1")
        ax_price.axhline(r["tp2"], color="#26a69a", linestyle=":", linewidth=1, label="TP2")
        ax_price.axhspan(r["entry_zone"][0], r["entry_zone"][1], color="#8e5cf7", alpha=0.15)

    ax_price.legend(loc="upper left", facecolor="#0d1117", labelcolor="#c9d1d9",
                     framealpha=0.7, fontsize=8)
    ax_price.set_title(f"{symbol} ({base_tf}) — {r['overall_bias'].upper()} "
                        f"[{r['long_count']}L/{r['short_count']}S]",
                        color="#c9d1d9", fontsize=12)
    ax_vol.set_xlabel("Świece (najnowsza po prawej)", color="#c9d1d9")
    plt.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------- TELEGRAM ----------------------
def _check_telegram_response(r, context):
    """Sprawdza odpowiedź Telegrama; zwraca True jeśli sukces, loguje szczegóły błędu jeśli nie."""
    try:
        data = r.json()
    except ValueError:
        log.error(f"Telegram ({context}): niepoprawna odpowiedź, status {r.status_code}")
        return False
    if not data.get("ok"):
        log.error(f"Telegram ({context}) błąd: {data.get('description')}")
        return False
    return True


def send_photo(api_url, chat_id, photo_bytes, caption=""):
    try:
        files = {"photo": ("chart.png", photo_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "Markdown"}
        r = _session.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=15)
        if not _check_telegram_response(r, "sendPhoto"):
            # Fallback: mogło paść na złym Markdown (np. obcięty caption) - spróbuj bez formatowania
            photo_bytes.seek(0)
            files = {"photo": ("chart.png", photo_bytes, "image/png")}
            data = {"chat_id": chat_id, "caption": caption[:1024]}
            _session.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=15)
        if len(caption) > 1024:
            send_message(api_url, chat_id, caption[1024:])
    except Exception as e:
        log.error(f"Błąd wysyłki zdjęcia Telegram: {e}")


def send_message(api_url, chat_id, text):
    try:
        r = _session.post(f"{api_url}/sendMessage", data={
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown",
        }, timeout=10)
        if not _check_telegram_response(r, "sendMessage"):
            # Fallback bez Markdown, gdyby formatowanie było niepoprawne
            _session.post(f"{api_url}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Błąd wysyłki Telegram: {e}")


def get_updates(api_url, offset=None):
    params = {"timeout": 20}
    if offset:
        params["offset"] = offset
    r = _session.get(f"{api_url}/getUpdates", params=params, timeout=25)
    return r.json().get("result", [])


# ---------------------- GŁÓWNA PĘTLA ----------------------
def main():
    token, default_chat_id, api_url = get_config()
    log.info(f"Bot startuje... symbole: {SYMBOLS}, timeframes: {TIMEFRAMES}")
    last_update_id = None
    last_auto_alert_bias = {s: None for s in SYMBOLS}

    send_message(api_url, default_chat_id,
                 f"🤖 Bot wystartował. Śledzę: {', '.join(SYMBOLS)} na {', '.join(TIMEFRAMES)}. "
                 f"Wpisz /analiza żeby dostać raport na żądanie.")

    while True:
        try:
            updates = get_updates(api_url, offset=last_update_id)
            for u in updates:
                last_update_id = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and text.strip().lower() in ("/analiza", "/start", "/analysis"):
                    for symbol in SYMBOLS:
                        r = analyze_symbol(symbol)
                        caption = format_report(r)
                        try:
                            chart = generate_chart(r)
                            send_photo(api_url, chat_id, chart, caption=caption)
                        except Exception as chart_err:
                            log.error(f"Błąd generowania wykresu {symbol}: {chart_err}")
                            send_message(api_url, chat_id, caption)

            for symbol in SYMBOLS:
                r = analyze_symbol(symbol)
                if r["overall_bias"] in ("long", "short"):
                    if r["overall_bias"] != last_auto_alert_bias[symbol]:
                        caption = "🔥 *Zgodność timeframe'ów wykryta!*\n\n" + format_report(r)
                        try:
                            chart = generate_chart(r)
                            send_photo(api_url, default_chat_id, chart, caption=caption)
                        except Exception as chart_err:
                            log.error(f"Błąd generowania wykresu {symbol}: {chart_err}")
                            send_message(api_url, default_chat_id, caption)
                        last_auto_alert_bias[symbol] = r["overall_bias"]
                else:
                    last_auto_alert_bias[symbol] = None

        except Exception as e:
            log.error(f"Błąd w pętli głównej: {e}")

        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
