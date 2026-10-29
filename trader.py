"""Paper options day trader with 3 risk profiles (base / risky / extreme), all run in one go.
Real stock/ETF quotes (Yahoo Finance via yfinance, free, ~15 min delayed).
Option premiums are MODELED with Black-Scholes, not real option quotes.
Each account stays frozen whenever the market is closed."""
import json, math, os, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf

ET = ZoneInfo("America/New_York")
START, FEE, RATE = 100_000.0, 0.65, 0.045
MAX_QUOTE_AGE_MIN = 30
MAX_QTY, MIN_PREMIUM = 100, 0.30   # realism: cap contracts per trade, skip penny options
IV = {"SPY": .16, "QQQ": .21, "GLD": .15, "USO": .35, "NVDA": .50, "TSLA": .55, "AMD": .55}  # modeled volatility
CORE = ["SPY", "QQQ", "GLD", "USO"]

# ---- EDIT THESE to change how risky each account is -------------------------------------------
# thr = % move from today's open needed to enter | risk = share of account per trade
# otm = strikes out-of-the-money (cheaper, riskier) | tp/sl = take-profit / stop-loss on the option
# same_day = may buy contracts expiring today | flatten = sell everything by 15:45 ET (no overnight)
PROFILES = {
    "base":    dict(file="state.json",         report="STATUS.md",         universe=CORE,
                    thr=.0015, risk=.05, maxpos=4, otm=0, tp=.30, sl=-.20, same_day=False, flatten=True),
    "risky":   dict(file="state_risky.json",   report="STATUS_risky.md",   universe=CORE + ["NVDA", "TSLA"],
                    thr=.0010, risk=.15, maxpos=4, otm=1, tp=.60, sl=-.35, same_day=True,  flatten=True),
    "extreme": dict(file="state_extreme.json", report="STATUS_extreme.md", universe=CORE + ["NVDA", "TSLA", "AMD"],
                    thr=.0005, risk=.40, maxpos=3, otm=2, tp=1.50, sl=-.60, same_day=True, flatten=False),
}
# -----------------------------------------------------------------------------------------------

def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs(kind, s, k, t, sg):
    if t <= 0: return max(0.0, s - k if kind == "C" else k - s)
    v = sg * math.sqrt(t); d1 = (math.log(s / k) + (RATE + sg * sg / 2) * t) / v; d2 = d1 - v
    df = k * math.exp(-RATE * t); c = s * ncdf(d1) - df * ncdf(d2)
    return c if kind == "C" else c - s + df

def tyears(exp, now): return max((exp - now) / 31_536_000, 1e-6)

def next_friday(now_et, include_today):
    i = 0 if include_today else 1
    while True:
        d = (now_et + dt.timedelta(days=i)).date()
        if d.weekday() == 4:
            return dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp()
        i += 1

def fresh():
    return dict(cash=START, eq=START, pos=[], log=[], curve=[[0, START]], last={}, h={}, h_date="",
                id=1, wins=0, losses=0, real=0.0, day_start={"d": "", "eq": START}, bot=True)

def get_quote(sym, now_et):
    t = yf.Ticker(sym)
    intra = t.history(period="1d", interval="1m")
    if intra.empty:
        print(sym, "no intraday data returned (Yahoo may be blocking or market closed)"); return None
    last_ts = intra.index[-1].to_pydatetime().astimezone(ET)
    if last_ts.date() != now_et.date() or (now_et - last_ts).total_seconds() > MAX_QUOTE_AGE_MIN * 60:
        print(sym, "quote not fresh: last bar", last_ts.strftime("%m-%d %H:%M"), "ET"); return None
    daily = t.history(period="7d", interval="1d")
    prev = [r for d, r in daily["Close"].items() if d.date() < now_et.date()]
    if not prev:
        print(sym, "no previous close found"); return None
    return dict(p=float(intra["Close"].iloc[-1]), o=float(intra["Open"].iloc[0]), pc=float(prev[-1]))

