#!/usr/bin/env python3
"""Heat Bot: runs the Day Trader skill pack on a schedule and sends the top pick to Telegram.

Alert only. It never places an order. It reads public data, does arithmetic, and tells you
what the pack's own rules found. Not financial advice.

    python3 heat.py auto                 # decide from New York time (what the GitHub workflow runs)
    python3 heat.py premarket            # daily setups across the whole universe -> watch list
    python3 heat.py orb                  # 10:15 ET breakouts (buys) and breakdowns (shorts) -> top pick -> exit alerts
    python3 heat.py demo                 # offline run on the pack's TSLA fixtures, prints the messages

Environment:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   where alerts go (without them, messages are printed)
    SEC_USER_AGENT                         "Your Name you@example.com" (optional, adds SEC 8-K filings)
    TRADER_REPO                            path to the day-trader-skill checkout (default ./day-trader-skill)

Standard library only, Python 3.9+.
"""
import argparse
import datetime as dt
import html
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "trader")          # the pack's trader/ folder
REPO = os.environ.get("TRADER_REPO") or os.path.join(HERE, "day-trader-skill")

# How a setup's score is built (expected R per trade, the Time Machine's own numbers):
MC_WEIGHT, HIST_WEIGHT = 0.6, 0.4
HIST_FULL_TRUST = 15        # history trades needed before the history counts fully
MIN_SCORE = 0.05            # below this expected R, the bot says "no pick" instead of forcing one
CANDIDATES = 12             # breakouts sent through the full pipeline
WATCH_EVERY = 120           # seconds between exit checks
DEMO_NOW = "2026-10-19 10:15"


# ---------- plumbing ----------

def log(*a):
    print(*a, file=sys.stderr, flush=True)


