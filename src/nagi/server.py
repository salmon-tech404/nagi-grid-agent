"""Nagi A2A surface: agent card, signed ERC-8183 quotes, job status, live regime.

Adapted from bnbagent-sdk python/examples/a2a-agent/src/server.py (copy-and-own).

Run:
    uv run uvicorn nagi.server:app --app-dir src --port 8010
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from bnbagent import EVMWalletProvider  # noqa: E402
from bnbagent.erc8183 import ERC8183Client, NegotiationHandler  # noqa: E402
from bnbagent.utils import RateLimitExceeded, SlidingWindowLimiter  # noqa: E402

from nagi import regime  # noqa: E402
from nagi.market import fetch_klines  # noqa: E402

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
CATEGORY = "grid"
# Render injects RENDER_EXTERNAL_URL; A2A_BASE_URL overrides it (e.g. a custom domain).
BASE_URL = (
    os.getenv("A2A_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8010"
).rstrip("/")
SERVICE_PRICE = os.getenv("ERC8183_SERVICE_PRICE", "1000000000000000000")  # 1 U
MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "BNBUSDT")

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

negotiate_limiter = SlidingWindowLimiter(max_requests=120, window_seconds=60.0, max_keys=10_000)
global_negotiate_limiter = SlidingWindowLimiter(max_requests=1_200, window_seconds=60.0, max_keys=1)

AGENT_CARD: dict[str, Any] = {
    "protocolVersion": "0.3.0",
    "name": AGENT_NAME,
    "description": AGENT_DESCRIPTION,
    "category": CATEGORY,
    "url": f"{BASE_URL}/a2a",
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

app = FastAPI(title=AGENT_NAME)


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return AGENT_CARD


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
