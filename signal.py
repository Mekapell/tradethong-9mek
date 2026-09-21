import os, json, requests, datetime as dt

LINE_TOKEN      = os.environ["LINE_TOKEN"]
LINE_TO         = os.environ["LINE_TO"]
TWELVE_KEY      = os.environ["TWELVEDATA_KEY"]
FINNHUB_KEY     = os.environ["FINNHUB_KEY"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "1000"))
RISK_PERCENT    = float(os.environ.get("RISK_PERCENT", "1"))
STATE_FILE      = "last_signal.txt"
LOG_FILE        = "signal_log.csv"
NEWS_CACHE      = "news_cache.json"
DXY_CACHE       = "dxy_cache.json"
CACHE_MINUTES   = 60

NEG_WORDS = ["war", "conflict", "crisis", "rate hike", "inflation surge", "recession",
             "sanction", "hawkish", "strong dollar", "yields rise", "geopolitical tension"]
POS_WORDS = ["rate cut", "stimulus", "safe haven", "dovish", "ceasefire", "easing",
             "weak dollar", "yields fall", "fed pause", "de-escalation"]


def push_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_TO, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def get_series(interval, size=100):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval, "outputsize": size, "apikey": TWELVE_KEY},
        timeout=10,
    ).json()
    vals = list(reversed(r["values"]))
    closes = [float(v["close"]) for v in vals]
    highs = [float(v["high"]) for v in vals]
    lows = [float(v["low"]) for v in vals]
    return closes, highs, lows


def sma(vals, n):
    return sum(vals[-n:]) / n


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, n + 1):
        diff = closes[-i] - closes[-i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain, avg_loss = sum(gains) / n, sum(losses) / n
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def macd(closes):
    ema12 = ema(closes[-40:], 12)
    ema26 = ema(closes[-40:], 26)
    return ema12 - ema26


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, n + 1):
        tr = max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i - 1]), abs(lows[-i] - closes[-i - 1]))
        trs.append(tr)
    return sum(trs) / n


def tech_signal(interval):
    closes, highs, lows = get_series(interval, 100)
    fast, slow = sma(closes, 10), sma(closes, 30)
    r = rsi(closes)
    m = macd(closes)

    votes = 0
    votes += 1 if fast > slow else -1 if fast < slow else 0
    votes += 1 if r < 30 else -1 if r > 70 else 0
    votes += 1 if m > 0 else -1 if m < 0 else 0

    sig = "BUY" if votes >= 2 else "SELL" if votes <= -2 else "WAIT"
    return sig, closes[-1], atr(highs, lows, closes)


def load_cache(path):
    if not os.path.exists(path):
        return None
    data = json.load(open(path))
    ts = dt.datetime.fromisoformat(data["ts"])
    if (dt.datetime.utcnow() - ts).total_seconds() < CACHE_MINUTES * 60:
        return data["value"]
    return None


def save_cache(path, value):
    json.dump({"ts": dt.datetime.utcnow().isoformat(), "value": value}, open(path, "w"))


def news_signal():
    cached = load_cache(NEWS_CACHE)
    if cached is not None:
        return cached

    news = requests.get(
        "https://finnhub.io/api/v1/news",
        params={"category": "forex", "token": FINNHUB_KEY},
        timeout=10,
    ).json()
    text = " ".join((n.get("headline", "") + n.get("summary", "")).lower() for n in news[:40])
    score = sum(w in text for w in POS_WORDS) - sum(w in text for w in NEG_WORDS)
    result = "BUY" if score >= 2 else "SELL" if score <= -2 else "WAIT"
    save_cache(NEWS_CACHE, result)
    return result


def combine(tf_signals, news):
    votes = list(tf_signals.values()) + [news, news]  # news นับ 2 เท่า
    if votes.count("BUY") >= 4:
        return "BUY"
    if votes.count("SELL") >= 4:
        return "SELL"
    return "WAIT"


def load_last():
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE).read().strip()
    return ""


def save_last(sig):
    open(STATE_FILE, "w").write(sig)


def calc_lot(entry, sl):
    risk_dollar = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    distance = abs(entry - sl)
    if distance == 0:
        return 0
    # XAU/USD: 1 lot = 100 oz, $1 เคลื่อนไหว = $100 ต่อ lot
    lot = risk_dollar / (distance * 100)
    return round(lot, 2)


def log_signal(now, final, price, tp, sl, lot):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("time,signal,price,tp,sl,lot\n")
        f.write(f"{now:%Y-%m-%d %H:%M},{final},{price:.2f},{tp:.2f},{sl:.2f},{lot}\n")
    return os.path.exists("paused.txt") and open("paused.txt").read().strip() == "true"


def is_low_liquidity(now):
    # ตลาดทองเบาบางช่วง 00:00-07:00 UTC (หลังนิวยอร์กปิด ก่อนลอนดอนเปิด)
    return now.hour < 7


def dxy_direction():
    cached = load_cache(DXY_CACHE)
    if cached is not None:
        return cached if cached != "NONE" else None
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "DXY", "interval": "1h", "outputsize": 10, "apikey": TWELVE_KEY},
            timeout=10,
        ).json()
        closes = [float(v["close"]) for v in reversed(r["values"])]
        result = "UP" if closes[-1] > closes[0] else "DOWN"
        save_cache(DXY_CACHE, result)
        return result
    except Exception:
        save_cache(DXY_CACHE, "NONE")
        return None  # เช็คไม่ได้ ข้ามฟิลเตอร์นี้ไป


def main():
    if is_paused():
        return

    now = dt.datetime.utcnow()
    if is_low_liquidity(now):
        return  # ข้าม ช่วง volume ต่ำ ไม่ส่ง signal

    tfs = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}
    tf_signals, price, a = {}, 0, 0
    for label, interval in tfs.items():
        sig, p, atr_val = tech_signal(interval)
        tf_signals[label] = sig
        if label == "1h":
            price, a = p, atr_val

    news = news_signal()
    final = combine(tf_signals, news)

    dxy = dxy_direction()
    if dxy:
        # ทองกับ dollar ปกติสวนทางกัน ถ้าวิ่งทิศเดียวกัน = สัญญาณไม่น่าเชื่อ ลดเป็น WAIT
        if final == "BUY" and dxy == "UP":
            final = "WAIT"
        elif final == "SELL" and dxy == "DOWN":
            final = "WAIT"

    last = load_last()

    if final != last:
        lines = " | ".join(f"{k}:{v}" for k, v in tf_signals.items())
        tp_sl, lot = "", 0
        if final == "BUY":
            tp, sl = price + a * 2, price - a * 1.5
            lot = calc_lot(price, sl)
            tp_sl = f"TP: {tp:.2f} | SL: {sl:.2f} | Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"
        elif final == "SELL":
            tp, sl = price - a * 2, price + a * 1.5
            lot = calc_lot(price, sl)
            tp_sl = f"TP: {tp:.2f} | SL: {sl:.2f} | Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"

        push_line(
            f"🟡 XAU/USD | {final}\n"
            f"ราคา: {price:.2f}\n"
            f"{tp_sl}\n"
            f"Tech: {lines}\n"
            f"News: {news}\n"
            f"เวลา: {now:%Y-%m-%d %H:%M} UTC\n"
            f"⚠️ ไม่การันตีผล ใช้ควบคู่การจัดการความเสี่ยงเอง"
        )
        save_last(final)
        if final in ("BUY", "SELL"):
            log_signal(now, final, price, tp, sl, lot)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        push_line(f"🛑 SYSTEM ERROR\n{type(e).__name__}: {e}\nเวลา: {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
        raise
