import asyncio
import json
import time
from types import SimpleNamespace

import pandas as pd

from nagi import jobs as jobs_mod
from nagi.jobs import JobDesk, brief_task, grid_plan_request, terms_forbid_trading

NAGI = "0xNagi"
U = "0xU"


class FakeEngine:
    def __init__(self):
        self.log = []

    def open_session(self, df, job_id, read_only=False):
        self.read_only = read_only
        e = {"kind": "trade", "action": "sell", "level": 1.0, "tx": "0xt", "receipt": "0xr",
             "ts": int(time.time()), "job_id": job_id}
        self.log.append(e)
        return [e]

    def events_between(self, start, end):
        return [e for e in self.log if start <= e["ts"] <= end]


class FakeOps:
    def __init__(self, submitted=()):
        self.submitted = list(submitted)

    async def get_submitted_jobs(self):
        return {"success": True, "jobs": self.submitted}


class FakeStorage:
    def __init__(self):
        self.uploads = []

    async def upload(self, data, name):
        self.uploads.append((name, data))
        return "ipfs://QmTest"


class FakeClient:
    """Chain double: job store + submit/settle recorders."""

    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.settled, self.submits = [], []
        self.policy = SimpleNamespace(dispute_window=lambda: 900, address="0xP")
        self.commerce = SimpleNamespace(address="0xC", w3=SimpleNamespace(eth=SimpleNamespace(chain_id=97)))
        self.router = SimpleNamespace(address="0xR")
        self.payment_token = U

    def get_job(self, job_id):
        return self.jobs[job_id]

    def job_payment_token(self, job_id):
        return U

    def submit(self, job_id, deliverable, opt_params):
        self.submits.append((job_id, deliverable, opt_params))
        return {"transactionHash": "0xsubmit"}

    def settle(self, job_id):
        self.settled.append(job_id)


def chain_job(job_id, status="FUNDED", provider=NAGI, budget=10**17, expires_in=90000):
    return SimpleNamespace(id=job_id, status=SimpleNamespace(name=status), provider=provider, budget=budget,
                           expired_at=int(time.time()) + expires_in)


def _desk(client=None, ops=None, storage=None, session_seconds=3600):
    return JobDesk(ops or FakeOps(), client or FakeClient(), FakeEngine(), asyncio.Lock(), session_seconds=session_seconds,
                   storage=storage or FakeStorage(), provider=NAGI, service_price=10**17)


async def _no_klines(*_):
    return pd.DataFrame()


POKTER_DEFAULT = json.dumps({
    "version": 1, "task": "Where is the BNB allocated and why?",
    "terms": {"deliverables": "A JSON assessment with assumptions and data sources.",
              "quality_standards": "Read-only analysis only. Execute no transaction and move no funds."},
})
NAGI_BRIEF = json.dumps({
    "version": 1, "task": "Run one grid session on BNB testnet and report every trade.",
    "terms": {"deliverables": "Session report with tx hashes", "quality_standards": "Trade only from Nagi's own wallet"},
})
# Exact shape of Pokter's grid-arithmetic check (job #1511 on BSC testnet).
POKTER_CHECK = json.dumps({
    "protocol": "pokter-job", "version": 1, "identity": {"chainId": 97, "tokenId": "2623", "name": "Nagi"},
    "category": "grid-trading", "provider": "0x3EE8",
    "task": json.dumps({
        "task": "Grid plan for BNB with 1000 USDT, range 900 to 1100, 8 grids. Geometric spacing. Reply with JSON only.",
        "data": {"suite": "grid-plan-2", "base": "BNB", "budget_usdt": 1000, "range": {"lower": 900, "upper": 1100},
                 "grids": 8, "spacing": "geometric"},
        "requested_by": "Pokter correctness check",
    }),
})


def test_pokter_check_is_parsed_as_a_grid_plan_request():
    req = grid_plan_request(POKTER_CHECK)
    assert {k: v for k, v in req.items() if k != "echo"} == {"base": "BNB", "budget": 1000.0, "lower": 900.0,
                                                             "upper": 1100.0, "grids": 8, "spacing": "geometric"}
    assert req["echo"]["budget_usdt"] == 1000 and req["echo"]["suite"] == "grid-plan-2"
    assert brief_task(POKTER_CHECK).startswith("Grid plan for BNB")
    assert grid_plan_request(NAGI_BRIEF) is None and grid_plan_request("plain text") is None


