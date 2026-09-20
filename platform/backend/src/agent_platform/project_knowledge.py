"""Project document versions, semantic indexing and retrieval shared by UI and blocks."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import connect
from .model_connections import ModelConnection
from .models import utc_now
from .project_store import ProjectConflict
from .providers.openai_chat import _is_loopback


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(default=0, ge=0)
    chunk_size: int = Field(default=600, ge=100, le=2000)
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    document_prefix: str = Field(default='', max_length=300)
    query_prefix: str = Field(default='', max_length=300)

    @model_validator(mode='after')
    def overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError('重叠长度必须小于片段长度')
        return self


class KnowledgeRevision(BaseModel):
    expected_revision: int = Field(ge=1)


class KnowledgeSource(KnowledgeRevision):
    title: str = Field(default='', max_length=240)
    text: str = Field(default='', max_length=1_000_000)
    source_path: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.text.strip()) == bool(self.source_path):
            raise ValueError('请选择一个项目文件，或填写一份文本')
        return self


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=5, ge=1, le=20)
    minimum_score: float = Field(default=0.3, ge=-1, le=1)


class KnowledgeSearchConfig(BaseModel):
    knowledge_ref: str = Field(default='', max_length=100)
    query: object = ''
    top_k: int = Field(default=5, ge=1, le=20)
    minimum_score: float = Field(default=0.3, ge=-1, le=1)


def parse_document(raw: bytes, name: str) -> list[dict]:
    """Return actionable parse errors without storing a partial source."""
    from zipfile import BadZipFile
    from lxml.etree import XMLSyntaxError
    from pypdf.errors import PdfReadError
    from docx.opc.exceptions import PackageNotFoundError
    try:
        return _parse_document(raw, name)
    except (BadZipFile, XMLSyntaxError, PdfReadError, PackageNotFoundError, KeyError) as error:
        raise ValueError('文档损坏或格式不完整，请重新导出 PDF、DOCX 或 UTF-8 文本') from error


def _parse_document(raw: bytes, name: str) -> list[dict]:
    """Keep page/paragraph boundaries and offsets instead of flattening sources."""
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError('请先提供未加密的 PDF')
        if len(reader.pages) > 500:
            raise ValueError('单份 PDF 最多 500 页，请拆分资料')
        sections = [{'text': page.extract_text() or '', 'page': i + 1} for i, page in enumerate(reader.pages)]
    elif suffix == '.docx':
        import zipfile
        from docx import Document
        from docx.table import Table
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(f.file_size for f in archive.infolist()) > 40_000_000:
                raise ValueError('文档解压后过大，请拆分资料')
        sections = []
        for i, block in enumerate(Document(io.BytesIO(raw)).iter_inner_content()):
            text = '\n'.join('\t'.join(cell.text for cell in row.cells) for row in block.rows) if isinstance(block, Table) else block.text
            sections.append({'text': text, 'paragraph': i + 1})
    elif suffix in {'.txt', '.md', '.csv', '.tsv', '.json', '.py'}:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise ValueError('文本需要 UTF-8 编码') from error
        sections = [{'text': text, 'line': 1}]
    else:
        raise ValueError('支持 PDF、DOCX 和 UTF-8 文本（TXT、MD、CSV、TSV、JSON、PY）')
    if not any(s['text'].strip() for s in sections):
        raise ValueError('没有提取到正文；扫描 PDF 请先提供 OCR 文本')
    if sum(len(s['text']) for s in sections) > 1_000_000:
        raise ValueError('单份正文最多 100 万字符，请拆分资料')
    return sections


def split_documents(documents: list[dict], config: dict) -> list[dict]:
    chunks = []
    for doc in documents:
        for section in doc['sections']:
            text = section['text']
            for start in range(0, len(text), config['chunk_size'] - config['chunk_overlap']):
                end = min(len(text), start + config['chunk_size'])
                content = text[start:end]
                if content.strip():
                    location = {k: v for k, v in section.items() if k != 'text'}
                    if 'line' in location:
                        location['line'] += text[:start].count('\n')
                    chunks.append({'id': f'{doc["id"]}:{len(chunks)}', 'document_id': doc['id'],
                        'title': doc['title'], 'source_path': doc['source_path'], 'sha256': doc['sha256'],
                        'location': {**location, 'start': start, 'end': end}, 'text': content})
                if len(chunks) > 1000:
                    raise ValueError('当前知识库最多 1000 个片段，请调整切分或拆分知识库')
                if end == len(text):
                    break
    if not chunks:
        raise ValueError('请先添加有正文的资料')
    return chunks


class ProjectKnowledge:
    def __init__(self, services):
        self.services = services
        self.db_path = services.storage.db_path
        self.builds: set[tuple[str, str]] = set()

    async def initialize(self):
        def create():
            with connect(self.db_path) as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS project_knowledge (
                        project_id TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL,
                        config TEXT NOT NULL, documents TEXT NOT NULL, active_version TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(project_id, ref));
                    CREATE TABLE IF NOT EXISTS project_knowledge_versions (
                        project_id TEXT NOT NULL, ref TEXT NOT NULL, id TEXT NOT NULL,
                        revision INTEGER NOT NULL, embedding TEXT NOT NULL, chunks TEXT NOT NULL,
                        created_at TEXT NOT NULL, PRIMARY KEY(project_id,ref,id));
                ''')
        await asyncio.to_thread(create)

    def connection(self, project_id):
        c = self.services.local_agents.connections.load(project_id, 'embedding')
        if not c or not c.runtime_enabled:
            raise ValueError('请在项目知识库中配置并启用 Embedding 模型')
        return c

    @staticmethod
    def identity(c):
        return {'base_url': c.base_url.rstrip('/'), 'model': c.model, 'protocol': c.protocol}

    def _read(self, db, project_id, ref):
        row = db.execute('SELECT * FROM project_knowledge WHERE project_id=? AND ref=?', (project_id, ref)).fetchone()
        if row is None:
            raise KeyError('没有找到当前项目的知识库')
        return {**dict(row), 'config': json.loads(row['config']), 'documents': json.loads(row['documents'])}

    async def _load(self, project_id, ref):
        def read():
            with connect(self.db_path) as db:
                return self._read(db, project_id, ref)
        return await asyncio.to_thread(read)

    async def list(self, project_id):
        def read():
            with connect(self.db_path) as db:
                rows = db.execute('SELECT ref FROM project_knowledge WHERE project_id=? ORDER BY ref', (project_id,)).fetchall()
                return [self._read(db, project_id, row['ref']) for row in rows]
        return [await self.describe(row) for row in await asyncio.to_thread(read)]

    async def describe(self, row):
        ready = False
        model = ''
        count = 0
        if row['active_version']:
            version = await self.version(row['project_id'], row['ref'], row['active_version'], metadata_only=True)
            model, count = version['embedding']['model'], version['chunk_count']
            try:
                ready = version['revision'] == row['revision'] and version['embedding'] == self.identity(self.connection(row['project_id']))
            except ValueError:
                pass
        return {'knowledge_ref': row['ref'], **row['config'], 'revision': row['revision'],
            'active_version': row['active_version'], 'status': 'ready' if ready else 'pending',
            'embedding_model': model, 'chunk_count': count,
            'documents': [{k: v for k, v in d.items() if k != 'sections'} for d in row['documents']]}

    async def get(self, project_id, ref):
        return await self.describe(await self._load(project_id, ref))

    async def save(self, project_id, ref, body):
        import re
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', ref):
            raise ValueError('知识库引用使用 1–80 个字母、数字、下划线或短横线')
        def write():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision FROM project_knowledge WHERE project_id=? AND ref=?', (project_id, ref)).fetchone()
                if (row['revision'] if row else 0) != body.expected_revision:
                    raise ProjectConflict('知识库已修改，请刷新后再保存')
                config = json.dumps(body.model_dump(exclude={'expected_revision'}), ensure_ascii=False)
                if row:
                    db.execute('UPDATE project_knowledge SET config=?,revision=revision+1 WHERE project_id=? AND ref=?', (config, project_id, ref))
                else:
                    db.execute('INSERT INTO project_knowledge(project_id,ref,revision,config,documents) VALUES(?,?,1,?,?)', (project_id, ref, config, '[]'))
        await asyncio.to_thread(write)
        return await self.get(project_id, ref)

    async def add(self, project_id, ref, body):
        await self._load(project_id, ref)
        def parse():
            if body.source_path:
                path = PurePosixPath(body.source_path)
                if path.is_absolute() or '..' in path.parts or '\\' in body.source_path or not path.parts or path.parts[0] not in {'requirement-package', 'results', 'solution'}:
                    raise ValueError('请选择当前项目中的资料或结果文件')
                source = self.services.projects.workspace(project_id).resolve()
                for part in path.parts:
                    source /= part
                    if source.is_symlink():
                        raise ValueError('知识库资料不能是符号链接')
                if not source.is_file() or source.stat().st_size > 20_000_000:
                    raise ValueError('资料不存在或超过 20 MB')
                raw, title = source.read_bytes(), body.title or source.name
                sections = parse_document(raw, source.name)
            else:
                raw, title = body.text.encode(), body.title or '未命名文本'
                sections = parse_document(raw, 'text.txt')
            return {'id': str(uuid4()), 'title': title, 'source_path': body.source_path,
                'sha256': hashlib.sha256(raw).hexdigest(), 'sections': sections, 'characters': sum(len(s['text']) for s in sections)}
        document = await asyncio.to_thread(parse)
        return await self._change_documents(project_id, ref, body.expected_revision, document=document)

    async def _change_documents(self, project_id, ref, revision, *, document=None, delete_id=None):
        def write():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = self._read(db, project_id, ref)
                if row['revision'] != revision:
                    raise ProjectConflict('知识库已修改，请刷新后重试')
                docs = row['documents']
                if document:
                    if any(d['sha256'] == document['sha256'] and d['title'] == document['title'] for d in docs):
                        return
                    if len(docs) >= 100:
                        raise ValueError('一个知识库最多 100 份资料')
                    docs.append(document)
                else:
                    if not any(d['id'] == delete_id for d in docs):
                        raise KeyError('资料不属于这个知识库')
                    docs = [d for d in docs if d['id'] != delete_id]
                db.execute('UPDATE project_knowledge SET documents=?,revision=revision+1 WHERE project_id=? AND ref=?', (json.dumps(docs, ensure_ascii=False), project_id, ref))
        await asyncio.to_thread(write)
        return await self.get(project_id, ref)

    async def embeddings(self, connection, texts):
        if not self.services.settings.model_egress_enabled and not _is_loopback(connection.base_url):
            raise ValueError('远程模型出口已关闭；Embedding 未发送任何资料')
        headers = {'Authorization': 'Bearer ' + connection.api_key.get_secret_value()} if connection.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(connection.base_url.rstrip('/') + '/embeddings', headers=headers,
                    json={'model': connection.model, 'input': texts, 'encoding_format': 'float'})
                if response.status_code != 200:
                    raise ValueError(f'Embedding 服务返回 HTTP {response.status_code}，请检查连接和模型')
                data = response.json()['data']
                if len(data) != len(texts) or sorted(d['index'] for d in data) != list(range(len(texts))):
                    raise ValueError('Embedding 返回的向量数量或序号不正确')
                vectors = [d['embedding'] for d in sorted(data, key=lambda d: d['index'])]
                if not vectors or not 1 <= len(vectors[0]) <= 8192:
                    raise ValueError('Embedding 向量维度无效')
                normalized = []
                for vector in vectors:
                    if len(vector) != len(vectors[0]) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                        raise ValueError('Embedding 返回不一致或非有限向量')
                    norm = math.hypot(*vector)
                    if not norm or not math.isfinite(norm):
                        raise ValueError('Embedding 返回空向量')
                    normalized.append([v / norm for v in vector])
                return normalized
        except httpx.HTTPError as error:
            raise ValueError('Embedding 服务无法连接或响应超时，请检查地址和服务状态') from error
        except (KeyError, TypeError) as error:
            raise ValueError('Embedding 服务未返回有效的向量数据') from error

    async def build(self, project_id, ref, expected_revision):
        key = project_id, ref
        if key in self.builds:
            raise ProjectConflict('这个知识库正在建立索引，请等待本次完成')
        self.builds.add(key)
        try:
            row = await self._load(project_id, ref)
            if row['revision'] != expected_revision:
                raise ProjectConflict('知识库已修改，请刷新后建立索引')
            connection = self.connection(project_id)
            if (await self.describe(row))['status'] == 'ready':
                return await self.get(project_id, ref)
            chunks = split_documents(row['documents'], row['config'])
            for offset in range(0, len(chunks), 16):
                batch = chunks[offset:offset + 16]
                vectors = await self.embeddings(connection, [row['config']['document_prefix'] + c['text'] for c in batch])
                for chunk, vector in zip(batch, vectors, strict=True):
                    chunk['vector'] = vector
            if len({len(c['vector']) for c in chunks}) != 1:
                raise ValueError('Embedding 服务在不同批次返回了不同维度，请重建')
            version_id = str(uuid4())
            def commit():
                with connect(self.db_path) as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self._read(db, project_id, ref)
                    if current['revision'] != expected_revision:
                        raise ProjectConflict('建立索引期间资料已修改，已有版本保留，请重新建立索引')
                    db.execute('INSERT INTO project_knowledge_versions VALUES(?,?,?,?,?,?,?)',
                        (project_id, ref, version_id, expected_revision,
                         json.dumps(self.identity(connection)), json.dumps({'config': row['config'], 'items': chunks}, ensure_ascii=False), utc_now()))
                    db.execute('UPDATE project_knowledge SET active_version=? WHERE project_id=? AND ref=?', (version_id, project_id, ref))
            await asyncio.to_thread(commit)
            return await self.get(project_id, ref)
        finally:
            self.builds.discard(key)

    async def version(self, project_id, ref, version_id, *, metadata_only=False):
        def read():
            with connect(self.db_path) as db:
                columns = "revision,embedding,json_array_length(chunks, '$.items') AS chunk_count" if metadata_only else '*'
                row = db.execute(f'SELECT {columns} FROM project_knowledge_versions WHERE project_id=? AND ref=? AND id=?', (project_id, ref, version_id)).fetchone()
                if not row:
                    raise ValueError('当前项目没有这个知识索引版本')
                if metadata_only:
                    return {**dict(row), 'embedding': json.loads(row['embedding'])}
                payload = json.loads(row['chunks'])
                return {**dict(row), 'embedding': json.loads(row['embedding']), 'config': payload['config'], 'chunks': payload['items']}
        return await asyncio.to_thread(read)

    async def search(self, project_id, ref, request, *, version_id=None):
        if not ref:
            raise ValueError('请为知识检索节点选择项目知识库')
        if version_id is None:
            info = await self.get(project_id, ref)
            if info['status'] != 'ready':
                raise ValueError(f'知识库“{info["name"]}”尚未建立可用索引，请配置模型并建立索引')
            version_id = info['active_version']
        if not version_id:
            raise ValueError('本次运行没有绑定可用知识索引；建立索引后请创建新运行')
        version = await self.version(project_id, ref, version_id)
        connection = self.connection(project_id)
        if self.identity(connection) != version['embedding']:
            raise ValueError('Embedding 连接已变化，与本次知识索引不一致；请恢复原连接或重建索引后重新运行')
        query = (await self.embeddings(connection, [version['config']['query_prefix'] + request.query]))[0]
        results = []
        for chunk in version['chunks']:
            if len(query) != len(chunk['vector']):
                raise ValueError('查询向量与索引维度不一致，请重建索引')
            score = sum(a * b for a, b in zip(query, chunk['vector'], strict=True))
            if score >= request.minimum_score:
                results.append({k: v for k, v in chunk.items() if k != 'vector'} | {'score': round(score, 6)})
        results.sort(key=lambda r: (-r['score'], r['id']))
        results = results[:request.top_k]
        for i, item in enumerate(results):
            item['citation'] = f'[{i + 1}]'
        context = '\n\n'.join(f'{r["citation"]} {r["title"]} {json.dumps(r["location"], ensure_ascii=False)}\n{r["text"]}' for r in results)
        return {'knowledge_ref': ref, 'version': version_id, 'embedding_model': connection.model,
                'results': results, 'context': context, 'retrieved_count': len(results)}


def register_knowledge_routes(router, services, invoke):
    knowledge = services.projects.knowledge

    @router.get('/embedding-model')
    async def connection(project_id: str):
        store = services.local_agents.connections
        c = store.load(project_id, 'embedding')
        return store.public(c) if c else {'provider': None, 'has_api_key': False, 'runtime_enabled': False}

    @router.put('/embedding-model')
    async def save_connection(project_id: str, body: ModelConnection):
        async def save():
            return services.local_agents.connections.save(project_id, body, role='embedding')
        return await invoke(save)

    @router.get('/knowledge')
    async def listing(project_id: str):
        return await invoke(knowledge.list, project_id)

    @router.put('/knowledge/{knowledge_ref}')
    async def save(project_id: str, knowledge_ref: str, body: KnowledgeSettings):
        return await invoke(knowledge.save, project_id, knowledge_ref, body)

    @router.get('/knowledge/{knowledge_ref}')
    async def read(project_id: str, knowledge_ref: str):
        return await invoke(knowledge.get, project_id, knowledge_ref)

    @router.post('/knowledge/{knowledge_ref}/documents')
    async def add(project_id: str, knowledge_ref: str, body: KnowledgeSource):
        return await invoke(knowledge.add, project_id, knowledge_ref, body)

    @router.delete('/knowledge/{knowledge_ref}/documents/{document_id}')
    async def remove(project_id: str, knowledge_ref: str, document_id: str, expected_revision: int):
        return await invoke(knowledge._change_documents, project_id, knowledge_ref, expected_revision, delete_id=document_id)

    @router.post('/knowledge/{knowledge_ref}/build')
    async def build(project_id: str, knowledge_ref: str, body: KnowledgeRevision):
        return await invoke(knowledge.build, project_id, knowledge_ref, body.expected_revision)

    @router.post('/knowledge/{knowledge_ref}/search')
    async def search(project_id: str, knowledge_ref: str, body: KnowledgeSearch):
        return await invoke(knowledge.search, project_id, knowledge_ref, body)
