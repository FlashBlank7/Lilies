"""Small content-free usage counters. Collection failure never fails user work."""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .conversation_scope import conversation_scope
from .project_store import connect

log = logging.getLogger(__name__)


class ProductUsage:
    def __init__(self, services):
        self.services = services
        self.db = services.projects.store.db_path

    def initialize(self):
        with connect(self.db) as db:
            db.execute('CREATE TABLE IF NOT EXISTS product_usage ('
                       'id TEXT PRIMARY KEY,created REAL NOT NULL,user_id TEXT NOT NULL,project_id TEXT NOT NULL,'
                       'conversation_id TEXT NOT NULL,root_id TEXT NOT NULL,actor TEXT NOT NULL,feature TEXT NOT NULL,'
                       'outcome TEXT NOT NULL,seconds REAL,tokens INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS product_usage_daily ('
                       'day TEXT NOT NULL,user_id TEXT NOT NULL,actor TEXT NOT NULL,feature TEXT NOT NULL,outcome TEXT NOT NULL,'
                       'count INTEGER NOT NULL,seconds REAL NOT NULL,tokens INTEGER NOT NULL,measured INTEGER NOT NULL,'
                       'PRIMARY KEY(day,user_id,actor,feature,outcome))')
            db.execute('CREATE TABLE IF NOT EXISTS product_feedback ('
                       'user_id TEXT NOT NULL,project_id TEXT NOT NULL,conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,'
                       'helpful INTEGER NOT NULL,PRIMARY KEY(user_id,project_id,conversation_id,request_id))')
            self.purge(db)

    @staticmethod
    def purge(db):
        now = time.time()
        db.execute('DELETE FROM product_usage WHERE created<?', (now-30*86400,))
        cutoff=datetime.fromtimestamp(now-180*86400,timezone.utc).date().isoformat()
        db.execute('DELETE FROM product_usage_daily WHERE day<?',(cutoff,))

    def record(self, *, key, user_id, project_id, conversation_id='', root_id='', actor='employee', feature,
               outcome='submitted', seconds=None, tokens=None):
        try:
            now=time.time(); day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
            with connect(self.db) as db:
                db.execute('PRAGMA busy_timeout=100')
                inserted=db.execute('INSERT OR IGNORE INTO product_usage VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (key,now,user_id,project_id,conversation_id,root_id,actor,feature,outcome,seconds,tokens)).rowcount
                if not inserted:return
                db.execute('INSERT INTO product_usage_daily VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(day,user_id,actor,feature,outcome) '
                           'DO UPDATE SET count=count+1,seconds=seconds+excluded.seconds,tokens=tokens+excluded.tokens,measured=measured+excluded.measured',
                           (day,user_id,actor,feature,outcome,1,seconds or 0,tokens or 0,int(tokens is not None)))
        except Exception:
            log.warning('Usage counter unavailable', exc_info=False)

    def report(self):
        with connect(self.db) as db:
            self.purge(db)
            rows=[dict(r) for r in db.execute('SELECT actor,feature,outcome,SUM(count) AS count,SUM(seconds) AS seconds,'
                'CASE WHEN SUM(measured)>0 THEN SUM(tokens) END AS tokens,SUM(measured) AS measured '
                'FROM product_usage_daily GROUP BY actor,feature,outcome')]
            users=db.execute("SELECT COUNT(DISTINCT user_id) FROM product_usage_daily WHERE actor='employee'").fetchone()[0]
            feedback=[dict(r) for r in db.execute('SELECT helpful,COUNT(*) AS count FROM product_feedback GROUP BY helpful')]
        return {'active_users':users,'features':rows,'feedback':feedback,'raw_days':30,'summary_days':180,
                'notes':['从启用统计后开始记录；账号额度另行展示。','成功请求不等于业务结果有用。工具调用与员工任务分开计数。','token 只汇总已报告的用量；未计量部分仍是未知。']}


def classify(path, method, query):
    if method=='POST':
        if path.endswith('/draft') and '/applications/' in path:return 'edit'
        if path.endswith('/messages') and '/conversations/' in path:return 'chat'
        if path.endswith('/workflow-generation'):return 'generate'
        if path.endswith(('/materials','/materials/copy','/datasets/upload','/datasets/uploaded')):return 'upload'
        if path.endswith('/train'):return 'train'
        if path.endswith('/predict'):return 'predict'
        if '/shared-methods/' in path and path.endswith('/install'):return 'reuse'
        if path.endswith('/shared-methods'):return 'share'
        if '/official-workflows/' in path:return 'reuse'
        if path.endswith('/tasks'):return 'workflow_run'
    if method=='PUT':
        if '/skills/' in path:return 'save_method'
        if path.endswith('/draft'):return 'edit'
    if method=='GET' and (path.endswith(('/download','/delivery-package','/export')) or query.get('download') in {'true','1'}):return 'download'
    return ''


def install_usage(app, services):
    @app.middleware('http')
    async def collect(request: Request, call_next):
        began=time.perf_counter()
        response=await call_next(request)
        try:
            feature=classify(request.url.path,request.method,request.query_params)
            user=getattr(request.state,'user',None)
            if feature and user and response.status_code<400:
                params=request.path_params
                pid=params.get('project_id','')
                if not pid and params.get('application_id'):
                    pid=await services.projects.store.membership(params['application_id']) or ''
                cid=params.get('conversation_id','')
                root_id=str(uuid4())
                if pid and feature=='chat':
                    with conversation_scope(pid,'' if cid=='legacy' else cid):
                        root_id=services.local_agents.load(pid).get('request_id') or root_id
                key=f'{user["id"]}:{pid}:{cid}:{feature}:{root_id}'
                if pid:
                    await asyncio.to_thread(services.product_usage.record,key=key,user_id=user['id'],project_id=pid,
                        conversation_id=cid,root_id=root_id,feature=feature,seconds=time.perf_counter()-began)
        except Exception:
            log.warning('Usage collection unavailable', exc_info=False)
        return response

    router=APIRouter(prefix='/api/v1')

    @router.get('/admin/usage')
    async def usage():
        return await asyncio.to_thread(services.product_usage.report)

    @router.put('/projects/{project_id}/conversations/{conversation_id}/feedback/{request_id}')
    async def feedback(project_id:str,conversation_id:str,request_id:str,body:Feedback,request:Request):
        await services.project_sessions.require(project_id,conversation_id,request.state.user)
        with conversation_scope(project_id,'' if conversation_id=='legacy' else conversation_id):
            if not any(e.get('request_id')==request_id for e in services.local_agents.load(project_id)['events']):
                raise HTTPException(404,'请求不存在')
        with connect(services.projects.store.db_path) as db:
            db.execute('INSERT OR REPLACE INTO product_feedback VALUES(?,?,?,?,?)',
                       (request.state.user['id'],project_id,conversation_id,request_id,int(body.helpful)))
        return {'helpful':body.helpful}
    app.include_router(router)


class Feedback(BaseModel):
    helpful: bool
