import asyncio
import logging
from datetime import datetime

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from app.services.ai import provider_is_configured
from app.services.grading import grade_attempt
from app.services.grading_jobs import expire_and_list

logger = logging.getLogger(__name__)


class GradingWorker:
    def __init__(self, application: FastAPI, enabled: bool):
        self.application = application
        self.enabled = enabled
        self.last_pulse: datetime | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._active: dict[int, asyncio.Task] = {}

    def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def run_once(self) -> None:
        state = self.application.state
        now = state.clock()
        self.last_pulse = now
        attempt_ids = await run_in_threadpool(
            expire_and_list, state.session_factory, now
        )
        if attempt_ids:
            await grade_attempt(self.application, attempt_ids[0])

    def _finished(self, attempt_id: int, task: asyncio.Task) -> None:
        self._active.pop(attempt_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Durable grading task failed outside its retry boundary.",
                exc_info=task.exception(),
            )

    async def dispatch_once(self) -> None:
        state = self.application.state
        now = state.clock()
        attempt_ids = await run_in_threadpool(
            expire_and_list, state.session_factory, now
        )
        self.last_pulse = now
        if not provider_is_configured(state.provider):
            return
        for attempt_id in attempt_ids:
            if attempt_id in self._active or len(self._active) >= 2:
                continue
            task = asyncio.create_task(grade_attempt(self.application, attempt_id))
            self._active[attempt_id] = task
            task.add_done_callback(
                lambda done, key=attempt_id: self._finished(key, done)
            )

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.dispatch_once()
            except Exception:
                logger.exception("Durable grading worker pulse failed.")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                pass
