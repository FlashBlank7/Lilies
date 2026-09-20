"""Read-only comparison data for examples/project-efficiency (never an industrial answer).

PYTHONPATH=platform/backend/src .venv/bin/python scripts/measure_project_efficiency.py \
  --url http://127.0.0.1:18011 --project PROJECT_ID --output /tmp/after.json

Uses the normal API credential; does not send agent messages or change tasks.
Measurements come from existing events. The independently checked first usable
result requires both valid and invalid trials and their real member execution.
"""
from __future__ import annotations
import argparse
import csv
import io
import json
from pathlib import Path
from datetime import datetime

import httpx
from agent_platform.config import get_settings
from agent_platform.project_metrics import payload_measurement, session_metrics

HEADERS = ['订单号', '商品', '实付金额', '优惠金额', '原价金额', '数量', '原始单价']
LINES = [
    {'order_id':'ORD-A','item':'轴套','quantity':3,'unit_price':'12.50','base':'37.50','discount':'3.75','payable':'33.75'},
    {'order_id':'ORD-A','item':'垫片','quantity':1,'unit_price':'2.675','base':'2.68','discount':'0.00','payable':'2.68'},
    {'order_id':'ORD-B','item':'接头','quantity':2,'unit_price':'3.335','base':'6.67','discount':'0.00','payable':'6.67'},
]


def check_valid(task):
    output = task['outputs']
    assert task['status'] == 'succeeded' and output['status'] == 'ok'
    assert output['lines'] == LINES, '行金额或原始输入不一致'
    assert output['totals'] == {'ORD-A':'36.43','ORD-B':'6.67'}
    assert output['errors'] == [] and output['report_path']
    assert len(task['runs']) == 3 and len({r['application_id'] for r in task['runs']}) == 3
    assert all(r['status'] == 'succeeded' for r in task['runs'])


def check_invalid(task):
    output = task['outputs']
    assert task['status'] == 'succeeded' and output['status'] == 'invalid'
    assert output['lines'] == [] and output['totals'] == {} and not output['report_path']
    assert len(output['errors']) >= 3
    assert len(task['runs']) == 2, '校验失败不应进入报价成员'


def seconds(later, earlier):
    return round((datetime.fromisoformat(later)-datetime.fromisoformat(earlier)).total_seconds(), 3)


def collect(url, project):
    with httpx.Client(base_url=url.rstrip('/')+'/api/v1/projects/'+project,
                     headers={'Authorization':'Bearer '+get_settings().api_token}, timeout=30) as client:
        def get(path):
            r=client.get(path);r.raise_for_status();return r.json()
        session = get('/agent-session')
        progress = get('/progress')
        tasks = sorted(get('/tasks?purpose=customer_trial&limit=100'), key=lambda t:t['created_at'])
        trials = [get('/tasks/'+t['id']) for t in tasks]
        initial = next(t for t in trials if not t.get('feedback_task_id') and t['outputs'].get('status')=='ok')
        invalid = next(t for t in trials if not t.get('feedback_task_id') and t['outputs'].get('status')=='invalid')
        revised = next(t for t in trials if t.get('feedback_task_id')==initial['id'] and t['outputs'].get('status')=='ok')
        check_valid(initial);check_invalid(invalid);check_valid(revised)
        csvs=[]
        for t in [initial,revised]:
            path=t['outputs']['report_path']
            r=client.post('/agent-tools',json={'name':'project_file','arguments':{'action':'read','path':path}})
            r.raise_for_status(); data=r.json()
            assert not data['truncated'], 'CSV读取不完整'
            content='\n'.join(data['lines']).lstrip('\ufeff')
            rows=list(csv.reader(io.StringIO(content)))
            assert len(rows)==4 and len(rows[0])==7
            assert all(any(ord(c)>127 for c in column) for column in rows[0]), 'CSV列名需中文'
            csvs.append({'path':path,'rows':rows,'sha256':payload_measurement(content)['sha256']})
        assert csvs[0]['path'] != csvs[1]['path'], '反馈后不能覆盖原报告'
        assert csvs[1]['rows'][0]==HEADERS
        assert csvs[1]['rows'][1:]==[[r['order_id'],r['item'],r['payable'],r['discount'],r['base'],str(r['quantity']),r['unit_price']] for r in LINES]
        old_revs={r['application_id']:r['draft_revision'] for r in initial['runs']}
        assert any(r['draft_revision']!=old_revs[r['application_id']] for r in revised['runs']), '修改应形成新草稿试用'
        assert any(i['status'] in {'waiting','paused'} and (i['blocker'] or i['questions']) for i in progress['value']['items']), '后续目标须保留具体待补条件'
        events=session['events']; presentations={}
        for e in events:
            if e['kind']=='result' and e.get('purpose')=='customer_trial':
                presentations.setdefault(e['task_id'], e)
        first_ready=max([presentations[t['id']] for t in [initial,invalid]], key=lambda e:e['time'])
        build_id=first_ready['request_id']; feedback_id=presentations[revised['id']]['request_id']
        first_user=lambda rid:next(e for e in events if e['kind']=='user' and e['request_id']==rid)
        return {'project_id':project,'url':url,'agent_version':session['version'],'model':session.get('model',''),
                'acceptance':'passed','initial_task_id':initial['id'],'invalid_task_id':invalid['id'],'revised_task_id':revised['id'],
                'build_request_id':build_id,'feedback_request_id':feedback_id,
                'first_usable_seconds':seconds(first_ready['time'],first_user(build_id)['time']),
                'feedback_usable_seconds':seconds(presentations[revised['id']]['time'],first_user(feedback_id)['time']),
                'metrics':session_metrics(events),'customer_messages':[{'request_id':e.get('request_id'),'text':e['text']} for e in events if e['kind']=='user'],
                'csvs':csvs,'trials':trials,'progress':progress,
                'notes':['首次可用按有效与无效试用均呈现、且通过同一独立业务检查计时；人工轮询等待不计入。',
                         '此脚本不替换业务结果，不写回项目。工程介入需核对customer_messages和实际操作；不从消息数量自动推断。']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--project',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();result=collect(args.url,args.project)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:result[k] for k in ['project_id','acceptance','first_usable_seconds','feedback_usable_seconds']},ensure_ascii=False))
