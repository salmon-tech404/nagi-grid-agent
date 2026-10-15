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
import time
from typing import Any

from nagi.market import fetch_klines

logger = logging.getLogger("nagi.jobs")

SUBMIT_MARGIN = 300  # submit at least 5 minutes before the policy's submit deadline


FINAL = ("COMPLETED", "REJECTED", "EXPIRED")


class HireBook:
    """Every ERC-8183 job whose provider is Nagi, read from chain, so it survives restarts.

    The first refresh scans all jobs; later ones read only new job ids plus the
    jobs that are still open. Synchronous: run it in a thread.
    """

    def __init__(self, commerce, provider: str, chunk: int = 200):
        self.commerce = commerce
        self.me = provider.lower()
        self.chunk = chunk
        self.scanned = 0
        self.jobs: dict[int, dict[str, Any]] = {}

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

    @staticmethod
    def _view(job) -> dict[str, Any]:
        return {
            "job_id": job.id, "client": job.client, "budget_u": job.budget / 1e18,
            "status": job.status.name, "expired_at": job.expired_at, "submitted_at": job.submitted_at,
            "brief": (job.description or "")[:300],
        }

    def summary(self) -> dict[str, Any]:
        jobs = sorted(self.jobs.values(), key=lambda j: j["job_id"], reverse=True)
        return {
            "total": len(jobs),
            "running": sum(j["status"] in ("FUNDED", "SUBMITTED") for j in jobs),
            "completed": sum(j["status"] == "COMPLETED" for j in jobs),
            "jobs": jobs,
        }


class JobDesk:
    def __init__(self, ops, client, engine, chain_lock: asyncio.Lock, session_seconds: int = 3600,
                 market_symbol: str = "BNBUSDT"):
        self.ops = ops
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
        submit_by = int(job["expiredAt"]) - await self.dispute_window() - SUBMIT_MARGIN
        end = min(now + self.session_seconds, submit_by)
        if end <= now + 60:
            logger.warning("job %s: not enough time left to run a session", job_id)
            return None
        df = await fetch_klines(self.market_symbol)
        async with self.lock:
            opened = await asyncio.to_thread(self.engine.open_session, df, job_id)
        self.sessions[job_id] = {"start": now, "end": end, "client": job.get("client"),
                                 "brief": job.get("description", ""), "submitted": False}
        logger.info("job %s: session open until %s (%s)", job_id, end, [e["kind"] for e in opened])
        return None

    def report(self, job_id: int) -> dict[str, Any]:
        s = self.sessions[job_id]
        events = self.engine.events_between(s["start"], s["end"])
        trades = [e for e in events if e["kind"] == "trade"]
        return {
            "agent": "Nagi", "category": "grid-trading", "job_id": job_id, "client": s["client"],
            "session": {"start": s["start"], "end": s["end"]},
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
            body = json.dumps(self.report(job_id), separators=(",", ":"))
            async with self.lock:
                res = await self.ops.submit_result(job_id, body, metadata={"agent": "Nagi", "kind": "grid-session"})
            if res.get("success"):
                s["submitted"] = True
                logger.info("job %s: report submitted %s", job_id, res.get("txHash") or res.get("transactionHash"))
            else:
                logger.error("job %s: submit failed: %s", job_id, res.get("error"))
                if not res.get("retryable"):
                    s["submitted"] = True  # e.g. deadline passed: stop retrying

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
