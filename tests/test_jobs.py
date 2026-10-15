import asyncio
import time

import pandas as pd

from nagi import jobs as jobs_mod
from nagi.jobs import JobDesk


class FakeEngine:
    def __init__(self):
        self.log = []

    def open_session(self, df, job_id):
        e = {"kind": "trade", "action": "sell", "level": 1.0, "tx": "0xt", "receipt": "0xr",
             "ts": int(time.time()), "job_id": job_id}
        self.log.append(e)
        return [e]

    def events_between(self, start, end):
        return [e for e in self.log if start <= e["ts"] <= end]


class FakeOps:
    def __init__(self, submitted=()):
        self.submits = []
        self.submitted = list(submitted)

    async def submit_result(self, job_id, body, metadata=None):
        self.submits.append((job_id, body))
        return {"success": True}

    async def get_submitted_jobs(self):
        return {"success": True, "jobs": self.submitted}


class FakePolicy:
    def dispute_window(self):
        return 900


class FakeClient:
    policy = FakePolicy()

    def __init__(self):
        self.settled = []

    def settle(self, job_id):
        self.settled.append(job_id)


def _desk(ops, client, session_seconds=3600):
    return JobDesk(ops, client, FakeEngine(), asyncio.Lock(), session_seconds=session_seconds)


async def _no_klines(*_):
    return pd.DataFrame()


def test_funded_job_opens_session_capped_by_submit_deadline(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    desk = _desk(FakeOps(), FakeClient())
    expires = int(time.time()) + 3000  # submit deadline = expires - 900 - 300
    asyncio.run(desk.on_funded({"jobId": 5, "expiredAt": expires, "client": "0xc"}))
    assert desk.sessions[5]["end"] == expires - 900 - 300


def test_job_too_close_to_deadline_is_skipped(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    desk = _desk(FakeOps(), FakeClient())
    asyncio.run(desk.on_funded({"jobId": 6, "expiredAt": int(time.time()) + 1000}))
    assert 6 not in desk.sessions


def test_finished_session_submits_report_once(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    ops = FakeOps()
    desk = _desk(ops, FakeClient())
    asyncio.run(desk.on_funded({"jobId": 9, "expiredAt": int(time.time()) + 90000, "client": "0xc"}))
    desk.sessions[9]["end"] = int(time.time())  # session over
    asyncio.run(desk.finish_due())
    asyncio.run(desk.finish_due())
    assert len(ops.submits) == 1 and '"trades":1' in ops.submits[0][1]


def test_settles_only_after_dispute_window():
    now = int(time.time())
    ops = FakeOps(submitted=[{"jobId": 1, "submittedAt": now - 1000}, {"jobId": 2, "submittedAt": now - 100}])
    client = FakeClient()
    asyncio.run(_desk(ops, client).settle_due())
    assert client.settled == [1]
