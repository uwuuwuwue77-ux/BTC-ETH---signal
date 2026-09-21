import os
import time
import json
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("market_bot")

# ============================================================
# KONFIGURACJA
# ============================================================
# Hyperliquid używa nazwy aktywa "XAUT" dla złota, nawet jeśli na wykresie
# chcesz traktować je jako XAUT/USDT. Bot automatycznie szuka najpierw
# XAUTUSDT, a potem XAUT w universe Hyperliquid.
SYMBOLS = [s.strip() for s in os.environ.get(
    "SYMBOLS", "ETH,BTC,XAUT"
).split(",") if s.strip()]

TIMEFRAMES = [s.strip() for s in os.environ.get(
    "TIMEFRAMES", "15m,1h,4h"
).split(",") if s.strip()]

CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "900"))
CONFLUENCE_THRESHOLD = int(os.environ.get("CONFLUENCE_THRESHOLD", "2"))
REQUEST_PAUSE_SECONDS = float(os.environ.get("REQUEST_PAUSE_SECONDS", "0.3"))

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

# Kandydaci dla złota. XAUT jest normalną nazwą aktywa w Hyperliquid;
# XAUTUSDT jest sprawdzane jako pierwsze, jeśli HL kiedyś udostępni je
# pod taką nazwą.
GOLD_COIN_CANDIDATES = [
    s.strip() for s in os.environ.get(
        "GOLD_COIN_CANDIDATES", "XAUTUSDT,XAUT"
    ).split(",") if s.strip()
]

GOLD_SPOT_API_URL = "https://api.gold-api.com/price/XAU"

# Opcjonalne ręczne wydarzenia makro, UTC.
MACRO_EVENTS = []

# Plik historii backtestu.
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trades.json")

# Plik z ID ostatniej wiadomości backupu Telegram.
BACKUP_STATE_PATH = os.environ.get("BACKUP_STATE_PATH", "telegram_backup_state.json")

# Domyślnie backup jest wykonywany po każdym zapisie historii.
TELEGRAM_BACKUP_ENABLED = os.environ.get(
    "TELEGRAM_BACKUP_ENABLED", "1"
).lower() not in ("0", "false", "no")

# ============================================================
# HTTP
# ============================================================
_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))


def get_config():
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    return token, chat_id, f"https://api.telegram.org/bot{token}"


def _check_telegram_response(r, context):
    try:
        data = r.json()
    except ValueError:
        log.error(f"Telegram ({context}): niepoprawna odpowiedź, status {r.status_code}")
        return False, None

    if not data.get("ok"):
        log.error(f"Telegram ({context}) błąd: {data.get('description')}")
        return False, data

    return True, data


def send_message(api_url, chat_id, text, reply_markup=None):
    try:
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        r = _session.post(
            f"{api_url}/sendMessage",
            data=data,
            timeout=10,
        )
        ok, _ = _check_telegram_response(r, "sendMessage")

        if not ok:
            data.pop("parse_mode", None)
            _session.post(
                f"{api_url}/sendMessage",
                data=data,
                timeout=10,
            )
    except Exception as e:
        log.error(f"Błąd wysyłki wiadomości Telegram: {e}")


def send_document(api_url, chat_id, file_path, caption=""):
    """Wysyła plik do wskazanego czatu i zwraca message_id."""
    try:
        with open(file_path, "rb") as f:
            files = {
                "document": (
                    os.path.basename(file_path),
                    f,
                    "application/json",
                )
            }
            data = {
                "chat_id": chat_id,
                "caption": caption[:1024],
            }
            r = _session.post(
                f"{api_url}/sendDocument",
                data=data,
                files=files,
                timeout=20,
            )
        ok, response = _check_telegram_response(r, "sendDocument")
        if ok:
            return response.get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"Błąd wysyłki backupu Telegram: {e}")
    return None


def pin_message(api_url, chat_id, message_id):
    """Przypina backup w czacie."""
    if not message_id:
        return False

    try:
        r = _session.post(
            f"{api_url}/pinChatMessage",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": True,
            },
            timeout=10,
        )
        ok, _ = _check_telegram_response(r, "pinChatMessage")
        return ok
    except Exception as e:
        log.error(f"Błąd przypinania backupu Telegram: {e}")
        return False


def unpin_previous_backup(api_url, chat_id):
    """Odpina poprzedni backup, jeśli bot zna jego message_id."""
    try:
        if not os.path.exists(BACKUP_STATE_PATH):
            return

        with open(BACKUP_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)

        previous_id = state.get("message_id")
        if not previous_id:
            return

        r = _session.post(
            f"{api_url}/unpinChatMessage",
            data={
                "chat_id": chat_id,
                "message_id": previous_id,
            },
            timeout=10,
        )
        ok, _ = _check_telegram_response(r, "unpinChatMessage")
        if not ok:
            # Brak możliwości odpięcia nie powinien zatrzymać backupu.
            log.warning("Nie udało się odpiąć poprzedniego backupu.")
    except Exception as e:
        log.warning(f"Nie udało się odczytać/usunąć starego pina: {e}")


def save_backup_state(message_id):
    try:
        with open(BACKUP_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "message_id": message_id,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
                indent=2,
            )
    except Exception as e:
        log.warning(f"Nie udało się zapisać stanu backupu: {e}")


