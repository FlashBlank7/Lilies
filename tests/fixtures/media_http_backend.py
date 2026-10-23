"""Synthetic HTTP contracts, NOT vendor-compatible emulators. No real files deleted."""
import copy
import json
import math
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERVICES = ['emby','jellyfin','radarr','sonarr','qbittorrent','qui','smartstrm','quark',
            'bazarr','trakt','yamtrack','maintainer','nas','unifi','ddns','pt']
TEST_KEY = 'synthetic-only-credential'
NAMES = ['可清理电影','Keep影片','另一用户未看完','仍需做种','刚看完','网盘影片',
         '缺少用户记录','重复外部编号','缺少完成时间','同名电影','同名电影','恰好一天','可清理剧集']

def fixture():
    rows = {s:[] for s in SERVICES}
    for n, title in enumerate(NAMES, 1):
        tmdb = str(1000+n); path=f'/media/{n}/video.mkv'; eid=f'E-{n}'
        manager = 'sonarr' if n == 13 else 'radarr'
        users = [{'UserId':u,'Played':not(n==3 and u=='bob'),
                  'CompletedAt':None if n==9 else ('2026-10-22T23:00:00Z' if n==5 else '2026-10-22T00:00:00Z' if n==12 else '2026-10-20T00:00:00Z')}
                 for u in ['alice','bob'] if not(n==7 and u=='bob')]
        rows['emby'].append({'Id':eid,'Name':title,'ProviderIds':{'Tmdb':tmdb},'Path':path,
                             'MediaSource':'cloud' if n==6 else 'nas','Users':users})
        rows['jellyfin'].append({'item_id':f'J-{n}','tmdb_id':tmdb,'last_seen_version':1})
        record={'id':700+n,'tmdbId':int(tmdb),'title':title,'file':{'path':path},
                'tags':[9] if n==2 else [],'monitored':True,'revision':1}
        rows[manager].append(record)
        if n==8:
            rows[manager].append({**copy.deepcopy(record),'id':999,'file':{'path':'/media/duplicate/video.mkv'}})
        rows['qbittorrent'].append({'hash':f'H-{n}','content_path':path,
                                    'state':'uploading' if n==4 else 'pausedUP','seed_goal_met':n!=4})
        rows['nas'].append({'path':path,'bytes':n*1024,'etag':f'file-{n}-v1','exists':True})
        rows['bazarr'].append({'media_path':path,'missing_languages':['zh'] if n==5 else []})
        rows['trakt'].append({'ids':{'tmdb':int(tmdb)},'plays':1})
        rows['yamtrack'].append({'external_id':tmdb,'status':'completed'})
    rows['smartstrm']=[{'local_path':'/pointers/movie.strm','remote_id':'Q-6','resolvable':False}]
    rows['quark']=[{'fid':'Q-6','name':'网盘影片','size':1024}]
    rows['maintainer']=[{'tag_id':9,'tag_label':'Keep','retention_hours':24}]
    rows['pt']=[{'torrent_id':'PT-1','name':'候选资源','downloaded':False}]
    rows['qui']=[{'client':'qbittorrent','connected':True}]
    rows['unifi']=[{'wan_connected':True,'nat_configured':True}]
    rows['ddns']=[{'hostname':'example.invalid','resolved':True}]
    return rows

