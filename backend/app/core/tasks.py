"""Fire-and-forget background tasks with a held reference.

asyncio only keeps a weak reference to tasks, so a bare
`asyncio.create_task(...)` can be garbage-collected mid-run. Route
background work through here so it stays alive until it finishes.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
