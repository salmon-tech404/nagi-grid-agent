from dataclasses import dataclass
from enum import Enum

from nagi.jobs import HireBook


class Status(Enum):
    OPEN = 0
    FUNDED = 1
    SUBMITTED = 2
    COMPLETED = 3


@dataclass
class Job:
    id: int
    provider: str
    status: Status
    client: str = "0xc"
    budget: int = 10**17
    expired_at: int = 0
    submitted_at: int = 0
    description: str = "brief"


class FakeCommerce:
    def __init__(self, jobs):
        self.jobs = {j.id: j for j in jobs}
        self.reads = []

    def job_counter(self):
        return max(self.jobs)

    def get_jobs_batch(self, ids):
        self.reads.append(list(ids))
        return [self.jobs.get(i) for i in ids]


NAGI = "0xNagi"


def test_first_refresh_keeps_only_nagi_jobs():
    c = FakeCommerce([Job(1, "0xOther", Status.FUNDED), Job(2, NAGI, Status.FUNDED), Job(3, NAGI, Status.COMPLETED)])
    book = HireBook(c, NAGI.upper(), chunk=2)
    book.refresh()
    s = book.summary()
    assert [j["job_id"] for j in s["jobs"]] == [3, 2]
    assert (s["total"], s["running"], s["completed"]) == (2, 1, 1)
    assert s["jobs"][0]["budget_u"] == 0.1


def test_later_refresh_reads_new_ids_and_open_jobs_only():
    c = FakeCommerce([Job(1, NAGI, Status.FUNDED), Job(2, NAGI, Status.COMPLETED)])
    book = HireBook(c, NAGI)
    book.refresh()
    c.jobs[1].status = Status.COMPLETED
    c.jobs[3] = Job(3, NAGI, Status.FUNDED)
    c.reads.clear()
    book.refresh()
    assert c.reads == [[3], [1]]  # new id 3, then still-open job 1; completed job 2 is not re-read
    assert book.jobs[1]["status"] == "COMPLETED" and book.jobs[3]["status"] == "FUNDED"
