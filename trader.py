"""Paper options day trader. Run every 5 min during market hours (GitHub Actions does this).
Real stock/ETF quotes (Yahoo Finance via yfinance, free, ~15 min delayed).
Option premiums are MODELED with Black-Scholes, not real option quotes.
Account stays frozen at its last value whenever the market is closed."""
import json, math, os, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf

ET = ZoneInfo("America/New_York")
START, FEE, RATE = 100_000.0, 0.65, 0.045
IV = {"SPY": .16, "QQQ": .21, "GLD": .15, "USO": .35}   # modeled volatility per ticker
STATE, REPORT = "state.json", "STATUS.md"
MAX_QUOTE_AGE_MIN = 30   # quote must be this fresh to count as "market live"


def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs(kind, s, k, t, sg):
    if t <= 0: return max(0.0, s - k if kind == "C" else k - s)
    v = sg * math.sqrt(t); d1 = (math.log(s / k) + (RATE + sg * sg / 2) * t) / v; d2 = d1 - v
    df = k * math.exp(-RATE * t); c = s * ncdf(d1) - df * ncdf(d2)
    return c if kind == "C" else c - s + df

def tyears(exp, now): return max((exp - now) / 31_536_000, 1e-6)

def fridays(now_et, n=4):
    out, i = [], 1
    while len(out) < n:
        d = (now_et + dt.timedelta(days=i)).date()
        if d.weekday() == 4:
            out.append(dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp())
        i += 1
    return out

def fresh():
    return dict(cash=START, eq=START, pos=[], log=[], curve=[[0, START]], last={}, h={}, h_date="",
                id=1, wins=0, losses=0, real=0.0, day_start={"d": "", "eq": START}, bot=True)

def get_quote(sym, now_et):
    """Return dict(p,o,pc) if there is a fresh quote from today's session, else None."""
    t = yf.Ticker(sym)
    intra = t.history(period="1d", interval="1m")
    if intra.empty: return None
    last_ts = intra.index[-1].to_pydatetime().astimezone(ET)
    if last_ts.date() != now_et.date() or (now_et - last_ts).total_seconds() > MAX_QUOTE_AGE_MIN * 60:
        return None
    daily = t.history(period="7d", interval="1d")
    prev = [r for d, r in daily["Close"].items() if d.date() < now_et.date()]
    if not prev: return None
    return dict(p=float(intra["Close"].iloc[-1]), o=float(intra["Open"].iloc[0]), pc=float(prev[-1]))

