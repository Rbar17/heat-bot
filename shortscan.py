#!/usr/bin/env python3
"""Opening-range BREAKDOWN scan: the mirror of the pack's breakout scan, for short-sell signals.

It loads the pack's own trader-scanner/scan.py (same data, same filters, same two-stage check)
and swaps in two rules: a candidate is a chart that closed at or below the range's lowest close,
and a hit is the first completed 5-minute bar to CLOSE below the opening-range low.

    python3 shortscan.py <path to scan.py> orb --now "..." --out trader/watchlist-short.json [scan.py flags]

Hits carry "side": "sell" and "pct_under" (how far under the range low the bar closed, percent).
"""
import datetime
import importlib.util
import sys


def load_scan(path):
    spec = importlib.util.spec_from_file_location("trader_scan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch(scan):
    def orb_candidate(bars, n_range):
        low = min(b["c"] for b in bars[:n_range])
        return any(b["c"] <= low for b in bars[n_range:])

    def orb_hit(bars, n_range):
        rng = bars[:n_range]
        if any(b["h"] is None or b["l"] is None for b in rng):
            return None
        oh = max(b["h"] for b in rng)
        ol = min(b["l"] for b in rng)
        for k in range(n_range, len(bars)):
            b = bars[k]
            if b["c"] < ol:
                closes_at = b["start"] + datetime.timedelta(minutes=scan.BAR_MIN)
                return {"side": "sell", "price": round(b["c"], 2), "at": scan.clock_label(closes_at),
                        "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                        "or_high": round(oh, 2), "or_low": round(ol, 2),
                        "pct_under": round((1 - b["c"] / ol) * 100, 2),
                        "pct_over": -round((1 - b["c"] / ol) * 100, 2), "_t": closes_at}
        return None

    scan.orb_candidate = orb_candidate
    scan.orb_hit = orb_hit
    scan.NOTE = "A breakdown scan is a list of charts that broke below their opening range, not a list to short."
    return scan


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sys.exit(patch(load_scan(sys.argv[1])).main(sys.argv[2:]))
