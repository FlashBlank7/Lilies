"""Project modeling orchestration, immutable inputs and one bounded CPU worker."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from .db import connect
from .models import utc_now
from .modeling_models import DatasetRequest, StudyRequest, CandidateRequest, FeaturePlan, AideSearch
from .modeling_search import choose_step
from .project_store import ProjectConflict, encode
from .workspace_copy import copy_workspace_file
from .training_notes import make_note, render_note


class Modeling:
    def __init__(self, services):
        self.services = services
        self.db_path = services.storage.db_path
        self.root = services.settings.data_dir.resolve() / 'modeling'
        self.slots = asyncio.Semaphore(1)
        self.locks: dict[str, asyncio.Lock] = {}
        self.containers: set[str] = set()
        self.owner_label = hashlib.sha256(str(self.root).encode()).hexdigest()[:20]

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS modeling_objects (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                kind TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '', request_key TEXT NOT NULL,
                document TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id,kind,parent_id,request_key));
                CREATE INDEX IF NOT EXISTS modeling_project ON modeling_objects(project_id,kind,updated_at);
            ''')
            for row in c.execute("SELECT * FROM modeling_objects WHERE kind IN ('study','candidate')").fetchall():
                doc = json.loads(row['document'])
                if doc.get('status') in ('running', 'queued') or (row['kind'] == 'study' and doc.get('active_since')):
                    doc.update(status='interrupted', error='服务重启；请继续原项目任务')
                    if row['kind'] == 'study':
                        # Persisted usage is authoritative; downtime is not work.
                        doc['active_since'] = None
                    c.execute('UPDATE modeling_objects SET document=?,updated_at=? WHERE id=?', (encode(doc), utc_now(), row['id']))
        # A previous API process may have died while Docker was still computing.
        # Scope cleanup to this data directory, never another development server.
        if shutil.which('docker'):
            try:
                process = await asyncio.create_subprocess_exec('docker', 'ps', '-aq', '--filter', 'label=lilies.modeling.owner=' + self.owner_label, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                output, _ = await asyncio.wait_for(process.communicate(), 3)
                for name in output.decode().split():
                    await self.remove_container(name)
            except (OSError, TimeoutError):
                if 'process' in locals() and process.returncode is None:
                    process.kill(); await process.wait()

    async def close(self):
        for name in list(self.containers):
            await self.remove_container(name)
        with connect(self.db_path) as c:
            rows = c.execute("SELECT project_id,document FROM modeling_objects WHERE kind='study'").fetchall()
        for row in rows:
            study = json.loads(row['document'])
            if study.get('active_since'):
                self.pause_clock(study)
                study.update(status='interrupted', next_action='继续已保存的研究，已完成实验保留')
                await self.put(row['project_id'], 'study', study)

    def path(self, project_id, object_id):
        # Both ids must already have been resolved through scoped DB reads.
        return self.root / project_id / object_id

    def feature_cache(self, project_id, dataset, features, image):
        key = hashlib.sha256(encode({'dataset': dataset['files'], 'mapping': dataset['mapping'], 'features': features, 'image': image}).encode()).hexdigest()
        return self.path(project_id, dataset['id']) / 'feature-cache' / (key + '.joblib')

    async def get(self, project_id, kind, object_id):
        def read():
            with connect(self.db_path) as c:
                row = c.execute('SELECT document FROM modeling_objects WHERE project_id=? AND kind=? AND id=?', (project_id, kind, object_id)).fetchone()
                if not row:
                    raise KeyError('没有找到当前项目的建模资料')
                return json.loads(row['document'])
        return await asyncio.to_thread(read)

    async def list(self, project_id, kind, parent_id='', offset=0, limit=50, summary=False, after=''):
        await self.services.projects.store.get(project_id)
        def read():
            with connect(self.db_path) as c:
                rows = c.execute('SELECT document FROM modeling_objects WHERE project_id=? AND kind=? AND (?=\'\' OR parent_id=?) AND updated_at>? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                                 (project_id, kind, parent_id, parent_id, after, min(limit, 100), offset)).fetchall()
                values = [json.loads(r['document']) for r in rows]
                if summary:
                    for value in values:
                        for key in ('request', 'profile', 'preview', 'features_result'):
                            value.pop(key, None)
                        if value.get('split'):
                            value['split'] = {k: v for k, v in value['split'].items() if k in {'missing_labels', 'evaluation_label'}}
                        for trial in value.get('trials', []):
                            for key in ('prediction_preview', 'group_errors', 'importance', 'fold_metrics', 'feature_columns', 'effective_model', 'search', 'requested_parameters'):
                                trial.pop(key, None)
                return values
        return await asyncio.to_thread(read)

    async def put(self, project_id, kind, doc):
        if kind == 'study' and doc.get('active_since'):
            doc['used_seconds'] = self.elapsed(doc)
            doc['active_since'] = time.time()
        doc['updated_at'] = utc_now()
        def write():
            with connect(self.db_path) as c:
                c.execute('INSERT INTO modeling_objects VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document,updated_at=excluded.updated_at',
                          (doc['id'], project_id, kind, doc.get('study_id', ''), doc.get('request_key', doc['id']), encode(doc), doc['updated_at']))
        await asyncio.to_thread(write)
        return doc

    async def link_task(self, project_id, candidate_id, task_id):
        await self.services.projects.store.get_task(project_id, task_id)
        def update():
            with connect(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute("SELECT document FROM modeling_objects WHERE project_id=? AND kind='candidate' AND id=?",
                                (project_id, candidate_id)).fetchone()
                if not row:
                    raise KeyError('没有找到当前项目的候选')
                doc = json.loads(row['document'])
                if doc.get('task_id') and doc['task_id'] != task_id:
                    raise ProjectConflict('候选已绑定其他项目任务，请继续原任务')
                doc.update(task_id=task_id, updated_at=utc_now())
                c.execute('UPDATE modeling_objects SET document=?,updated_at=? WHERE id=?',
                          (encode(doc), doc['updated_at'], candidate_id))
        await asyncio.to_thread(update)

    async def training_note(self, project_id, study_id, candidate_id, slot):
        study = await self.get(project_id, 'study', study_id)
        candidate = await self.get(project_id, 'candidate', candidate_id)
        if candidate['study_id'] != study_id:
            raise KeyError('候选不属于当前研究')
        trial = next((t for t in candidate['trials'] if t['slot'] == slot), None)
        if trial is None:
            raise KeyError('试验记录不存在或尚未结束')
        dataset = await self.get(project_id, 'dataset', study['dataset_id'])
        candidates, offset = [], 0
        while True:
            page = await self.list(project_id, 'candidate', study_id, offset, 100)
            candidates.extend(page)
            if len(page) < 100:
                break
            offset += len(page)
        def read():
            folder = self.path(project_id, candidate_id) / 'output' / f'trial-{slot}'
            files = sorted(p.name for p in folder.iterdir() if p.is_file() and not p.is_symlink()) if folder.exists() else []
            split_path = self.path(project_id, study_id) / 'output/split.json'
            split = json.loads(split_path.read_text()) if split_path.is_file() else None
            descriptions = self.path(project_id, candidate_id) / 'output/features.json'
            if descriptions.is_file():
                candidate['features_result'] = json.loads(descriptions.read_text())
            return make_note(project_id, dataset, study, candidate, trial, split, candidates, files)
        return await asyncio.to_thread(read)

    async def save_training_note(self, project_id, study_id, candidate_id, slot):
        note = await self.training_note(project_id, study_id, candidate_id, slot)
        def write():
            folder = self.path(project_id, candidate_id) / 'output' / f'trial-{slot}'
            folder.mkdir(parents=True, exist_ok=True)
            for name, content in [('training-note.json', encode(note)), ('training-note.md', render_note(note))]:
                temp = folder / (name + '.' + str(uuid4()) + '.tmp')
                temp.write_text(content)
                temp.replace(folder / name)
        await asyncio.to_thread(write)

    async def duplicate(self, project_id, kind, parent_id, request):
        with connect(self.db_path) as c:
            row = c.execute('SELECT document FROM modeling_objects WHERE project_id=? AND kind=? AND parent_id=? AND request_key=?', (project_id, kind, parent_id, request['request_key'])).fetchone()
        if row:
            doc = json.loads(row['document'])
            saved_request = dict(doc['request'])
            if kind == 'candidate':
                saved_request.setdefault('search_space', {})
                saved_request.setdefault('autogluon_hyperparameters', None)
            if kind == 'study':
                saved_request.setdefault('search_strategy', 'codex')
                saved_request.setdefault('aide', AideSearch().model_dump())
            if saved_request != request:
                raise ProjectConflict('同一请求标识的内容已改变；请使用新的请求标识')
            return doc

    def source(self, project_id, relative):
        root = self.services.projects.workspace(project_id)
        path = root / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or not Path(relative).parts or Path(relative).parts[0] not in {'requirement-package', 'solution', 'results'}:
            raise ValueError('数据／特征代码只能来自当前项目 requirement-package、solution 或 results')
        if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            # Checking parents outside root is unnecessary and can reject a symlinked home.
            if path.resolve() != path.absolute():
                raise ValueError('不支持符号链接输入')
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError('文件不存在或不属于当前项目')
        return path

    async def dataset(self, project_id, body: DatasetRequest, upload: bytes | None = None):
        await self.services.projects.store.get(project_id)
        if body.replaces_id:
            await self.get(project_id, 'dataset', body.replaces_id)
        ident = str(uuid4()); folder = self.path(project_id, ident)
        folder.mkdir(parents=True)
        doc = {'id': ident, 'name': body.name or Path(body.source_path).name, 'mapping': body.mapping.model_dump(), 'replaces_id': body.replaces_id,
               'created_at': utc_now(), 'status': 'registered', 'files': {}, 'profile': None}
        try:
            for key, relative in [('source', body.source_path), ('labels', body.labels_path)]:
                if not relative:
                    continue
                suffix = Path(relative).suffix.lower()
                if suffix not in {'.csv', '.tsv', '.xlsx'}:
                    raise ValueError('支持 CSV、TSV、XLSX 数据文件')
                dest = folder / (key + suffix)
                if key == 'source' and upload is not None:
                    dest.write_bytes(upload)
                else:
                    await asyncio.to_thread(copy_workspace_file, self.source(project_id, relative), dest)
                doc['files'][key] = {'name': dest.name, 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest(), 'bytes': dest.stat().st_size, 'original': relative}
            return await self.put(project_id, 'dataset', doc)
        except BaseException:
            shutil.rmtree(folder)
            raise

    async def image(self):
        image = self.services.settings.modeling_image
        process = await asyncio.create_subprocess_exec('docker', 'image', 'inspect', '--format', '{{.Id}}', image, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            process.kill(); await process.wait(); raise
        if process.returncode:
            raise RuntimeError(f'建模环境 {image} 尚未就绪，请维护方执行 docker build -f Dockerfile.modeling -t {image} .；{err.decode()[-300:]}')
        return out.decode().strip()

    async def snapshot_environment(self, image):
        folder = self.root / '_environments' / image.replace(':', '-')
        if (folder / 'worker.py').exists():
            return folder
        script = "import json,pathlib,importlib.metadata,platform; print(json.dumps({'worker':pathlib.Path('/opt/modeling/worker.py').read_text(),'requirements':'\\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in importlib.metadata.distributions())),'python':platform.python_version(),'machine':platform.machine()}))"
        process = await asyncio.create_subprocess_exec('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', image, '-c', script,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            process.kill(); await process.wait(); raise
        if process.returncode:
            raise RuntimeError('无法固定建模环境：' + error.decode()[-500:])
        metadata = json.loads(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'requirements.lock').write_text(metadata.pop('requirements'))
        worker = metadata.pop('worker')
        metadata.update(image=image, worker_sha256=hashlib.sha256(worker.encode()).hexdigest())
        (folder / 'environment.json').write_text(encode(metadata))
        (folder / 'worker.py').write_text(worker)
        return folder

    async def remove_container(self, name):
        process = await asyncio.create_subprocess_exec('docker', 'rm', '-f', name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        self.containers.discard(name)

    async def compute(self, project_id, dataset, config, folder, *, image='', on_event=None, timeout=1800, extra_mounts=None, writable_mounts=None):
        """Only this transport knows Docker; tests may use a real local worker instead."""
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / 'output'; output.mkdir(exist_ok=True)
        data = self.path(project_id, dataset['id'])
        config = {**config, 'mapping': dataset['mapping'], 'output': '/output',
                  **{k: '/data/' + v['name'] for k, v in dataset['files'].items()}}
        config_path = folder / 'job.json'
        config_path.write_text(encode(config))
        name = 'lilies-modeling-' + str(uuid4())
        deadline = time.monotonic() + timeout
        if on_event and self.slots.locked():
            await on_event({'kind': 'queued'})
        await asyncio.wait_for(self.slots.acquire(), timeout)
        try:
            if on_event:
                await on_event({'kind': 'compute_started'})
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError('等待计算资源时已用尽预算')
            if config.get('remaining_seconds'):
                config['remaining_seconds'] = min(config['remaining_seconds'], timeout)
                config_path.write_text(encode(config))
            self.containers.add(name)
            command = ['docker', 'run', '--rm', '--name', name, '--label', 'lilies.modeling.owner=' + self.owner_label, '--network', 'none', '--cpus', '4', '--memory', '4g', '--memory-swap', '4g', '--pids-limit', '128',
                       '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}', '--read-only', '--tmpfs', '/tmp:rw,size=512m',
                       '-e', 'HOME=/tmp', '-v', f'{data}:/data:ro', '-v', f'{config_path}:/job.json:ro', '-v', f'{output}:/output:rw']
            for host, target in extra_mounts or []:
                command += ['-v', f'{host}:{target}:ro']
            for host, target in writable_mounts or []:
                command += ['-v', f'{host}:{target}:rw']
            command += [image or await self.image(), '/job.json']
            process = None
            try:
                process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024)
                # Drain stderr throughout training so library logs cannot block progress.
                async def errors():
                    tail = b''
                    while chunk := await process.stderr.read(8192):
                        tail = (tail + chunk)[-8000:]
                    return tail.decode(errors='replace')
                stderr = asyncio.create_task(errors())
                result = None; failure = ''
                async with asyncio.timeout(timeout):
                    while line := await process.stdout.readline():
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if event.get('kind') == 'result':
                            result = event['result']
                            result['runtime'] = {k: event[k] for k in ('seconds', 'peak_memory_bytes')}
                        elif event.get('kind') == 'error':
                            failure = event['error']
                        if on_event:
                            await on_event(event)
                    await process.wait()
                log = await stderr
                if process.returncode or result is None:
                    raise RuntimeError(failure or log[-1200:] or '建模计算未返回结果')
                return result
            finally:
                await asyncio.shield(self.remove_container(name))
                if process and process.returncode is None:
                    process.kill(); await process.wait()
                if 'stderr' in locals() and not stderr.done():
                    stderr.cancel()
        finally:
            self.slots.release()

    async def profile(self, project_id, dataset_id, sampled=False):
        dataset = await self.get(project_id, 'dataset', dataset_id)
        async with self.locks.setdefault(dataset_id, asyncio.Lock()):
            key = 'preview' if sampled else 'profile'
            if dataset.get(key):
                return dataset[key]
            result = await self.compute(project_id, dataset, {'action': 'profile', 'sampled': sampled}, self.path(project_id, dataset_id) / key)
            dataset[key] = result; dataset['status'] = 'profiled' if not sampled else 'previewed'
            await self.put(project_id, 'dataset', dataset)
            return result

    @staticmethod
    def elapsed(study):
        return study.get('used_seconds', 0) + (time.time() - study['active_since'] if study.get('active_since') else 0)

    @classmethod
    def pause_clock(cls, study):
        study['used_seconds'] = cls.elapsed(study)
        study['active_since'] = None

    async def study(self, project_id, body: StudyRequest):
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            request = body.model_dump()
            if previous := await self.duplicate(project_id, 'study', '', request):
                return previous
            dataset = await self.get(project_id, 'dataset', body.dataset_id)
            if body.parent_study_id:
                await self.get(project_id, 'study', body.parent_study_id)
            if body.item_id:
                await self.services.projects.conversation.item(project_id, body.item_id)
            ident = str(uuid4())
            doc = {**request, 'id': ident, 'request': request, 'status': 'ready', 'created_at': utc_now(), 'image': await self.image(),
                   'active_since': time.time(), 'used_seconds': (dataset.get('profile') or {}).get('runtime', {}).get('seconds', 0),
                   'trials_used': 0, 'no_improvement_batches': 0, 'best': None, 'baseline': None, 'next_action': '提交首批模型方案', 'error': ''}
            self.path(project_id, ident).mkdir(parents=True)
            await self.snapshot_environment(doc['image'])
            return await self.put(project_id, 'study', doc)

    async def next_step(self, project_id, study_id):
        study = await self.get(project_id, 'study', study_id)
        if study.get('search_strategy', 'codex') != 'aide':
            raise ValueError('next_step 用于 AIDE 策略研究；当前研究由 Codex 自主选择方案')
        if study['status'] in {'finished', 'budget_exhausted', 'target_reached', 'sealed', 'interrupted'}:
            return {'stage': 'stop', 'status': study['status'], 'instructions': study['next_action']}
        if self.elapsed(study) >= study['budget']['seconds'] or study['trials_used'] >= study['budget']['trials']:
            return {'stage': 'stop', 'status': 'budget_exhausted', 'instructions': '预算已用尽，结束搜索并保留最佳方案'}
        candidates, offset = [], 0
        while True:
            page = await self.list(project_id, 'candidate', study_id, offset, 100)
            candidates.extend(page)
            if len(page) < 100:
                break
            offset += len(page)
        pending = [c for c in candidates if c['status'] in {'ready', 'running', 'queued', 'interrupted'}]
        if pending:
            candidate = min(pending, key=lambda c: (c['created_at'], c['id']))
            return {'stage': 'run', 'candidate_id': candidate['id'], 'task_id': candidate.get('task_id', ''),
                    'status': candidate['status'], 'instructions': '先运行或继续此候选的原任务，完成后再选择下一方案'}
        return choose_step(study, candidates)

    async def candidate(self, project_id, study_id, body: CandidateRequest, *, submission=None):
        async with self.locks.setdefault(study_id, asyncio.Lock()):
            request = body.model_dump()
            if previous := await self.duplicate(project_id, 'candidate', study_id, request):
                if submission is not None:
                    saved = previous.get('submission')
                    if saved and any(encode(saved[k]) != encode(submission[k]) for k in ('workflow_id', 'inputs')):
                        raise ProjectConflict('此候选已用于不同工作流或输入；请使用新的候选请求标识')
                    if not saved:
                        if previous.get('task_id'):
                            raise ProjectConflict('候选已绑定任务，请继续原任务，不要重新提交运行')
                        previous['submission'] = submission
                        await self.put(project_id, 'candidate', previous)
                return previous
            study = await self.get(project_id, 'study', study_id)
            if study['status'] in {'finished', 'budget_exhausted', 'target_reached', 'sealed'}:
                raise ProjectConflict('本次研究已结束；调整预算后继续或创建关联的新研究')
            decision = None
            if study.get('search_strategy') == 'aide':
                decision = await self.next_step(project_id, study_id)
                if decision['stage'] not in {'draft', 'debug', 'improve'}:
                    raise ProjectConflict(decision['instructions'])
                if body.batch_size != 1 or len(body.models) != 1:
                    raise ValueError('AIDE 每个候选对应一次方案评价，使用 batch_size=1 和一个 models 条目')
                if body.parent_id != decision['parent_id']:
                    raise ProjectConflict('请使用 next_step 返回的 parent_id；搜索历史可能已更新')
            if body.parent_id:
                parent = await self.get(project_id, 'candidate', body.parent_id)
                if parent['study_id'] != study_id:
                    raise ValueError('候选父项不属于当前研究')
            if body.feedback_task_id:
                await self.services.projects.store.get_task(project_id, body.feedback_task_id)
            if body.engine == 'autogluon' and (body.features.transformer_path or body.features.select_k):
                raise ValueError('AutoGluon 对照当前接受相同原始特征方案；不支持自定义拟合转换或 select_k')
            dataset = await self.get(project_id, 'dataset', study['dataset_id'])
            gap = study['evaluation'].get('gap_seconds', 0)
            if dataset['mapping']['kind'] == 'timeseries' and study['evaluation']['split'] == 'time' and gap:
                if body.features.window_seconds is None or body.features.window_seconds > gap:
                    raise ValueError('时序候选的窗口不得超过研究已固定的时间隔离间隔；窗口变大需创建新研究')
            ident = str(uuid4()); folder = self.path(project_id, ident); folder.mkdir(parents=True)
            doc = {**request, 'id': ident, 'study_id': study_id, 'request': request, 'status': 'ready', 'trials': [], 'created_at': utc_now(), 'task_id': '', 'run_id': '', 'error': '', 'image': study['image']}
            if submission is not None:
                doc['submission'] = submission
            if decision:
                doc['search_decision'] = {k: v for k, v in decision.items() if k != 'history'}
            if body.features.transformer_path:
                source = self.source(project_id, body.features.transformer_path)
                if source.suffix != '.py':
                    raise ValueError('自定义特征转换必须是 Python 文件')
                copy_workspace_file(source, folder / 'transformer.py')
                doc['code_sha256'] = hashlib.sha256((folder / 'transformer.py').read_bytes()).hexdigest()
            return await self.put(project_id, 'candidate', doc)

    async def run_candidate(self, project_id, study_id, candidate_id, task_id, run_id, *, pause_when_done=False):
        async with self.locks.setdefault(study_id, asyncio.Lock()):
            study = await self.get(project_id, 'study', study_id)
            candidate = await self.get(project_id, 'candidate', candidate_id)
            if candidate['study_id'] != study_id:
                raise ValueError('候选不属于此研究')
            if candidate['status'] == 'completed':
                return candidate
            if study['status'] in {'sealed', 'finished', 'target_reached'}:
                raise ProjectConflict('本次研究已结束；保留当前最佳结果，不执行剩余候选')
            if candidate['task_id'] and candidate['task_id'] != task_id:
                raise ProjectConflict('候选已绑定其他项目任务，请继续原任务或提交新候选')
            if candidate['trials'] and len(candidate['trials']) >= candidate.get('scheduled_slots', candidate['batch_size']):
                # The process may have stopped after the final trial commit but
                # before the batch result. No remaining budget is needed to finish.
                candidate.update(status='completed', current=None, error='')
                features = self.path(project_id, candidate_id) / 'output/features.json'
                if features.exists():
                    candidate['features_result'] = json.loads(features.read_text())
                self.finish_batch(study, candidate)
                if pause_when_done:
                    self.pause_clock(study)
                await self.put(project_id, 'study', study)
                return await self.put(project_id, 'candidate', candidate)
            dataset = await self.get(project_id, 'dataset', study['dataset_id'])
            remaining = study['budget']['seconds'] - self.elapsed(study)
            slots = study['budget']['trials'] - study['trials_used'] + len(candidate['trials'])
            if remaining <= 0 or slots <= len(candidate['trials']):
                study.update(status='budget_exhausted', next_action='已保存最佳方案；可增加预算或生成预测流程')
                self.pause_clock(study); await self.put(project_id, 'study', study)
                raise ValueError('本次实验预算已用尽，已完成结果保留')
            study.update(status='running', active_since=study.get('active_since') or time.time(), next_action='运行候选并保存每次结果', error='')
            candidate.setdefault('starting_best', study['best'])
            candidate.setdefault('scheduled_slots', min(slots, candidate['batch_size']))
            candidate.update(status='running', task_id=task_id, run_id=run_id, error='')
            await self.put(project_id, 'study', study); await self.put(project_id, 'candidate', candidate)
            folder = self.path(project_id, candidate_id)
            study_folder = self.path(project_id, study_id)
            split_path = study_folder / 'output' / 'split.json'
            async def event(event):
                if event['kind'] in {'queued', 'compute_started'}:
                    candidate['status'] = 'queued' if event['kind'] == 'queued' else 'running'
                elif event['kind'] == 'trial_started':
                    candidate['current'] = event
                elif event['kind'] == 'trial':
                    trial = {k: v for k, v in event.items() if k not in {'kind', 'replayed'}}
                    if any(t['slot'] == trial['slot'] for t in candidate['trials']):
                        return
                    trial.update(task_id=task_id, run_id=run_id, recorded_at=utc_now())
                    trial['note_url'] = f'/api/v1/projects/{project_id}/modeling/studies/{study_id}/candidates/{candidate_id}/trials/{trial["slot"]}/note'
                    candidate['trials'].append(trial)
                    study['trials_used'] += 1
                    if trial['status'] == 'completed':
                        metric = study['evaluation']['metric']; score = trial['metrics'][metric]
                        study['baseline'] = trial['baseline']
                        best = study['best']
                        improve = best is None or (score < best['score'] if metric in {'mae', 'rmse'} else score > best['score'])
                        if improve:
                            study['best'] = {'candidate_id': candidate_id, 'slot': trial['slot'], 'score': score, 'model': trial['model'], 'metrics': trial['metrics']}
                    await self.put(project_id, 'study', study)
                await self.put(project_id, 'candidate', candidate)
                if event['kind'] == 'trial':
                    await self.save_training_note(project_id, study_id, candidate_id, event['slot'])
            try:
                if not split_path.exists():
                    study['split'] = await self.compute(project_id, dataset, {'action': 'prepare', 'evaluation': study['evaluation']}, study_folder, image=study['image'], timeout=remaining)
                    await self.put(project_id, 'study', study)
                config = {**candidate['request'], 'action': 'train', 'evaluation': study['evaluation'], 'split': '/split.json',
                          'remaining_seconds': max(1, study['budget']['seconds'] - self.elapsed(study)), 'trial_seconds': study['budget']['trial_seconds'], 'batch_size': min(slots, candidate['batch_size'])}
                mounts = [(split_path, '/split.json')]
                cache_path = self.feature_cache(project_id, dataset, candidate['features'], study['image'])
                if cache_path.exists():
                    config['feature_cache'] = '/feature-cache.joblib'; mounts.append((cache_path, '/feature-cache.joblib'))
                if candidate.get('code_sha256'):
                    config['transformer'] = '/transformer.py'; mounts.append((folder / 'transformer.py', '/transformer.py'))
                search_config = {'features': candidate['features'], 'code': candidate.get('code_sha256'), 'models': candidate['models'], 'parameters': candidate['parameters'], 'engine': candidate['engine']}
                if candidate.get('search_space'):
                    search_config['search_space'] = candidate['search_space']
                search_key = hashlib.sha256(encode(search_config).encode()).hexdigest()
                search = study_folder / 'parameter-search' / search_key
                search.mkdir(parents=True, exist_ok=True)
                config['optuna_storage'] = '/search/optuna.db'
                result = await self.compute(project_id, dataset, config, folder, image=study['image'], on_event=event, timeout=config['remaining_seconds'], extra_mounts=mounts, writable_mounts=[(search, '/search')])
                if (folder / 'output/features.joblib').exists() and not cache_path.exists():
                    cache_path.parent.mkdir(exist_ok=True)
                    copy_workspace_file(folder / 'output/features.joblib', cache_path)
                candidate.update(status=result['status'], features_result=result['features'], runtime=result.get('runtime'), current=None)
                if candidate['status'] == 'completed':
                    self.finish_batch(study, candidate)
                    if pause_when_done:
                        self.pause_clock(study)
                else:
                    study.update(status='budget_exhausted', next_action='预算已用尽，已完成试验保留；增加预算后继续原任务')
                    self.pause_clock(study)
            except BaseException as error:
                candidate.update(status='interrupted' if isinstance(error, (asyncio.CancelledError, TimeoutError)) else 'failed', error=str(error) or '用户停止或服务中断', current=None)
                study.update(status='interrupted', error=candidate['error'], next_action=(
                    '任务已中断；等待用户要求继续原任务，已完成试验不会重跑'
                    if isinstance(error, asyncio.CancelledError) else '继续原项目任务，已完成试验不会重跑'))
                self.pause_clock(study)
                raise
            finally:
                await asyncio.shield(self.put(project_id, 'candidate', candidate))
                await asyncio.shield(self.put(project_id, 'study', study))
            return candidate

    def finish_batch(self, study, candidate):
        good = [trial for trial in candidate['trials'] if trial['status'] == 'completed']
        errors = [trial.get('error', '训练失败') for trial in candidate['trials'] if trial['status'] == 'failed']
        only_failed = bool(errors) and not good
        if not candidate.get('batch_accounted'):
            if good:
                study['no_improvement_batches'] = 0 if study['best'] != candidate.get('starting_best') else study['no_improvement_batches'] + 1
                study['failure_streak'] = {'error': '', 'count': 0}
                study.pop('repair_candidate_id', None)
            elif only_failed:
                previous = study.get('failure_streak', {})
                study['failure_streak'] = {'error': errors[-1], 'count': previous.get('count', 0) + 1 if previous.get('error') == errors[-1] else 1}
                study['repair_candidate_id'] = candidate['id']
            candidate['batch_accounted'] = True
        study.update(status='ready', error='', next_action='读取分组误差，提出一项具体特征或模型改动，再提交下一批候选')
        if only_failed:
            study.update(next_action='本批训练未成功，不计入效果无改善次数；读取错误，提交以本候选为 parent_id 的修复候选，已完成试验保留。')
        target = study['evaluation'].get('target_score')
        if target is not None and study['best'] and (study['best']['score'] <= target if study['evaluation']['metric'] in {'mae', 'rmse'} else study['best']['score'] >= target):
            study.update(status='target_reached', next_action='验证指标已达到目标；生成预测流程，独立测试尚未执行')
        elif study['trials_used'] >= study['budget']['trials'] or self.elapsed(study) >= study['budget']['seconds']:
            study.update(status='budget_exhausted', next_action='预算已用尽；交付最佳方案或明确下一步改进')
        elif only_failed and study.get('failure_streak', {}).get('count', 0) >= 3:
            study.update(status='interrupted', error=errors[-1], next_action='连续三批出现同一训练错误，已暂停此研究；保留最佳模型和修复候选关联，修复后继续，其他事项可继续。')
        elif study['no_improvement_batches'] >= study['budget']['patience']:
            study.update(status='finished', next_action='连续多批没有改善；保留最佳模型，说明差距和后续方案')
        if study['status'] != 'ready':
            self.pause_clock(study)

    async def run_block(self, project, kind, args, run_id, node_id=''):
        project_id = project['project_id']
        if kind == 'model_predict' and not args.get('model_ref') and not (args.get('study_id') and args.get('candidate_id')):
            raise ValueError('模型预测节点尚未选择模型，请配置模型引用后重新运行')
        if kind == 'model_predict' and args.get('model_ref'):
            from .project_resources import binding_from_context
            binding = binding_from_context(project, args['model_ref'])
            args = {**args, **{k: binding[k] for k in ('study_id', 'candidate_id', 'slot')}}
        if project.get('record_scope') and kind == 'model_train':
            if args.get('finalize'):
                study = await self.get(project_id, 'study', args['study_id'])
                if study.get('test_result'):
                    return study['test_result']
            else:
                candidate = await self.get(project_id, 'candidate', args['candidate_id'])
                if candidate['study_id'] != args['study_id']:
                    raise ValueError('候选不属于此研究')
                if candidate['status'] == 'completed':
                    return candidate
            raise ValueError('请先在研究任务中完成候选训练或最终评价；保存用例只回放已固定实验，并可真实运行预测')
        if kind == 'data_analysis':
            return {'dataset_id': args['dataset_id'], **await self.profile(project_id, args['dataset_id'])}
        if kind == 'model_train':
            if args.get('finalize'):
                return await self.finalize(project_id, args['study_id'], run_id)
            study = await self.get(project_id, 'study', args['study_id'])
            if args.get('dataset_id') and args['dataset_id'] != study['dataset_id']:
                raise ValueError('上游数据集与研究固定版本不一致，请创建新研究')
            if args.get('features'):
                candidate = await self.get(project_id, 'candidate', args['candidate_id'])
                if FeaturePlan.model_validate(args['features']).model_dump() != candidate['features']:
                    raise ValueError('上游特征方案已改变，请提交关联新候选后运行')
            return await self.run_candidate(project_id, args['study_id'], args['candidate_id'], project['task_id'], run_id)
        dataset = await self.get(project_id, 'dataset', args['dataset_id'])
        folder = self.path(project_id, dataset['id']) / ('run-' + run_id + '-' + hashlib.sha256(node_id.encode()).hexdigest()[:10])
        if kind == 'feature_extract':
            plan = FeaturePlan.model_validate(args['features'])
            image = await self.image()
            cache = self.feature_cache(project_id, dataset, plan.model_dump(), image)
            mounts = [(cache, '/feature-cache.joblib')] if cache.exists() else []
            config = {'action': 'features', 'features': plan.model_dump()}
            if mounts:
                config['feature_cache'] = '/feature-cache.joblib'
            result = await self.compute(project_id, dataset, config, folder, image=image, extra_mounts=mounts)
            if not cache.exists():
                cache.parent.mkdir(exist_ok=True)
                copy_workspace_file(folder / 'output/features.joblib', cache)
            return {'dataset_id': dataset['id'], 'feature_plan': plan.model_dump(), **result}
        study = await self.get(project_id, 'study', args['study_id'])
        candidate = await self.get(project_id, 'candidate', args['candidate_id'])
        if candidate['study_id'] != study['id']:
            raise ValueError('候选不属于当前研究')
        good = [t for t in candidate['trials'] if t['status'] == 'completed']
        if not good:
            raise ValueError('候选没有可用模型')
        metric = study['evaluation']['metric']
        best = (next((t for t in good if t['slot'] == args['slot']), None) if args.get('slot') is not None
                else sorted(good, key=lambda t: t['metrics'][metric], reverse=metric not in {'mae', 'rmse'})[0])
        if best is None:
            raise ValueError('指定模型版本不可用，请重新绑定')
        original = await self.get(project_id, 'dataset', study['dataset_id'])
        # Only file location changes at inference; preserve trained field semantics.
        dataset = {**dataset, 'mapping': original['mapping']}
        model = self.path(project_id, candidate['id']) / 'output' / f'trial-{best["slot"]}'
        config = {'action': 'predict', 'features': candidate['features'], 'engine': candidate['engine'], 'model': '/model', 'feature_columns': best['feature_columns']}
        mounts = [(model, '/model')]
        if candidate.get('code_sha256'):
            config['transformer'] = '/transformer.py'; mounts.append((model.parent.parent / 'transformer.py', '/transformer.py'))
        result = await self.compute(project_id, dataset, config, folder, image=candidate['image'], extra_mounts=mounts)
        result['model_version'] = {'study_id': study['id'], 'candidate_id': candidate['id'], 'slot': best['slot'], 'image': candidate['image']}
        result['artifact'] = f'datasets/{dataset["id"]}/files/{folder.name}/output/predictions.csv'
        return result

    async def budget(self, project_id, study_id, budget):
        async with self.locks.setdefault(study_id, asyncio.Lock()):
            study = await self.get(project_id, 'study', study_id)
            if study['status'] == 'sealed':
                raise ProjectConflict('保留测试结果已查看，此研究不再继续搜索；请创建关联的新研究')
            study.update(budget=budget.model_dump(), status='ready', active_since=study.get('active_since') or time.time(), next_action='按调整后的预算继续实验')
            return await self.put(project_id, 'study', study)

    async def finish(self, project_id, study_id, reason):
        lock = self.locks.setdefault(study_id, asyncio.Lock())
        if lock.locked():
            raise ProjectConflict('研究仍在处理；请先等待运行结束或停止关联任务')
        async with lock:
            study = await self.get(project_id, 'study', study_id)
            if study['status'] == 'sealed' or (study['status'] == 'finished' and not study.get('active_since')):
                return study
            if study['status'] in {'running', 'queued'}:
                raise ProjectConflict('研究仍在计算；请先等待运行结束或停止关联任务')
            self.pause_clock(study)
            study.update(status='finished', stop_reason=reason, next_action=reason + '。已保存试验和模型；需要继续时调整预算，沿用原评价。')
            return await self.put(project_id, 'study', study)

    async def finalize(self, project_id, study_id, run_id):
        async with self.locks.setdefault(study_id, asyncio.Lock()):
            study = await self.get(project_id, 'study', study_id)
            if study.get('test_result'):
                return study['test_result']
            if not study.get('best') or not study.get('split', {}).get('holdout'):
                raise ValueError('需要已完成模型和未使用的保留测试样本')
            candidate = await self.get(project_id, 'candidate', study['best']['candidate_id'])
            dataset = await self.get(project_id, 'dataset', study['dataset_id'])
            trial = next(t for t in candidate['trials'] if t['slot'] == study['best']['slot'])
            folder = self.path(project_id, candidate['id'])
            config = {'action': 'holdout', 'features': candidate['features'], 'evaluation': study['evaluation'], 'engine': candidate['engine'],
                      'feature_columns': trial['feature_columns'], 'classes': trial['classes'], 'model': '/model', 'split': '/split.json'}
            mounts = [(folder / 'output' / f'trial-{trial["slot"]}', '/model'), (self.path(project_id, study_id) / 'output/split.json', '/split.json')]
            if candidate.get('code_sha256'):
                config['transformer'] = '/transformer.py'; mounts.append((folder / 'transformer.py', '/transformer.py'))
            # Seal before exposing test outcomes, including failed attempts.
            study.update(status='sealed', next_action='使用固定最佳模型预测；保留测试集已用于最终评价，不能再参与搜索')
            self.pause_clock(study); await self.put(project_id, 'study', study)
            result = await self.compute(project_id, dataset, config, self.path(project_id, study_id) / 'holdout', image=study['image'], extra_mounts=mounts)
            study.update(test_result=result, final_run_id=run_id)
            await self.put(project_id, 'study', study)
            await self.save_training_note(project_id, study_id, candidate['id'], trial['slot'])
            return result

    async def revise_dataset(self, project_id, dataset_id, mapping):
        previous = await self.get(project_id, 'dataset', dataset_id)
        ident = str(uuid4()); folder = self.path(project_id, ident); folder.mkdir(parents=True)
        for value in previous['files'].values():
            copy_workspace_file(self.path(project_id, dataset_id) / value['name'], folder / value['name'])
        doc = {**previous, 'id': ident, 'mapping': mapping.model_dump(), 'replaces_id': dataset_id, 'profile': None, 'preview': None, 'status': 'registered', 'created_at': utc_now()}
        return await self.put(project_id, 'dataset', doc)

    async def export_dataset(self, project_id, dataset_id):
        """Expose a verified independent copy to existing project file workflows."""
        dataset = await self.get(project_id, 'dataset', dataset_id)
        root = self.services.projects.workspace(project_id)
        relative = Path('results/datasets') / dataset['id']
        destination = root / relative

        def export():
            # No caller-selected destination and no traversal through symlinks.
            for path in (root, root / 'results', root / 'results/datasets', destination):
                if path.is_symlink():
                    raise ValueError('数据导出路径不能包含符号链接')
            destination.parent.mkdir(parents=True, exist_ok=True)
            files = {key: {**value, 'path': (relative / value['name']).as_posix()}
                     for key, value in dataset['files'].items()}

            def matches(path, metadata):
                return (not path.is_symlink() and path.is_file()
                        and path.stat().st_size == metadata['bytes']
                        and hashlib.sha256(path.read_bytes()).hexdigest() == metadata['sha256'])

            if destination.exists():
                if not all(matches(root / value['path'], value) for value in files.values()):
                    raise ProjectConflict('已有数据导出副本被修改或不完整；请移走该副本后重试，原始数据未改动')
            else:
                temporary = destination.parent / ('.export-' + str(uuid4()))
                temporary.mkdir()
                try:
                    for value in files.values():
                        source = self.path(project_id, dataset_id) / value['name']
                        if (any(p.is_symlink() for p in (self.root / project_id, source.parent, source))
                                or not matches(source, value)):
                            raise ValueError('原始数据文件与登记的散列不一致，不能导出')
                        copy_workspace_file(source, temporary / value['name'])
                        if not matches(temporary / value['name'], value):
                            raise ValueError('数据导出校验失败，请重试')
                    temporary.rename(destination)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            return {'dataset_id': dataset_id, 'files': files,
                    'source_path': files['source']['path'],
                    'labels_path': files.get('labels', {}).get('path', '')}

        async with self.locks.setdefault('export/' + dataset_id, asyncio.Lock()):
            return await asyncio.to_thread(export)

    async def tool(self, project_id, args):
        from .modeling_summary import dataset_summary, study_summary, candidate_summary, profile_summary, note_summary, detail
        value = await self._tool(project_id, args)
        if args.view == 'full':
            return value
        if args.action in {'datasets', 'register_dataset', 'revise_dataset'}:
            summarize = dataset_summary
        elif args.action in {'studies', 'create_study', 'read_study', 'budget', 'finish'}:
            summarize = study_summary
        elif args.action in {'submit_candidate', 'candidates'}:
            summarize = candidate_summary
        elif args.action == 'training_note':
            summarize = note_summary
        elif args.action == 'profile':
            return {**profile_summary(value), 'detail': detail('profile', dataset_id=args.dataset_id, sampled=args.sampled)}
        else:
            return value
        return [summarize(v) for v in value] if isinstance(value, list) else summarize(value)

    async def _tool(self, project_id, args):
        action = args.action
        if action == 'datasets':
            return await self.list(project_id, 'dataset', offset=args.offset, limit=args.limit)
        if action == 'register_dataset':
            if args.dataset is None:
                raise ValueError('需要 dataset 配置')
            return await self.dataset(project_id, args.dataset)
        if action == 'revise_dataset':
            if args.mapping is None:
                raise ValueError('需要完整 mapping 配置')
            return await self.revise_dataset(project_id, args.dataset_id, args.mapping)
        if action == 'export_dataset':
            return await self.export_dataset(project_id, args.dataset_id)
        if action == 'profile':
            return await self.profile(project_id, args.dataset_id, args.sampled)
        if action == 'studies':
            return await self.list(project_id, 'study', offset=args.offset, limit=args.limit)
        if action == 'create_study':
            if args.study is None:
                raise ValueError('需要 study 配置')
            return await self.study(project_id, args.study)
        if action == 'read_study':
            return await self.get(project_id, 'study', args.study_id)
        if action == 'next_step':
            return await self.next_step(project_id, args.study_id)
        if action == 'submit_candidate':
            if args.candidate is None:
                raise ValueError('需要 candidate 配置')
            return await self.candidate(project_id, args.study_id, args.candidate)
        if action == 'candidates':
            await self.get(project_id, 'study', args.study_id)
            if args.candidate_id:
                candidate = await self.get(project_id, 'candidate', args.candidate_id)
                if candidate['study_id'] != args.study_id:
                    raise KeyError('候选不属于当前研究')
                return candidate
            return await self.list(project_id, 'candidate', args.study_id, args.offset, args.limit)
        if action == 'training_note':
            return await self.training_note(project_id, args.study_id, args.candidate_id, args.slot)
        if action == 'finish':
            return await self.finish(project_id, args.study_id, args.reason)
        if action != 'budget':
            raise ValueError('此操作需要项目任务上下文')
        if args.budget is None:
            raise ValueError('需要 budget 配置')
        return await self.budget(project_id, args.study_id, args.budget)
