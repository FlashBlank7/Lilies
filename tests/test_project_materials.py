import hashlib
from uuid import uuid4

from tests.test_projects import configured


def test_append_preserves_original_bytes_and_separate_same_name_inputs(configured):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    workspace = app.state.services.projects.workspace(project['id'])
    original = workspace / 'requirement-package' / '原始.pdf'
    original.parent.mkdir(exist_ok=True)
    original.write_bytes(b'original')
    uploaded = []
    for content in (b'first source', b'new source'):
        response = client.post(base + '/materials', files={'file': ('源码.zip', content)})
        assert response.status_code == 201, response.text
        value = response.json()
        assert value['sha256'] == hashlib.sha256(content).hexdigest()
        assert value['size'] == len(content)
        assert (workspace / value['path']).read_bytes() == content
        uploaded.append(value['path'])
    assert uploaded[0] != uploaded[1]
    assert (workspace / uploaded[0]).read_bytes() == b'first source'
    assert original.read_bytes() == b'original'
    listed = client.get(f'/api/v1/applications/{project["id"]}/workspace/files').json()
    assert set(uploaded) <= {item['path'] for item in listed}
    assert client.get(base + '/tasks').json() == []
    assert not app.state.services.local_agents.tasks


def test_rejects_partial_empty_and_symlink_uploads(configured, monkeypatch):
    client, app, project, settings = configured
    base = '/api/v1/projects/' + project['id']
    workspace = app.state.services.projects.workspace(project['id'])
    monkeypatch.setattr('agent_platform.project_materials.MAX_UPLOAD_BYTES', 4)
    for content in (b'', b'too large'):
        response = client.post(base + '/materials', files={'file': ('test.pdf', content)})
        assert response.status_code == 422
    assert list((workspace / 'requirement-package' / '追加资料').iterdir()) == []
    assert list((settings.workspace_root / '.material-uploads').iterdir()) == []
    uploads = workspace / 'requirement-package' / '追加资料'
    uploads.rmdir()
    outside = settings.data_dir / 'outside'
    outside.mkdir()
    uploads.symlink_to(outside, target_is_directory=True)
    assert client.post(base + '/materials', files={'file': ('test.pdf', b'abc')}).status_code == 422
    assert list(outside.iterdir()) == []


def test_requires_project_and_auth_and_keeps_names_inside_project(configured):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/materials', files={'file': ('../../evil.txt', b'content')})
    assert response.status_code == 201
    result = response.json()
    assert result['name'] == 'evil.txt'
    assert result['path'].startswith('requirement-package/追加资料/')
    assert '..' not in result['path']
    assert client.post('/api/v1/projects/' + str(uuid4()) + '/materials', files={'file': ('x', b'x')}).status_code == 404
    client.headers.clear()
    assert client.post(base + '/materials', files={'file': ('x', b'x')}).status_code == 401


def test_copy_material_preserves_bytes_and_same_name_versions(configured):
    client, app, project, settings = configured
    source_id = client.post('/api/v1/projects', json={'name': 'source'}).json()['id']
    source_root = settings.workspace_root / source_id / 'requirement-package'
    source_root.mkdir()
    copies = []
    for version, content in [('a', '每隔500毫秒'), ('b', '每隔1000毫秒')]:
        source = source_root / version / '设计.md'
        source.parent.mkdir(); source.write_text(content)
        response = client.post('/api/v1/projects/' + project['id'] + '/materials/copy', json={
            'source_project_id': source_id, 'source_path': f'requirement-package/{version}/设计.md'})
        assert response.status_code == 201, response.text
        result = response.json(); copies.append(result['path'])
        assert result['name'] == '设计.md'
        assert result['size'] == len(content.encode())
        assert result['sha256'] == hashlib.sha256(content.encode()).hexdigest()
        assert (settings.workspace_root / project['id'] / result['path']).read_bytes() == source.read_bytes()
    assert copies[0] != copies[1]
    assert (settings.workspace_root / project['id'] / copies[0]).read_text() == '每隔500毫秒'
    listed = client.get(f'/api/v1/applications/{project["id"]}/workspace/files').json()
    assert set(copies) <= {item['path'] for item in listed}
    assert client.get('/api/v1/projects/' + project['id'] + '/tasks').json() == []
    assert not app.state.services.local_agents.tasks


def test_copy_rejects_non_material_paths_links_and_partial_files(configured, monkeypatch):
    client, app, project, settings = configured
    source_id = client.post('/api/v1/projects', json={'name': 'source'}).json()['id']
    source_root = settings.workspace_root / source_id / 'requirement-package'
    source_root.mkdir(); (source_root / 'large.zip').write_bytes(b'too large')
    (source_root / 'empty.zip').touch()
    (source_root / 'link.zip').symlink_to(source_root / 'large.zip')
    (source_root / 'alias').symlink_to(source_root, target_is_directory=True)
    monkeypatch.setattr('agent_platform.project_materials.MAX_UPLOAD_BYTES', 4)
    base = '/api/v1/projects/' + project['id'] + '/materials/copy'
    for path in ['results/report.pdf', '/requirement-package/large.zip',
                 'requirement-package/../secret', 'requirement-package\\large.zip',
                 'requirement-package/link.zip', 'requirement-package/alias/large.zip',
                 'requirement-package/large.zip', 'requirement-package/empty.zip',
                 'requirement-package/missing.zip', 'requirement-package']:
        response = client.post(base, json={'source_project_id': source_id, 'source_path': path})
        assert response.status_code == 422, (path, response.text)
    assert list((settings.workspace_root / project['id'] / 'requirement-package' / '追加资料').iterdir()) == []
    assert list((settings.workspace_root / '.material-uploads').iterdir()) == []
    assert client.post(base, json={'source_project_id': str(uuid4()), 'source_path': 'requirement-package/x'}).status_code == 404
    client.headers.clear()
    assert client.post(base, json={'source_project_id': source_id, 'source_path': 'requirement-package/x'}).status_code == 401
