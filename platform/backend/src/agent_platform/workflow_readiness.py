"""Read-only resource hints. Never a draft-save gate or a provider request."""
import asyncio
import time


async def environment_ready(services, image):
    cache = getattr(services.modeling, '_readiness_images', {})
    cached = cache.get(image)
    if cached and time.monotonic() - cached[0] < 10:
        return cached[1]
    try:
        await asyncio.wait_for(services.modeling.image(image), 5)
        ready = True
    except (OSError, RuntimeError, TimeoutError):
        ready = False
    cache[image] = (time.monotonic(), ready)
    services.modeling._readiness_images = cache
    return ready


def nodes_in(value):
    if isinstance(value, dict):
        if 'type' in value and 'config' in value and 'id' in value:
            yield value
        for child in value.values():
            yield from nodes_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes_in(child)


async def readiness(services, workflow, project_id=None, seen=None, related=None):
    issues, checked = [], set()
    seen = set(seen or ())

    def missing(code, message, setup, title='', help=''):
        if code not in checked:
            checked.add(code)
            issues.append({'code': code, 'message': message, 'setup': setup, 'node': title, 'help': help})

    for node in nodes_in(workflow):
        kind, config = node['type'], node['config']
        title = node.get('title') or node['id']
        if kind in ('llm', 'knowledge_search'):
            from .providers.openai_chat import _is_loopback
            role = 'embedding' if kind == 'knowledge_search' else ('vision' if config.get('model_role') == 'vision' else 'main')
            connection = services.local_agents.connections.load(project_id, role) if project_id else None
            label = {'main': '工作流大模型', 'vision': '视觉模型', 'embedding': 'Embedding 模型'}[role]
            if not project_id or not services.local_agents.connections.enabled(project_id, role):
                missing('model:'+role, label+'尚未配置或启用。官方智能体连接不能代替工作流模型连接。', 'settings', title)
            if not services.settings.model_egress_enabled and not (connection and _is_loopback(connection.base_url)):
                missing('egress', '平台模型调用尚未启用，请管理员配置模型出口。', 'admin', title,
                        '由管理员在部署配置中启用 MODEL_EGRESS_ENABLED，按发布流程重启后端；项目仍需单独配置模型连接。')
        image = services.settings.sandbox_image if kind == 'code' else services.settings.modeling_image if kind in ('data_analysis', 'feature_extract', 'model_train', 'model_predict') else ''
        if image and 'environment:'+image not in checked and not await environment_ready(services, image):
            missing('environment:'+image, '代码执行环境尚未就绪。' if kind == 'code' else '训练与预测环境尚未就绪。', 'admin', title,
                    '请管理员启动 Docker，并检查部署镜像 '+image+'；代码环境参照 Dockerfile.sandbox 或 Dockerfile.documents，训练环境参照 Dockerfile.modeling。')
        if kind == 'model_predict':
            from .project_resources import model_resources
            ref = config.get('model_ref')
            resources = await model_resources(services, project_id) if project_id else []
            if isinstance(ref, str) and not any(r['model_ref'] == ref and r['status'] == 'ready' for r in resources):
                missing('resource:'+ref, '预测模型尚未绑定可用版本：'+ref, 'models', title)
        if kind == 'knowledge_search':
            resources = await services.projects.knowledge.list(project_id) if project_id else []
            if not any(r['knowledge_ref'] == config.get('knowledge_ref') and r['status'] == 'ready' for r in resources):
                missing('knowledge', '知识资料尚未完成索引，请在“资料与知识”添加资料并建立索引。', 'materials', title)
        target = config.get('tool_name', '')
        if not project_id and isinstance(target, str) and target.startswith('workflow:@workflow:') and target not in seen:
            seen.add(target)
            child = await readiness(services, (related or {}).get(target[19:], {}), seen=seen, related=related)
            for issue in child['issues']:
                missing(issue['code'], issue['message'], issue['setup'], issue['node'], issue['help'])
        if project_id and isinstance(target, str) and target.startswith('workflow:') and target[9:] not in seen:
            ident = target[9:]
            seen.add(ident)
            project = await services.projects.store.get(project_id)
            if ident not in {m['id'] for m in project['members']}:
                missing('workflow:'+ident, '所调用的工作流不在本项目中，请在画布中重新选择。', 'flow', title)
            else:
                draft = await services.workflow_store.get_draft(ident)
                child = await readiness(services, draft['snapshot'].workflow.model_dump(), project_id, seen)
                for issue in child['issues']:
                    missing(issue['code'], issue['message'], issue['setup'], issue['node'], issue['help'])
    return {'status': 'needs_setup' if issues else 'configured', 'issues': issues,
            'note': '这里只检查已知资源配置和本机计算环境；实际输入、依赖及连接有效性在运行时检查。创建和编辑不受影响。'}


async def project_readiness(services, project_id, workflow_id):
    project = await services.projects.store.get(project_id)
    if workflow_id not in {m['id'] for m in project['members']}:
        raise ValueError('工作流不属于当前项目')
    draft = await services.workflow_store.get_draft(workflow_id)
    result = await readiness(services, draft['snapshot'].workflow.model_dump(), project_id, {workflow_id})
    result['revision'] = draft['revision']
    return result
