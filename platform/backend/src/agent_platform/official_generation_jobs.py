"""Durable graph-generation requests reuse the official model queue."""
import asyncio
import json
import os
import time
from contextvars import ContextVar
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .conversation_scope import conversation_scope, conversation_for
from .official_agent import actor_id
from .project_store import connect

generation_job: ContextVar[str] = ContextVar('official_generation_job', default='')


def path(service, job_id):
    return service.root / 'generation-requests' / (str(UUID(job_id)) + '.json')


def save(service, job_id, value):
    target=path(service,job_id);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=target.with_suffix('.tmp')
    with open(tmp,'w',opener=lambda p,flags:os.open(p,flags,0o600)) as out:
        json.dump(jsonable_encoder(value),out,ensure_ascii=False)
    tmp.replace(target)


async def enqueue(service, project_id, body, *, in_conversation):
    await service.authorize(project_id)
    cid=conversation_for(project_id)
    key=body.request_key
    job_id=str(uuid5(NAMESPACE_URL, f'{project_id}:{cid}:{actor_id.get()}:generation:{key}')) if key else str(uuid4())
    target=path(service,job_id)
    if target.exists():
        old=json.loads(target.read_text())
        if old['body']!=body.model_dump(mode='json'):
            raise ValueError('同一请求标识的内容发生变化，请重新提交')
        return {'job_id':job_id,'status':'queued'}
    value={'project_id':project_id,'conversation_id':cid,'user_id':actor_id.get(),
           'body':body.model_dump(mode='json'),'in_conversation':in_conversation}
    save(service,job_id,value)
    await service.enqueue(project_id,job_id,'generation')
    launch(service,job_id,value)
    return {'job_id':job_id,'status':'queued'}


def launch(service, job_id, value):
    async def run():
        from .project_workflow_edit import ConversationGeneration, GenerateWorkflow, generate_in_conversation, generate_workflow
        actor=actor_id.set(value['user_id']);token=generation_job.set(job_id)
        try:
            with conversation_scope(value['project_id'],value['conversation_id']):
                await service.authorize(value['project_id'])
                body=(ConversationGeneration if value['in_conversation'] else GenerateWorkflow).model_validate(value['body'])
                fn=generate_in_conversation if value['in_conversation'] else generate_workflow
                result=await fn(service.services,value['project_id'],body)
                save(service,job_id,{**value,'result':result})
                service.update(job_id,status='completed',ended=time.time(),error='')
        except BaseException as error:
            row=service.job(job_id) or {}
            if not (service.shutting_down and row.get('status')=='queued'):
                service.update(job_id,status='interrupted' if isinstance(error,asyncio.CancelledError) else 'error',
                               error=str(error) or '生成已停止；已有草稿保持原样',ended=time.time())
        finally:
            row = service.job(job_id)
            if row and row['status'] not in {'queued', 'running', 'waiting'}:
                service.services.product_usage.record(key=job_id+':result', user_id=row['user_id'], project_id=row['project_id'],
                    conversation_id=row['conversation_id'], root_id=job_id, feature='generation_result',
                    outcome=row['status'], tokens=row['tokens'], seconds=row['execution_seconds'])
            actor_id.reset(actor);generation_job.reset(token)
    task=asyncio.create_task(run())
    service.generation_tasks[job_id]=task
    task.add_done_callback(lambda _:service.generation_tasks.pop(job_id,None))


async def read(service, project_id, job_id, user):
    with connect(service.db) as db:
        row=db.execute('SELECT * FROM official_agent_jobs WHERE id=? AND project_id=? AND user_id=? AND kind=?',
                       (job_id,project_id,user['id'],'generation')).fetchone()
    if not row:
        raise HTTPException(404,'生成任务不存在')
    await service.services.accounts.require_project(user,project_id)
    if row['conversation_id']:
        await service.services.project_sessions.require(project_id,row['conversation_id'],user)
    saved=json.loads(path(service,job_id).read_text())
    return {'job_id':job_id,'status':row['status'],'error':row['error'],
            **({'result':saved['result']} if row['status']=='completed' and 'result' in saved else {})}
