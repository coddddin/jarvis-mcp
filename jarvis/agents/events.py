"""Durable ordered task events and pull-based asynchronous backpressure."""
import asyncio
import time
from typing import Literal

from pydantic import Field

from jarvis.agents.models import Contract, Role, Status, TERMINAL


class EventError(ValueError):
    """An invalid or missing event cursor requires explicit client recovery."""


class EventState(Contract):
    status: Status
    role: Role
    steps_started: int = Field(ge=0, le=20)
    correction_retries: int = Field(ge=0, le=3)
    generation_tokens_reserved: int = Field(ge=0, le=32768)
    # No raw prompts, proposals, memory values, secrets or hidden reasoning.


class TaskEvent(Contract):
    version: Literal[1] = 1
    event_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    sequence: int = Field(ge=1)
    kind: Literal['task.created', 'task.state']
    agent_id: Role
    timestamp: float
    payload: EventState


class EventBatch(Contract):
    events: tuple[TaskEvent, ...]
    next_sequence: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    terminal: bool


class AsyncEventBus:
    """No unbounded background queue: slow consumers resume from durable sequence.

    Each consumer holds at most one bounded page. Disconnects do not drop the
    persisted terminal event. Transport authentication belongs to the bridge.
    """
    def __init__(self, tasks, *, poll_seconds=0.05):
        if not 0.01 <= poll_seconds <= 1:
            raise EventError('Invalid event polling interval.')
        self.tasks = tasks
        self.poll_seconds = poll_seconds

    async def stream(self, task_id, *, after=0, idle_seconds=30):
        if not 0.1 <= idle_seconds <= 60:
            raise EventError('Invalid bounded stream idle timeout.')
        cursor, last_activity = after, time.monotonic()
        while True:
            page = await asyncio.to_thread(self.tasks.events, task_id, after=cursor, limit=32)
            for event in page.events:
                yield event
                cursor = event.sequence
                last_activity = time.monotonic()
            if page.terminal and cursor >= page.latest_sequence:
                return
            if time.monotonic() - last_activity >= idle_seconds:
                return
            await asyncio.sleep(self.poll_seconds)