class Backend:
    def __init__(self):
        self.cases={}; self.audit=[]; self.lock=threading.RLock()
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.handle_request()
            def do_POST(self):self.handle_request()
            def do_DELETE(self):self.handle_request()
            def handle_request(self):
                with outer.lock:
                    parsed=urlparse(self.path); bits=parsed.path.strip('/').split('/')
                    body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
                    record={'method':self.command,'path':parsed.path,'query':parse_qs(parsed.query),'body':body}
                    status,value,headers=outer.dispatch(bits,self.command,parse_qs(parsed.query),body,self.headers)
                    record.update(status=status,response=copy.deepcopy(value)); outer.audit.append(record)
                    payload=json.dumps(value,ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type','application/json; charset=utf-8')
                    self.send_header('Content-Length',str(len(payload)))
                    for key,val in headers.items():self.send_header(key,str(val))
                    self.end_headers();self.wfile.write(payload)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def add_case(self,name,fault='', *, page_size=3, variant=False):
        self.cases[name]={'rows':fixture(),'fault':fault,'counts':Counter(),'receipts':{},'effects':[], 'page_size':page_size}
        if variant:
            rows = self.cases[name]['rows']
            for movie in rows['emby']:
                movie['MediaId'] = 'V-' + movie.pop('Id').split('-')[1]
                movie['DisplayName'] = movie.pop('Name')
                movie['ProviderIds']['Tmdb'] = str(int(movie['ProviderIds']['Tmdb']) + 10000)
            for service in ('radarr', 'sonarr'):
                for record in rows[service]:
                    record['id'] += 5000
                    record['tmdbId'] += 10000
            for record in rows['qbittorrent']:
                record['hash'] = 'NEW-' + record['hash']
            for record in rows['yamtrack']:
                record['external_id'] = str(int(record['external_id']) + 10000)

    def dispatch(self,bits,method,query,body,headers):
        if len(bits)!=3 or bits[0] not in self.cases or bits[1] not in SERVICES:
            return 404,{'error':'unknown synthetic endpoint'},{}
        case,service,action=bits; state=self.cases[case]
        if headers.get('X-Test-Key')!=TEST_KEY or state['fault']=='auth':
            return 401,{'error':'synthetic authentication rejected'},{}
        state['counts'][(service,action)]+=1
        if method=='GET' and action=='redirect':
            return 302, {}, {'Location': query.get('to', ['https://example.invalid/'])[0]}
        if method=='GET' and action=='echo':
            return 200, {'echo': headers.get('X-Test-Key')}, {'X-Echo':headers.get('X-Test-Key')}
        if method=='GET' and action=='status':
            field = {'nas':'path', 'radarr':'id', 'sonarr':'id', 'qbittorrent':'hash'}.get(service)
            if field:
                matches = [row for row in state['rows'][service] if str(row[field]) == query.get(field, [''])[0]]
                return (200, copy.deepcopy(matches[0]), {}) if len(matches)==1 else (404, {'error':'ambiguous or absent'}, {})
            return 200, {'logged': any(row.get('media_id')==query.get('media_id',[''])[0] for row in state['rows']['yamtrack'])}, {}
        if method=='GET' and action=='records':
            page=int(query.get('page',['1'])[0]); size=state['page_size']
            if service=='emby' and page==2:
                if state['fault']=='permanent_read':return 503,{'error':'synthetic outage'},{}
                if state['fault']=='transient_read' and state['counts'][('emby','fault')]==0:
                    state['counts'][('emby','fault')]+=1
                    return 503,{'error':'synthetic transient outage'},{}
            data=copy.deepcopy(state['rows'][service]); count=len(data)
            values=data[(page-1)*size:page*size]
            if service=='emby':payload={'Items':values,'TotalRecordCount':count}
            elif service in {'radarr','sonarr'}:payload={'records':values,'totalRecords':count}
            elif service=='qbittorrent':payload=values
            else:payload={'data':values,'total':count}
            if state['fault']=='schema' and service=='emby':payload={'unexpected_items':values}
            return 200,payload,{'X-Test-Pages':max(1,math.ceil(count/size)),'X-Test-Page':page}
        if method!='POST':return 405,{'error':'method not implemented in test contract'},{}
        key=headers.get('Idempotency-Key','')
        if not key:return 400,{'error':'idempotency key required'},{}
        dedupe=(service,action,key)
        if dedupe in state['receipts']:
            prior=state['receipts'][dedupe]
            if prior['body']!=body:return 409,{'error':'key reused with different body'},{}
            return 200,{**prior['result'],'replayed':True},{}
        if service=='nas' and action=='delete':
            target=next((x for x in state['rows']['nas'] if x['path']==body.get('path')),None)
            if state['fault']=='stale' and target:target['etag']='changed-after-read'
            if not target or target['etag']!=body.get('etag'):
                return 412,{'error':'file changed since inspection'},{}
            target['exists']=False
        elif service in {'radarr','sonarr'} and action=='unmonitor':
            if state['fault']=='partial' and body.get('id')==710:
                return 503,{'error':'synthetic manager outage after file deletion'},{}
            target=next((x for x in state['rows'][service] if x['id']==body.get('id')),None)
            if not target:return 404,{'error':'unknown manager id'},{}
            target['monitored']=False
        elif service=='qbittorrent' and action=='remove':
            target=next((x for x in state['rows'][service] if x['hash']==body.get('hash')),None)
            if not target:return 404,{'error':'unknown torrent'},{}
            if target['seed_goal_met'] is not True:return 409,{'error':'seeding incomplete'},{}
            target['removed']=True
        elif service=='yamtrack' and action=='log':
            state['rows'][service].append(copy.deepcopy(body))
        else:return 404,{'error':'unsupported write operation'},{}
        result={'ok':True,'service':service,'action':action,'idempotency_key':key}
        state['effects'].append({'service':service,'action':action,'body':copy.deepcopy(body),'key':key})
        state['receipts'][dedupe]={'body':copy.deepcopy(body),'result':result}
        if state['fault']=='lost_ack' and service=='nas' and state['counts'][('nas','lost')]==0:
            state['counts'][('nas','lost')]+=1
            return 503,{'error':'synthetic operation committed but acknowledgement failed'},{}
        return 200,result,{}

    def snapshot(self,name):
        s=self.cases[name]
        return {'effects':copy.deepcopy(s['effects']), 'rows':copy.deepcopy(s['rows']),
                'counts':{str(k):v for k,v in s['counts'].items()}}

    def close(self):self.server.shutdown();self.server.server_close()
