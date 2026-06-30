#!/usr/bin/env python3
"""Use Lilies to generate a deployment automation agent and produce all-in-one script."""
import json, sys, time
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, 'platform/backend/src')
from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

tmp = TemporaryDirectory(); tp = Path(tmp.name)
s = Settings(api_token='test-token-2024', data_dir=tp/'data', workspace_root=tp/'workspaces')
s.prepare(); (tp/'workspaces').mkdir(parents=True, exist_ok=True)
ws = tp/'workspaces'/'deploy'
ws.mkdir(parents=True, exist_ok=True)
for p in [ws]+list(ws.rglob('*')): p.chmod(0o777) if p.exists() else None

app = create_app(settings=s)
H = {'Authorization': 'Bearer test-token-2024'}

with TestClient(app) as c:
    print('Generating deployment automation Agent via Lilies Factory...')
    print()

    deploy_req = (
        'Generate an Android deployment agent. It writes self-contained shell scripts '
        'for Termux that automate DingTalk check-in via input tap and crontab. '
        'The agent produces complete, working shell scripts. Keep it concise.'
    )

    for attempt in range(2):
        gr = c.post('/v1/agent-generations', headers=H, json={
            'requirement': deploy_req + (' Keep short.' if attempt > 0 else ''),
            'workspace_path': str(ws), 'auto_publish': True
        })
        gid = gr.json()['generation_id']
        print(f'  Generation: {gid[:12]}... (attempt {attempt+1})')
        for i in range(240):
            g = c.get(f'/v1/agent-generations/{gid}', headers=H).json()
            st = g.get('status','?')
            if i % 45 == 0: print(f'    [{i}s] {st}')
            if st in ('published','failed'): break
            time.sleep(1)
        g = c.get(f'/v1/agent-generations/{gid}', headers=H).json()
        if g.get('status') == 'published':
            break

    agent_id = g.get('agent_id')
    status = g.get('status','?')
    print(f'\n  Agent: {status}')

    if agent_id and status == 'published':
        a = c.get(f'/v1/agents/{agent_id}', headers=H).json()
        spec = a.get('spec',{})
        print(f'  Name: {spec.get("name","?")}')
        print(f'  Tools: {spec.get("tools",[])}')
        print(f'  Prompt: {len(spec.get("system_prompt",""))} chars')

        # Run agent to produce the script
        print(f'\n  Running agent to produce all-in-one deployment script...')
        sr = c.post('/v1/sessions', headers=H, json={
            'agent_id': agent_id, 'workspace_path': str(ws)
        })
        if sr.status_code != 201:
            print(f'  Session failed: {sr.text[:200]}')
            sys.exit(1)

        sid = sr.json()['session_id']
        task = (
            'Write a SINGLE self-contained shell script called all_in_one.sh in the current '
            'workspace. The script does EVERYTHING for DingTalk auto check-in on Android Termux: '
            '1) pkg install cronie termux-api  '
            '2) Create crontab entries: 45 8 * * 1-5 for checkin, 0 19 * * 1-5 for checkout '
            '3) Include a calibration wizard that asks for tap coordinates '
            '4) Include test mode '
            '5) Start cron service '
            '6) Print clear status at each step '
            'Write the script NOW. Use the Write tool.'
        )
        c.post(f'/v1/sessions/{sid}/messages', headers=H, json={'content': task})

        approved = set()
        tools_used = []
        for _ in range(200):
            time.sleep(0.3)
            sess = c.get(f'/v1/sessions/{sid}', headers=H).json()
            for e in c.get(f'/v1/streams/{sid}', headers=H).json():
                t = e.get('type',''); d = e.get('data',{})
                if t == 'permission.requested':
                    rid = d.get('request_id','')
                    if rid and rid not in approved:
                        c.post(f'/v1/sessions/{sid}/permissions/{rid}', headers=H, json={'behavior':'allow'})
                        approved.add(rid)
                elif t == 'tool.started':
                    tn = d.get('tool','')
                    if tn not in tools_used: tools_used.append(tn)
                    if len(tools_used) <= 5: print(f'    [{tn}]')
            if sess['status'] in ('ready','error'): break

        sess = c.get(f'/v1/sessions/{sid}', headers=H).json()
        print(f'\n  Session: {sess["status"]} | Tools: {tools_used}')

        # Extract script
        script = ''
        if (ws/'all_in_one.sh').exists():
            script = (ws/'all_in_one.sh').read_text()

        if not script:
            for m in reversed(sess.get('messages',[])):
                if m.get('role') == 'assistant':
                    answer = ''.join(b.get('text','') for b in m.get('content',[]) if b.get('type')=='text')
                    if answer:
                        if '```' in answer:
                            parts = answer.split('```')
                            script = parts[1] if len(parts) > 1 else answer
                            if script.startswith('bash\n'): script = script[5:]
                            elif script.startswith('sh\n'): script = script[3:]
                        else:
                            script = answer
                        break

        if script:
            out = Path('/home/jiangzhijun/Lilies/mobile_app/automation/all_in_one.sh')
            out.write_text(script)
            out.chmod(0o755)
            lines = len(script.split('\n'))
            print(f'\n  Generated: all_in_one.sh ({len(script)} chars, {lines} lines)')
        else:
            print('\n  No script produced by agent')
    else:
        err = (g or {}).get('error','') or ''
        print(f'  Failed: {err[:200]}')

try: tmp.cleanup()
except: pass
