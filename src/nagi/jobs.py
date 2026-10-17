"""Job desk: turn funded ERC-8183 jobs into grid sessions, deliver reports, settle.

Lifecycle of one hire:
  FUNDED    -> open a grid session (re-centre + entry trade when calm)
  session   -> the background grid loop keeps trading; every event is logged
  end       -> upload the session report (IPFS) and submit its hash onchain
  SUBMITTED -> after the policy's dispute window, Nagi calls settle itself,
               otherwise the hire never becomes COMPLETED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from bnbagent.erc8183.schema import SCHEMA_VERSION, DeliverableManifest

from nagi.market import fetch_klines
from nagi.plan import grid_plan

logger = logging.getLogger("nagi.jobs")

SUBMIT_MARGIN = 300  # submit at least 5 minutes before the policy's submit deadline

# Pokter's default brief says "Read-only analysis only. Execute no transaction and move no funds."
READ_ONLY = re.compile(r"read[- ]only|no transactions?|do not execute|don't execute|move no funds|without (wallet )?execution",
                       re.IGNORECASE)


def terms_forbid_trading(description: str) -> bool:
    """True when the job's anchored brief (task + terms) asks for no transactions."""
    try:
        doc = json.loads(description)
        text = " ".join([str(doc.get("task", "")), *map(str, (doc.get("terms") or {}).values())])
    except (ValueError, AttributeError):
        text = description or ""
    return bool(READ_ONLY.search(text))


FINAL = ("COMPLETED", "REJECTED", "EXPIRED")

# Marketplace wallets that hire agents to test them. Their jobs are real work, but Set and Earn only counts
# hires from independent wallets, so they never count toward the campaign target.
CHECKER_WALLETS = {
    "0x93847647d7eb72a69e162117df6a8697df53edda": "Pokter correctness check",
}


class HireBook:
    """Every ERC-8183 job whose provider is Nagi, read from chain, so it survives restarts.

    The first refresh scans all jobs; later ones read only new job ids plus the
    jobs that are still open. Synchronous: run it in a thread.
    """

    def __init__(self, commerce, provider: str, chunk: int = 200, reader=None):
        self.commerce = commerce
        self.me = provider.lower()
        self.chunk = chunk
        self.reader = reader  # optional read-only ERC8183Client on a log-serving RPC, for report links
        self.scanned = 0
        self.jobs: dict[int, dict[str, Any]] = {}
        self._reports: dict[int, str | None] = {}

    def refresh(self) -> None:
        counter = self.commerce.job_counter()
        new_ids = list(range(self.scanned + 1, counter + 1))
        open_ids = [i for i, j in self.jobs.items() if j["status"] not in FINAL]
        for ids in (new_ids, open_ids):
            for i in range(0, len(ids), self.chunk):
                for job in self.commerce.get_jobs_batch(ids[i:i + self.chunk]):
                    if job is not None and job.provider.lower() == self.me:
                        self.jobs[job.id] = self._view(job)
        self.scanned = counter
        for job_id, j in self.jobs.items():
            if j["submitted_at"] and self._reports.get(job_id) is None and self.reader is not None:
                try:
                    self._reports[job_id] = self.reader.get_deliverable_url(job_id)
                except Exception as exc:
                    logger.warning("report link for job %s not found yet: %s", job_id, exc)
            j["report_url"] = _gateway(self._reports.get(job_id))

    @staticmethod
    def _view(job) -> dict[str, Any]:
        return {
            "job_id": job.id, "client": job.client, "budget_u": job.budget / 1e18,
            "status": job.status.name, "expired_at": job.expired_at, "submitted_at": job.submitted_at,
            "task": brief_task(job.description or ""), "read_only": terms_forbid_trading(job.description or ""),
            "checker": CHECKER_WALLETS.get(job.client.lower()),
        }

    def summary(self) -> dict[str, Any]:
        jobs = sorted(self.jobs.values(), key=lambda j: j["job_id"], reverse=True)
        independent = [j for j in jobs if j["client"].lower() != self.me and j["client"].lower() not in CHECKER_WALLETS]
        outside = {j["client"].lower() for j in independent}
        completed_outside = {j["client"].lower() for j in independent if j["status"] == "COMPLETED"}
        return {
            "total": len(jobs),
            "running": sum(j["status"] in ("FUNDED", "SUBMITTED") for j in jobs),
            "completed": sum(j["status"] == "COMPLETED" for j in jobs),
            "clients": len(outside),
            "completed_clients": len(completed_outside),
            "pending_u": round(sum(j["budget_u"] for j in jobs if j["status"] in ("FUNDED", "SUBMITTED")), 6),
            "jobs": jobs,
        }