def telegram_backup(api_url, chat_id):
    """
    Wysyła aktualny trades.json do własnego czatu Telegrama i przypina
    najnowszą kopię. Poprzedni backup jest odpinany.
    """
    if not TELEGRAM_BACKUP_ENABLED:
        return

    if not os.path.exists(TRADE_LOG_PATH):
        return

    try:
        unpin_previous_backup(api_url, chat_id)

        caption = (
            "💾 *BACKUP BACKTESTU*\n"
            f"Plik: `{os.path.basename(TRADE_LOG_PATH)}`\n"
            f"Czas UTC: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`\n"
            "Najnowsza kopia historii sygnałów."
        )

        message_id = send_document(
            api_url,
            chat_id,
            TRADE_LOG_PATH,
            caption=caption,
        )

        if message_id:
            if pin_message(api_url, chat_id, message_id):
                save_backup_state(message_id)
                log.info(
                    f"Backup backtestu wysłany i przypięty. message_id={message_id}"
                )
            else:
                log.warning("Backup wysłany, ale nie udało się go przypiąć.")
    except Exception as e:
        log.error(f"Błąd Telegram backupu: {e}")


# ============================================================
# HYPERLIQUID
# ============================================================
_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def get_klines(symbol, interval, limit=100, start_time=None):
    interval_ms = _INTERVAL_MS.get(interval, 900_000)
    end_time = int(time.time() * 1000)

    if start_time is None:
        start_time = end_time - interval_ms * limit

    r = _session.post(
        HYPERLIQUID_INFO_URL,
        json={
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
            },
        },
        timeout=10,
    )
    r.raise_for_status()

    raw = r.json()

    return [{
        "open_time": c["t"],
        "open": float(c["o"]),
        "high": float(c["h"]),
        "low": float(c["l"]),
        "close": float(c["c"]),
        "volume": float(c["v"]),
    } for c in raw]


