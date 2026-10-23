"""Bound HTTP execution; progress lives in the existing workflow run checkpoint."""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urljoin, urlsplit

import httpx

from .project_http_connections import target_url
from .project_store import encode


class HTTPActionUncertain(RuntimeError):
    pass


def clean(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, '[凭据已隐藏]')
    if isinstance(value, dict):
        return {clean(k, secret): clean(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, secret) for v in value]
    return value


async def execute(runtime, run):
    from .workflow_runtime import WorkflowHTTPError, _coerce_http_body

    config, state = run.config, run.state
    if state is None or not state.project_context:
        raise ValueError('服务连接只能通过所属项目运行')
    service = runtime.projects.http_connections
    context = state.project_context
    connection, secret = await service.authorize(context, config.connection_id)
    payload = runtime._resolve({'path': config.path, 'headers': config.headers, 'query': config.query, 'body': config.body}, run.context)
    method = config.method
    target_url(connection, str(payload['path']), method)
    runtime.harness.enforce_secret_policy(surface='project_http', payload=payload)
    if runtime.harness.contains_secret_reference(payload):
        raise ValueError('项目 HTTP 请求的凭据请配置在服务连接中')
    forbidden_headers = {'host', 'content-length', 'connection', 'transfer-encoding', connection['auth_header'].lower(), connection['idempotency_header'].lower()} - {''}
    if any(k.lower() in forbidden_headers for k in payload['headers']):
        raise ValueError('不能覆盖服务连接的认证、幂等或传输请求头')
    write = method not in {'GET', 'HEAD'}
    fingerprint = hashlib.sha256(encode([config.connection_id, connection['scope_digest'], method, payload]).encode()).hexdigest()
    recovery = context.get('recovery', {})
    recovered = []
    while recovery:
        for previous in recovery.get('runs', []):
            if previous['workflow_id'] == state.application_id:
                recovered.extend(op for op in previous.get('http_operations', {}).values() if op['fingerprint'] == fingerprint)
        recovery = recovery.get('original', {})
    by_id = {op['operation_id']: op for op in recovered}
    if write and run.scoped_id not in state.http_operations and len(by_id) == 1:
        # Explicit repair runs retain the identity/receipt of an identical old
        # action, even if nodes have moved. No matching by title or row position.
        state.http_operations[run.scoped_id] = dict(next(iter(by_id.values())))
    elif write and run.scoped_id not in state.http_operations and len(by_id) > 1:
        raise ValueError('原任务有多个相同请求，无法唯一对应；请先核对原操作再修复')
    operation = state.http_operations.setdefault(run.scoped_id, {
        'operation_id': hashlib.sha256((run.run_id + ':' + run.scoped_id).encode()).hexdigest(),
        'fingerprint': fingerprint, 'method': method, 'path': payload['path'],
        'status': 'ready', 'attempts': 0,
    })
    if operation['fingerprint'] != fingerprint:
        raise ValueError('原请求内容已变化，不能继续重发；请使用修复运行保留原目标')

    async def persist():
        await runtime.workflow_store.update_run(run.run_id, status='running', state=state)

    async def request(request_method, path, query, body=None, *, operation_key=''):
        # Resolve the credential immediately before each attempt. A rotated key is
        # allowed; changed scope/disable is not. Never persist the injected value.
        current, credential = await service.authorize(context, config.connection_id)
        url = target_url(current, str(path), request_method)
        headers = {k: str(v) for k, v in payload['headers'].items()}
        if current['auth_header'] and credential:
            headers[current['auth_header']] = credential
        if operation_key and current['idempotency_header']:
            headers[current['idempotency_header']] = operation_key
        async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False) as client:
            for _ in range(6):
                await service.authorize(context, config.connection_id)
                runtime.harness.enforce_network_egress_policy(surface='http_request', hostname=urlsplit(url).hostname)
                try:
                    response = await client.request(request_method, url, headers=headers, params=query, json=body)
                except httpx.RequestError as error:
                    # httpx error strings can contain sensitive query/header data.
                    raise ConnectionError('服务连接中断或超时：' + type(error).__name__) from None
                if response.status_code in {301, 302, 303, 307, 308}:
                    if request_method not in {'GET', 'HEAD'} and response.status_code not in {307, 308}:
                        raise ValueError('写入请求不允许通过重定向改变 HTTP 方法')
                    redirected = urljoin(str(response.url), response.headers.get('location', ''))
                    base = current['base_url']
                    if not redirected.startswith(base + '/'):
                        raise ValueError('服务重定向超出连接地址范围')
                    relative = redirected[len(base):]
                    parts = urlsplit(relative)
                    # The target is checked again before credentials are sent.
                    url = target_url(current, parts.path, request_method)
                    if parts.fragment or parts.username or parts.password:
                        raise ValueError('服务重定向地址不合法')
                    url += ('?' + parts.query) if parts.query else ''
                    query = None
                    continue
                value = clean(_coerce_http_body(response.headers.get('content-type', ''), response.text), credential)
                if response.is_error:
                    raise WorkflowHTTPError(response.status_code, str(value)[:1000])
                if response.is_redirect:
                    raise ValueError('不支持的服务重定向')
                return {'output': value, 'status': response.status_code, 'headers': clean(dict(response.headers), credential)}
        raise ValueError('服务重定向次数过多')

    async def reconcile():
        check = config.outcome_query
        if check is None:
            return 'unknown', None
        check_context = {**run.context, 'operation': {'id': operation['operation_id'], 'request': payload}}
        try:
            result = await request('GET', runtime._resolve(check.path, check_context), runtime._resolve(check.query, check_context))
            value = result['output']
            for key in check.result_path:
                value = value[int(key)] if isinstance(value, list) else value[key]
            if type(value) is type(check.completed_value) and value == check.completed_value:
                return 'confirmed', {**result, 'confirmed_by_query': True}
            if type(value) is type(check.not_applied_value) and value == check.not_applied_value:
                return 'ready', None
        except Exception:
            # A failed query is not proof that the write did not happen.
            return 'unknown', None
        return 'unknown', None

    async def uncertain():
        operation['status'] = 'unknown'
        await persist()
        raise HTTPActionUncertain(f"待确认操作 {operation['operation_id']}：{method} {payload['path']} 可能已执行，无法确认结果，已停止自动重发。请核对服务状态后继续原任务或修复流程。")

    # A server acknowledgement persisted before a crash also protects the gap
    # between HTTP return and the enclosing node checkpoint.
    if write and operation['status'] == 'confirmed':
        return operation['result']
    idempotent = bool(connection['idempotency_header'])
    if write and operation['status'] in {'sending', 'unknown'} and not idempotent:
        disposition, result = await reconcile()
        if disposition == 'confirmed':
            operation.update(status='confirmed', result=result)
            await persist()
            return result
        if disposition != 'ready':
            await uncertain()

    for attempt in range(3):
        operation.update(status='sending', attempts=operation['attempts'] + 1)
        await persist()
        try:
            result = await request(method, payload['path'], payload['query'], payload['body'], operation_key=operation['operation_id'] if write else '')
        except (WorkflowHTTPError, ConnectionError) as error:
            retryable = isinstance(error, ConnectionError) or error.status_code in {408, 429} or error.status_code >= 500
            if not retryable:
                operation['status'] = 'rejected'
                await persist()
                raise
            if write:
                disposition, result = await reconcile()
                if disposition == 'confirmed':
                    operation.update(status='confirmed', result=result)
                    await persist()
                    return result
                if not idempotent and disposition != 'ready':
                    await uncertain()
            operation['status'] = 'unknown' if write else 'ready'
            await persist()
            if attempt == 2:
                raise
            await runtime._emit(run.run_id, 'node.retry', {'node_id': run.scoped_id, 'attempt': attempt + 2, 'operation_id': operation['operation_id']})
            await asyncio.sleep(0.1 * (attempt + 1))
        else:
            operation.update(status='confirmed', result=result)
            await persist()
            return result
    raise RuntimeError('unreachable')


