"""HTTP authorization shared by project pages and legacy resource URLs."""
from fastapi import HTTPException, Request


def request_token(request: Request):
    value = request.headers.get('authorization', '')
    return value[7:] if value.lower().startswith('bearer ') else None


def authorization_dependency(services):
    accounts = services.accounts

    async def authorize(request: Request):
        path = request.url.path
        if not path.startswith(('/api/', '/v1/')):
            return
        if path in {'/api/v1/auth/register', '/api/v1/auth/login'}:
            return
        # These machine integrations verify their own signed, expiring identity
        # in the handler; a browser session is not their authentication method.
        if request.method == 'POST' and path in {'/api/v1/embedding/invoke', '/api/v1/connectors/callbacks'}:
            return
        token = request_token(request)
        user = await accounts.authenticate(token)
        request.state.user, request.state.auth_token = user, token
        if user['role'] == 'admin':
            return
        if path in {'/api/v1/me', '/api/v1/me/onboarding', '/api/v1/auth/logout', '/api/v1/auth/password',
                    '/api/v1/projects', '/api/v1/projects/requirement-packages/import'}:
            return

        params = request.path_params
        project_ids = set()
        if params.get('project_id'):
            project_ids.add(params['project_id'])
        application_ids = {value for value in (params.get('application_id'),
                            params.get('workflow_id'), request.query_params.get('application_id')) if value}
        try:
            if params.get('run_id'):
                run = await services.workflow_store.get_run(params['run_id'])
                application_ids.add(run['application_id'])
            if params.get('build_id'):
                build = await services.workflow_store.get_build(params['build_id'])
                application_ids.add(build['application_id'])
        except KeyError as error:
            raise HTTPException(404, '没有找到这个运行记录') from error
        for application_id in application_ids:
            owner = await services.projects.store.membership(application_id)
            if not owner:
                raise HTTPException(404, '没有找到这个项目或没有访问权限')
            project_ids.add(owner)
        roles = [await accounts.require_project(user, project_id) for project_id in project_ids]
        request.state.project_access_ids = project_ids
        # An ID under project A must not smuggle a nested resource from project B,
        # even when the requesting user is a member of both projects.
        if params.get('project_id') and project_ids != {params['project_id']}:
            raise HTTPException(404, '资源不属于当前项目')

        catalog = path.startswith('/api/v1/blocks') or path == '/api/v1/block-manuals'
        if request.method == 'GET' and catalog:
            return
        if not project_ids:
            raise HTTPException(403, '此平台功能仅对管理员开放')
        if path.endswith('/capabilities') and request.method != 'GET':
            raise HTTPException(403, '只有平台管理员可以授权完整智能体能力')
        settings_change = (path.endswith(('/agent-session', '/vision-model', '/generation-model', '/embedding-model', '/model-connection/copy'))
                           and request.method not in {'GET', 'HEAD'})
        if settings_change and any(role not in {'owner', 'admin'} for role in roles):
            raise HTTPException(403, '只有项目负责人可以修改模型连接')
        if settings_change and request.method in {'POST', 'PUT', 'PATCH'} and request.headers.get('content-type', '').startswith('application/json'):
            body = await request.json()
            if (path.endswith(('/agent-session', '/vision-model', '/generation-model')) and body.get('provider') != 'api') or body.get('executable'):
                raise HTTPException(403, '本机智能体和执行程序需由平台管理员配置')

        if params.get('project_id'):
            return
        # Ordinary users work through project-bound tasks. Legacy build/agent
        # entrypoints that use global credentials remain administrator-only.
        if '/applications/' in path:
            suffix = path.split('/applications/', 1)[1].partition('/')[2]
            if request.method == 'GET' and suffix.startswith(('versions', 'builds')):
                return
            if suffix.startswith('runs') and request.method != 'GET':
                raise HTTPException(403, '请通过项目运行入口启动工作流')
            if suffix.startswith(('workspace', 'draft', 'project', 'runs')) or not suffix:
                if suffix in {'draft/natural-language-edit', 'draft/preview-patch'}:
                    raise HTTPException(403, '请在项目工作流页面使用生成或修改功能')
                return
        if path.startswith('/api/v1/runs/'):
            if request.method == 'GET':
                return
            raise HTTPException(403, '请通过项目运行记录停止或继续任务')
        if request.method == 'GET' and path.startswith('/api/v1/builds/'):
            return
        if path.startswith('/api/v1/use/'):
            if request.method == 'GET':
                return
            raise HTTPException(403, '请从项目页面运行此工作流')
        raise HTTPException(403, '请通过项目页面操作此功能')

    return authorize


async def authorized_stream(source, request, accounts):
    """Recheck before every event/heartbeat; revoked sessions cannot keep reading."""
    try:
        async for chunk in source:
            try:
                user = await accounts.authenticate(request.state.auth_token)
                for project_id in getattr(request.state, 'project_access_ids', ()):
                    await accounts.require_project(user, project_id)
            except HTTPException:
                yield 'event: access_revoked\ndata: {"message":"登录或项目权限已失效"}\n\n'
                return
            yield chunk
    finally:
        await source.aclose()
