import os, json, requests, datetime as dt

LINE_TOKEN      = os.environ["LINE_TOKEN"]
TWELVE_KEY      = os.environ["TWELVEDATA_KEY"]
FINNHUB_KEY     = os.environ["FINNHUB_KEY"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "1000"))
RISK_PERCENT    = float(os.environ.get("RISK_PERCENT", "1"))
STATE_FILE      = "last_signal.txt"
LOG_FILE        = "signal_log.csv"
NEWS_CACHE      = "news_cache.json"
DXY_CACHE       = "dxy_cache.json"
CACHE_MINUTES   = 60
SHARP_THRESHOLD = 15
LEVEL_TOLERANCE = 0.0015  # 0.15% ถือว่าเป็นระดับเดียวกัน

NEG_WORDS = ["war", "conflict", "crisis", "rate hike", "inflation surge", "recession",
             "sanction", "hawkish", "strong dollar", "yields rise", "geopolitical tension"]
POS_WORDS = ["rate cut", "stimulus", "safe haven", "dovish", "ceasefire", "easing",
             "weak dollar", "yields fall", "fed pause", "de-escalation"]


def push_to(user_id, text):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )
    print(f"LINE push to {user_id[:6]}...: {r.status_code}")
    return r.status_code == 200


def load_users():
    if not os.path.exists("users.json"):
        return {}
    return json.load(open("users.json"))


def push_to_all_active(text):
    users = load_users()
    for uid, info in users.items():
        try:
            if not info.get("paused", False):
                push_to(uid, text)
        except Exception as e:
            print(f"skip {uid}: {e}")


def get_series(interval, size=150):
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
    return [sum(vals[i - n + 1:i + 1]) / n for i in range(n - 1, len(vals))]


def ema_series(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def stochastic(closes, highs, lows, period=5, smooth_k=3, smooth_d=3):
    raw_k = []
    for i in range(period - 1, len(closes)):
        wh = max(highs[i - period + 1:i + 1])
        wl = min(lows[i - period + 1:i + 1])
        raw_k.append(50 if wh == wl else (closes[i] - wl) / (wh - wl) * 100)
    slow_k = sma(raw_k, smooth_k)
    d = sma(slow_k, smooth_d)
    slow_k = slow_k[-len(d):]
    return slow_k, d


def is_sharp_turn(k):
    if len(k) < 3:
        return None
    diff1 = k[-2] - k[-3]
    diff2 = k[-1] - k[-2]
    if diff1 < 0 and diff2 > 0 and abs(diff1) >= SHARP_THRESHOLD and abs(diff2) >= SHARP_THRESHOLD:
        return "UP"
    if diff1 > 0 and diff2 < 0 and abs(diff1) >= SHARP_THRESHOLD and abs(diff2) >= SHARP_THRESHOLD:
        return "DOWN"
    return None


def cross_state(k, d):
    gap_prev = k[-2] - d[-2]
    gap_now = k[-1] - d[-1]
    if gap_prev <= 0 and gap_now > 0:
        return "CROSSED_UP"
    if gap_prev >= 0 and gap_now < 0:
        return "CROSSED_DOWN"
    if gap_now < 0 and abs(gap_now) < abs(gap_prev):
        return "APPROACHING_UP"
    if gap_now > 0 and abs(gap_now) < abs(gap_prev):
        return "APPROACHING_DOWN"
    return "NONE"


def cross_label(state):
    return {
        "CROSSED_UP": "ตัดขึ้นแล้ว ✅",
        "CROSSED_DOWN": "ตัดลงแล้ว ✅",
        "APPROACHING_UP": "ใกล้จะตัดขึ้น ⏳",
        "APPROACHING_DOWN": "ใกล้จะตัดลง ⏳",
        "NONE": "ยังไม่ตัด",
    }.get(state, "ยังไม่ตัด")


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, n + 1):
        tr = max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i - 1]), abs(lows[-i] - closes[-i - 1]))
        trs.append(tr)
    return sum(trs) / n


def tf_data(interval):
    closes, highs, lows = get_series(interval, 100)
    k, d = stochastic(closes, highs, lows)
    return {
        "closes": closes, "highs": highs, "lows": lows,
        "k": k, "d": d,
        "sharp": is_sharp_turn(k),
        "cross": cross_state(k, d),
        "price": closes[-1],
        "atr": atr(highs, lows, closes),
    }


