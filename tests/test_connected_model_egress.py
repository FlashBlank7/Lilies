"""Project models use the same default-off remote egress switch as global models."""
from uuid import uuid4

import httpx
import pytest

from agent_platform.config import Settings
from agent_platform.connected_model import ConnectedModel
from agent_platform.agent_runtime_factory import build_agent_runtime_core
from agent_platform.model_connections import ModelConnection, ModelConnections
from agent_platform.providers.base import ProviderError
from tests.test_model_connections import complete


def connection(url='https://example.test/v1', protocol='openai'):
    return ModelConnection(provider='api', protocol=protocol, model='example', base_url=url,
                           api_key='test-only-key', runtime_enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
async def test_disabled_project_connection_does_not_send_any_request(tmp_path, protocol):
    requests = []
    model = ConnectedModel(connection(protocol=protocol), tmp_path,
                           transport=httpx.MockTransport(lambda r: requests.append(r)), egress_enabled=False)
    with pytest.raises(ProviderError, match='模型出口已关闭'):
        await complete(model)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['main', 'vision', 'generation'])
async def test_saved_connection_roles_cannot_bypass_disabled_egress(tmp_path, monkeypatch, role):
    store = ModelConnections(tmp_path, egress_enabled=False)
    project_id = str(uuid4())
    store.save(project_id, connection(), role=role)
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled project provider opened an HTTP client')
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    with pytest.raises(ProviderError, match='模型出口已关闭'):
        await complete(store.provider(project_id, role=role))


@pytest.mark.asyncio
@pytest.mark.parametrize('url,enabled', [('http://127.0.0.1:9000/v1',False), ('http://[::1]:9000/v1',False), ('https://example.test/v1',True)])
async def test_explicitly_enabled_remote_and_local_models_keep_working(tmp_path, url, enabled):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]})
    result = await complete(ConnectedModel(connection(url), tmp_path, egress_enabled=enabled,
                                          transport=httpx.MockTransport(respond)))
    assert requests and result.blocks[0].text == 'ok'


def test_server_settings_are_explicit_for_runtime_project_models(tmp_path, monkeypatch):
    # A caller can disable the server even if an inherited shell enables egress.
    monkeypatch.setenv('MODEL_EGRESS_ENABLED','true')
    core = build_agent_runtime_core(Settings(data_dir=tmp_path/'data', workspace_root=tmp_path/'workspace', model_egress_enabled=False))
    project_id = str(uuid4())
    core.provider.connections.save(project_id, connection())
    assert core.provider.connections.provider(project_id).egress_enabled is False


def test_direct_construction_respects_default_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('MODEL_EGRESS_ENABLED',raising=False)
    monkeypatch.delenv('LILIES_MODEL_EGRESS_ENABLED',raising=False)
    assert ConnectedModel(connection(),tmp_path).egress_enabled is False
    monkeypatch.setenv('MODEL_EGRESS_ENABLED','true')
    assert ConnectedModel(connection(),tmp_path).egress_enabled is True
    monkeypatch.setenv('LILIES_MODEL_EGRESS_ENABLED','false')
    assert ConnectedModel(connection(),tmp_path).egress_enabled is False
