"""E1:RAG + file_read 集成——工作流读取真实文档文件 → 索引 → 检索 → 生成式回答。"""
import sys, time, os, json
sys.path.insert(0, '/home/jiangzhijun/Lilies/platform/backend/src')
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient
from dotenv import load_dotenv
load_dotenv('/home/jiangzhijun/Lilies/.env')
os.environ['MODEL_EGRESS_ENABLED'] = 'true'
from agent_platform.api import create_app
from agent_platform.config import Settings
H = {'Authorization': 'Bearer workflow-test'}

DOCS = [
    {"source_id": "leave-policy", "title": "休假政策", "revision": "v1", "url": "policies/leave",
     "content": "全职员工每年可累积 20 天年假,请假须至少提前 5 个工作日申请。"},
    {"source_id": "travel-policy", "title": "差旅政策", "revision": "v1", "url": "policies/travel",
     "content": "差旅须提前审批,经济舱标准,住宿每日上限 800 元。"},
    {"source_id": "benefits-policy", "title": "福利政策", "revision": "v1", "url": "policies/benefits",
     "content": "员工享有年度体检、补充医疗保险与弹性工作制。"},
    {"source_id": "remote-work", "title": "远程办公", "revision": "v1", "url": "policies/remote",
     "content": "每周最多 3 天远程办公,需团队经理批准并保持每周例会参与。"},
    {"source_id": "security-policy", "title": "信息安全", "revision": "v1", "url": "policies/security",
     "content": "所有外部设备须经 IT 审批,数据加密存储,禁止明文口令。"},
]

def main():
    tmp = TemporaryDirectory()
    s = Settings(api_token='workflow-test', data_dir=Path(tmp.name)/'data',
                 workspace_root=Path(tmp.name)/'ws', model_egress_enabled=True)
    s.prepare()
    app = create_app(s)
    with TestClient(app) as c:
        # 在 workspace 写入真实文档文件
        ws = Path(tmp.name)/'ws'
        ws.mkdir(parents=True, exist_ok=True)
        (ws/'documents.json').write_text(json.dumps(DOCS, ensure_ascii=False), encoding='utf-8')
        aid = c.post('/api/v1/applications', headers=H, json={'name':'file-rag','requirement':'rag'}).json()['id']
        rev = c.get(f'/api/v1/applications/{aid}/draft', headers=H).json()['revision']
        def mu(op, data):
            nonlocal rev
            r = c.post(f'/api/v1/applications/{aid}/draft', headers=H, json={
                'expected_revision': rev, 'idempotency_key': f'k{op}{rev}', 'op': op, 'data': data})
            assert r.status_code == 200, r.text
            rev = r.json()['revision']
        template = ("你是企业政策助手。基于以下检索材料回答,并在用到某段时用 [n] 标注来源。\n\n"
                    "{{passages}}\n\n问题: {{query}}\n请给出有依据的回答,末尾列出引用来源。")
        nodes = [
            {'id': 'start', 'type': 'start', 'title': 's', 'config': {'inputs': [
                {'name': 'query', 'type': 'string'},
                {'name': 'deleted_source_ids', 'type': 'array'},
                {'name': 'event_id', 'type': 'string'},
                {'name': 'principal_roles', 'type': 'array'}]}},
            {'id': 'read', 'type': 'file_read', 'title': '读取文档文件', 'config': {
                'path': 'documents.json', 'format': 'json'}},
            {'id': 'sync', 'type': 'knowledge_index_sync', 'title': '索引', 'config': {
                'index_name': 'policy-handbook',
                'documents': {'$ref': {'node_id': 'read', 'path': ['records']}},
                'deleted_source_ids': {'$ref': {'node_id': 'start', 'path': ['deleted_source_ids']}},
                'event_id': {'$ref': {'node_id': 'start', 'path': ['event_id']}},
                'replace': True}},
            {'id': 'retrieve', 'type': 'knowledge_retrieval', 'title': '检索', 'config': {
                'index_name': 'policy-handbook',
                'query': {'$ref': {'node_id': 'start', 'path': ['query']}},
                'principal_roles': {'$ref': {'node_id': 'start', 'path': ['principal_roles']}},
                'top_k': 3, 'minimum_score': 0.01}},
            {'id': 'build', 'type': 'template_transform', 'title': '组装', 'config': {
                'template': template,
                'variables': {
                    'passages': {'$ref': {'node_id': 'retrieve', 'path': ['results']}},
                    'query': {'$ref': {'node_id': 'start', 'path': ['query']}}}}},
            {'id': 'answer', 'type': 'llm', 'title': '生成回答', 'config': {
                'system': '你是严谨的企业政策顾问,必须基于给定材料回答并标注引用。',
                'prompt': {'$ref': {'node_id': 'build', 'path': ['text']}}}},
            {'id': 'end', 'type': 'end', 'title': 'e', 'config': {'outputs': {
                'answer': {'$ref': {'node_id': 'answer', 'path': ['text']}},
                'file_sha': {'$ref': {'node_id': 'read', 'path': ['sha256']}}}}},
        ]
        for n in nodes:
            mu('add_node', {'node': n})
        for i, (src, tgt, sp, tp) in enumerate([
            ('start', 'read', 'output', 'input'), ('read', 'sync', 'records', 'input'),
            ('sync', 'retrieve', 'output', 'input'), ('retrieve', 'build', 'output', 'input'),
            ('build', 'answer', 'text', 'input'), ('answer', 'end', 'text', 'input')]):
            mu('add_edge', {'edge': {'id': f'e{i}', 'source': src, 'target': tgt,
                                     'source_port': sp, 'target_port': tp}})
        inputs = {'query': '员工每年有多少天年假?如何申请?', 'deleted_source_ids': [],
                  'event_id': 'file-rag-sync-0001', 'principal_roles': ['employee']}
        created = c.post(f'/api/v1/applications/{aid}/runs', headers=H, json={
            'inputs': inputs, 'use_draft': True, 'workspace_path': str(ws)})
        rid = created.json()['run_id']
        for _ in range(400):
            run = c.get(f'/api/v1/runs/{rid}', headers=H).json()
            if run['status'] in ('succeeded', 'failed'):
                break
            time.sleep(0.5)
        print('status:', run['status'])
        if run['status'] == 'failed':
            print('error:', str(run.get('error'))[:300])
        else:
            ans = run.get('outputs', {}).get('answer', '')
            sha = run.get('outputs', {}).get('file_sha', '')
            print('file_read 读取文件 sha256:', sha[:16], '...')
            print('生成式回答:', ans[:400])
            has_cite = ('[1]' in ans or '休假政策' in ans or '来源' in ans)
            ok = ('20' in ans) and has_cite and sha
            print(f'E1 file_read+RAG 集成: {"✅ 通过" if ok else "⚠️ 部分"}')
    tmp.cleanup()

main()