def tool(script, *args, offline=None, timeout=900):
    """Run one of the pack's scripts from WORK's parent, so its trader/ paths resolve. -> (rc, stdout)."""
    cmd = [sys.executable, "-I", os.path.join(REPO, script)] + [str(x) for x in args]
    if offline:
        cmd += ["--offline", offline]
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("timeout:", script, args)
        return 124, ""
    if p.returncode not in (0, 1, 2):
        log("error %d: %s %s\n%s" % (p.returncode, script, " ".join(map(str, args)), p.stderr[-800:]))
    return p.returncode, p.stdout


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def jout(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def ny_now(fake=None):
    if fake:
        return dt.datetime.strptime(fake, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    return dt.datetime.now(NY)


def paris(t):
    return t.astimezone(PARIS).strftime("%H:%M")


def send(text, dry=False):
    """Telegram, or stdout when there is no token (or --dry-run)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not token or not chat:
        print("\n----- message -----\n" + text + "\n-------------------", flush=True)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    for attempt in range(3):
        try:
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token, data, timeout=20).read()
            return
        except Exception as e:  # noqa: BLE001 - keep the run alive, log and retry
            log("telegram failed (%s), retry %d" % (e, attempt + 1))
            time.sleep(3)


def e(s):
    return html.escape(str(s))


def market_open(now_s):
    rc, out = tool("trader-alerts/alerts.py", "clock", "--now", now_s, "--json")
    st = jout(out) or {}
    return st.get("open", True), st


# ---------- news ----------

def news_for(symbols, now_iso=None, offline=None):
    if not symbols:
        return {}
    args = ["--tickers"] + list(symbols) + ["--hours", "12", "--dry-run", "--json"]
    if not os.environ.get("SEC_USER_AGENT"):
        args.append("--no-sec")
    if now_iso:
        args += ["--now", now_iso]
    rc, out = tool("trader-news/news.py", *args, offline=offline)
    doc = jout(out) or {}
    by = {s: {"items": [], "move": None} for s in symbols}
    for it in doc.get("items", []):
        for s in it.get("touches") or [it.get("ticker")]:
            if s in by:
                by[s]["items"].append(it)
    for m in doc.get("moves", []):
        if m.get("ticker") in by:
            by[m["ticker"]]["move"] = m
    return by


def news_line(n):
    if not n:
        return "no news check"
    items = n["items"]
    tags = sorted({t for it in items for t in it.get("tags", [])})
    bits = ["%d headline%s in 12h" % (len(items), "" if len(items) == 1 else "s")]
    if tags:
        bits.append("tags: " + ", ".join(tags))
    if n.get("move"):
        bits.append("today %+.1f%%" % n["move"]["pct"])
    top = next((it for it in items if it.get("tags")), items[0] if items else None)
    line = " · ".join(bits)
    if top:
        line += '\n   "%s"' % e(top["title"][:110])
    return line


# ---------- pre-market ----------

def premarket(a):
    now = ny_now(a.now)
    tool("trader-scanner/scan.py", "universe", timeout=600)
    rc, _ = tool("trader-scanner/scan.py", "daily", "--quiet", "--out", "trader/daily.json", timeout=3000)
    doc = read(os.path.join(WORK, "daily.json"), {})
    hits = doc.get("hits", [])
    if not hits:
        send("<b>Pre-market</b> %s ET / %s Paris\nNo daily setups found (or the data source failed)."
             % (now.strftime("%H:%M"), paris(now)), a.dry_run)
        return
    hits.sort(key=lambda h: h.get("volume") or 0, reverse=True)
    top = hits[:6]
    news = news_for([h["symbol"] for h in top])
    lines = ["<b>Pre-market watch list</b> · %s ET / %s Paris" % (now.strftime("%a %d %b %H:%M"), paris(now)),
             "%d of %s charts set up. Most-traded six:" % (len(hits), doc.get("scanned", "?")), ""]
    for h in top:
        rsi = " · RSI %.0f" % h["rsi"] if h.get("rsi") is not None else ""
        lines.append("<b>%s</b> $%.2f · %s%s" % (e(h["symbol"]), h["price"], ", ".join(h["setups"]), rsi))
        lines.append("   " + news_line(news.get(h["symbol"])))
    lines += ["", "A watch list, not buy signals. The ranked pick comes after the 10:15 ET scan (%s Paris)."
              % paris(now.replace(hour=10, minute=15))]
    send("\n".join(lines), a.dry_run)


# ---------- opening-range pipeline ----------

def run_candidate(sym, now_s, offline):
    """scanner hit -> entry -> exit -> time machine -> budget -> risk. -> dict or None."""
    d = "trader/c/%s" % sym
    os.makedirs(os.path.join(HERE, d), exist_ok=True)
    tool("trader-entry/entry.py", "check", sym, "--setup", "orb", "--now", now_s, "--write", d + "/entry.json",
         offline=offline.get("entry", {}).get(sym) if offline else None)
    entry = read(os.path.join(HERE, d, "entry.json"))
    if not entry or entry.get("status") != "triggered":
        return None
    tool("trader-exit/exit.py", "plan", "--from", d + "/entry.json", "--stop", "or-mid", "--target", "1R",
         "--now", now_s, "--write", d + "/plan.json")
    plan = read(os.path.join(HERE, d, "plan.json"))
    if not plan:
        return None
    tool("trader-timemachine/timemachine.py", "run", "--plan", d + "/plan.json", "--now", now_s,
         "--write", d + "/tm.json", offline=offline.get("tm", {}).get(sym) if offline else None)
    tm = read(os.path.join(HERE, d, "tm.json"))
    if not tm:
        return None
    tool("trader-budget/size.py", "--plan", d + "/plan.json", "--now", now_s, "--write", d + "/orders.json")
    rc, out = tool("trader-risk/check.py", "--orders", d + "/orders.json", "--now", now_s,
                   "--out", d + "/risk.json", "--json")
    risk = jout(out) or {}
    res = (risk.get("results") or [{}])[0]
    return {"symbol": sym, "side": "buy", "dir": d, "plan": plan, "mc": tm["mc"], "hist": tm["history"],
            "verdict": tm.get("verdict"), "risk": res, "score": score_of(tm)}


def cents_up(x):
    return math.ceil(round(x * 100, 6)) / 100.0


def run_short(hit, now_s, offline):
    """breakdown hit -> plan (mirror of the pack's or-mid stop, 1R target) -> time machine (sell side)
    -> budget (sized as the mirror-image long, same risk) -> risk (allow_short). -> dict or None."""
    sym = hit["symbol"]
    d = "trader/c/%s-short" % sym
    os.makedirs(os.path.join(HERE, d), exist_ok=True)
    cfg = read(os.path.join(WORK, "config.json"), {})
    entry, oh, ol = hit["price"], hit["or_high"], hit["or_low"]
    stop = cents_up((oh + ol) / 2.0)
    risk = round(stop - entry, 2)
    if risk <= 0:
        return None
    target = round(entry - risk, 2)
    plan = {"created_by": "heat-bot", "symbol": sym, "side": "sell", "date": now_s[:10], "setup": "orb",
            "entry": entry, "entry_time": hit["at"], "entry_bar": hit["bar_index"], "interval": "5m",
            "opening_range": {"high": oh, "low": ol, "minutes": 15}, "stop": stop, "stop_rule": "or_mid",
            "stop_label": "opening-range midpoint", "target": target, "target_r": 1.0,
            "risk_per_share": risk, "reward_risk": 1.0, "flat_by": cfg.get("flat_by", "15:55")}
    with open(os.path.join(HERE, d, "plan.json"), "w") as fh:
        json.dump(plan, fh, indent=2)
    tool("trader-timemachine/timemachine.py", "run", "--plan", d + "/plan.json", "--now", now_s,
         "--write", d + "/tm.json", offline=offline.get("tm", {}).get(sym) if offline else None)
    tm = read(os.path.join(HERE, d, "tm.json"))
    if not tm:
        return None
    # The pack sizes longs only: size the mirror-image long (same entry, same risk a share), then flip it.
    mirror = dict(plan, side="buy", stop=round(2 * entry - stop, 2), target=round(2 * entry - target, 2))
    with open(os.path.join(HERE, d, "mirror.json"), "w") as fh:
        json.dump(mirror, fh, indent=2)
    tool("trader-budget/size.py", "--plan", d + "/mirror.json", "--now", now_s, "--write", d + "/orders.json")
    orders = read(os.path.join(HERE, d, "orders.json"), {})
    res = {"verdict": "BLOCK", "reasons": ["sizing refused it"]}
    if orders.get("orders"):
        o = orders["orders"][0]
        o.update(side="sell", stop_loss_stop_price="%.2f" % stop, take_profit_limit_price="%.2f" % target,
                 why=o["why"].replace("opening-range breakout", "opening-range breakdown (short)"))
        o["plan"].update(stop=stop, target=target)
        with open(os.path.join(HERE, d, "orders.json"), "w") as fh:
            json.dump(orders, fh, indent=2)
        rc, out = tool("trader-risk/check.py", "--orders", d + "/orders.json", "--now", now_s,
                       "--out", d + "/risk.json", "--json")
        res = ((jout(out) or {}).get("results") or [res])[0]
    return {"symbol": sym, "side": "sell", "dir": d, "plan": plan, "mc": tm["mc"], "hist": tm["history"],
            "verdict": tm.get("verdict"), "risk": res, "score": score_of(tm)}


def score_of(tm):
    mc, hist = tm["mc"], tm["history"]
    trust = min(1.0, (hist.get("triggered") or 0) / float(HIST_FULL_TRUST))
    hist_r = hist.get("avg_R") or 0.0
    return round((MC_WEIGHT * mc["avg_R"] + HIST_WEIGHT * trust * hist_r) / (MC_WEIGHT + HIST_WEIGHT * trust), 3)


def wait_until(target, fake):
    if fake:
        return
    while True:
        left = (target - dt.datetime.now(NY)).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60))


def orb(a, offline=None):
    now = ny_now(a.now)
    if not a.now and not a.no_wait:
        wait_until(now.replace(hour=10, minute=15, second=5, microsecond=0), None)
        now = ny_now()
    now_s = now.strftime("%Y-%m-%d %H:%M")
    is_open, st = market_open(now_s)
    if not is_open:
        log("market closed:", st.get("reason"))
        return
    scan_args = ["orb", "--now", now_s, "--quiet", "--out", "trader/watchlist.json"]
    if offline:
        scan_args += ["--min-price", "0"]
    tool("trader-scanner/scan.py", *scan_args, offline=offline and offline["scan"], timeout=2400)
    short_args = [os.path.join(REPO, "trader-scanner/scan.py")] + scan_args[:-1] + ["trader/watchlist-short.json"]
    if offline:
        short_args += ["--offline", offline["scan"]]
    subprocess.run([sys.executable, "-I", os.path.join(HERE, "shortscan.py")] + short_args, cwd=HERE,
                   capture_output=True, text=True, timeout=2400)
    longs = read(os.path.join(WORK, "watchlist.json"), {}).get("hits", [])
    shorts = read(os.path.join(WORK, "watchlist-short.json"), {}).get("hits", [])
    if offline:
        longs = [h for h in longs if h["symbol"] in offline["tm"]]
        shorts = [h for h in shorts if h["symbol"] in offline["tm"]]
    # Cleanest moves first: through the range, but not stretched far past it.
    longs.sort(key=lambda h: abs((h.get("pct_over") or 0) - 0.5))
    shorts.sort(key=lambda h: abs((h.get("pct_under") or 0) - 0.5))
    hits = longs + shorts
    ranked = []
    jobs = [(h, "buy") for h in longs[:CANDIDATES]] + [(h, "sell") for h in shorts[:CANDIDATES]]
    for h, side in jobs:
        try:
            r = run_candidate(h["symbol"], now_s, offline) if side == "buy" else run_short(h, now_s, offline)
        except Exception as ex:  # noqa: BLE001
            log("candidate %s %s failed: %s" % (side, h["symbol"], ex))
            r = None
        if r:
            ranked.append(r)
    ranked.sort(key=lambda r: r["score"], reverse=True)
    passing = [r for r in ranked if r["risk"].get("verdict") == "PASS"]
    head = "%s ET / %s Paris" % (now.strftime("%a %d %b %H:%M"), paris(now))
    if getattr(a, "demo", False):
        head = "DEMO, old data · " + head
    if not ranked:
        send("<b>10:15 scan</b> · %s\n%d breakouts and %d breakdowns, none made it through the pipeline. "
             "No pick today." % (head, len(longs), len(shorts)), a.dry_run)
        return
    best = passing[0] if passing else None
    news = news_for([r["symbol"] for r in ranked[:4]],
                    now_iso=(now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if offline else None),
                    offline=offline and offline.get("news"))
    if best and best["score"] >= MIN_SCORE:
        send(pick_message(best, news.get(best["symbol"]), head, ranked, len(longs), len(shorts)), a.dry_run)
        watch_exits(best, a, offline)
    else:
        lines = ["<b>10:15 scan</b> · %s" % head,
                 "No pick today: no setup cleared the bar (expected edge ≥ %.2fR and risk PASS)." % MIN_SCORE, "",
                 "Closest:"]
        for r in ranked[:3]:
            lines.append("%s %s · score %+.2fR · %s · risk %s" % (SIDE_WORD[r["side"]], e(r["symbol"]), r["score"],
                                                                 e(r["verdict"]), r["risk"].get("verdict", "?")))
        lines += ["", "Sitting out is a valid trade."]
        send("\n".join(lines), a.dry_run)


SIDE_WORD = {"buy": "BUY", "sell": "SHORT"}


def pick_message(r, n, head, ranked, n_long, n_short):
    p, mc, h, rk = r["plan"], r["mc"], r["hist"], r["risk"]
    short = r["side"] == "sell"
    lines = [
        ("🔻 <b>TOP PICK: %s · SELL SHORT</b> (opening-range breakdown)" if short else
         "🔥 <b>TOP PICK: %s · BUY</b> (opening-range breakout)") % e(r["symbol"]),
        head, "",
        "Entry  <b>%.2f</b>  (%s close %s %.2f)" % (p["entry"], p["entry_time"], "below" if short else "above",
                                                  p["opening_range"]["low" if short else "high"]),
        "Stop   <b>%.2f</b>  (range midpoint, %.2f/share at risk)" % (p["stop"], p["risk_per_share"]),
        "Target <b>%.2f</b>  (1R) · flat by %s ET" % (p["target"], p["flat_by"]),
        "Size   %s%s · max loss $%.2f" % ("short " if short else "", rk.get("amount", "?"), rk.get("max_loss") or 0),
        "",
        "Time Machine: target first %.0f%% · stop first %.0f%% · neither %.0f%%"
        % (100 * mc["p_target"], 100 * mc["p_stop"], 100 * mc["p_neither"]),
        "History: fired %d times in %d sessions → %d target / %d stop / %d neither"
        % (h["triggered"], h["sessions"], h["target"], h["stop"], h["close"]),
        "Score %+.2fR expected per trade · %s" % (r["score"], e(r["verdict"])),
        "Risk check: <b>%s</b>" % rk.get("verdict", "?"),
        "News: " + news_line(n),
    ]
    if len(ranked) > 1:
        others = [x for x in ranked if x is not r][:4]
        lines += ["", "Runners-up: " + " · ".join("%s %s %+.2fR" % (SIDE_WORD[x["side"]], e(x["symbol"]), x["score"])
                                                  for x in others)]
    if short:
        lines += ["", "Shorting: you profit if it falls, and the loss has no ceiling if it gaps up past the stop. "
                  "Your broker must allow short selling, and the stock must be available to borrow."]
    lines += ["", "%d breakouts · %d breakdowns scanned. Alert only, nothing was placed. Check the price "
              "in your broker first." % (n_long, n_short)]
    return "\n".join(lines)


def watch_exits(r, a, offline):
    """Stop, target and flat-by-close alerts for the pick, until flat_by."""
    d, p = r["dir"], r["plan"]
    rs = ["--rules", d + "/alerts.json", "--state", d + "/alerts-state.json"]
    if r["side"] == "buy":
        tool("trader-alerts/alerts.py", "add", "--from-plan", "--plan", d + "/plan.json", *rs)
    else:  # the pack's plan alerts assume a long; a short's stop is above and its target below
        sym, day = r["symbol"], p["date"]
        tool("trader-alerts/alerts.py", "add", "--symbol", sym, "--above", p["stop"], "--note", "short stop",
             "--date", day, "--id", sym.lower() + "-short-stop", *rs)
        tool("trader-alerts/alerts.py", "add", "--symbol", sym, "--below", p["target"], "--note", "short target",
             "--date", day, "--id", sym.lower() + "-short-target", *rs)
        tool("trader-alerts/alerts.py", "add", "--symbol", sym, "--at", p["flat_by"], "--label",
             "flat by close: buy back the short", "--before-close", "--date", day, *rs)
    if a.no_watch or offline or os.environ.get("HEAT_NO_WATCH") == "1":
        return
    hh, mm = map(int, r["plan"]["flat_by"].split(":"))
    end = dt.datetime.now(NY).replace(hour=hh, minute=mm, second=0) + dt.timedelta(minutes=2)
    while dt.datetime.now(NY) < end:
        rc, out = tool("trader-alerts/alerts.py", "check", "--rules", d + "/alerts.json",
                       "--state", d + "/alerts-state.json", "--prices-out", d + "/prices.json", "--json")
        for ring in (jout(out) or {}).get("rings", []):
            send("🔔 <b>%s</b>\n%s" % (e(r["symbol"] or "Alert"), e(ring["message"])), a.dry_run)
        time.sleep(WATCH_EVERY)


# ---------- entry points ----------

def demo(a):
    fx = os.path.join(REPO, "tests", "fixtures")
    offline = {"scan": os.path.join(fx, "trader-scanner", "wall5m-2026-10-19.json.gz"),
               "entry": {"TSLA": os.path.join(fx, "trader-entry", "TSLA-2026-10-19-5m.json")},
               "tm": {"TSLA": os.path.join(fx, "trader-timemachine", "TSLA-60d5m.json.gz")},
               "news": os.path.join(fx, "trader-news")}
    global MIN_SCORE
    MIN_SCORE = -9.0            # show the pick format even though TSLA's edge was ~0
    a.now, a.demo = DEMO_NOW, True
    send("🧪 <b>Heat Bot test</b>: if you can read this, Telegram works. Next message replays the "
         "README's TSLA trade from 19 Oct 2026 (old data, not a signal).", a.dry_run)
    orb(a, offline)


def auto(a):
    t = ny_now(a.now)
    hm = t.hour * 60 + t.minute
    if 8 * 60 + 30 <= hm < 9 * 60 + 30:
        premarket(a)
    elif 9 * 60 + 55 <= hm < 11 * 60:
        orb(a)
    else:
        log("nothing scheduled at %s New York" % t.strftime("%H:%M"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["auto", "premarket", "orb", "demo"])
    ap.add_argument("--now", help='pretend it is this New York time, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--no-wait", action="store_true", help="orb: do not wait for 10:15 ET")
    ap.add_argument("--no-watch", action="store_true", help="orb: skip the exit alerts loop")
    a = ap.parse_args(argv)
    os.makedirs(WORK, exist_ok=True)
    if not os.path.isdir(REPO):
        sys.exit("day-trader-skill not found at %s (set TRADER_REPO)" % REPO)
    if a.mode in ("auto", "premarket", "orb"):
        is_open, st = market_open(ny_now(a.now).strftime("%Y-%m-%d %H:%M"))
        if st.get("reason", "").startswith(("NYSE holiday", "weekend")) or st.get("weekday") in ("Saturday", "Sunday"):
            log("no session today:", st.get("reason"))
            return
    {"auto": auto, "premarket": premarket, "orb": orb, "demo": demo}[a.mode](a)


if __name__ == "__main__":
    main()
