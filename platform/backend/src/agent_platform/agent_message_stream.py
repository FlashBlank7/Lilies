"""Persist public assistant text without adding a history event for every token."""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from .models import utc_now


def finish_saved_message(state: dict) -> None:
    """Keep an interrupted reply readable after service restart."""
    message = state.pop('streaming_message', None)
    if message and message.get('text'):
        state['events'].append({**message, 'incomplete': True})


class AgentMessageStream:
    def __init__(self, manager, project_id: str, request_id: str):
        self.manager, self.project_id, self.request_id = manager, project_id, request_id
        self.message: dict | None = None
        self.item_key = None
        self.completed: set[tuple] = set()
        self.last_saved = 0.0
        self.checkpoint: asyncio.TimerHandle | None = None
        self.closed = False

    def handle(self, method: str, params: dict, *, thread_id=None, turn_id=None) -> bool:
        item = params.get('item', {})
        delta = method == 'item/agentMessage/delta'
        complete = method == 'item/completed' and item.get('type') == 'agentMessage'
        if not (delta or complete):
            return False
        # Notifications from an earlier thread/turn must not enter a new reply.
        if self.closed or any(expected and params.get(field) and params[field] != expected
                              for field, expected in (('threadId', thread_id), ('turnId', turn_id))):
            return True
        item_id = params.get('itemId') if delta else item.get('id')
        key = (params.get('threadId') or thread_id, params.get('turnId') or turn_id, item_id)
        if item_id and key in self.completed:
            return True
        text = params.get('delta') if delta else item.get('text')
        if not isinstance(text, str) or (delta and not item_id):
            return True
        if self.message and self.item_key != key:
            self.finish()
        if not self.message:
            self.message = {'id': str(uuid4()), 'kind': 'assistant', 'text': '',
                            'time': utc_now(), 'request_id': self.request_id}
            self.item_key = key
            self.last_saved = 0.0
        # Match the existing saved-message limit; repeated text is legitimate.
        self.message['text'] = ((self.message['text'] + text) if delta else text)[:30_000]
        if complete:
            if item_id:
                self.completed.add(key)
            self.finish(incomplete=False)
        elif self.message['text']:
            remaining = .5 - (time.monotonic() - self.last_saved)
            if not self.last_saved or remaining <= 0:
                self.flush()
            elif self.checkpoint is None:
                # Flush the trailing fragment even if the model pauses here.
                # call_later preserves the original conversation ContextVar.
                self.checkpoint = asyncio.get_running_loop().call_later(remaining, self.flush_later)
        return True

    def flush(self) -> None:
        if self.checkpoint:
            self.checkpoint.cancel()
            self.checkpoint = None
        if self.message and not self.closed:
            state = self.manager.load(self.project_id)
            if state.get('request_id', '') == self.request_id:
                state['streaming_message'] = dict(self.message)
                # Polling reads this without refreshing all project resources.
                self.manager.save(self.project_id, state, advance_revision=False)
            self.last_saved = time.monotonic()

    def flush_later(self) -> None:
        try:
            self.flush()
        except OSError:
            # A later delta or final save retries through the ordinary path.
            logging.getLogger(__name__).exception('Could not checkpoint assistant reply')

    def finish(self, *, incomplete: bool = True, close: bool = False) -> None:
        if self.checkpoint:
            self.checkpoint.cancel()
            self.checkpoint = None
        self.closed = self.closed or close
        if not self.message:
            return
        state = self.manager.load(self.project_id)
        if state.get('request_id', '') == self.request_id:
            state.pop('streaming_message', None)
            if self.message['text']:
                message = dict(self.message)
                if incomplete:
                    message['incomplete'] = True
                state['events'].append(message)
            self.manager.save(self.project_id, state)
        self.message = None
        self.item_key = None
