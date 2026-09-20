import json
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.requirement_discussion import material_context
from tests.test_requirement_package import package
from tests.test_v04_00_ai_requirement_intake import IntakeProvider


def question_response():
    return {"status": "needs_input", "confidence": .6,
            "detected_goal": "我理解你希望按日整理库存。",
            "reasoning_summary": "需求.md与数据/input.csv提供了初步资料，但统计周期需要确认。",
            "questions": [{"id": "period", "question": "按什么周期整理？", "label": "统计周期",
                           "options": [{"id": "day", "label": "每天"}, {"id": "month", "label": "每月"}]}]}


def test_material_analysis_corrections_confirmation_and_build_handoff(tmp_path: Path):
    provider = IntakeProvider(question_response())
    settings = Settings(api_token="test", data_dir=tmp_path / "data", workspace_root=tmp_path / "ws",
                        model_egress_enabled=False, scheduler_poll_seconds=3600)
    app = create_app(settings, provider)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test"
        imported = c.post('/api/v1/requirement-packages/import', files={'file': ('project.zip', package())}).json()
        app_id = imported['application']['id']
        base = '/api/v1/applications/' + app_id
        workspace = settings.workspace_root / app_id
        initial = c.get(base + '/requirements').json()
        assert initial['enabled'] and initial['status'] == 'not_started'
        assert initial['turns'] == [] and initial['document'] == '' and provider.calls == []
        blocked = c.post(base + '/builds', json={'requirement': '不应把企业原文直接送去搭建工作流'})
        assert blocked.status_code == 409 and provider.calls == []

        response = c.post(base + '/requirements/messages', json={'revision': 0})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['status'] == 'discussing' and state['revision'] == 1
        prompt = json.loads(provider.calls[0]['message'])
        materials = {f['path']: f for f in prompt['materials']['files']}
        assert '请读取数据' in materials['requirement-package/需求.md']['content']
        assert '棒号' in materials['requirement-package/数据/input.csv']['content']
        assert '400' in materials['requirement-package/数据/input.csv']['content']
        assert not (workspace / 'requirements/requirements.md').exists()
        assert c.post(base + '/requirements/confirm', json={'revision': 1}).status_code == 409

        document = '# 库存需求\n为仓库负责人按月整理输入库存，输出月度库存表。'
        provider.payload = {'status': 'ready', 'confidence': .9, 'detected_goal': '按月整理库存',
                            'reasoning_summary': '已按企业回复将日度改成月度。', 'completed_requirement': document}
        response = c.post(base + '/requirements/messages', json={'revision': 1, 'message': '你理解错了，我们要按月整理。'})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['status'] == 'review' and state['document'] == document
        prompt = json.loads(provider.calls[1]['message'])
        assert prompt['conversation'][0]['analysis']['detected_goal'] == '我理解你希望按日整理库存。'
        assert prompt['conversation'][-1]['user'] == '你理解错了，我们要按月整理。'
        assert any('按月' in answer['legacy_answer'] for answer in prompt['prior_answers'])
        # Neither a model-generated draft nor a refresh counts as owner confirmation.
        assert c.get(base + '/requirements').json() == state
        assert c.get(base).json()['requirement'] == imported['application']['requirement']
        assert c.post(base + '/builds', json={'requirement': document}).status_code == 409
        assert c.post(base + '/requirements/confirm', json={'revision': 1}).status_code == 409

        confirmed = c.post(base + '/requirements/confirm', json={'revision': 2})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()['status'] == 'confirmed'
        assert (workspace / 'requirements/requirements.md').read_text().strip() == document
        assert c.get(base + '/workspace/files/requirements/requirements.md').text.strip() == document
        saved = c.get(base).json()['requirement']
        assert saved.startswith(document) and 'requirement-package/' in saved
        assert c.get(base + '/draft').json()['snapshot']['workflow']['nodes'] == []
        assert c.get(base + '/builds').json() == [] and len(provider.calls) == 2
        assert c.post(base + '/requirements/confirm', json={'revision': 2}).status_code == 200
        # Do not run a Builder/model in this test; verify that its persisted input is the confirmed document.
        app.state.services.builders.get('classic').start = lambda _build_id: None
        started = c.post(base + '/builds', json={'requirement': saved, 'auto_publish': False})
        assert started.status_code == 202, started.text
        build = c.get('/api/v1/builds/' + started.json()['build_id']).json()
        assert build['requirement'] == saved

    # Conversation and document survive a fresh server, not just component state.
    with TestClient(create_app(settings, provider)) as c:
        c.headers['Authorization'] = 'Bearer test'
        state = c.get(base + '/requirements').json()
        assert state['status'] == 'confirmed' and len(state['turns']) == 2


def test_failed_analysis_and_stale_reply_preserve_conversation(tmp_path):
    provider = IntakeProvider(question_response())
    settings = Settings(api_token='test', data_dir=tmp_path/'data', workspace_root=tmp_path/'ws', model_egress_enabled=False)
    with TestClient(create_app(settings, provider)) as c:
        c.headers['Authorization'] = 'Bearer test'
        imported = c.post('/api/v1/requirement-packages/import', files={'file': ('project.zip', package())}).json()
        base = '/api/v1/applications/' + imported['application']['id'] + '/requirements'
        c.post(base + '/messages', json={'revision': 0})
        previous = c.get(base).json()
        assert c.post(base + '/messages', json={'revision': 0, 'message': '过期回复'}).status_code == 409
        assert len(provider.calls) == 1
        provider.payload = {'status': 'ready', 'confidence': .8, 'completed_requirement': ''}
        failed = c.post(base + '/messages', json={'revision': 1, 'message': '按月整理'})
        assert failed.status_code == 502
        assert c.get(base).json() == previous


def test_preview_keeps_raw_values_and_marks_what_has_not_been_read(tmp_path):
    root = tmp_path/'requirement-package'
    root.mkdir()
    (root/'数据.csv').write_text('棒号,氧含量\nA,8.1\nB,\n')
    (root/'输入.json').write_text(json.dumps({'records': [{'id': i} for i in range(10)]}))
    (root/'长文.txt').write_text('原文' * 20000)
    (root/'图.pdf').write_bytes(b'pdf')
    outside = tmp_path/'private.txt'
    outside.write_text('not package data')
    (root/'link.txt').symlink_to(outside)
    files = {f['path']:f for f in material_context(tmp_path)['files']}
    assert '8.1' in files['requirement-package/数据.csv']['content']
    assert '氧含量' in files['requirement-package/数据.csv']['content']
    assert '前5行' in files['requirement-package/数据.csv']['notice']
    assert json.loads(files['requirement-package/输入.json']['content'])['records']['record_count'] == 10
    assert files['requirement-package/长文.txt']['truncated'] is True
    assert '未解析' in files['requirement-package/图.pdf']['notice']
    assert 'requirement-package/link.txt' not in files