def get_meta():
    r = _session.post(
        HYPERLIQUID_INFO_URL,
        json={"type": "meta"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


_gold_coin_cache = None


def resolve_gold_coin():
    """
    Rozpoznaje prawdziwą nazwę złota w Hyperliquid.
    Preferuje XAUTUSDT, a następnie XAUT.
    """
    global _gold_coin_cache

    if _gold_coin_cache:
        return _gold_coin_cache

    data = get_meta()
    universe = data.get("universe", [])
    available_names = [a["name"] for a in universe]

    # Najpierw dokładnie po nazwach podanych przez użytkownika.
    for candidate in GOLD_COIN_CANDIDATES:
        if candidate in available_names:
            _gold_coin_cache = candidate
            log.info(f"Znaleziono złoto na Hyperliquid: {candidate}")
            return candidate

    # Dodatkowy fallback: szukamy aktywów zaczynających się od XAUT.
    xaut_candidates = [
        name for name in available_names
        if name.upper().startswith("XAUT")
    ]

    if xaut_candidates:
        _gold_coin_cache = xaut_candidates[0]
        log.info(
            f"Nie znaleziono dokładnego tickera {GOLD_COIN_CANDIDATES}; "
            f"używam wykrytego aktywa złota: {_gold_coin_cache}"
        )
        return _gold_coin_cache

    raise RuntimeError(
        f"Nie znaleziono złota na Hyperliquid. Szukano: "
        f"{GOLD_COIN_CANDIDATES}. "
        f"Sprawdź universe Hyperliquid."
    )


def normalize_symbol(symbol):
    """
    Zamienia przyjazną nazwę XAUTUSDT na faktyczną nazwę aktywa HL.
    BTC/ETH pozostają bez zmian.
    """
    upper = symbol.upper()

    if upper in ("XAUT", "XAUTUSDT", "GOLD", "XAU"):
        return resolve_gold_coin()

    return symbol


def get_asset_ctx(symbol):
    actual_symbol = normalize_symbol(symbol)

    r = _session.post(
        HYPERLIQUID_INFO_URL,
        json={"type": "metaAndAssetCtxs"},
        timeout=10,
    )
    r.raise_for_status()

    data = r.json()
    universe = data[0]["universe"]
    asset_ctxs = data[1]

    idx = next(
        (i for i, a in enumerate(universe) if a["name"] == actual_symbol),
        None,
    )

    if idx is None:
        raise RuntimeError(
            f"Symbol {actual_symbol} nie znaleziony w danych Hyperliquid"
        )

    ctx = asset_ctxs[idx]

    return {
        "symbol": actual_symbol,
        "mark_price": float(ctx["markPx"]),
        "oracle_price": float(ctx["oraclePx"]),
        "funding_rate_pct": float(ctx["funding"]) * 100,
        "open_interest": float(ctx["openInterest"]),
        "premium_pct": (
            float(ctx["premium"]) * 100
            if ctx.get("premium") is not None else None
        ),
        "prev_day_price": float(ctx["prevDayPx"]),
    }


def get_24h_stats(symbol):
    actual_symbol = normalize_symbol(symbol)
    candles = get_klines(actual_symbol, "5m", limit=288)

    if not candles:
        raise RuntimeError(
            f"Brak świec do wyliczenia 24h stats dla {actual_symbol}"
        )

    high = max(c["high"] for c in candles)
    low = min(c["low"] for c in candles)
    change_pct = (
        (candles[-1]["close"] - candles[0]["open"])
        / candles[0]["open"] * 100
    )
    volume = sum(c["volume"] for c in candles)

    return {
        "high": high,
        "low": low,
        "change_pct": change_pct,
        "volume": volume,
    }


_oi_history = {}


def get_futures_flow(symbol):
    try:
        ctx = get_asset_ctx(symbol)
        actual_symbol = ctx["symbol"]

        funding = ctx["funding_rate_pct"]
        current_oi = ctx["open_interest"]

        oi_change_pct = None
        prev = _oi_history.get(actual_symbol)

        if prev is not None:
            prev_oi, _ = prev
            if prev_oi:
                oi_change_pct = (
                    (current_oi - prev_oi) / prev_oi * 100
                )

        _oi_history[actual_symbol] = (current_oi, time.time())

        return {
            "funding_rate_pct": funding,
            "oi_change_pct": oi_change_pct,
        }

    except Exception as e:
        log.warning(f"Brak danych futures dla {symbol}: {e}")
        return None


def get_spot_gold_price():
    try:
        r = _session.get(GOLD_SPOT_API_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Brak danych spot gold: {e}")
        return None


def get_upcoming_macro_events(hours_ahead=72):
    now = datetime.now(timezone.utc)
    upcoming = []

    for event in MACRO_EVENTS:
        try:
            event_time = datetime.strptime(
                event["when_utc"],
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=timezone.utc)

            hours_until = (
                event_time - now
            ).total_seconds() / 3600

            if 0 <= hours_until <= hours_ahead:
                upcoming.append({
                    "name": event["name"],
                    "when_utc": event["when_utc"],
                    "hours_until": hours_until,
                })

        except Exception as e:
            log.warning(
                f"Błąd parsowania wydarzenia makro {event}: {e}"
            )

    return sorted(
        upcoming,
        key=lambda e: e["hours_until"],
    )


# ============================================================
# WSKAŹNIKI
# ============================================================
def ema(values, period):
    k = 2 / (period + 1)
    ema_vals = [values[0]]

    for v in values[1:]:
        ema_vals.append(
            v * k + ema_vals[-1] * (1 - k)
        )

    return ema_vals


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

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

    if period <= 0:
        return 0

    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        trs.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )

    return sum(trs[-period:]) / period if trs else 0


def detect_candle_pattern(candles_window):
    if len(candles_window) < 2:
        return None

    candle = candles_window[-1]
    prev_candle = candles_window[-2]

    body = abs(candle["close"] - candle["open"])
    range_ = candle["high"] - candle["low"]

    if range_ == 0:
        return None

    upper_wick = (
        candle["high"] - max(candle["close"], candle["open"])
    )
    lower_wick = (
        min(candle["close"], candle["open"]) - candle["low"]
    )

    prev_body = abs(
        prev_candle["close"] - prev_candle["open"]
    )

    prev_is_down = prev_candle["close"] < prev_candle["open"]
    prev_is_up = prev_candle["close"] > prev_candle["open"]

    if len(candles_window) >= 3:
        c1, c2, c3 = candles_window[-3:]

        c1_body = abs(c1["close"] - c1["open"])
        c3_body = abs(c3["close"] - c3["open"])

        if (
            c1["close"] < c1["open"]
            and c1_body > 0
            and abs(c2["close"] - c2["open"]) < c1_body * 0.4
            and c3["close"] > c3["open"]
            and c3_body > c1_body * 0.6
            and c3["close"] > (c1["open"] + c1["close"]) / 2
        ):
            return "Morning Star"

        if (
            c1["close"] > c1["open"]
            and c1_body > 0
            and abs(c2["close"] - c2["open"]) < c1_body * 0.4
            and c3["close"] < c3["open"]
            and c3_body > c1_body * 0.6
            and c3["close"] < (c1["open"] + c1["close"]) / 2
        ):
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

    if (
        candle["close"] > candle["open"]
        and prev_candle["close"] < prev_candle["open"]
        and candle["close"] > prev_candle["open"]
        and candle["open"] < prev_candle["close"]
        and body > prev_body
    ):
        return "Bullish Engulfing"

    if (
        candle["close"] < candle["open"]
        and prev_candle["close"] > prev_candle["open"]
        and candle["open"] > prev_candle["close"]
        and candle["close"] < prev_candle["open"]
        and body > prev_body
    ):
        return "Bearish Engulfing"

    return None


BULLISH_PATTERNS = (
    "Hammer",
    "Bullish Engulfing",
    "Morning Star",
    "Inverted Hammer",
)

BEARISH_PATTERNS = (
    "Bearish Engulfing",
    "Evening Star",
    "Shooting Star",
    "Hanging Man",
)


# ============================================================
# SMART MONEY / STRUKTURA
# ============================================================
def detect_fvg(candles, lookback=20):
    gaps = []
    start = max(2, len(candles) - lookback)

    for i in range(start, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]

        if c1["high"] < c3["low"]:
            gap = {
                "type": "bullish",
                "top": c3["low"],
                "bottom": c1["high"],
                "index": i,
            }

        elif c1["low"] > c3["high"]:
            gap = {
                "type": "bearish",
                "top": c1["low"],
                "bottom": c3["high"],
                "index": i,
            }

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
    if len(candles) < swing_lookback + 2:
        return None

    last = candles[-1]
    prior = candles[-(swing_lookback + 1):-1]

    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)

    if last["high"] > prior_high and last["close"] < prior_high:
        return {
            "type": "sweep_high",
            "level": prior_high,
        }

    if last["low"] < prior_low and last["close"] > prior_low:
        return {
            "type": "sweep_low",
            "level": prior_low,
        }

    return None


def find_swing_points(candles, window=3):
    highs = []
    lows = []

    for i in range(window, len(candles) - window):
        segment = candles[
            i - window:i + window + 1
        ]

        if candles[i]["high"] == max(
            c["high"] for c in segment
        ):
            highs.append((i, candles[i]["high"]))

        if candles[i]["low"] == min(
            c["low"] for c in segment
        ):
            lows.append((i, candles[i]["low"]))

    return highs, lows


def market_structure(candles):
    highs, lows = find_swing_points(candles)

    if len(highs) < 2 or len(lows) < 2:
        return {
            "structure": "brak wystarczających danych",
            "detail": "",
            "hh": False,
            "hl": False,
            "lh": False,
            "ll": False,
        }

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

    detail = (
        f"Ostatnie swingi — High: "
        f"{last_two_highs[0][1]:.2f} → "
        f"{last_two_highs[1][1]:.2f}, "
        f"Low: {last_two_lows[0][1]:.2f} → "
        f"{last_two_lows[1][1]:.2f}"
    )

    return {
        "structure": structure,
        "detail": detail,
        "hh": hh,
        "hl": hl,
        "lh": lh,
        "ll": ll,
    }


# ============================================================
# ANALIZA TIMEFRAME
# ============================================================
def analyze_timeframe(candles):
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = (
        ema(closes, 50)
        if len(closes) >= 50
        else ema(closes, max(2, len(closes) - 1))
    )

    current_price = closes[-1]
    current_rsi = rsi(closes)
    current_atr = atr(candles)

    avg_volume = (
        sum(volumes[-20:])
        / min(20, len(volumes))
    )
    volume_ratio = (
        volumes[-1] / avg_volume
        if avg_volume else 1
    )

    trend = (
        "wzrostowy"
        if ema20[-1] > ema50[-1]
        else "spadkowy"
    )

    pattern = detect_candle_pattern(
        candles[-3:]
        if len(candles) >= 3
        else candles[-2:]
    )

    recent_n = min(4, len(closes) - 1)
    recent_closes = closes[-(recent_n + 1):]

    recent_change_pct = (
        (recent_closes[-1] - recent_closes[0])
        / recent_closes[0] * 100
    )

    recent_diffs = [
        recent_closes[i + 1] - recent_closes[i]
        for i in range(len(recent_closes) - 1)
    ]

    falling_streak = all(
        d < 0 for d in recent_diffs
    )
    rising_streak = all(
        d > 0 for d in recent_diffs
    )

    structure = market_structure(candles)
    fvgs = detect_fvg(candles)
    sweep = detect_liquidity_sweep(candles)

    bullish_votes = []
    bearish_votes = []

    if trend == "wzrostowy":
        bullish_votes.append("EMA20>EMA50")
    else:
        bearish_votes.append("EMA20<EMA50")

    if current_rsi is not None:
        if current_rsi < 30:
            bullish_votes.append(
                f"RSI wyprzedany ({current_rsi:.0f})"
            )
        elif current_rsi > 70:
            bearish_votes.append(
                f"RSI wykupiony ({current_rsi:.0f})"
            )

    if pattern in BULLISH_PATTERNS:
        bullish_votes.append(
            f"formacja {pattern}"
        )
    elif pattern in BEARISH_PATTERNS:
        bearish_votes.append(
            f"formacja {pattern}"
        )

    if falling_streak and abs(recent_change_pct) > 0.3:
        bearish_votes.append(
            "świeże momentum spadkowe"
        )
    elif rising_streak and abs(recent_change_pct) > 0.3:
        bullish_votes.append(
            "świeże momentum wzrostowe"
        )

    if structure.get("hh") and structure.get("hl"):
        bullish_votes.append("struktura HH+HL")
    elif structure.get("lh") and structure.get("ll"):
        bearish_votes.append("struktura LH+LL")

    if sweep:
        if sweep["type"] == "sweep_low":
            bullish_votes.append(
                f"sweep dołu @ {sweep['level']:.2f}"
            )
        else:
            bearish_votes.append(
                f"sweep szczytu @ {sweep['level']:.2f}"
            )

    for gap in fvgs:
        if gap["type"] == "bullish":
            bullish_votes.append(
                f"bullish FVG {gap['bottom']:.2f}-{gap['top']:.2f}"
            )
        else:
            bearish_votes.append(
                f"bearish FVG {gap['bottom']:.2f}-{gap['top']:.2f}"
            )

    if len(bullish_votes) > len(bearish_votes):
        bias = "long"
    elif len(bearish_votes) > len(bullish_votes):
        bias = "short"
    else:
        bias = "neutralny"

    return {
        "price": current_price,
        "atr": current_atr,
        "rsi": current_rsi,
        "trend": trend,
        "volume_ratio": volume_ratio,
        "pattern": pattern,
        "structure": structure,
        "fvgs": fvgs,
        "sweep": sweep,
        "bullish_votes": bullish_votes,
        "bearish_votes": bearish_votes,
        "bias": bias,
        "candles": candles,
    }


# ============================================================
# ANALIZA WIELO-TIMEFRAME
# ============================================================
def analyze_symbol(symbol):
    actual_symbol = normalize_symbol(symbol)

    per_tf = {}

    for tf in TIMEFRAMES:
        try:
            candles = get_klines(
                actual_symbol,
                tf,
            )
            if len(candles) >= 20:
                per_tf[tf] = analyze_timeframe(candles)
            else:
                log.warning(
                    f"Za mało świec dla {actual_symbol} {tf}"
                )
        except Exception as e:
            log.warning(
                f"Pominięto {tf} dla {actual_symbol}: {e}"
            )

        time.sleep(REQUEST_PAUSE_SECONDS)

    if not per_tf:
        raise RuntimeError(
            f"Nie udało się pobrać timeframe'u dla {actual_symbol}"
        )

    active_timeframes = list(per_tf.keys())

    stats_24h = None

    try:
        stats_24h = get_24h_stats(actual_symbol)
    except Exception as e:
        log.warning(
            f"Brak danych 24h dla {actual_symbol}: {e}"
        )

    flow = get_futures_flow(actual_symbol)

    biases = [
        per_tf[tf]["bias"]
        for tf in active_timeframes
    ]

    long_count = biases.count("long")
    short_count = biases.count("short")

    if (
        long_count >= CONFLUENCE_THRESHOLD
        and long_count > short_count
    ):
        overall_bias = "long"

    elif (
        short_count >= CONFLUENCE_THRESHOLD
        and short_count > long_count
    ):
        overall_bias = "short"

    else:
        overall_bias = "mieszany / brak zgodności"

    # Bierzemy najniższy skonfigurowany timeframe jako bazę.
    base_tf_name = active_timeframes[0]
    base_tf = per_tf[base_tf_name]

    entry_zone = None
    stop_loss = None
    tp1 = None
    tp2 = None

    if overall_bias == "long":
        p = base_tf["price"]
        a_ = base_tf["atr"]

        entry_zone = (
            p - a_ * 0.3,
            p,
        )
        stop_loss = p - a_ * 1.5
        tp1 = p + a_ * 1.5
        tp2 = p + a_ * 3

    elif overall_bias == "short":
        p = base_tf["price"]
        a_ = base_tf["atr"]

        entry_zone = (
            p,
            p + a_ * 0.3,
        )
        stop_loss = p + a_ * 1.5
        tp1 = p - a_ * 1.5
        tp2 = p - a_ * 3

    return {
        "symbol": symbol,
        "actual_symbol": actual_symbol,
        "per_tf": per_tf,
        "active_timeframes": active_timeframes,
        "stats_24h": stats_24h,
        "flow": flow,
        "overall_bias": overall_bias,
        "long_count": long_count,
        "short_count": short_count,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "base_tf": base_tf_name,
    }


# ============================================================
# RAPORT
# ============================================================
_BIAS_EMOJI = {
    "long": "🟢",
    "short": "🔴",
    "neutralny": "⚪",
}


def format_report(r):
    symbol = r["symbol"]
    display_symbol = (
        "XAUT/USDT"
        if symbol.upper() in ("XAUT", "XAUTUSDT", "XAU", "GOLD")
        else symbol
    )

    base = r["per_tf"][r["base_tf"]]
    price = base["price"]

    change_txt = ""

    if r["stats_24h"]:
        change_txt = (
            f" ({r['stats_24h']['change_pct']:+.1f}%)"
        )

    lines = [
        f"📊 *{display_symbol}* `{price:.2f}`{change_txt}"
    ]

    if r["stats_24h"]:
        s = r["stats_24h"]
        lines.append(
            f"24h: {s['low']:.2f} — {s['high']:.2f}"
        )

    tf_line = " | ".join(
        f"{tf} {_BIAS_EMOJI[r['per_tf'][tf]['bias']]}"
        for tf in r["active_timeframes"]
    )

    lines.append(tf_line)

    lines.append(
        f"➡️ *{r['overall_bias'].upper()}* "
        f"({r['long_count']}L/{r['short_count']}S)"
    )

    highlights = []

    for tf in r["active_timeframes"]:
        d = r["per_tf"][tf]

        if d["sweep"]:
            sweep_txt = (
                "sweep dołu"
                if d["sweep"]["type"] == "sweep_low"
                else "sweep szczytu"
            )

            highlights.append(
                f"⚡ {sweep_txt} {tf} @{d['sweep']['level']:.2f}"
            )

        if d["pattern"] and (
            d["pattern"] in BULLISH_PATTERNS
            or d["pattern"] in BEARISH_PATTERNS
        ):
            highlights.append(
                f"🕯️ {d['pattern']} ({tf})"
            )

    if highlights:
        lines.append(
            " · ".join(highlights[:3])
        )

    if r["flow"] and (
        r["flow"]["funding_rate_pct"] > 0.03
        or r["flow"]["funding_rate_pct"] < -0.03
    ):
        f = r["flow"]

        tag = (
            "long-heavy⚠️"
            if f["funding_rate_pct"] > 0
            else "short-heavy⚠️"
        )

        lines.append(
            f"💰 Funding {f['funding_rate_pct']:+.3f}% "
            f"({tag})"
        )

    if r["entry_zone"]:
        lines.append(
            f"🎯 Entry `{r['entry_zone'][0]:.2f}-"
            f"{r['entry_zone'][1]:.2f}` "
            f"SL `{r['stop_loss']:.2f}` "
            f"TP `{r['tp1']:.2f}/{r['tp2']:.2f}`"
        )

    return "\n".join(lines)


# ============================================================
# WYKRES
# ============================================================
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
        2,
        1,
        figsize=(11, 6.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
        facecolor="#0d1117",
    )

    for ax in (ax_price, ax_vol):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#c9d1d9")

        for spine in ax.spines.values():
            spine.set_color("#30363d")

    up_color = "#26a69a"
    down_color = "#ef5350"
    width = 0.6

    for i, c in enumerate(candles):
        color = (
            up_color
            if c["close"] >= c["open"]
            else down_color
        )

        ax_price.plot(
            [i, i],
            [c["low"], c["high"]],
            color=color,
            linewidth=1,
        )

        body_low = min(
            c["open"],
            c["close"],
        )

        body_height = abs(
            c["close"] - c["open"]
        ) or (
            c["high"] - c["low"]
        ) * 0.01

        ax_price.add_patch(
            Rectangle(
                (i - width / 2, body_low),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
            )
        )

        ax_vol.bar(
            i,
            c["volume"],
            color=color,
            width=width,
        )

    for gap in d["fvgs"]:
        idx = gap["index"] - offset

        if idx < 0:
            continue

        color = (
            up_color
            if gap["type"] == "bullish"
            else down_color
        )

        ax_price.axhspan(
            gap["bottom"],
            gap["top"],
            xmin=max(0, idx - 2) / len(candles),
            color=color,
            alpha=0.12,
        )

    if r["stats_24h"]:
        ax_price.axhline(
            r["stats_24h"]["high"],
            color="#f0b90b",
            linestyle="-",
            linewidth=1,
            alpha=0.6,
            label="24h High",
        )

        ax_price.axhline(
            r["stats_24h"]["low"],
            color="#f0b90b",
            linestyle="-",
            linewidth=1,
            alpha=0.6,
            label="24h Low",
        )

    if r["entry_zone"]:
        ax_price.axhline(
            r["stop_loss"],
            color="#ef5350",
            linestyle="--",
            linewidth=1,
            label="SL",
        )

        ax_price.axhline(
            r["tp1"],
            color="#26a69a",
            linestyle="--",
            linewidth=1,
            label="TP1",
        )

        ax_price.axhline(
            r["tp2"],
            color="#26a69a",
            linestyle=":",
            linewidth=1,
            label="TP2",
        )

        ax_price.axhspan(
            r["entry_zone"][0],
            r["entry_zone"][1],
            color="#8e5cf7",
            alpha=0.15,
        )

    ax_price.legend(
        loc="upper left",
        facecolor="#0d1117",
        labelcolor="#c9d1d9",
        framealpha=0.7,
        fontsize=8,
    )

    ax_price.set_title(
        f"{symbol} ({base_tf}) — "
        f"{r['overall_bias'].upper()} "
        f"[{r['long_count']}L/{r['short_count']}S]",
        color="#c9d1d9",
        fontsize=12,
    )

    ax_vol.set_xlabel(
        "Świece (najnowsza po prawej)",
        color="#c9d1d9",
    )

    plt.tight_layout()

    buf = BytesIO()

    fig.savefig(
        buf,
        format="png",
        facecolor=fig.get_facecolor(),
    )

    plt.close(fig)

    buf.seek(0)

    return buf


def send_photo(api_url, chat_id, photo_bytes, caption=""):
    try:
        files = {
            "photo": (
                "chart.png",
                photo_bytes,
                "image/png",
            )
        }

        data = {
            "chat_id": chat_id,
            "caption": caption[:1024],
            "parse_mode": "Markdown",
        }

        r = _session.post(
            f"{api_url}/sendPhoto",
            data=data,
            files=files,
            timeout=15,
        )

        ok, _ = _check_telegram_response(
            r,
            "sendPhoto",
        )

        if not ok:
            photo_bytes.seek(0)

            files = {
                "photo": (
                    "chart.png",
                    photo_bytes,
                    "image/png",
                )
            }

            data = {
                "chat_id": chat_id,
                "caption": caption[:1024],
            }

            _session.post(
                f"{api_url}/sendPhoto",
                data=data,
                files=files,
                timeout=15,
            )

        if len(caption) > 1024:
            send_message(
                api_url,
                chat_id,
                caption[1024:],
            )

    except Exception as e:
        log.error(
            f"Błąd wysyłki zdjęcia Telegram: {e}"
        )


# ============================================================
# BACKTEST / TRACKING
# ============================================================
def _load_trades():
    if not os.path.exists(TRADE_LOG_PATH):
        return []

    try:
        with open(
            TRADE_LOG_PATH,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    except Exception as e:
        log.error(
            f"Błąd odczytu {TRADE_LOG_PATH}: {e}"
        )
        return []


def _save_trades(
    trades,
    api_url=None,
    chat_id=None,
    backup=True,
):
    """
    Zapisuje trades.json atomowo, a następnie robi backup na Telegramie.
    Backup zawiera CAŁY aktualny plik, więc po restarcie można odzyskać historię.
    """
    tmp_path = TRADE_LOG_PATH + ".tmp"

    try:
        with open(
            tmp_path,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                trades,
                f,
                indent=2,
                ensure_ascii=False,
            )

        os.replace(
            tmp_path,
            TRADE_LOG_PATH,
        )

    except Exception as e:
        log.error(
            f"Błąd zapisu {TRADE_LOG_PATH}: {e}"
        )

        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

        return False

    # Telegram backup dopiero po udanym zapisie lokalnym.
    if (
        backup
        and TELEGRAM_BACKUP_ENABLED
        and api_url
        and chat_id
    ):
        telegram_backup(
            api_url,
            chat_id,
        )

    return True


def record_signal(r, api_url=None, chat_id=None):
    if (
        r["overall_bias"]
        not in ("long", "short")
        or not r["entry_zone"]
    ):
        return

    trades = _load_trades()

    # Nie zapisuj identycznego sygnału drugi raz.
    # To dodatkowe zabezpieczenie oprócz last_auto_alert_bias.
    entry_price = r["per_tf"][r["base_tf"]]["price"]

    for t in trades[-20:]:
        if (
            t.get("symbol") == r["actual_symbol"]
            and t.get("direction") == r["overall_bias"]
            and t.get("status") == "open"
            and abs(
                t.get("entry_price", 0) - entry_price
            ) < max(entry_price * 0.001, 0.01)
        ):
            return

    trades.append({
        "symbol": r["actual_symbol"],
        "display_symbol": (
            "XAUTUSDT"
            if r["actual_symbol"].upper().startswith("XAUT")
            else r["actual_symbol"]
        ),
        "direction": r["overall_bias"],
        "timeframe": r["base_tf"],
        "entry_time_ms": int(time.time() * 1000),
        "entry_price": entry_price,
        "stop_loss": r["stop_loss"],
        "tp1": r["tp1"],
        "tp2": r["tp2"],
        "status": "open",
        "closed_at_ms": None,
    })

    _save_trades(
        trades,
        api_url=api_url,
        chat_id=chat_id,
    )

    log.info(
        f"Zapisano nowy sygnał: "
        f"{r['actual_symbol']} {r['overall_bias']}"
    )


def _check_single_trade_outcome(trade):
    try:
        candles = get_klines(
            trade["symbol"],
            trade["timeframe"],
            limit=500,
            start_time=trade["entry_time_ms"],
        )

    except Exception as e:
        log.warning(
            f"Nie udało się sprawdzić trade'a "
            f"{trade['symbol']}: {e}"
        )
        return None

    for c in candles:
        if c["open_time"] < trade["entry_time_ms"]:
            continue

        if trade["direction"] == "long":
            hit_sl = (
                c["low"] <= trade["stop_loss"]
            )
            hit_tp = (
                c["high"] >= trade["tp1"]
            )

        else:
            hit_sl = (
                c["high"] >= trade["stop_loss"]
            )
            hit_tp = (
                c["low"] <= trade["tp1"]
            )

        # Konserwatywne założenie przy świecy,
        # która dotknęła SL i TP.
        if hit_sl:
            return "loss"

        if hit_tp:
            return "win"

    return None


def update_open_trades(
    api_url=None,
    chat_id=None,
):
    trades = _load_trades()
    changed = False

    for trade in trades:
        if trade["status"] != "open":
            continue

        outcome = _check_single_trade_outcome(
            trade
        )

        if outcome:
            trade["status"] = outcome
            trade["closed_at_ms"] = int(
                time.time() * 1000
            )

            changed = True

            log.info(
                f"Trade zamknięty: "
                f"{trade['symbol']} "
                f"{trade['direction']} → {outcome}"
            )

    if changed:
        _save_trades(
            trades,
            api_url=api_url,
            chat_id=chat_id,
        )


def format_backtest_report():
    trades = _load_trades()

    if not trades:
        return (
            "📉 Brak zapisanych sygnałów jeszcze. "
            "Bot zbiera dane od pierwszego pełnego "
            "potwierdzenia timeframe'ów."
        )

    closed = [
        t for t in trades
        if t["status"] in ("win", "loss")
    ]

    open_trades = [
        t for t in trades
        if t["status"] == "open"
    ]

    wins = [
        t for t in closed
        if t["status"] == "win"
    ]

    losses = [
        t for t in closed
        if t["status"] == "loss"
    ]

    lines = [
        "📊 *Backtest — skuteczność sygnałów*",
        "",
    ]

    if closed:
        win_rate = (
            len(wins) / len(closed) * 100
        )

        lines.append(
            f"✅ Zamknięte: {len(closed)} "
            f"({len(wins)} trafione / "
            f"{len(losses)} SL)"
        )

        lines.append(
            f"🎯 Win rate: *{win_rate:.1f}%*"
        )

    else:
        lines.append(
            "Brak jeszcze zamkniętych sygnałów "
            "(wszystkie nadal otwarte)."
        )

    lines.append(
        f"🔓 Nadal otwarte: {len(open_trades)}"
    )

    lines.append("")
    lines.append("Rozbicie per para:")

    for symbol in sorted(
        set(t["symbol"] for t in trades)
    ):
        sym_closed = [
            t for t in closed
            if t["symbol"] == symbol
        ]

        if sym_closed:
            sym_wins = sum(
                1 for t in sym_closed
                if t["status"] == "win"
            )

            lines.append(
                f"  {symbol}: "
                f"{sym_wins}/{len(sym_closed)} "
                f"trafionych "
                f"({sym_wins / len(sym_closed) * 100:.0f}%)"
            )

        else:
            sym_open = sum(
                1 for t in trades
                if (
                    t["symbol"] == symbol
                    and t["status"] == "open"
                )
            )

            lines.append(
                f"  {symbol}: brak zamkniętych "
                f"({sym_open} otwartych)"
            )

    lines.append("")
    lines.append(
        "_To statystyka historyczna tego bota, "
        "nie gwarancja przyszłych wyników. "
        "Mała próbka = mało wiarygodne wnioski._"
    )

    return "\n".join(lines)


# ============================================================
# TELEGRAM MENU / UPDATES
# ============================================================
def answer_callback_query(
    api_url,
    callback_query_id,
):
    try:
        _session.post(
            f"{api_url}/answerCallbackQuery",
            data={
                "callback_query_id":
                    callback_query_id
            },
            timeout=10,
        )

    except Exception as e:
        log.error(
            f"Błąd answerCallbackQuery: {e}"
        )


def main_menu_keyboard():
    return {
        "inline_keyboard": [[
            {
                "text": "📊 Analiza",
                "callback_data": "analiza",
            },
            {
                "text": "📈 Backtest",
                "callback_data": "backtest",
            },
            {
                "text": "💾 Backup",
                "callback_data": "backup",
            },
        ]]
    }


def get_updates(api_url, offset=None):
    params = {
        "timeout": 20,
    }

    if offset is not None:
        params["offset"] = offset

    r = _session.get(
        f"{api_url}/getUpdates",
        params=params,
        timeout=25,
    )

    return r.json().get(
        "result",
        [],
    )


# ============================================================
# AKCJE
# ============================================================
def run_analiza(api_url, chat_id):
    for symbol in SYMBOLS:
        try:
            r = analyze_symbol(symbol)
            caption = format_report(r)

            try:
                chart = generate_chart(r)

                send_photo(
                    api_url,
                    chat_id,
                    chart,
                    caption=caption,
                )

            except Exception as chart_err:
                log.error(
                    f"Błąd wykresu {symbol}: "
                    f"{chart_err}"
                )

                send_message(
                    api_url,
                    chat_id,
                    caption,
                )

        except Exception as e:
            log.error(
                f"Błąd analizy {symbol}: {e}"
            )

            send_message(
                api_url,
                chat_id,
                f"⚠️ Nie udało się przeanalizować "
                f"{symbol}: `{e}`",
            )


def run_backtest(api_url, chat_id):
    send_message(
        api_url,
        chat_id,
        format_backtest_report(),
    )


# ============================================================
# MAIN
# ============================================================
def main():
    token, default_chat_id, api_url = get_config()

    log.info(
        f"Bot startuje... symbole: {SYMBOLS}, "
        f"timeframes: {TIMEFRAMES}"
    )

    # Sprawdź złoto od razu, żeby błąd tickera był widoczny
    # już przy starcie, a nie dopiero po kilku minutach.
    if any(
        s.upper() in (
            "XAUT",
            "XAUTUSDT",
            "GOLD",
            "XAU",
        )
        for s in SYMBOLS
    ):
        try:
            gold = resolve_gold_coin()
            log.info(
                f"Gold mapping: XAUT/USDT -> {gold}"
            )
        except Exception as e:
            log.error(
                f"Nie udało się rozpoznać złota: {e}"
            )

    last_auto_alert_bias = {
        s: None for s in SYMBOLS
    }

    # Wyczyść stare update'y.
    try:
        stale_updates = get_updates(
            api_url
        )

        last_update_id = (
            stale_updates[-1]["update_id"] + 1
            if stale_updates
            else None
        )

        if stale_updates:
            log.info(
                f"Pominięto {len(stale_updates)} "
                "zaległych update'ów."
            )

    except Exception as e:
        log.warning(
            f"Nie udało się wyczyścić update'ów: {e}"
        )

        last_update_id = None

    send_message(
        api_url,
        default_chat_id,
        "🤖 Cześć! Co potrzebujesz?",
        reply_markup=main_menu_keyboard(),
    )

    # Jeśli trades.json już istnieje, od razu zrób jego backup
    # i przypnij najnowszą kopię.
    if os.path.exists(TRADE_LOG_PATH):
        telegram_backup(
            api_url,
            default_chat_id,
        )

    last_market_check = 0

    while True:
        try:
            updates = get_updates(
                api_url,
                offset=last_update_id,
            )

            for u in updates:
                last_update_id = (
                    u["update_id"] + 1
                )

                cq = u.get(
                    "callback_query"
                )

                if cq:
                    answer_callback_query(
                        api_url,
                        cq["id"],
                    )

                    cq_chat_id = cq[
                        "message"
                    ]["chat"]["id"]

                    if cq["data"] == "analiza":
                        run_analiza(
                            api_url,
                            cq_chat_id,
                        )

                    elif cq["data"] == "backtest":
                        run_backtest(
                            api_url,
                            cq_chat_id,
                        )

                    elif cq["data"] == "backup":
                        telegram_backup(
                            api_url,
                            cq_chat_id,
                        )

                    continue

                msg = u.get(
                    "message",
                    {},
                )

                text = msg.get(
                    "text",
                    "",
                )

                chat_id = msg.get(
                    "chat",
                    {},
                ).get("id")

                cmd = (
                    text.strip().lower()
                    if text
                    else ""
                )

                if cmd in (
                    "/analiza",
                    "/analysis",
                ):
                    run_analiza(
                        api_url,
                        chat_id,
                    )

                elif cmd == "/backtest":
                    run_backtest(
                        api_url,
                        chat_id,
                    )

                elif cmd == "/backup":
                    telegram_backup(
                        api_url,
                        chat_id,
                    )

                elif chat_id:
                    send_message(
                        api_url,
                        chat_id,
                        "Siema! Co potrzebujesz? 👇",
                        reply_markup=main_menu_keyboard(),
                    )

            now = time.time()

            if (
                now - last_market_check
                >= CHECK_EVERY_SECONDS
            ):
                update_open_trades(
                    api_url,
                    default_chat_id,
                )

                for symbol in SYMBOLS:
                    try:
                        r = analyze_symbol(
                            symbol
                        )

                        if r["overall_bias"] in (
                            "long",
                            "short",
                        ):
                            if (
                                r["overall_bias"]
                                != last_auto_alert_bias[
                                    symbol
                                ]
                            ):
                                caption = (
                                    "🔥 "
                                    + format_report(r)
                                )

                                try:
                                    chart = generate_chart(
                                        r
                                    )

                                    send_photo(
                                        api_url,
                                        default_chat_id,
                                        chart,
                                        caption=caption,
                                    )

                                except Exception as chart_err:
                                    log.error(
                                        f"Błąd wykresu "
                                        f"{symbol}: "
                                        f"{chart_err}"
                                    )

                                    send_message(
                                        api_url,
                                        default_chat_id,
                                        caption,
                                    )

                                record_signal(
                                    r,
                                    api_url,
                                    default_chat_id,
                                )

                                last_auto_alert_bias[
                                    symbol
                                ] = r[
                                    "overall_bias"
                                ]

                        else:
                            last_auto_alert_bias[
                                symbol
                            ] = None

                    except Exception as e:
                        log.error(
                            f"Błąd cyklu dla "
                            f"{symbol}: {e}"
                        )

                last_market_check = now

        except Exception as e:
            log.error(
                f"Błąd w pętli głównej: {e}"
            )

            time.sleep(2)


if __name__ == "__main__":
    main()
