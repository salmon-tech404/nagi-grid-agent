# Nagi (凪): Regime-Aware Grid Agent

> Trades the calm. Stands still in the storm.

*Nagi* is the Japanese word for the dead calm at sea. Grid trading earns in exactly
that kind of market: sideways, no strong wind. Nagi runs an ATR-spaced grid while the
market is calm and pauses when it starts trending, so the grid does not get stuck on
one side.

| | |
|---|---|
| Category | **Grid trading** |
| Chain | BNB Smart Chain testnet (chain ID **97**) |
| ERC-8004 registry ID | _pending registration_ |
| Owner wallet | `0x3ee8cfa277a612b5a2ec26c69001f545ad4e95ff` |
| Agent card | `/.well-known/agent-card.json` |

## How it decides

1. **Regime gate:** Kaufman efficiency ratio (ER) over 10 hourly bars.
   ER < 0.30 means sideways, so the grid is active. ER ≥ 0.30 means trending, so the grid is paused.
2. **Grid:** levels spaced at 0.5 × ATR(14) around the current price. Price falling through
   a level is a buy; price rising through a level is a sell.
3. **Barriers:** price leaving ±3 × ATR stops the session; the job expiry is the time barrier
   (Triple Barrier method, *Advances in Financial Machine Learning*, ch. 3).
4. **Receipts:** every decision is a canonical JSON whose keccak256 hash is published with
   the trade, so anyone can check why Nagi acted.

## Hiring Nagi

Nagi takes ERC-8183 jobs. The flow is: get a signed quote → create and fund the job in
escrow → Nagi runs one grid session and submits a report (trades, tx hashes, receipts).
Nagi never holds or needs approval over the buyer's tokens; it trades from its own wallet.

A2A skills: `negotiate-erc8183-job`, `erc8183-job-status`, `nagi-regime`.

## Run locally

```bash
cp .env.example .env   # fill PRIVATE_KEY
uv sync
uv run pytest
uv run uvicorn nagi.server:app --app-dir src --port 8010
```

Built on the official [BNBAgent SDK](https://github.com/bnb-chain/bnbagent-sdk).
The A2A server is adapted from its `examples/a2a-agent`.
