"""Background worker: runs triage and CRM sync jobs from the Postgres queue."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from ticket_triage import jobs
from ticket_triage.crm.hubspot import HubSpotClient
from ticket_triage.llm.providers import build_triage_model
from ticket_triage.pipeline import Pipeline, Retry
from ticket_triage.settings import Settings
from ticket_triage.taxonomy import Taxonomy

log = logging.getLogger("ticket_triage.worker")


def build_pipeline(settings: Settings, taxonomy: Taxonomy) -> Pipeline:
    hubspot_factory: Callable[[], HubSpotClient] | None = None
    if settings.hubspot_enabled:

        def hubspot_factory() -> HubSpotClient:
            return HubSpotClient(
                settings.hubspot_access_token.get_secret_value(),
                pipeline_id=settings.hubspot_pipeline_id,
                pipeline_stage_id=settings.hubspot_pipeline_stage_id,
                base_url=settings.hubspot_base_url,
            )

    return Pipeline(
        settings=settings,
        taxonomy=taxonomy,
        model_factory=lambda: build_triage_model(settings),
        hubspot_factory=hubspot_factory,
    )


class Worker:
    def __init__(
        self,
        pipeline: Pipeline,
        sessions: sessionmaker[Session],
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.sessions = sessions
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def loop(self) -> None:
        log.info("worker %s started", self.worker_id)
        while not self._stopping.is_set():
            try:
                busy = self.run_once()
            except Exception:  # database unavailable etc.; keep the loop alive
                log.exception("worker %s: queue error", self.worker_id)
                busy = False
            if not busy:
                self._stopping.wait(self.settings.worker_poll_interval_seconds)
        log.info("worker %s stopped", self.worker_id)

    def run_once(self) -> bool:
        """Process one job. Returns False when the queue is empty."""
        # The claim is committed on its own so a crash mid-job still counts
        # the attempt and leaves a lock that goes stale.
        with self.sessions.begin() as session:
            job = jobs.claim(session, self.worker_id, self.settings.worker_stale_lock_seconds)
        if job is None:
            return False

        handler = {
            jobs.TRIAGE: self.pipeline.run_triage,
            jobs.CRM_SYNC: self.pipeline.run_crm_sync,
        }.get(job.kind)
        started = time.monotonic()
        try:
            with self.sessions.begin() as session:
                if handler is None:
                    raise ValueError(f"unknown job kind {job.kind!r}")
                outcome = handler(session, job)
                if isinstance(outcome, Retry):
                    self._fail(session, job, outcome.error)
                else:
                    jobs.complete(session, job.id)
        except Exception as exc:
            log.exception("job %s (%s) crashed", job.id, job.kind)
            with self.sessions.begin() as session:
                self._fail(session, job, f"{type(exc).__name__}: {exc}")
            return True
        if not isinstance(outcome, Retry):
            log.info(
                "job %s %s ticket=%s done in %.2fs",
                job.id,
                job.kind,
                job.ticket_id,
                time.monotonic() - started,
            )
        return True

    def _fail(self, session: Session, job: jobs.ClaimedJob, error: str) -> None:
        if jobs.fail(session, job, error, retryable=True):
            log.error("job %s %s gave up: %s", job.id, job.kind, error)
            self.pipeline.on_job_dead(session, job, error)
        else:
            log.warning("job %s %s will retry: %s", job.id, job.kind, error)


def run_workers(
    pipeline: Pipeline, sessions: sessionmaker[Session], settings: Settings, concurrency: int
) -> None:
    """Run ``concurrency`` worker loops in threads until SIGTERM/SIGINT. Jobs
    are claimed with SKIP LOCKED, so loops (and processes) never collide."""
    base = f"{socket.gethostname()}:{os.getpid()}"
    workers = [
        Worker(pipeline, sessions, settings, worker_id=f"{base}:{i}") for i in range(concurrency)
    ]

    def _stop(signum: int, frame: FrameType | None) -> None:
        for worker in workers:
            worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    threads = [threading.Thread(target=w.loop, name=w.worker_id) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
