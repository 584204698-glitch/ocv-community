"""At most two independent reviews, with inherited routing and parent-only saves."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from .production_metrics import production_scope


class IncompleteReview(ValueError):
    """A service/response failure, not permission to rewrite a valid candidate."""


def run_reviews(jobs):
    def perform(stage, function):
        with production_scope(stage=stage):
            return function()

    if len(jobs) <= 1:
        for name, function in jobs.items():
            try:
                yield name, perform(name, function), None
            except Exception as exc:
                yield name, None, exc
        return
    # Both reviews consume immutable copies of the same validated candidate.
    # Collect every terminal response even if the other review fails, so an
    # explicit resume can reuse successful paid work. No state writes in workers.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='ocv-review') as pool:
        tasks = {pool.submit(copy_context().run, perform, name, function): name
                 for name, function in jobs.items()}
        for future in as_completed(tasks):
            try:
                yield tasks[future], future.result(), None
            except Exception as exc:
                yield tasks[future], None, exc
