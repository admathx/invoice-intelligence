from redis import Redis
from rq import SimpleWorker

from app.config import settings

if __name__ == "__main__":
    # SimpleWorker runs jobs in-process instead of forking a work-horse per job.
    # The default rq.Worker forks, and forking a Python process that has touched
    # Objective-C frameworks (httpx/certifi pull these in on macOS) crashes with
    # "may have been in progress in another thread when fork() was called" — a
    # known macOS fork-safety issue, not specific to this app.
    conn = Redis.from_url(settings.redis_url)
    worker = SimpleWorker(["invoices"], connection=conn)
    worker.work()
