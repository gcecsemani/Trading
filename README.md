# NIFTY option selling

One intraday structure on NIFTY. The side comes from open interest only.

- **Bullish** — buy the nearest OTM put whose ask is under ₹10, then sell the put.
- **Bearish** — buy the nearest OTM call whose ask is under ₹10, then sell the call.

On a normal day the sold strike is ATM. On expiry day (0 DTE) ATM is sold only when its bid is greater than ₹100. If it is not, the bot walks ITM1, ITM2, then ITM3 and sells the first bid greater than ₹100. A put's in-the-money strikes are above ATM. A call's are below ATM. If none of those bids is above ₹100, nothing is sold that minute.

Support is the strongest put-OI buildup at or below spot inside the tracked band. Resistance is the strongest call-OI buildup at or above spot. Both are in the five-minute note.

Dominance compares the ATM put-OI change with the ATM call-OI change from the 09:26 print. More than 30% put writing is bullish, and the strike one step below must agree at more than 15%. More than 30% call writing is bearish, and the strike one step above must agree at more than 15%. A ratio sitting on the line does not vote. When spot moves to a new at-the-money strike, that strike keeps the open interest it had at 09:26 if it was already inside the band.

Both checks are required to open, and the same pair is required to close. A fast or slow minute does not change that. After a signal exit the next spread waits two minutes, then is taken only if the signal at that minute still qualifies. A stop or a target does not start that wait.

The hedge is bought first. Its premium has no stop. The sold option is stopped if its premium rises 50 points. The spread is closed when that premium trades at 10 or below, when both checks flip, or at the square-off.

It stays in **paper** until both live switches are on. Paper fills walk the live bid/ask book.

## Run

```bash
pip3 install -r requirements.txt
python3 login_zerodha.py
python3 main.py --check
python3 main.py
```

`--dry-run` prints one minute of OI and places nothing. `--mode live` does not send real orders unless `LIVE_TRADING_CONFIRMED=true` is also set in `.env`.

## Clock (IST)

| Time | What happens |
|---|---|
| 09:15–09:25 | Market is open. Open interest from these minutes is not used. |
| 09:26 | First print. This is the baseline. Nothing is traded. |
| 09:27 | First change, against the 09:26 print. |
| every minute | A new print is stored for ATM ±7. Dominance, support and resistance are recomputed from 09:26. |
| every 5 min | Telegram gets that read, support, resistance, and the open trade. The first note is 09:31. |
| 09:26–09:40 | Fifteen minutes of analysis. No trade. |
| 09:41–15:00 | If nothing is open, ATM dominance is past 30%, and the strike one step away is past 15%, the spread is sold. |
| any minute after that | If both checks have crossed the other way, the spread is closed. The next one waits 2 minutes, then follows the signal. |
| 15:00 | Last minute that may open a spread. |
| 15:15 | Whatever is open is squared off, then the day summary is sent. |

On expiry day the contract expiring today is traded. A restart loads today's saved prints, fills missing minutes and missing strikes from exchange candles back to 09:26, and recomputes day dominance before anything new is sold. If the 09:26 print cannot be rebuilt, new sells stay off. An open spread is still stopped and targeted. A failed square-off is kept and retried. A dead feed flattens. An expired token stops the bot. A hedge or a short that cannot be filled rolls the entry back. A flip leaves the book flat until the cooldown has passed.

A ratio that fades back under 30%, without crossing the other way, holds the open spread. The stop and the target still apply.

## Knobs

Everything that changes behaviour is in `config.py`.

| Setting | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | `live` also needs `LIVE_TRADING_CONFIRMED` |
| `TRADE["lots"]` | `1` | or `LOTS=2` in the environment |
| `SYMBOLS["NIFTY"]["chain_strikes"]` | `7` | ATM and seven strikes either side. Dominance stays on ATM |
| `OI["min_ratio"]` | `0.30` | ATM dominance must be more than this to open or to close |
| `OI["neighbor_ratio"]` | `0.15` | the strike one step away must be more than this, same direction |
| `TRADE["zero_dte_min_premium"]` | `100` | on 0 DTE the sold bid must be greater than this |
| `TRADE["sl_points"]` | `50` | added to the sold premium |
| `TRADE["target_premium"]` | `10` | close when the sold premium is at or under this |
| `TRADE["hedge_max_premium"]` | `10` | wing ask must be strictly below this |
| `SESSION["analysis_start"]` | `09:26` | first print. 09:15–09:25 is not read |
| `SESSION["first_trade"]` | `09:41` | no entry before this. 09:26–09:40 is the 15-minute analysis |
| `SESSION["last_entry"]` | `15:00` | last minute that may open |
| `SESSION["hard_square_off"]` | `15:15` | flatten, then the day summary |
| `ENGINE["report_minutes"]` | `5` | how often the note is posted |
| `ENGINE["flip_cooldown_min"]` | `2` | wait after a signal exit, then read the signal again |

## Telegram

Every message is tagged `[PAPER]` or `[LIVE]`.

- The 5-minute note is the OI dominance from 09:26, the nearby strike, support and resistance, and the open trade.
- A signal exit, an entry, and a stop or target are posted when they happen. The exit does not open the other side in the same minute.
- The day summary is sent at the 15:15 square-off.

Closed trades are appended to `state/trades.csv`.
