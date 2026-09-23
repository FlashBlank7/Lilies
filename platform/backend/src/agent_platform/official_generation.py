"""Graph-only generation through the official agent, with no business tools."""
import asyncio
import json
import time
from uuid import uuid4

from .connected_model import completion_events


class OfficialGeneration:
    def __init__(self, services, project_id):
        self.services, self.project_id = services, project_id
        self.usage_known = False

    async def stream(self, *, system, messages, tools, **kwargs):
        if tools:
            raise ValueError('工作流生成不调用业务工具')
        service = self.services.official_agent
        from .official_generation_jobs import generation_job
        managed = bool(generation_job.get())
        job_id = generation_job.get() or str(uuid4())
        if not managed:
            await service.enqueue(self.project_id, job_id, 'generation')
        began = None
        client = None
        text = []
        usage = {}
        status, error = 'completed', ''
        try:
            await service.acquire(self.project_id, job_id)
            began = time.monotonic()
            client = service.client(self.project_id, service.root / 'generation' / job_id)
            await client.start([], system)

            async def event(method, params):
                if method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
                    text.append(params['item'].get('text', ''))
                if method == 'thread/tokenUsage/updated':
                    self.usage_known = True
                    total = (params.get('tokenUsage') or {}).get('total') or {}
                    usage.update(input_tokens=total.get('inputTokens', 0), output_tokens=total.get('outputTokens', 0))
                    service.update(job_id, tokens=total.get('totalTokens'))
                    if (total.get('totalTokens') or 0) >= service.config().max_tokens:
                        task = asyncio.create_task(client.interrupt())
                        self.services.background_tasks.add(task)
                        task.add_done_callback(self.services.background_tasks.discard)

            async def reject(name, arguments):
                raise ValueError('生成模式只返回工作流，不执行操作')

            prompt = json.dumps([m.model_dump(mode='json') for m in messages], ensure_ascii=False)
            async with asyncio.timeout(service.config().max_seconds):
                result = await client.turn(prompt, event, reject)
            if result.get('status') != 'completed':
                raise ValueError('工作流生成已中断，原草稿保持不变')
            await service.authorize(self.project_id)
            for item in completion_events([{'type': 'text', 'text': '\n'.join(text)}], usage):
                yield item
        except BaseException as cause:
            status, error = ('interrupted' if isinstance(cause, asyncio.CancelledError) else 'error'), str(cause)[:300]
            raise
        finally:
            if client:
                try:
                    await client.interrupt()
                except Exception:
                    pass
                await client.close()
            if began is not None or not managed:
                service.update(job_id, status='waiting' if managed else status, error=error,
                               ended=None if managed else time.time(), execution_seconds=time.monotonic()-began if began else 0)
            row = service.job(job_id)
            if row and not managed:
                self.services.product_usage.record(key=job_id+':result', user_id=row['user_id'], project_id=self.project_id,
                    conversation_id=row['conversation_id'], root_id=job_id, feature='generation_result',
                    outcome=status, tokens=row['tokens'], seconds=row['execution_seconds'])