def test_grid_plan_job_is_answered_at_once_without_trading(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    client, storage = FakeClient({1511: chain_job(1511)}), FakeStorage()
    desk = _desk(client, storage=storage)
    asyncio.run(desk.on_funded({"jobId": 1511, "expiredAt": int(time.time()) + 90000, "description": POKTER_CHECK}))
    asyncio.run(desk.finish_due())
    assert desk.engine.log == []  # no session, no trade
    (job_id, deliverable, opt), = client.submits
    assert job_id == 1511 and len(deliverable) == 32
    assert opt["deliverable_url"] == "https://gateway.pinata.cloud/ipfs/QmTest"
    plan = json.loads(storage.uploads[0][1]["response"]["content"])
    assert plan["grids"] == 8 and len(plan["price_levels"]) == 9 and plan["per_grid_quote"] == 125


def test_served_bytes_hash_to_the_committed_value_even_after_reordering():
    from web3 import Web3
    from bnbagent.erc8183.schema import SCHEMA_VERSION, DeliverableManifest
    from nagi.jobs import canonical_manifest_bytes
    m = DeliverableManifest(version=SCHEMA_VERSION, job_id=1511, chain_id=97, contracts={"router": "0xR", "commerce": "0xC"},
                            response={"content": '{"note":"price left the ±3 ATR band"}', "content_type": "application/json"},
                            metadata={"kind": "grid-plan", "agent": "Nagi"})
    gateway_copy = json.loads(json.dumps(m.to_dict()))  # what a pinning service hands back: same data, its own order
    reordered = {k: gateway_copy[k] for k in reversed(list(gateway_copy))}
    assert Web3.keccak(canonical_manifest_bytes(reordered)) == m.manifest_hash()


def test_deliverable_link_points_at_the_agent_when_a_public_base_is_set(monkeypatch):
    client = FakeClient({1511: chain_job(1511)})
    desk = JobDesk(FakeOps(), client, FakeEngine(), asyncio.Lock(), storage=FakeStorage(), provider=NAGI,
                   service_price=10**17, public_base="https://agent.example/")
    desk.sessions[1511] = {"kind": "plan", "start": 0, "end": 0, "client": "0xc", "submitted": False,
                           "request": grid_plan_request(POKTER_CHECK)}
    asyncio.run(desk.finish_due())
    assert client.submits[0][2]["deliverable_url"] == "https://agent.example/deliverables/QmTest"


def test_grid_plan_answer_keeps_the_briefs_own_fields():
    desk = _desk()
    desk.sessions[1] = {"kind": "plan", "start": 0, "end": 0, "client": "0xc", "submitted": False,
                        "request": grid_plan_request(POKTER_CHECK)}
    a = desk.report(1)
    assert (a["base"], a["budget_usdt"], a["range"], a["grids"], a["spacing"]) == (
        "BNB", 1000, {"lower": 900, "upper": 1100}, 8, "geometric")
    assert a["asset"] == "BNB" and len(a["levels"]) == 9 and a["levels"][0] == 900 and a["levels"][-1] == 1100
    assert a["level_count"] == 9 and abs(a["step"] - (1100 / 900) ** (1 / 8)) < 1e-9


def test_funded_job_opens_session_capped_by_submit_deadline(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    desk = _desk()
    expires = int(time.time()) + 3000  # submit deadline = expires - 900 - 300
    asyncio.run(desk.on_funded({"jobId": 5, "expiredAt": expires, "client": "0xc"}))
    assert desk.sessions[5]["end"] == expires - 900 - 300


def test_job_too_close_to_deadline_is_skipped(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    desk = _desk()
    asyncio.run(desk.on_funded({"jobId": 6, "expiredAt": int(time.time()) + 1000}))
    assert 6 not in desk.sessions


def test_finished_session_delivers_report_once(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    client = FakeClient({9: chain_job(9)})
    desk = _desk(client)
    asyncio.run(desk.on_funded({"jobId": 9, "expiredAt": int(time.time()) + 90000, "client": "0xc"}))
    desk.sessions[9]["end"] = int(time.time())  # session over
    asyncio.run(desk.finish_due())
    asyncio.run(desk.finish_due())
    assert len(client.submits) == 1


def test_delivery_stops_for_good_when_job_is_not_ours_or_not_funded(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    client = FakeClient({1: chain_job(1, provider="0xOther"), 2: chain_job(2, status="COMPLETED")})
    desk = _desk(client)
    for jid in (1, 2):
        desk.sessions[jid] = {"kind": "plan", "start": 0, "end": 0, "client": "0xc", "submitted": False,
                              "request": {"base": "BNB", "budget": 100, "lower": 1, "upper": 2, "grids": 2,
                                          "spacing": "geometric"}}
    asyncio.run(desk.finish_due())
    assert client.submits == [] and all(desk.sessions[j]["submitted"] for j in (1, 2))


def test_upload_failure_is_retried_next_tick(monkeypatch):
    client = FakeClient({3: chain_job(3)})

    class FlakyStorage(FakeStorage):
        calls = 0

        async def upload(self, data, name):
            FlakyStorage.calls += 1
            if FlakyStorage.calls == 1:
                raise ConnectionError("pinata down")
            return await super().upload(data, name)

    desk = _desk(client, storage=FlakyStorage())
    desk.sessions[3] = {"kind": "plan", "start": 0, "end": 0, "client": "0xc", "submitted": False,
                        "request": {"base": "BNB", "budget": 100, "lower": 1, "upper": 2, "grids": 2, "spacing": "geometric"}}
    asyncio.run(desk.finish_due())
    assert not desk.sessions[3]["submitted"]
    asyncio.run(desk.finish_due())
    assert desk.sessions[3]["submitted"] and len(client.submits) == 1


def test_terms_forbid_trading_reads_the_anchored_brief():
    assert terms_forbid_trading(POKTER_DEFAULT)
    assert not terms_forbid_trading(NAGI_BRIEF)
    assert terms_forbid_trading("please do not execute anything")
    assert not terms_forbid_trading("")


def test_read_only_brief_opens_session_without_trading(monkeypatch):
    monkeypatch.setattr(jobs_mod, "fetch_klines", _no_klines)
    desk = _desk()
    asyncio.run(desk.on_funded({"jobId": 11, "expiredAt": int(time.time()) + 90000, "description": POKTER_DEFAULT}))
    assert desk.engine.read_only is True and desk.sessions[11]["read_only"] is True
    assert desk.report(11)["session"]["mode"].startswith("read-only")


def test_settles_only_after_dispute_window():
    now = int(time.time())
    ops = FakeOps(submitted=[{"jobId": 1, "submittedAt": now - 1000}, {"jobId": 2, "submittedAt": now - 100}])
    client = FakeClient()
    asyncio.run(_desk(client, ops=ops).settle_due())
    assert client.settled == [1]