def _task_doc(description: str) -> dict[str, Any]:
    """The task object inside a job description. Pokter's "pokter-job" format nests the task as a JSON string."""
    try:
        doc = json.loads(description)
    except (ValueError, TypeError):
        return {"task": description}
    if not isinstance(doc, dict):
        return {"task": description}
    task = doc.get("task") or doc.get("task_description") or ""
    if isinstance(task, str):
        try:
            inner = json.loads(task)
            if isinstance(inner, dict):
                return inner
        except ValueError:
            pass
    return {"task": task, "terms": doc.get("terms")}


def brief_task(description: str) -> str:
    """The human request inside a job's anchored description (SDK JSON, Pokter JSON, or plain text)."""
    return str(_task_doc(description).get("task") or "")[:240]


def grid_plan_request(description: str) -> dict[str, Any] | None:
    """Structured grid-plan request (Pokter's grid-arithmetic suite), or None for a normal hire."""
    data = _task_doc(description).get("data")
    if not isinstance(data, dict):
        return None
    rng = data.get("range") or {}
    try:
        return {
            "base": str(data.get("base") or "BNB"),
            "budget": float(data.get("budget_usdt") or data["budget"]),
            "lower": float(rng["lower"]), "upper": float(rng["upper"]),
            "grids": int(data["grids"]), "spacing": str(data.get("spacing") or "geometric"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _gateway(url: str | None) -> str | None:
    if url and url.startswith("ipfs://"):
        return "https://gateway.pinata.cloud/ipfs/" + url.removeprefix("ipfs://")
    return url


class JobDesk:
    def __init__(self, ops, client, engine, chain_lock: asyncio.Lock, session_seconds: int = 3600,
                 market_symbol: str = "BNBUSDT", storage=None, provider: str | None = None, service_price: int = 0):
        self.ops = ops
        self.storage = storage
        self.provider = provider
        self.service_price = service_price
        self.client = client
        self.engine = engine
        self.lock = chain_lock
        self.session_seconds = session_seconds
        self.market_symbol = market_symbol
        self.sessions: dict[int, dict[str, Any]] = {}
        self._dispute_window: int | None = None

    async def dispute_window(self) -> int:
        if self._dispute_window is None:
            self._dispute_window = int(await asyncio.to_thread(self.client.policy.dispute_window))
        return self._dispute_window

    async def on_funded(self, job: dict[str, Any]) -> bool | None:
        job_id = int(job["jobId"])
        if job_id in self.sessions:
            return None
        now = int(time.time())
        plan_request = grid_plan_request(job.get("description", ""))
        if plan_request is not None:
            # A pure calculation (e.g. Pokter's grid-arithmetic check): answer at once, no trading session.
            self.sessions[job_id] = {"kind": "plan", "start": now, "end": now, "client": job.get("client"),
                                     "request": plan_request, "submitted": False}
            logger.info("job %s: grid-plan request %s", job_id, plan_request)
            return None
        submit_by = int(job["expiredAt"]) - await self.dispute_window() - SUBMIT_MARGIN
        end = min(now + self.session_seconds, submit_by)
        if end <= now + 60:
            logger.warning("job %s: not enough time left to run a session", job_id)
            return None
        read_only = terms_forbid_trading(job.get("description", ""))
        df = await fetch_klines(self.market_symbol)
        async with self.lock:
            opened = await asyncio.to_thread(self.engine.open_session, df, job_id, read_only)
        self.sessions[job_id] = {"start": now, "end": end, "client": job.get("client"),
                                 "brief": job.get("description", ""), "read_only": read_only, "submitted": False}
        logger.info("job %s: session open until %s (%s)", job_id, end, [e["kind"] for e in opened])
        return None

    def report(self, job_id: int) -> dict[str, Any]:
        s = self.sessions[job_id]
        if s.get("kind") == "plan":
            r = s["request"]
            return grid_plan(r["base"], r["budget"], r["lower"], r["upper"], r["grids"], r["spacing"])
        events = self.engine.events_between(s["start"], s["end"])
        trades = [e for e in events if e["kind"] == "trade"]
        return {
            "agent": "Nagi", "category": "grid-trading", "job_id": job_id, "client": s["client"],
            "session": {"start": s["start"], "end": s["end"],
                        "mode": "read-only (no entry trade, per your terms)" if s.get("read_only") else "trading"},
            "summary": {"trades": len(trades), "buys": sum(e["action"] == "buy" for e in trades),
                        "sells": sum(e["action"] == "sell" for e in trades),
                        "paused": any(e["kind"] in ("pause", "session-standby") for e in events)},
            "trades": [{"action": e["action"], "level": e["level"], "tx": e["tx"], "receipt": e["receipt"]}
                       for e in trades],
            "events": events,
            "verify": "each trade's receipt is the keccak256 of its decision JSON, appended to the swap calldata",
        }

    async def finish_due(self) -> None:
        now = int(time.time())
        for job_id, s in list(self.sessions.items()):
            if s["submitted"] or s["end"] > now:
                continue
            kind = "grid-plan" if s.get("kind") == "plan" else "grid-session"
            try:
                body = json.dumps(self.report(job_id), separators=(",", ":"))
                res = await self.deliver(job_id, body, {"agent": "Nagi", "kind": kind})
            except Exception as exc:
                res = {"success": False, "error": str(exc), "permanent": False}
            if res.get("success"):
                s["submitted"] = True
                logger.info("job %s: %s delivered, tx %s, %s", job_id, kind, res["txHash"], res["deliverableUrl"])
            else:
                logger.error("job %s: delivery failed: %s", job_id, res.get("error"))
                if res.get("permanent"):
                    s["submitted"] = True  # wrong status, not ours, or past the submit deadline: stop retrying

    async def check_job(self, job_id: int) -> str | None:
        """Our own pre-submit checks. The SDK's verify_job rejects job descriptions it did not
        negotiate itself (e.g. Pokter's "pokter-job" format), so it cannot gate delivery here."""
        job = await asyncio.to_thread(self.client.get_job, job_id)
        if job.status.name != "FUNDED":
            return f"status is {job.status.name}, expected FUNDED"
        if self.provider and job.provider.lower() != self.provider.lower():
            return "this agent is not the job's provider"
        if int(time.time()) > job.expired_at - await self.dispute_window():
            return "submit deadline (expiredAt - disputeWindow) has passed"
        if job.budget < self.service_price:
            return f"budget {job.budget} is below the service price {self.service_price}"
        token = await asyncio.to_thread(self.client.job_payment_token, job_id)
        if token.lower() != (await asyncio.to_thread(lambda: self.client.payment_token)).lower():
            return "job is paid in an unexpected token"
        return None

    async def deliver(self, job_id: int, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Upload a deliverable manifest and submit its hash onchain (same manifest format as the SDK)."""
        problem = await self.check_job(job_id)
        if problem:
            return {"success": False, "error": problem, "permanent": True}
        c = self.client
        manifest = DeliverableManifest(
            version=SCHEMA_VERSION, job_id=job_id, chain_id=await asyncio.to_thread(lambda: c.commerce.w3.eth.chain_id),
            contracts={"commerce": c.commerce.address, "router": c.router.address, "policy": c.policy.address},
            response={"content": content, "content_type": "application/json"}, metadata=metadata,
        )
        url = await self.storage.upload(manifest.to_dict(), f"nagi-job-{job_id}.json")
        public_url = _gateway(url)  # https gateway link: any client or voter can fetch it
        async with self.lock:
            tx = await asyncio.to_thread(c.submit, job_id, manifest.manifest_hash(), {"deliverable_url": public_url})
        return {"success": True, "txHash": tx["transactionHash"], "deliverableUrl": public_url}

    async def settle_due(self) -> None:
        res = await self.ops.get_submitted_jobs()
        if not res.get("success"):
            return
        window = await self.dispute_window()
        now = int(time.time())
        for job in res["jobs"]:
            if int(job["submittedAt"]) + window >= now:
                continue
            try:
                async with self.lock:
                    await asyncio.to_thread(self.client.settle, int(job["jobId"]))
                logger.info("job %s: settled", job["jobId"])
            except Exception as exc:
                logger.error("job %s: settle failed: %s", job["jobId"], exc)
