"""Graph-only generation through the official agent, with no business tools."""
import asyncio
import json
import time
from uuid import uuid4

from .connected_model import completion_events
from .codex_app_server import CodexAuthenticationError


_RETURN_WORKFLOW = {
    'name': 'return_workflow',
    'description': '提交本次生成的工作流定义。不运行或保存工作流，不调用任何业务能力。',
    'inputSchema': {
        'type': 'object', 'properties': {'workflow': {
            'type': 'object', 'properties': {
                'nodes': {'type': 'array', 'items': {'type': 'object'}},
                'edges': {'type': 'array', 'items': {'type': 'object'}}},
            'required': ['nodes', 'edges']}},
        'required': ['workflow']},
}


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
        document = None
        usage = {}
        status, error = 'completed', ''
        try:
            await service.acquire(self.project_id, job_id)
            began = time.monotonic()
            client = service.client(self.project_id, service.root / 'generation' / job_id)
            await client.start([_RETURN_WORKFLOW], system + '\n交付方式：将完整 JSON 对象作为参数提交给 return_workflow，'
                               '它只是本次结果的接收器，不是业务工具。通过这个接收器交付，不在正文重复输出 JSON。'
                               '提交成功后简短结束，不执行工作流或其他操作。')

            async def event(method, params):
                if method == 'thread/tokenUsage/updated':
                    self.usage_known = True
                    total = (params.get('tokenUsage') or {}).get('total') or {}
                    usage.update(input_tokens=total.get('inputTokens', 0), output_tokens=total.get('outputTokens', 0))
                    for source, target in [('cachedInputTokens', 'cache_read_input_tokens'),
                                           ('reasoningOutputTokens', 'reasoning_tokens')]:
                        if source in total:
                            usage[target] = total[source]
                    service.update(job_id, tokens=total.get('totalTokens'))
                    limit = service.config().max_tokens
                    if limit is not None and (total.get('totalTokens') or 0) >= limit:
                        task = asyncio.create_task(client.interrupt())
                        self.services.background_tasks.add(task)
                        task.add_done_callback(self.services.background_tasks.discard)

            async def receive(name, arguments):
                nonlocal document
                if name != 'return_workflow':
                    raise ValueError('生成模式只接收工作流，不执行操作')
                workflow = arguments.get('workflow') if isinstance(arguments, dict) else None
                if not isinstance(workflow, dict) or not all(isinstance(workflow.get(key), list) for key in ('nodes', 'edges')):
                    raise ValueError('请提交 workflow 对象，其中 nodes 和 edges 必须为数组')
                # The protocol parses tool arguments. Serialize once here rather
                # than asking the model to hand-escape an entire JSON document.
                # Structural/capability/revision checks still run in the caller
                # before its single save; this receiver has no project effects.
                document = json.dumps({'workflow': workflow}, ensure_ascii=False)
                return {'received': True}

            # The generation caller already supplies one complete JSON context.
            # Re-encoding its text inside a message envelope escapes the entire
            # catalog/graph again without adding information for the model.
            if len(messages) == 1 and messages[0].role == 'user' and all(b.type == 'text' for b in messages[0].content):
                prompt = '\n\n'.join(b.text or '' for b in messages[0].content)
            else:
                prompt = json.dumps([m.model_dump(mode='json') for m in messages], ensure_ascii=False)
            async with asyncio.timeout(service.config().max_seconds):
                result = await client.turn(prompt, event, receive)
            if result.get('status') != 'completed':
                raise ValueError('工作流生成已中断，原草稿保持不变')
            await service.authorize(self.project_id)
            if document is None:
                raise ValueError('模型未提交工作流定义，原草稿保持不变')
            for item in completion_events([{'type': 'text', 'text': document}], usage):
                yield item
        except BaseException as cause:
            status, error = ('interrupted' if isinstance(cause, asyncio.CancelledError) else 'error'), str(cause)[:300]
            if isinstance(cause, CodexAuthenticationError):
                service.set_connection('blocked', error)
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
