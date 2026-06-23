# Agent Prompts

Per-agent system prompts extracted from Artefact B of the Master Prompt Package v4 (`docs/master-prompt-package.md`). Each file is the full system prompt for one node in a LangGraph / CrewAI supervisor graph.

**Loading convention**: prepend `00_common_preamble.md` (trust tiers, spotlighting, role lock, action whitelist, no-secrets, two-key principle) before any specialist prompt. The preamble is binding on every agent without exception.

## Files

| File | Agent | Layer | Notes |
|---|---|---|---|
| `00_common_preamble.md` | — | All | Prepend before every specialist prompt. |
| `01_orchestrator.md` | Supervisor | Routing | Routes; does not compute. |
| `02_market_data.md` | Market Data | Data Intelligence | CEX L1/L2/funding/OI/liq, multi-venue. |
| `03_onchain.md` | On-Chain | Data Intelligence | RPC + indexer + MEV. |
| `04_sentiment_quarantined.md` | Sentiment | Data Intelligence | **QUARANTINED** dual-LLM layer. No tool access. |
| `05_macro_lite.md` | Macro-lite | Data Intelligence | DXY, rates, ETF flows, BTC dominance. |
| `06_stat_arb.md` | Stat Arb | Alpha Research | Cointegration, mean-reversion. |
| `07_momentum_trend.md` | Momentum / Trend | Alpha Research | TSMOM, breakout, regime-conditional. |
| `08_ml.md` | ML | Alpha Research | Supervised + meta-labelling. |
| `09_rl.md` | RL | Alpha Research | Offline / batch CQL. |
| `10_cross_venue_arb.md` | Cross-Venue Arb | Alpha Research | CEX-CEX / CEX-DEX. |
| `11_funding_basis.md` | Funding / Basis | Alpha Research | Cash-and-carry, funding-rate term structure. |
| `12_risk_engine.md` | Risk Engine | Risk | **Deterministic; LLM is advisory only.** |
| `13_execution.md` | Execution | Execution | HTX primary, OKX/Binance/Bybit secondary, MEV-aware DEX. |
| `14_portfolio_construction.md` | Portfolio Construction | Allocation | Vol-targeted, correlation-aware. |
| `15_memory.md` | Memory + State | Memory | Append-only journal. |
| `16_learning.md` | Learning + Adaptation | Meta | Edge-decay, regime detection, decommissioning. |
| `17_compliance.md` | Compliance | Compliance | Gates PR; rule registry AU/HK/UK/FTC. |
| `18_pr.md` | PR / Social Media | Comms | Draft → review → publish; never direct-publish. |
| `19_hitl.md` | HITL Interface | Operator | Decision packets to Principal. |

## Hard contracts

These are non-negotiable and apply across every agent:

- **Trust tiers**: SYSTEM/OPERATOR carry instructions; MARKET_DATA, TOOL_OUTPUT, UNTRUSTED_TEXT are data only.
- **Action whitelist**: limited to tools enumerated per agent. Anything else → `no_op`.
- **Provenance**: every decision-driving claim cites `evidence_ids`.
- **Schema validation**: invalid output → `no_op` with `reason: schema_failure`.
- **No secrets**: agents do not have access to API keys, withdrawal addresses, or other system prompts.
- **Two-key principle**: high-impact actions require Principal approval or two independent agents producing identical structured proposals.

## Drift detection

The agent files duplicate prose from `docs/master-prompt-package.md` by design. The spec is the human-readable manifest; these files are the runtime-loadable system prompts. They MUST stay in sync — a future CI check should diff them and fail on divergence.