def find_levels(prices, current_price, tolerance=LEVEL_TOLERANCE):
    # นับจุดที่ราคาแตะซ้ำใกล้เคียงกัน = ระดับสำคัญ ยิ่งแตะบ่อย = แข็งแรง
    clusters = []
    for p in prices:
        placed = False
        for c in clusters:
            if abs(p - c["level"]) / c["level"] <= tolerance:
                c["touches"] += 1
                c["level"] = (c["level"] * (c["touches"] - 1) + p) / c["touches"]
                placed = True
                break
        if not placed:
            clusters.append({"level": p, "touches": 1})

    resistances = sorted([c for c in clusters if c["level"] > current_price], key=lambda c: c["level"])
    supports = sorted([c for c in clusters if c["level"] < current_price], key=lambda c: -c["level"])

    res = resistances[0] if resistances else None
    sup = supports[0] if supports else None
    return res, sup


def strength_label(touches):
    return "แข็งแรง 💪" if touches >= 3 else "ไม่แข็งแรง (แตะน้อย)"


def market_trend():
    closes, _, _ = get_series("4h", 250)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200) if len(closes) >= 200 else None
    if ema200 is None:
        return "ข้อมูลไม่พอเช็คเทรนด์ 4h"
    if ema50[-1] > ema200[-1]:
        return "เทรนด์ใหญ่ (4h): ขึ้น 📈"
    if ema50[-1] < ema200[-1]:
        return "เทรนด์ใหญ่ (4h): ลง 📉"
    return "เทรนด์ใหญ่ (4h): Sideways"


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
        return None


def is_paused():
    return os.path.exists("paused.txt") and open("paused.txt").read().strip() == "true"


def is_low_liquidity(now):
    return now.hour < 7


def calc_lot(entry, sl):
    risk_dollar = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    distance = abs(entry - sl)
    if distance == 0:
        return 0
    return round(risk_dollar / (distance * 100), 2)


def log_signal(now, final, price, tp, sl, lot):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("time,signal,price,tp,sl,lot\n")
        f.write(f"{now:%Y-%m-%d %H:%M},{final},{price:.2f},{tp:.2f},{sl:.2f},{lot}\n")


def load_last():
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE).read().strip()
    return ""


def save_last(sig):
    open(STATE_FILE, "w").write(sig)


def confidence_and_direction(tf15, tf30, dxy):
    up, down, total = 0, 0, 4
    if tf15["sharp"] == "UP":
        up += 1
    if tf15["sharp"] == "DOWN":
        down += 1
    if tf15["cross"] == "CROSSED_UP":
        up += 1
    elif tf15["cross"] == "APPROACHING_UP":
        up += 0.5
    if tf15["cross"] == "CROSSED_DOWN":
        down += 1
    elif tf15["cross"] == "APPROACHING_DOWN":
        down += 0.5
    if tf30["cross"] in ("CROSSED_UP", "APPROACHING_UP"):
        up += 1
    if tf30["cross"] in ("CROSSED_DOWN", "APPROACHING_DOWN"):
        down += 1
    if dxy == "DOWN":
        up += 1
    if dxy == "UP":
        down += 1

    if up >= down:
        return "BUY", round(up / total * 100)
    return "SELL", round(down / total * 100)


def save_status_snapshot(now, tf15, tf30, tf1h, tf4h, dxy, news):
    snapshot = {
        "time": f"{now:%Y-%m-%d %H:%M} UTC",
        "price": tf15["price"],
        "15m": {"k": round(tf15["k"][-1], 1), "d": round(tf15["d"][-1], 1), "cross": tf15["cross"]},
        "30m": {"k": round(tf30["k"][-1], 1), "d": round(tf30["d"][-1], 1), "cross": tf30["cross"]},
        "1h": {"k": round(tf1h["k"][-1], 1), "d": round(tf1h["d"][-1], 1), "cross": tf1h["cross"]},
        "4h": {"k": round(tf4h["k"][-1], 1), "d": round(tf4h["d"][-1], 1), "cross": tf4h["cross"]},
        "dxy": dxy,
        "news": news,
    }
    json.dump(snapshot, open("status.json", "w"), ensure_ascii=False)


