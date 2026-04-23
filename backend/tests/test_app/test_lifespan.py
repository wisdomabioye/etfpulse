"""Lifespan composition tests.

We verify the AsyncExitStack ordering (tasks enter in order, exit in reverse)
with an injected fake task. STARTUP_TASKS is a list at module level, so tests
mutate it through a fixture that snapshots and restores.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from etfpulse.api import lifespan as lifespan_mod
from etfpulse.app import create_app


@pytest.fixture
def clean_startup_tasks():
    """Snapshot + restore STARTUP_TASKS so tests can safely mutate it."""
    original = list(lifespan_mod.STARTUP_TASKS)
    lifespan_mod.STARTUP_TASKS.clear()
    yield lifespan_mod.STARTUP_TASKS
    lifespan_mod.STARTUP_TASKS.clear()
    lifespan_mod.STARTUP_TASKS.extend(original)


def test_scheduler_registered_at_import():
    """D14 guardrail — `start_scheduler` must be in STARTUP_TASKS after
    importing the module. If a future refactor breaks the registration,
    Coolify would silently boot without a scheduler — this catches that."""
    from etfpulse.pipeline.scheduler import start_scheduler

    assert start_scheduler in lifespan_mod.STARTUP_TASKS


def test_empty_task_list_starts_and_stops_cleanly(clean_startup_tasks):
    """With no tasks registered, lifespan must complete both enter and exit."""
    app = create_app()
    with TestClient(app):
        pass  # just entering + exiting the context exercises lifespan


def test_tasks_fire_in_order_and_teardown_in_reverse(clean_startup_tasks):
    """Enter: A, B, C. Exit: C, B, A."""
    events: list[str] = []

    def make_task(name: str):
        @asynccontextmanager
        async def _task(_app: FastAPI) -> AsyncIterator[None]:
            events.append(f"start_{name}")
            try:
                yield
            finally:
                events.append(f"stop_{name}")

        return _task

    clean_startup_tasks.extend([make_task("A"), make_task("B"), make_task("C")])

    app = create_app()
    with TestClient(app):
        pass

    assert events == [
        "start_A",
        "start_B",
        "start_C",
        "stop_C",
        "stop_B",
        "stop_A",
    ]


def test_failed_task_tears_down_prior_tasks(clean_startup_tasks):
    """If task N raises during startup, tasks 0..N-1 must still teardown."""
    events: list[str] = []

    @asynccontextmanager
    async def ok_task(_app: FastAPI) -> AsyncIterator[None]:
        events.append("start_ok")
        try:
            yield
        finally:
            events.append("stop_ok")

    @asynccontextmanager
    async def bad_task(_app: FastAPI) -> AsyncIterator[None]:
        events.append("start_bad")
        raise RuntimeError("boom")
        yield  # unreachable — keeps mypy quiet about the generator shape

    clean_startup_tasks.extend([ok_task, bad_task])

    app = create_app()
    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass

    assert events == ["start_ok", "start_bad", "stop_ok"]
