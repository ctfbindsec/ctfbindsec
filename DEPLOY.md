# DEPLOY — tier −1 micro experiment

Same-day operator runbook for the $200 NAV HTX funding-capture experiment. Read top-to-bottom before doing anything that touches real money.

## What this is

A tightly-scoped slice of the v4 master prompt package (`docs/master-prompt-package.md`):

- **One venue** (HTX USDT-M perpetuals)
- **One strategy** (funding-rate capture, Tier −1 parameters)
- **$200 trading capital**, willing to lose
- **Mandatory HITL approval** for every order in week 1 (Telegram bot)
- **Deterministic Risk Engine + kill-switches** running on every tick
- **No PR pipeline, no Compliance gating, no sentiment, no DEX legs, no leverage, no withdraw-permission API keys**

The goal is to exercise the system end-to-end with real microstructure. The PnL number is decoration; the kill-switch logs and HITL latency are the actual deliverable.

## What this is NOT

- Not advice. Not a regulated product. Not safe at scale without operational defences not present in v0.
- Not a strategy validator — sample size at $200 NAV is too small to claim edge.
- Not a substitute for legal/regulatory review if you ever decide to scale up.

## Pre-flight (operator does, not the system)

### 1. HTX account setup
1. Sign up / log in at htx.com.
2. Complete KYC sufficient to deposit USDT.
3. **Deposit $200 USDT** to your USDT-M perpetual account (not spot, not coin-M).
4. Enable USDT-margined perpetual contracts on the account.

### 2. HTX API key
1. Visit https://www.htx.com/en-us/apiManagement (or your locale equivalent).
2. Create a new API key with permissions:
   - **Read: ON**
   - **Trade: ON**
   - **Withdraw: OFF** (this is non-negotiable; do not enable)
3. Find your machine's public IP: `curl https://api.ipify.org`.
4. **IP-whitelist that exact IP** on the API key. No "any IP" allowed.
5. Copy the access key + secret to `.env` (see step 5).

### 3. Telegram bot
1. Message `@BotFather` on Telegram, run `/newbot`, follow prompts. Save the bot token.
2. Start a chat with your new bot, send any message.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`, find `"chat":{"id":NNNNN,...}`. Save that chat id.

### 4. Local environment
```bash
git clone <this repo>
cd ctfbindsec
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -q     # MUST pass before any live run
```

### 5. Configuration
```bash
cp .env.example .env
# fill in: HTX_ACCESS_KEY, HTX_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

`.env` is gitignored. Never commit it. Never paste it anywhere.

## Operating the system

### First run — dry mode
```bash
CTFQUANT_DRY_RUN=true ctfquant
```

Dry mode connects to HTX public WS, runs the Risk Engine, generates proposed orders, sends them to Telegram, but **does not place real orders even on approval**. Use this until you have seen at least one full Telegram approval cycle and one synthetic kill-switch trip.

### Going live
```bash
CTFQUANT_DRY_RUN=false ctfquant
```

Real orders fire on `/approve <id>` from the Telegram bot. Every order is gated:

```
proposed_order
   │
   ▼
schema validation (pydantic)
   │
   ▼
Risk Engine deterministic gate (16-point check)
   │
   ├─ rejected → drop, log, notify Telegram
   └─ approved or size_cut → HITL queue
                             │
                             ▼
                       Telegram /approve <id>
                             │
                             ▼
                       venue_htx.place_order()
                             │
                             ▼
                       fill simulator (dry) OR real venue
```

### Telegram commands

| Command | Effect |
|---|---|
| `/status` | NAV, open positions, pending HITL queue, kill-switch state |
| `/positions` | Detailed view of open positions and accrued funding |
| `/approve <id>` | Approve a pending order. Order goes to venue immediately. |
| `/reject <id>` | Reject a pending order. Logged. |
| `/halt` | **Manual kill-switch.** Flatten everything, halt new orders. Requires `/resume` to reactivate. |
| `/resume` | Resume after a kill-switch trip. **Operator only.** Logs the resume reason. |
| `/postmortem <kill_id>` | Print the post-mortem packet for a tripped kill switch. |

### Daily ritual

1. Morning: `/status` check. Reconcile against HTX UI. Note the NAV high-water marks.
2. After each Telegram approval prompt: read the binding constraints. If the Risk Engine cut size or rejected, understand why before approving the next one.
3. End of day: confirm SQLite journal at `var/state.db` got committed (file size > 0, recent timestamp).
4. Weekly: write a one-paragraph post-mortem for the week. Even on green weeks. **Especially** on green weeks.

## Kill-switch trip — operator response

If a kill switch trips, the system emits one Telegram message and stops accepting new orders:

```
🚨 KILL SWITCH TRIPPED
reason: peak_to_trough_dd:50.50%>=50.00%
ts: 2026-05-07T12:34:56Z
state: READ-ONLY, flatten-only
post-mortem: /postmortem 7
```

Within 1 hour:
1. Run `/postmortem <id>` and read the output.
2. Identify root cause. Was it expected (a real loss)? Or a system bug (false trip)?
3. If real: walk away. **Do not /resume** unless you have decided in writing to continue with a (now smaller) NAV.
4. If bug: file in `docs/post-mortems/` with timestamp, fix, and `/resume`.

The default-correct action after a kill-switch trip is **stop trading for the day**. The temptation to immediately re-enable is strong; resist it.

## What the system can and cannot do without operator action

**Can do automatically:**
- Read HTX market data (no key needed).
- Run the Risk Engine deterministic gate.
- Trip kill-switches.
- Send Telegram messages.
- Append to SQLite journal.

**Cannot do without explicit operator action:**
- Place real orders on HTX (requires API key + Telegram approval).
- Withdraw funds (API key has withdraw OFF; withdrawal is operator-only via HTX UI).
- Modify risk parameters (compile-time constants).
- Reset a tripped kill switch (`/resume` is HITL-only).

## Cost expectations

- **HTX fees**: maker 0.02%, taker 0.05% on USDT-M. Round-trip on a $20 position ≈ $0.02–$0.04 per trade.
- **Funding**: at +0.5%/8h funding on a $20 short, you accrue $0.10 per funding interval ≈ $0.30/day if held continuously.
- **Slippage**: 5–20bps typical on liquid alts; higher on thin alts. Already inside `max_slippage_bps`.
- **Net expected**: ~$0–$2 per week of paper-realised PnL across a few captures. Maybe negative if funding flips. The point is the system, not the PnL.

## When to stop the experiment

Stop and do not resume if:
- NAV reaches $100 (peak-to-trough kill, hard halt).
- Two unresolved system bugs cause unintended trades.
- HTX adds withdrawal restrictions, halts trading on the universe, or has a public security incident.
- You are not enjoying it. Live trading at this scale is mostly waiting; if it's stressful, it's not the right experiment.

## Out of scope for v0; deferred to v1

- Sentiment ingestion (UNTRUSTED_TEXT pipeline).
- PR pipeline + Compliance Agent rule registry.
- Multi-venue / hot-failover.
- DEX legs with private-mempool routing.
- Cold custody / sweep cron.
- ML / RL agents.
- Portfolio Construction with multiple strategies.
- Learning loop / weekly digest.
- Backtester for the funding strategy (we observe live).

If v0 runs cleanly for 14 days without kill-switch trips you can't explain, the conversation about v1 is worth having. Not before.
