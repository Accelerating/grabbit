"""Task state contract shared by future download engine adapters.

Transitions are intentionally centralized so an engine cannot invent a new
database state or turn a terminal result into an active task implicitly.
"""

from enum import StrEnum


class TaskKind(StrEnum):
    GENERAL = "general"
    VIDEO = "video"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    AWAITING_SELECTION = "awaiting_selection"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    RETRY_WAIT = "retry_wait"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RESOLVING, TaskStatus.DOWNLOADING, TaskStatus.PAUSED, TaskStatus.STOPPED, TaskStatus.CANCELLED}),
    TaskStatus.RESOLVING: frozenset({TaskStatus.AWAITING_SELECTION, TaskStatus.DOWNLOADING, TaskStatus.RETRY_WAIT, TaskStatus.FAILED, TaskStatus.STOPPED, TaskStatus.CANCELLED}),
    TaskStatus.AWAITING_SELECTION: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.DOWNLOADING: frozenset({TaskStatus.POSTPROCESSING, TaskStatus.COMPLETED, TaskStatus.RETRY_WAIT, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.STOPPED, TaskStatus.CANCELLED}),
    TaskStatus.POSTPROCESSING: frozenset({TaskStatus.COMPLETED, TaskStatus.RETRY_WAIT, TaskStatus.FAILED, TaskStatus.STOPPED, TaskStatus.CANCELLED}),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.STOPPED, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.STOPPED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.QUEUED}),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return current == target or target in TRANSITIONS[current]
