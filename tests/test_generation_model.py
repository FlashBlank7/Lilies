"""Generation connections remain project-owned and independent of business LLMs."""
import pytest
from agent_platform.model_connections import ModelConnection
from agent_platform.providers.base import ProviderError
from tests.test_projects import configured  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401


def config(model='generator', **extra):
    return dict(provider='api', model=model, base_url='https://models.test/v1',
                api_key='test-only-secret', runtime_enabled=True, **extra)


def test_independent_generation_override_inheritance_and_key_preservation(configured):
    client, app, p, _ = configured
    pid = p['id']; base = f'/api/v1/projects/{pid}'
    store = app.state.services.local_agents.connections
    assert client.get(base+'/generation-model').json()['mode'] == 'inherit'
    store.save(pid, ModelConnection(**config('business')))
    assert store.provider(pid, 'generation').connection.model == 'business'
    result = client.put(base+'/generation-model', json=config(thinking='high'))
    assert result.status_code == 200, result.text
    assert result.json()['mode'] == 'independent'
    assert 'test-only-secret' not in result.text and 'api_key' not in result.json()
    assert store.path(pid, 'generation').stat().st_mode & 0o777 == 0o600
    assert store.provider(pid).connection.model == 'business'
    assert store.provider(pid, 'generation').connection.thinking == 'high'
    store.save(pid, ModelConnection(**config('changed-business')))
    assert store.provider(pid, 'generation').connection.model == 'generator'
    changed = config('changed-generator'); changed.pop('api_key')
    assert client.put(base+'/generation-model', json=changed).status_code == 200
    assert store.load(pid, 'generation').api_key.get_secret_value() == 'test-only-secret'
    changed['runtime_enabled'] = False
    assert client.put(base+'/generation-model', json=changed).status_code == 200
    with pytest.raises(ProviderError, match='启用工作流生成模型'):
        store.provider(pid, 'generation')
    # Explicitly disabled override never falls back to the working main connection.
    assert store.provider(pid).connection.model == 'changed-business'
    assert client.delete(base+'/generation-model').json()['mode'] == 'inherit'
    assert store.provider(pid, 'generation').connection.model == 'changed-business'
    assert client.put(base+'/generation-model', json={'provider':'codex'}).status_code == 422


def test_generation_owner_collaborator_and_copy_permissions(platform):
    client, app = platform
    _, owner = signup(client, 'owner'); _, member = signup(client, 'member')
    _, outsider = signup(client, 'outsider')
    pid = project(client, owner); base = f'/api/v1/projects/{pid}'
    assert client.post(base+'/access-members', headers=owner, json={'name':'member','role':'collaborator'}).status_code == 200
    assert client.put(base+'/generation-model', headers=owner, json=config()).status_code == 200
    assert client.get(base+'/generation-model', headers=member).json()['model'] == 'generator'
    for method, body in [('put', config()), ('delete', None)]:
        kwargs = {'json':body} if body else {'headers':{**member, 'Content-Type':'application/json'}}
        if body: kwargs['headers'] = member
        assert getattr(client, method)(base+'/generation-model', **kwargs).status_code == 403
    assert client.get(base+'/generation-model', headers=outsider).status_code == 404
    source = project(client, owner)
    source_base = f'/api/v1/projects/{source}'
    assert client.put(source_base+'/generation-model', headers=owner, json=config('source-generator')).status_code == 200
    copied = client.post(base+'/model-connection/copy', headers=owner,
                         json={'source_project_id':source,'source_role':'generation','role':'generation'})
    assert copied.status_code == 200 and copied.json()['model'] == 'source-generator'
    # Cookie proxy sends JSON content type even for an empty DELETE body.
    assert client.delete(base+'/generation-model', headers={**owner,'Content-Type':'application/json'}).status_code == 200
    assert client.get(base+'/generation-model', headers=owner).json()['mode'] == 'inherit'
