"""Visitor limits across an authenticated deployment ingress, with no model calls."""
import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings

SECRET = 'proxy-test-key-not-a-real-secret-12345'


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path/'data', workspace_root=tmp_path/'workspaces',
                              api_token='', model_egress_enabled=False, scheduler_poll_seconds=3600,
                              auth_proxy_secret=SECRET, auth_register_per_hour=1,
                              auth_login_failures_per_15m=1))
    with TestClient(app) as client:
        yield client


def ingress(address, key=SECRET):
    return {'x-lilies-client-ip': address, 'x-lilies-proxy-key': key}


def register(client, name, headers):
    return client.post('/api/v1/auth/register', json={'name': name, 'password': 'test-password'}, headers=headers)


def test_independent_registration_limits_and_canonical_addresses(client):
    assert register(client, 'first', ingress('192.0.2.10')).status_code == 201
    blocked = register(client, 'first-again', ingress('::ffff:192.0.2.10'))
    assert blocked.status_code == 429
    assert int(blocked.headers['retry-after']) > 0
    assert register(client, 'second', ingress('192.0.2.11')).status_code == 201
    assert register(client, 'ipv6', ingress('2001:db8::a')).status_code == 201
    assert register(client, 'ipv6-again', ingress('2001:db8:0:0:0:0:0:a')).status_code == 429


@pytest.mark.parametrize('key', ['', 'incorrect-key', 'x'*len(SECRET)])
def test_forged_headers_cannot_reset_the_direct_client_limit(client, key):
    assert register(client, 'first', ingress('192.0.2.10', key)).status_code == 201
    forged = ingress('192.0.2.11', key)
    forged.update({'x-forwarded-for': '192.0.2.12', 'x-real-ip': '192.0.2.13'})
    assert register(client, 'second', forged).status_code == 429


@pytest.mark.parametrize('address', ['192.0.2.1, 192.0.2.2', 'not-an-ip', 'fe80::1%eth0'])
def test_invalid_proxy_address_uses_direct_limit(client, address):
    assert register(client, 'first', ingress(address)).status_code == 201
    assert register(client, 'second', {}).status_code == 429


def test_login_limits_are_independent_and_success_clears_only_own_limit(client):
    assert register(client, 'account', ingress('192.0.2.10')).status_code == 201
    def login(address, password):
        return client.post('/api/v1/auth/login', json={'name':'account','password':password}, headers=ingress(address))
    assert login('192.0.2.10', 'incorrect').status_code == 401
    assert login('192.0.2.10', 'test-password').status_code == 429
    assert login('192.0.2.11', 'test-password').status_code == 200
    assert login('192.0.2.11', 'test-password').status_code == 200
    assert login('192.0.2.10', 'test-password').status_code == 429
    # Knowing the proxy key is not an account credential.
    assert client.get('/api/v1/projects', headers=ingress('192.0.2.11')).status_code == 401


def test_no_proxy_configuration_never_trusts_forwarded_identity(tmp_path):
    app = create_app(Settings(data_dir=tmp_path/'data',workspace_root=tmp_path/'workspaces',
                              api_token='',auth_proxy_secret='',auth_register_per_hour=1,
                              model_egress_enabled=False,scheduler_poll_seconds=3600))
    with TestClient(app) as client:
        assert register(client, 'first', ingress('192.0.2.10')).status_code == 201
        assert register(client, 'second', ingress('192.0.2.11')).status_code == 429
