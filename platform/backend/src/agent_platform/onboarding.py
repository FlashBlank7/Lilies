"""Optional account tutorial. It never launches or gates project work."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .db import connect

Step = Literal['project', 'materials', 'workflow', 'conversation', 'results', 'next']
Action = Literal['project', 'materials', 'workflow', 'conversation', 'results', 'next']
Status = Literal['new', 'active', 'skipped', 'finished']


class OnboardingState(BaseModel):
    status: Status = 'skipped'  # Existing accounts have no record: never surprise them.
    step: Step = 'project'
    completed_steps: list[Action] = Field(default_factory=list)
    project_id: str | None = None


class OnboardingPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Status | None = None
    step: Step | None = None
    completed_steps: list[Action] = Field(default_factory=list, max_length=6)
    project_id: str | None = Field(default=None, max_length=100)
    restart: bool = False


def _accessible(db, user, project_id):
    if not project_id:
        return False
    if not db.execute('SELECT id FROM projects WHERE id=?', (project_id,)).fetchone():
        return False
    return user['role'] == 'admin' or bool(db.execute(
        'SELECT 1 FROM project_access_members WHERE project_id=? AND user_id=?',
        (project_id, user['id']),
    ).fetchone())


async def onboarding(accounts, user, patch: OnboardingPatch | None = None):
    def transaction():
        with connect(accounts.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state_json FROM user_onboarding WHERE user_id=?', (user['id'],)).fetchone()
            state = OnboardingState.model_validate_json(row['state_json']) if row else OnboardingState()
            if state.project_id and not _accessible(db, user, state.project_id):
                state.project_id, state.step = None, 'project'
            if patch:
                if patch.restart:
                    state = OnboardingState(status='active')
                if 'project_id' in patch.model_fields_set:
                    if patch.project_id and not _accessible(db, user, patch.project_id):
                        raise HTTPException(404, '没有找到这个项目或没有访问权限，请重新选择项目')
                    state.project_id = patch.project_id
                if patch.status is not None:
                    state.status = patch.status
                if patch.step is not None:
                    state.step = patch.step
                state.completed_steps = list(dict.fromkeys([*state.completed_steps, *patch.completed_steps]))
            # Legacy reads remain read-only; write progress and revoked-project repairs only.
            if patch or (row and json.loads(row['state_json']).get('project_id') != state.project_id):
                db.execute('INSERT INTO user_onboarding VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET state_json=excluded.state_json',
                           (user['id'], state.model_dump_json()))
            return state
    return await asyncio.to_thread(transaction)
