"""Conversation identity propagates into tasks; project resource identity stays unchanged."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_current: ContextVar[tuple[str, str] | None] = ContextVar('project_conversation', default=None)


def conversation_for(project_id: str) -> str:
    value = _current.get()
    return value[1] if value and value[0] == project_id else ''


@contextmanager
def conversation_scope(project_id: str, conversation_id: str = ''):
    if conversation_id:
        conversation_id = str(UUID(conversation_id))
    token = _current.set((project_id, conversation_id))
    try:
        yield
    finally:
        _current.reset(token)
