"""Nagi A2A surface: agent card, signed ERC-8183 quotes, job status, live regime.

Adapted from bnbagent-sdk python/examples/a2a-agent/src/server.py (copy-and-own).

With NAGI_RUN_LOOPS=1 (set only on the deployed instance, never locally at the
same time: both would trade from the same wallet) the server also runs:
  - the grid loop       every GRID_INTERVAL seconds
  - the funded-job watcher (opens a grid session per hire)
  - the job desk        submits session reports and settles after the dispute window

Run:
    uv run uvicorn nagi.server:app --app-dir src --port 8010
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from bnbagent import EVMWalletProvider  # noqa: E402
from bnbagent.erc8183 import ERC8183Client, ERC8183JobOps, NegotiationHandler, funded_job_watcher  # noqa: E402
from bnbagent.storage import IPFSStorageProvider  # noqa: E402
from bnbagent.utils import RateLimitExceeded, SlidingWindowLimiter  # noqa: E402

from nagi import regime  # noqa: E402
from nagi.engine import GridEngine  # noqa: E402
from nagi.executor import Executor  # noqa: E402
from nagi.jobs import HireBook, JobDesk  # noqa: E402
from nagi.market import fetch_klines  # noqa: E402
from nagi.perf import grid_pnl  # noqa: E402
from nagi.history import LOGS_RPC, ChainHistory  # noqa: E402
from bnbagent.config import resolve_network  # noqa: E402
import dataclasses  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("nagi")

NETWORK = os.getenv("NETWORK", "bsc-testnet")
AGENT_NAME = os.getenv("AGENT_NAME", "Nagi")
AGENT_DESCRIPTION = os.getenv(
    "AGENT_DESCRIPTION",
    "Regime-aware grid trading agent on BNB Chain. Runs an ATR-spaced grid while the market "
    "is sideways and stands still when the Kaufman efficiency ratio signals a trend. "
    "Every decision is published with an onchain receipt hash.",
)
CATEGORY = "grid-trading"
# Render injects RENDER_EXTERNAL_URL; A2A_BASE_URL overrides it (e.g. a custom domain).
BASE_URL = (
    os.getenv("A2A_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8010"
).rstrip("/")
SERVICE_PRICE = os.getenv("ERC8183_SERVICE_PRICE", "100000000000000000")  # 0.1 U
MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "BNBUSDT")
AGENT_ID = os.getenv("AGENT_ID", "2623")
DASHBOARD = Path(__file__).resolve().parent / "static" / "index.html"
LOGO = Path(__file__).resolve().parent / "static" / "logo.png"
RUN_LOOPS = os.getenv("NAGI_RUN_LOOPS") == "1"
GRID_INTERVAL = int(os.getenv("GRID_INTERVAL", "300"))
SESSION_SECONDS = int(os.getenv("NAGI_SESSION_MINUTES", "60")) * 60

_private_key = os.getenv("PRIVATE_KEY")
_wallet_password = os.getenv("WALLET_PASSWORD")
if not _private_key or not _wallet_password:
    raise SystemExit("PRIVATE_KEY and WALLET_PASSWORD are required (see .env.example)")
wallet = EVMWalletProvider(password=_wallet_password, private_key=_private_key, persist=False)

client = ERC8183Client(wallet_provider=wallet, network=NETWORK)

# Bind the quote signature to this chain + commerce contract (anti-replay).
negotiation_handler = NegotiationHandler(
    service_price=SERVICE_PRICE,
    currency=client.payment_token,
    wallet_provider=wallet,
    chain_id=client.network.chain_id,
    verifying_contract=client.commerce.address,
)

executor = Executor(wallet)
engine = GridEngine(executor)
chain_lock = asyncio.Lock()  # one onchain writer at a time, so nonces never collide
POKTER_URL = os.getenv("POKTER_URL", f"https://pokter.xyz/agents/97/{AGENT_ID}")
DEADLINE = 1793880000  # Set and Earn closes 2026-11-05 12:00 UTC
LOW_GAS_BNB = 0.06
# Read-only client on a node that serves eth_getLogs (the default testnet RPC refuses them).
reader = ERC8183Client(None, dataclasses.replace(resolve_network(NETWORK), rpc_url=LOGS_RPC))
hirebook = HireBook(client.commerce, wallet.address, reader=reader)  # read-only, safe to run anywhere
history = ChainHistory(wallet.address, client.commerce.address, client.payment_token)
desk: JobDesk | None = None
STARTED_AT = int(time.time())
_wallet_cache: dict[str, Any] = {"ts": 0, "bnb": None, "busd": None}

negotiate_limiter = SlidingWindowLimiter(max_requests=120, window_seconds=60.0, max_keys=10_000)
global_negotiate_limiter = SlidingWindowLimiter(max_requests=1_200, window_seconds=60.0, max_keys=1)

AGENT_CARD: dict[str, Any] = {
    "protocolVersion": "0.3.0",
    "name": AGENT_NAME,
    "description": AGENT_DESCRIPTION,
    "category": CATEGORY,
    "url": f"{BASE_URL}/a2a",
    "iconUrl": f"{BASE_URL}/logo.png",
    "documentationUrl": "https://github.com/salmon-tech404/nagi-grid-agent",
    "preferredTransport": "JSONRPC",
    "version": "0.1.0",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [
        {
            "id": "negotiate-erc8183-job",
            "name": "Hire Nagi for a grid session",
            "description": (
                'Send {"skill": "negotiate-erc8183-job", "task_description": "...", "terms": {...}} '
                "and receive a wallet-signed quote. Anchor it onchain via createJob and fund it; "
                "Nagi then runs one grid session on BSC testnet and delivers a report with every "
                "trade's tx hash and decision receipt."
            ),
            "tags": ["grid-trading", "grid", "erc8183", "bnb-chain"],
            "examples": [
                "Run one grid session on BNB testnet (WBNB/BUSD) and report every trade with its tx hash "
                "and decision receipt. Trade only from Nagi's own wallet.",
                "Read-only: give me the current BNB regime and the grid levels you would trade, "
                "with no transactions.",
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "erc8183-job-status",
            "name": "Job status",
            "description": 'Send {"skill": "erc8183-job-status", "job_id": <int>} for an onchain job lookup.',
            "tags": ["erc8183", "status"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "nagi-regime",
            "name": "Current market regime",
            "description": (
                'Send {"skill": "nagi-regime"} to see whether Nagi considers the market calm '
                "(grid active) or stormy (grid paused), with the efficiency ratio and ATR behind it."
            ),
            "tags": ["grid-trading", "regime", "read-only"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
    ],
}

async def _grid_loop() -> None:
    while True:
        try:
            df = await fetch_klines(MARKET_SYMBOL)
            async with chain_lock:
                events = await asyncio.to_thread(engine.tick, df)
            for e in events:
                logger.info("grid %s %s", e["kind"], e.get("tx") or e.get("reason") or e.get("note", ""))
        except Exception as exc:
            logger.error("grid tick failed: %s", exc)
        await asyncio.sleep(GRID_INTERVAL)


async def _desk_loop(desk: JobDesk) -> None:
    tick = 0
    while True:
        # settle_due rescans every job onchain, so run it every 5 minutes (dispute window is 15).
        steps = (desk.finish_due, desk.settle_due) if tick % 5 == 0 else (desk.finish_due,)
        for step in steps:
            try:
                await step()
            except Exception as exc:
                logger.error("job desk %s failed: %s", step.__name__, exc)
        tick += 1
        await asyncio.sleep(60)


async def _hires_loop() -> None:
    while True:
        for name, fn in (("hire book", hirebook.refresh), ("chain history", history.refresh)):
            try:
                await asyncio.to_thread(fn)
            except Exception as exc:
                logger.error("%s refresh failed: %s", name, exc)
        await asyncio.sleep(60)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global desk
    tasks: list[asyncio.Task] = [asyncio.create_task(_hires_loop())]
    if RUN_LOOPS:
        storage = IPFSStorageProvider.from_env()  # Render's disk is wiped on restart
        ops = ERC8183JobOps(
            wallet, NETWORK, storage_provider=storage, service_price=int(SERVICE_PRICE), agent_url=f"{BASE_URL}/erc8183",
        )
        desk = JobDesk(ops, client, engine, chain_lock, session_seconds=SESSION_SECONDS, market_symbol=MARKET_SYMBOL,
                       storage=storage, provider=wallet.address, service_price=int(SERVICE_PRICE))
        tasks += [
            asyncio.create_task(_grid_loop()),
            asyncio.create_task(funded_job_watcher(ops, desk.on_funded, interval=30)),
            asyncio.create_task(_desk_loop(desk)),
        ]
        logger.info("loops running: grid every %ss, job watcher, job desk", GRID_INTERVAL)
    else:
        logger.info("loops disabled (set NAGI_RUN_LOOPS=1 on the deployed instance only)")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title=AGENT_NAME, lifespan=lifespan)


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return AGENT_CARD


@app.api_route("/", methods=["GET", "HEAD"])
async def dashboard():
    return FileResponse(DASHBOARD, media_type="text/html")


@app.api_route("/logo.png", methods=["GET", "HEAD"])
async def logo():
    return FileResponse(LOGO, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api")
async def api_index():
    return {
        "agent": AGENT_NAME,
        "agent_id": AGENT_ID,
        "owner": wallet.address,
        "hires": f"{BASE_URL}/hires",
        "category": CATEGORY,
        "agent_card": f"{BASE_URL}/.well-known/agent-card.json",
        "a2a": f"{BASE_URL}/a2a",
        "health": f"{BASE_URL}/health",
        "activity": f"{BASE_URL}/activity",
    }


@app.get("/hires")
async def hires():
    """ERC-8183 jobs where Nagi is the provider, straight from chain, newest first."""
    s = hirebook.summary()
    if desk is not None:
        for j in s["jobs"]:
            session = desk.sessions.get(j["job_id"])
            if session:
                j["session_end"] = session["end"]
    return {"agent_id": AGENT_ID, "scanned_jobs": hirebook.scanned, **s}


async def _wallet() -> dict[str, Any]:
    if time.time() - _wallet_cache["ts"] > 60:
        try:
            bal = await asyncio.to_thread(executor.balances)
            u = await asyncio.to_thread(client.token_balance)
            _wallet_cache.update(ts=time.time(), bnb=bal["bnb"] / 1e18, busd=bal["busd"] / 1e18, u=u / 1e18)
        except Exception as exc:
            logger.warning("wallet balance read failed: %s", exc)
    return {"tbnb": _wallet_cache.get("bnb"), "busd": _wallet_cache.get("busd"), "u": _wallet_cache.get("u")}


@app.get("/overview")
async def overview():
    """Everything the dashboard shows, in one call. Trades, earnings and hires come from chain."""
    w = await _wallet()
    r = engine.last_regime
    trades = history.trade_list()
    price_now = r.price if r else next((t["price_ref"] for t in reversed(trades) if t.get("price_ref")), None)
    hires = hirebook.summary()
    if desk is not None:
        for j in hires["jobs"]:
            session = desk.sessions.get(j["job_id"])
            if session:
                j["session_end"] = session["end"]
    delivered = [j for j in hires["jobs"] if j["submitted_at"]]
    action_days = set(history.active_days()) | {
        time.strftime("%Y-%m-%d", time.gmtime(j["submitted_at"])) for j in delivered}
    decisions = [e for e in reversed(engine.log) if e["kind"] != "trade"][:20]
    return {
        "agent": {"name": AGENT_NAME, "id": AGENT_ID, "owner": wallet.address, "category": CATEGORY,
                  "listing": POKTER_URL, "card": f"{BASE_URL}/.well-known/agent-card.json"},
        "status": {
            "automation": RUN_LOOPS, "last_check": engine.last_tick, "interval": GRID_INTERVAL,
            "market": r.state if r else None, "er": round(r.er, 4) if r else None, "atr": r.atr if r else None,
            "price": price_now, "gas_low": w["tbnb"] is not None and w["tbnb"] < LOW_GAS_BNB,
            "history_block": history.next_block - 1, "started_at": STARTED_AT,
        },
        "wallet": w,
        "grid": None if engine.plan is None else {"center": engine.plan.center, "step": engine.plan.step,
                                                  "levels": [round(lv, 2) for lv in engine.plan.levels]},
        "hires": hires,
        "earnings": {"earned_u": history.earned_u(), "pending_u": hires["pending_u"],
                     "payouts": sorted(history.earnings.values(), key=lambda e: e["ts"], reverse=True)},
        "progress": {
            "hires": {"value": hires["completed_clients"], "target": 3},
            "actions": {"value": len(trades) + len(delivered), "target": 5},
            "days": {"value": len(action_days), "target": 3},
            "deadline": DEADLINE,
        },
        "performance": grid_pnl(trades, price_now),
        "trades": list(reversed(trades)),
        "decisions": decisions,
    }


@app.get("/performance")
async def performance():
    """Paper P&L vs holding BNB (real prices, since this process started) + live testnet wallet balances."""
    events = list(engine.log)
    price_now = next((e["price_ref"] for e in reversed(events) if e.get("price_ref")), None)
    if time.time() - _wallet_cache["ts"] > 60:
        try:
            bal = await asyncio.to_thread(executor.balances)
            _wallet_cache.update(ts=time.time(), bnb=bal["bnb"] / 1e18, busd=bal["busd"] / 1e18)
        except Exception as exc:
            logger.warning("wallet balance read failed: %s", exc)
    return {
        "since": STARTED_AT, "price_now": price_now, **grid_pnl(events, price_now),
        "wallet": {"tbnb": _wallet_cache["bnb"], "busd": _wallet_cache["busd"]},
        "note": "Paper P&L at real BNB prices vs simply holding BNB; fills happen on BSC testnet. "
                "Resets when the server restarts. Wallet balances are live from chain.",
    }


@app.get("/activity")
async def activity(limit: int = 50):
    """Latest decisions, newest first. Trades link to BscScan; receipts are in the tx input."""
    events = [
        {**e, "bscscan": f"https://testnet.bscscan.com/tx/{e['tx']}"} if e.get("tx") else e
        for e in list(engine.log)[-max(1, min(limit, 500)):][::-1]
    ]
    return {"agent": AGENT_NAME, "loops": RUN_LOOPS, "state": engine.state,
            "grid": None if engine.plan is None else {"center": engine.plan.center, "step": engine.plan.step,
                                                      "levels": [round(lv, 2) for lv in engine.plan.levels]},
            "events": events}


@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_NAME, "address": wallet.address, "network": NETWORK}


def _rpc_error(req_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _agent_message(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "data", "data": data}],
    }


def _extract_data_part(message: dict[str, Any]) -> dict[str, Any] | None:
    for part in message.get("parts", []):
        if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict):
            return part["data"]
    return None


@app.post("/a2a")
async def a2a_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error", status=400)

    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0" or "method" not in body:
        return _rpc_error(req_id, -32600, "Invalid Request", status=400)
    if body["method"] != "message/send":
        return _rpc_error(req_id, -32601, f"Method not found: {body['method']}")

    message = (body.get("params") or {}).get("message") or {}
    data = _extract_data_part(message)
    if data is None:
        return _rpc_error(req_id, -32602, "message must carry a data part with a 'skill' field")

    skill = data.get("skill")

    if skill == "negotiate-erc8183-job":
        client_ip = request.client.host if request.client else "unknown"
        try:
            negotiate_limiter.check(client_ip)
            global_negotiate_limiter.check("global")
        except RateLimitExceeded:
            return _rpc_error(req_id, -32000, "Rate limited, retry later")
        terms = data.get("terms")
        task_description = data.get("task_description")
        if not isinstance(terms, dict) or not isinstance(task_description, str):
            return _rpc_error(
                req_id, -32602, "negotiate-erc8183-job requires 'task_description' (string) and 'terms' (object)"
            )
        try:
            result = negotiation_handler.negotiate({"task_description": task_description, "terms": terms})
        except Exception as exc:
            logger.error("negotiation failed: %s", exc)
            return _rpc_error(req_id, -32603, "Negotiation failed")
        envelope = result.to_dict()
        envelope["provider_address"] = wallet.address
        return {"jsonrpc": "2.0", "id": req_id, "result": _agent_message(envelope)}

    if skill == "erc8183-job-status":
        job_id = data.get("job_id")
        if not isinstance(job_id, int):
            return _rpc_error(req_id, -32602, "erc8183-job-status requires an integer 'job_id'")
        try:
            job = client.get_job(job_id)
        except Exception as exc:
            logger.error("job lookup failed: %s", exc)
            return _rpc_error(req_id, -32603, f"Job {job_id} lookup failed")
        payload = {
            "job_id": job.id,
            "client": job.client,
            "provider": job.provider,
            "status": job.status.name,
            "budget": str(job.budget),
            "expired_at": job.expired_at,
            "submitted_at": job.submitted_at,
            "deliverable": "0x" + job.deliverable.hex(),
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": _agent_message(payload)}

    if skill == "nagi-regime":
        try:
            r = regime.classify(await fetch_klines(MARKET_SYMBOL))
        except Exception as exc:
            logger.error("regime lookup failed: %s", exc)
            return _rpc_error(req_id, -32603, "Market data unavailable")
        payload = {
            "symbol": MARKET_SYMBOL,
            "state": r.state,
            "grid_allowed": r.grid_allowed,
            "efficiency_ratio": round(r.er, 4),
            "atr": r.atr,
            "price": r.price,
            "reason": r.reason,
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": _agent_message(payload)}

    return _rpc_error(req_id, -32602, f"Unknown skill: {skill!r}")


logger.info("Nagi %s: card at %s/.well-known/agent-card.json", wallet.address, BASE_URL)