def run_profile(name, cfg, allq, now):
    now_ts = now.timestamp(); today = str(now.date()); mins = now.hour * 60 + now.minute
    S = json.load(open(cfg["file"])) if os.path.exists(cfg["file"]) else fresh()
    quotes = {s: q for s, q in allq.items() if s in cfg["universe"]}
    if not quotes:
        if not os.path.exists(cfg["file"]): save(name, cfg, S, now)
        return
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
        if mo > cfg["thr"] and q["p"] > q["pc"] and sl >= 0: return 1
        if mo < -cfg["thr"] and q["p"] < q["pc"] and sl <= 0: return -1
        return 0

    for p in list(S["pos"]):
        if now_ts >= p["exp"]: close(p, settle=True)

    if S["bot"]:
        for p in list(S["pos"]):
            r = mid(p) / p["e"] - 1; g = sig(p["sym"])
            if cfg["flatten"] and mins >= 945: close(p, "(end of day)")
            elif r >= cfg["tp"]: close(p, "(take profit)")
            elif r <= cfg["sl"]: close(p, "(stop loss)")
            elif (p["t"] == "C" and g == -1) or (p["t"] == "P" and g == 1): close(p, "(signal flip)")
        if 585 <= mins < 945:
            exp = next_friday(now, cfg["same_day"] and mins < 930)
            for s in cfg["universe"]:
                if len(S["pos"]) >= cfg["maxpos"]: break
                if s not in quotes or any(p["sym"] == s for p in S["pos"]): continue
                g = sig(s)
                if not g: continue
                sp = quotes[s]["p"]; st = 1 if sp < 100 else 2.5 if sp < 300 else 5
                K = round(sp / st) * st + (cfg["otm"] * st if g > 0 else -cfg["otm"] * st); kind = "C" if g > 0 else "P"
                ask = max(.01, bs(kind, sp, K, tyears(exp, now_ts), IV[s]) * 1.01)
                if ask < MIN_PREMIUM: continue
                qty = min(MAX_QTY, int(equity() * cfg["risk"] // (ask * 100)))
                if qty < 1 or ask * 100 * qty + FEE * qty > S["cash"]: continue
                S["cash"] -= ask * 100 * qty + FEE * qty
                S["pos"].append(dict(id=S["id"], sym=s, t=kind, K=K, exp=exp, q=qty, e=ask, src="bot")); S["id"] += 1
                log(f"BUY {qty} {s} {K}{kind} exp {dt.datetime.fromtimestamp(exp, ET):%m-%d} @ {ask:.2f}")

    S["eq"] = equity(); S["curve"].append([int(now_ts), round(S["eq"], 2)]); S["curve"] = S["curve"][-1500:]
    save(name, cfg, S, now, mid)

def save(name, cfg, S, now, mid=None):
    json.dump(S, open(cfg["file"], "w"), indent=1)
    pl = S["eq"] - START; day = S["eq"] - S["day_start"]["eq"]; n = S["wins"] + S["losses"]
    L = [f"# Paper Options Trader: {name.upper()} profile", "",
         f"**Account value: ${S['eq']:,.2f}**  |  Total P/L: ${pl:+,.2f} ({pl / START * 100:+.2f}%)  |  Today: ${day:+,.2f}", "",
         f"Cash ${S['cash']:,.2f} | Realized ${S['real']:+,.2f} | Closed trades {n}" + (f" | Win rate {S['wins'] / n * 100:.0f}%" if n else ""),
         f"Last update: {now:%Y-%m-%d %H:%M} ET. The account only changes while the US market is open.", "",
         f"Tickers: {', '.join(cfg['universe'])}. Risk per trade {cfg['risk'] * 100:.0f}%, {cfg['otm']} strikes OTM, "
         f"{'0DTE allowed' if cfg['same_day'] else 'no 0DTE'}, {'closes by 15:45 ET' if cfg['flatten'] else 'can hold overnight'}.", "",
         "_Real stock/ETF quotes (delayed). Option prices are modeled with Black-Scholes._", "", "## Open positions", ""]
    if S["pos"] and mid:
        L += ["| Contract | Qty | Entry | Mark | P/L |", "|---|---|---|---|---|"]
        for p in S["pos"]:
            m = mid(p); L.append(f"| {p['sym']} {p['K']}{p['t']} exp {dt.datetime.fromtimestamp(p['exp'], ET):%m-%d} | {p['q']} | {p['e']:.2f} | {m:.2f} | ${(m - p['e']) * 100 * p['q']:+,.2f} |")
    else: L.append("None")
    L += ["", "## Recent trades", ""] + [f"- {t} {m}" + (f" (**${pl:+,.2f}**)" if pl is not None else "") for t, m, pl in S["log"][:20]]
    open(cfg["report"], "w").write(("\n".join(L) + "\n").replace("$", "\\$"))

def main():
    now = dt.datetime.now(ET)
    allq = {}
    for s in sorted({s for c in PROFILES.values() for s in c["universe"]}):
        try:
            q = get_quote(s, now)
            if q: allq[s] = q
        except Exception as e:
            print("quote error", s, e)
    if not allq: print("Market not live (no fresh quotes). Accounts frozen.")
    for name, cfg in PROFILES.items(): run_profile(name, cfg, allq, now)

if __name__ == "__main__":
    main()