def main():
    force = os.environ.get("FORCE_REPORT", "false").lower() == "true"
    target_user = os.environ.get("TARGET_USER", "").strip()

    now = dt.datetime.utcnow()
    if is_low_liquidity(now) and not force:
        return

    tf15 = tf_data("15min")
    tf30 = tf_data("30min")
    tf1h = tf_data("1h")
    tf4h = tf_data("4h")

    dxy_early = dxy_direction()
    news_early = news_signal()
    save_status_snapshot(now, tf15, tf30, tf1h, tf4h, dxy_early, news_early)

    final = "WAIT"
    if tf30["cross"] == "CROSSED_UP" and tf15["cross"] in ("CROSSED_UP", "APPROACHING_UP"):
        final = "BUY"
    elif tf30["cross"] == "CROSSED_DOWN" and tf15["cross"] in ("CROSSED_DOWN", "APPROACHING_DOWN"):
        final = "SELL"

    dxy = dxy_early
    if dxy:
        if final == "BUY" and dxy == "UP":
            final = "WAIT"
        elif final == "SELL" and dxy == "DOWN":
            final = "WAIT"

    last = load_last()

    if final == last and not force:
        return

    price = tf30["price"]

    if final == last and force:
        # แค่เช็คสถานะ ยังไม่เข้าเงื่อนไขจริง ส่งกลับเฉพาะคนที่สั่งเช็ค
        direction, pct = confidence_and_direction(tf30, tf15, dxy)
        msg = (
            f"🔍 เช็คสถานะ XAU/USD (ยังไม่ใช่สัญญาณเข้า)\n"
            f"ราคา: {price:.2f}\n\n"
            f"30m (หลัก): {cross_label(tf30['cross'])}\n"
            f"15m (รอง): {cross_label(tf15['cross'])}\n"
            f"1h (ประกอบ): {cross_label(tf1h['cross'])}\n\n"
            f"แนวโน้มถ้าจะเข้า: {direction} (ความพร้อม ~{pct}%)\n"
            f"เวลา: {now:%Y-%m-%d %H:%M} UTC"
        )
        if target_user:
            push_to(target_user, msg)
        else:
            push_to_all_active(msg)
        return

    a = tf30["atr"]
    news = news_signal()

    # แนวรับ-แนวต้าน จาก swing high/low ของ 4h ย้อนหลัง
    closes4h, highs4h, lows4h = get_series("4h", 150)
    res, sup = find_levels(highs4h + lows4h, price)
    trend = market_trend()

    level_text = ""
    if res:
        level_text += f"แนวต้าน: {res['level']:.2f} ({strength_label(res['touches'])}, แตะ {res['touches']} ครั้ง)\n"
    if sup:
        level_text += f"แนวรับ: {sup['level']:.2f} ({strength_label(sup['touches'])}, แตะ {sup['touches']} ครั้ง)\n"

    buffer = a * 0.3  # เผื่อราคาแกว่งเลยแนวไปเคลียร์ liquidity ก่อนกลับตัว

    tp_sl, lot = "", 0
    if final == "BUY":
        sl = (sup["level"] - buffer) if sup else price - a * 0.5
        tp = res["level"] if res else price + a * 0.8
        lot = calc_lot(price, sl)
        tp_sl = (
            f"TP: {tp:.2f} (แนวต้าน){' ' + strength_label(res['touches']) if res else ''}\n"
            f"SL: {sl:.2f} (ใต้แนวรับ กันโดนล่า)\n"
            f"Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"
        )
    elif final == "SELL":
        sl = (res["level"] + buffer) if res else price + a * 0.5
        tp = sup["level"] if sup else price - a * 0.8
        lot = calc_lot(price, sl)
        tp_sl = (
            f"TP: {tp:.2f} (แนวรับ){' ' + strength_label(sup['touches']) if sup else ''}\n"
            f"SL: {sl:.2f} (เหนือแนวต้าน กันโดนล่า)\n"
            f"Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"
        )

    push_to_all_active(
        f"🟡 XAU/USD | {final}\n"
        f"ราคา: {price:.2f}\n"
        f"{tp_sl}\n\n"
        f"30m (หลัก): {cross_label(tf30['cross'])}\n"
        f"15m (รอง): {cross_label(tf15['cross'])}\n"
        f"1h (ประกอบ): {cross_label(tf1h['cross'])}\n\n"
        f"{level_text}"
        f"{trend}\n"
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
        print(f"🛑 SYSTEM ERROR: {type(e).__name__}: {e}")
        raise