def main():
    now = dt.datetime.now(ET); now_ts = now.timestamp()
    S = json.load(open(STATE)) if os.path.exists(STATE) else fresh()
    quotes = {}
    for s in IV:
        try:
            q = get_quote(s, now)
            if q: quotes[s] = q
        except Exception as e:
            print("quote error", s, e)
    if not quotes:
        print("Market not live (no fresh quotes). Account frozen at", round(S["eq"], 2))
        if not os.path.exists(STATE): save(S, now)
        return
    today = str(now.date())
    if S["h_date"] != today: S["h"], S["h_date"] = {}, today
    if S["day_start"]["d"] != today: S["day_start"] = {"d": today, "eq": S["eq"]}
    for s, q in quotes.items():
        S["last"][s] = q["p"]; S["h"].setdefault(s, []).append(q["p"]); S["h"][s] = S["h"][s][-40:]

    def mid(p): return bs(p["t"], S["last"].get(p["sym"], p["K"]), p["K"], tyears(p["exp"], now_ts), IV[p["sym"]])
    def equity(): return S["cash"] + sum(mid(p) * 100 * p["q"] for p in S["pos"])
    def log(m, pl=None): S["log"].insert(0, [now.strftime("%m-%d %H:%M"), m, pl]); del S["log"][80:]

    def close(p, why="", settle=False):
        m = mid(p); bid = m if settle else max(0.0, m * .99)
        proc = bid * 100 * p["q"] - (0 if settle else FEE * p["q"])
        pl = proc - (p["e"] * 100 * p["q"] + FEE * p["q"])
        S["cash"] += proc; S["pos"].remove(p); S["real"] += pl; S["wins" if pl >= 0 else "losses"] += 1
        log(f'{"EXPIRED" if settle else "SELL"} {p["q"]} {p["sym"]} {p["K"]}{p["t"]} @ {bid:.2f} {why}', round(pl, 2))

    def sig(s):
        q = quotes.get(s)
        if not q: return 0
        h = S["h"].get(s, []); mo = (q["p"] - q["o"]) / q["o"]; sl = h[-1] - h[-4] if len(h) >= 4 else 0
        if mo > .0015 and q["p"] > q["pc"] and sl >= 0: return 1
        if mo < -.0015 and q["p"] < q["pc"] and sl <= 0: return -1
        return 0

    for p in list(S["pos"]):                      # settle expired contracts
        if now_ts >= p["exp"]: close(p, settle=True)

    mins = now.hour * 60 + now.minute
    if S["bot"]:
        for p in list(S["pos"]):
            if p.get("src") != "bot": continue
            r = mid(p) / p["e"] - 1; g = sig(p["sym"])
            if mins >= 945: close(p, "(end of day)")
            elif r >= .30: close(p, "(take profit)")
            elif r <= -.20: close(p, "(stop loss)")
            elif (p["t"] == "C" and g == -1) or (p["t"] == "P" and g == 1): close(p, "(signal flip)")
        nbot = sum(1 for p in S["pos"] if p.get("src") == "bot")
        if 585 <= mins < 945 and nbot < 4:
            eq = equity(); exp = fridays(now)[0]
            for s in IV:
                if nbot >= 4 or any(p["sym"] == s and p.get("src") == "bot" for p in S["pos"]): continue
                g = sig(s)
                if not g or s not in quotes: continue
                sp = quotes[s]["p"]; st = 1 if sp < 100 else 2.5 if sp < 300 else 5
                K = round(sp / st) * st; kind = "C" if g > 0 else "P"
                ask = max(.01, bs(kind, sp, K, tyears(exp, now_ts), IV[s]) * 1.01)
                qty = max(1, int(eq * .05 // (ask * 100)))
                cost = ask * 100 * qty + FEE * qty
                if cost > S["cash"]: continue
                S["cash"] -= cost; nbot += 1
                S["pos"].append(dict(id=S["id"], sym=s, t=kind, K=K, exp=exp, q=qty, e=ask, src="bot")); S["id"] += 1
                log(f"🤖 BUY {qty} {s} {K}{kind} exp {dt.datetime.fromtimestamp(exp, ET):%m-%d} @ {ask:.2f}")

    S["eq"] = equity(); S["curve"].append([int(now_ts), round(S["eq"], 2)]); S["curve"] = S["curve"][-1500:]
    save(S, now, mid)

def save(S, now, mid=None):
    json.dump(S, open(STATE, "w"), indent=1)
    pl = S["eq"] - START; day = S["eq"] - S["day_start"]["eq"]; n = S["wins"] + S["losses"]
    L = ["# Paper Options Trader", "",
         f"**Account value: ${S['eq']:,.2f}**  |  Total P/L: ${pl:+,.2f} ({pl / START * 100:+.2f}%)  |  Today: ${day:+,.2f}", "",
         f"Cash ${S['cash']:,.2f} · Realized ${S['real']:+,.2f} · Closed trades {n}" + (f" · Win rate {S['wins'] / n * 100:.0f}%" if n else ""),
         f"Last update: {now:%Y-%m-%d %H:%M} ET. The account only changes while the US market is open.", "",
         "_Real stock/ETF quotes (delayed). Option prices are modeled with Black-Scholes._", "", "## Open positions", ""]
    if S["pos"] and mid:
        L += ["| Contract | Qty | Entry | Mark | P/L |", "|---|---|---|---|---|"]
        for p in S["pos"]:
            m = mid(p); L.append(f"| {p['sym']} {p['K']}{p['t']} exp {dt.datetime.fromtimestamp(p['exp'], ET):%m-%d} | {p['q']} | {p['e']:.2f} | {m:.2f} | ${(m - p['e']) * 100 * p['q']:+,.2f} |")
    else: L.append("None")
    L += ["", "## Recent trades", ""] + [f"- {t} {m}" + (f" (**${pl:+,.2f}**)" if pl is not None else "") for t, m, pl in S["log"][:20]]
    open(REPORT, "w").write("\n".join(L) + "\n")

if __name__ == "__main__":
    main()
