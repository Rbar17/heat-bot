# Heat Bot

Runs the Day Trader skill pack on GitHub Actions every US trading day and sends you two Telegram alerts:

| When (Paris) | What |
|---|---|
| ~15:05 | Pre-market watch list: the six most-traded daily setups (breakouts, flags, oversold bounces) with news |
| 16:15 | The 10:15 ET opening-range scan, both ways: **breakouts** (close above the 15-minute range → BUY) and **breakdowns** (close below it → SELL SHORT). Up to 12 of each go through plan → Time Machine → budget → risk. The best one, long or short, is sent as the **top pick**. Then you get stop / target / flat-by-close alerts until 21:55 Paris |

**Alert only.** Nothing here can place an order. If no setup shows a positive edge, it says "No pick today" instead of forcing one.

## Setup (about 10 minutes)

1. **Make a Telegram bot.** In Telegram, message `@BotFather` → `/newbot` → pick a name. Copy the token it gives you.
2. **Get your chat ID.** Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. The number after `"chat":{"id":` is your chat ID.
3. **Make a GitHub repo** (public is best: Actions minutes are free and unlimited; nothing secret is stored in the code). Upload everything in this folder, including the `.github` folder.
4. **Add secrets.** In the repo, go to Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN`: the token from step 1
   - `TELEGRAM_CHAT_ID`: the number from step 2
   - `SEC_USER_AGENT` (optional): `Roy Lastname you@example.com`. This adds SEC 8-K filings to the news.
5. **Test it.** Actions tab → Heat Bot → Run workflow → mode `demo`. You should get two Telegram messages within a minute or two.

After that it runs by itself, Monday to Friday, and skips US holidays.

## Shorts

A short pick works like this: sell at the entry, buy back at the target (lower) or the stop (higher), and be flat by 15:55 ET. The stop is the opening-range midpoint and the target is 1R, the exact mirror of the long rule. The Time Machine replays it as a sell, including every past session where the same breakdown fired. Sizing uses the same $50 max-loss rule.

`trader/config.json` has `"allow_short": true` so the risk check will pass shorts. Set it to `false` to go back to buys only. Real shorting needs a margin account and shares available to borrow, and a gap up can fill well past the stop.

## Settings

- **Budget and risk limits:** `trader/config.json`. Default: $5,000 budget, $50 max loss per trade, $150 daily loss limit, paper.
- **Private repo?** The exit-alert loop keeps a job running for about 6 hours a day, which uses up the free 2,000 private minutes in about a week. Either keep the repo public, or add a repository *variable* `HEAT_NO_WATCH` = `1` (Settings → Secrets and variables → Actions → Variables). That skips stop/target alerts, and each day costs about 30 minutes.
- **Scoring:** a pick's score is the expected R per trade, a blend of the Time Machine's 10,000 replays (60 %) and the real past sessions where the same rule fired (40 %). A pick needs a score of at least +0.05R and a risk PASS. You can change this in `heat.py` (`MIN_SCORE`, `CANDIDATES`).

## Limits

- GitHub's scheduler can start runs 5–30 minutes late. The breakout run starts at 10:05 ET and waits for 10:15, which usually absorbs the delay.
- Data is Yahoo Finance's free, unofficial endpoint, which may rate-limit GitHub's servers. If alerts stop arriving, check the Actions log.
- Prices are delayed bars, not live ticks. Always check the price in your broker before acting.
- Not financial advice. Most day traders lose money. Stay on paper until weeks of alerts show a real edge.