async def legacy_write(runtime, run):
    """Keep old standalone URL requests usable, without replaying unknown writes."""
    from .workflow_runtime import WorkflowHTTPError
    state = run.state
    operation = state.http_operations.setdefault(run.scoped_id, {
        'operation_id': hashlib.sha256((run.run_id + ':' + run.scoped_id).encode()).hexdigest(),
        'method': run.config.method, 'path': '(独立工作流 URL)', 'fingerprint': '',
        'status': 'ready', 'attempts': 0,
    })
    if operation['status'] == 'confirmed':
        return operation['result']
    if operation['status'] in {'sending', 'unknown'}:
        raise HTTPActionUncertain('独立 HTTP 写入可能已执行，不能自动重发：' + operation['operation_id'])
    operation.update(status='sending', attempts=operation['attempts'] + 1)
    await runtime.workflow_store.update_run(run.run_id, status='running', state=state)
    try:
        kwargs = {'run_id': run.run_id} if run.run_id in runtime._network_host_allowlists else {}
        result = await runtime._http(run.config, run.context, owner_id=state.application_id, **kwargs)
    except WorkflowHTTPError as error:
        operation['status'] = 'rejected' if 400 <= error.status_code < 500 and error.status_code not in {408,429} else 'unknown'
        raise
    except (httpx.RequestError, ConnectionError):
        operation['status'] = 'unknown'
        raise HTTPActionUncertain('HTTP 写入响应丢失，已停止自动重发：' + operation['operation_id']) from None
    except Exception:
        # Parsing a malformed response can fail after the server applied a write.
        # Without a persisted acknowledgement, resending is not safe.
        operation['status'] = 'unknown'
        raise HTTPActionUncertain('未取得可确认的 HTTP 写入结果，已停止自动重发：' + operation['operation_id']) from None
    else:
        operation.update(status='confirmed', result=result)
        return result
    finally:
        await runtime.workflow_store.update_run(run.run_id, status='running', state=state)
