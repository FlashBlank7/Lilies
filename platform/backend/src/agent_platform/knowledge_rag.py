from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import sqlite3
import unicodedata

from .db import connect as _sqlite_connect
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


EMBEDDING_MODEL = "hashing-embedding-v1"
RETRIEVAL_MODEL = "acl-hybrid-retrieval-v2"
ANSWER_MODEL = "grounded-extractive-answer-v1"
EMBEDDING_DIMENSIONS = 192
_SCORING_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class KnowledgeIndexConflict(ValueError):
    """An idempotency, revision, or immutable index contract did not match."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        candidate = parser.text()
    except (ValueError, AssertionError):
        candidate = value
    candidate = html.unescape(candidate)
    return re.sub(r"\s+", " ", candidate).strip()


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = re.findall(r"[a-z0-9]+(?:[_-][a-z0-9]+)*", normalized)
    cjk_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", normalized)
    cjk: list[str] = []
    for run in cjk_runs:
        cjk.extend(run)
        cjk.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return words + cjk


def _scoring_tokens(value: str) -> list[str]:
    tokens = _tokens(value)
    discriminative = [item for item in tokens if item not in _SCORING_STOPWORDS]
    return discriminative or tokens


def _embedding(value: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in _tokens(value):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(item * item for item in vector))
    if norm:
        vector = [item / norm for item in vector]
    return vector


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def _chunk_text(value: str, size: int, overlap: int) -> list[str]:
    text = _plain_text(value)
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + size)
        end = hard_end
        if hard_end < len(text):
            boundary = max(
                text.rfind(". ", start + size // 2, hard_end),
                text.rfind("。", start + size // 2, hard_end),
                text.rfind("\n", start + size // 2, hard_end),
            )
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _revision_key(value: str) -> tuple[int, Any]:
    stripped = value.strip()
    if re.fullmatch(r"\d+", stripped):
        return (2, int(stripped))
    try:
        return (1, datetime.fromisoformat(stripped.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return (0, stripped)


class KnowledgeDocument(BaseModel):
    source_id: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=5_000_000)
    revision: str = Field(min_length=1, max_length=200)
    url: str = Field(default="", max_length=2_000)
    allowed_roles: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_roles")
    @classmethod
    def normalize_roles(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        if not normalized:
            raise ValueError("allowed_roles must include at least one non-empty role")
        return normalized


class KnowledgeIndexCreateRequest(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,119}$")
    embedding_model: Literal["hashing-embedding-v1"] = EMBEDDING_MODEL
    chunk_size: int = Field(default=1_000, ge=200, le=4_000)
    chunk_overlap: int = Field(default=120, ge=0, le=500)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def valid_overlap(self) -> KnowledgeIndexCreateRequest:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self


class KnowledgeSyncRequest(BaseModel):
    documents: list[KnowledgeDocument] = Field(default_factory=list, max_length=2_000)
    deleted_source_ids: list[str] = Field(default_factory=list, max_length=2_000)
    event_id: str = Field(min_length=8, max_length=200)
    # 写隔离（读共享）：首个写入的应用登记为属主；他应用写入必须显式
    # shared=True（防止两个客户项目撞名互踩——缺陷 #8）。
    application_id: str = ""
    shared: bool = False

    @model_validator(mode="after")
    def unique_sources(self) -> KnowledgeSyncRequest:
        document_ids = [item.source_id for item in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("documents must have unique source_id values")
        deleted = [item.strip() for item in self.deleted_source_ids if item.strip()]
        if len(deleted) != len(set(deleted)):
            raise ValueError("deleted_source_ids must be unique")
        overlap = set(document_ids).intersection(deleted)
        if overlap:
            raise ValueError(f"source cannot be updated and deleted together: {sorted(overlap)}")
        self.deleted_source_ids = deleted
        return self


class KnowledgeRetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    principal_roles: list[str] = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=20)
    minimum_score: float = Field(default=0.12, ge=0, le=1)

    @field_validator("principal_roles")
    @classmethod
    def normalize_principal_roles(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        if not normalized:
            raise ValueError("principal_roles must include at least one non-empty role")
        return normalized


class GroundedAnswerRequest(KnowledgeRetrieveRequest):
    refusal_message: str = Field(
        default="I cannot answer from the authorized knowledge currently available.",
        min_length=1,
        max_length=2_000,
    )


class KnowledgeIndexSyncConfig(BaseModel):
    index_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,119}$")
    documents: Any
    deleted_source_ids: Any = Field(default_factory=list)
    event_id: Any
    # replace=True: this sync's documents ARE the whole corpus — anything else
    # in the index is deleted first. The right mode when the workflow input
    # provides the documents each run (stale runs must not pollute retrieval).
    replace: bool = False
    # shared=True：明知该索引归属另一个应用仍要共享写入（默认写隔离）。
    shared: bool = False


class KnowledgeRetrievalConfig(BaseModel):
    index_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,119}$")
    query: Any
    principal_roles: Any
    top_k: int = Field(default=5, ge=1, le=20)
    minimum_score: float = Field(default=0.12, ge=0, le=1)


class GroundedAnswerConfig(BaseModel):
    query: Any
    retrieval: Any
    refusal_message: str = Field(
        default="I cannot answer from the authorized knowledge currently available.",
        min_length=1,
        max_length=2_000,
    )


class KnowledgeIndexService:
    """Persistent, host-neutral document synchronization and ACL-first retrieval."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = _sqlite_connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_indexes (
                  name TEXT PRIMARY KEY,
                  embedding_model TEXT NOT NULL,
                  chunk_size INTEGER NOT NULL,
                  chunk_overlap INTEGER NOT NULL,
                  revision INTEGER NOT NULL,
                  content_digest TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  owner_application_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                  index_name TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  content TEXT NOT NULL,
                  revision TEXT NOT NULL,
                  url TEXT NOT NULL,
                  allowed_roles_json TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  document_digest TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(index_name, source_id),
                  FOREIGN KEY(index_name) REFERENCES knowledge_indexes(name) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                  index_name TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  chunk_id TEXT NOT NULL,
                  ordinal INTEGER NOT NULL,
                  content TEXT NOT NULL,
                  tokens_json TEXT NOT NULL,
                  embedding_json TEXT NOT NULL,
                  PRIMARY KEY(index_name, chunk_id),
                  FOREIGN KEY(index_name, source_id)
                    REFERENCES knowledge_documents(index_name, source_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS knowledge_idempotency (
                  scope TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(scope, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source
                  ON knowledge_chunks(index_name, source_id, ordinal);
                """
            )
            # 旧库迁移：补 owner_application_id 列（新库建表已自带，此处吞重复错）
            try:
                connection.execute(
                    "ALTER TABLE knowledge_indexes ADD COLUMN owner_application_id TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                pass
            connection.commit()
        finally:
            connection.close()

    async def create_index(self, request: KnowledgeIndexCreateRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._create_index_sync, request)

    def _create_index_sync(self, request: KnowledgeIndexCreateRequest) -> dict[str, Any]:
        scope = f"knowledge-index-create:{request.name}"
        payload = request.model_dump(mode="json")
        digest = _sha256(payload)
        connection = self._connect()
        try:
            replay = connection.execute(
                """
                SELECT request_digest, response_json
                FROM knowledge_idempotency
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, request.idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_digest"] != digest:
                    raise KnowledgeIndexConflict(
                        "idempotency key was already used for a different index contract"
                    )
                response = json.loads(replay["response_json"])
                response["replayed"] = True
                return response

            existing = connection.execute(
                "SELECT * FROM knowledge_indexes WHERE name = ?",
                (request.name,),
            ).fetchone()
            if existing is not None:
                actual_contract = {
                    "embedding_model": existing["embedding_model"],
                    "chunk_size": existing["chunk_size"],
                    "chunk_overlap": existing["chunk_overlap"],
                }
                expected_contract = {
                    "embedding_model": request.embedding_model,
                    "chunk_size": request.chunk_size,
                    "chunk_overlap": request.chunk_overlap,
                }
                if actual_contract != expected_contract:
                    raise KnowledgeIndexConflict(
                        "knowledge index already exists with a different immutable contract"
                    )
                created = False
            else:
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO knowledge_indexes(
                      name, embedding_model, chunk_size, chunk_overlap, revision,
                      content_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        request.name,
                        request.embedding_model,
                        request.chunk_size,
                        request.chunk_overlap,
                        _sha256([]),
                        now,
                        now,
                    ),
                )
                created = True
            response = {
                "name": request.name,
                "embedding_model": request.embedding_model,
                "chunk_size": request.chunk_size,
                "chunk_overlap": request.chunk_overlap,
                "revision": int(existing["revision"]) if existing is not None else 0,
                "content_digest": (
                    str(existing["content_digest"]) if existing is not None else _sha256([])
                ),
                "created": created,
                "replayed": False,
            }
            connection.execute(
                """
                INSERT INTO knowledge_idempotency(
                  scope, idempotency_key, request_digest, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scope, request.idempotency_key, digest, _canonical_json(response), _utc_now()),
            )
            connection.commit()
            return response
        finally:
            connection.close()

    async def list_indexes(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_indexes_sync)

    def _list_indexes_sync(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT i.*, COUNT(DISTINCT d.source_id) AS document_count,
                       COUNT(c.chunk_id) AS chunk_count
                FROM knowledge_indexes i
                LEFT JOIN knowledge_documents d ON d.index_name = i.name
                LEFT JOIN knowledge_chunks c
                  ON c.index_name = d.index_name AND c.source_id = d.source_id
                GROUP BY i.name
                ORDER BY i.name
                """
            ).fetchall()
            return [self._index_row(row) for row in rows]
        finally:
            connection.close()

    async def get_index(self, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_index_sync, name)

    def _get_index_sync(self, name: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT i.*, COUNT(DISTINCT d.source_id) AS document_count,
                       COUNT(c.chunk_id) AS chunk_count
                FROM knowledge_indexes i
                LEFT JOIN knowledge_documents d ON d.index_name = i.name
                LEFT JOIN knowledge_chunks c
                  ON c.index_name = d.index_name AND c.source_id = d.source_id
                WHERE i.name = ?
                GROUP BY i.name
                """,
                (name,),
            ).fetchone()
            if row is None:
                raise KeyError(f"knowledge index not found: {name}")
            return self._index_row(row)
        finally:
            connection.close()

    @staticmethod
    def _index_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "embedding_model": row["embedding_model"],
            "retrieval_model": RETRIEVAL_MODEL,
            "answer_model": ANSWER_MODEL,
            "chunk_size": int(row["chunk_size"]),
            "chunk_overlap": int(row["chunk_overlap"]),
            "revision": int(row["revision"]),
            "content_digest": row["content_digest"],
            "document_count": int(row["document_count"]),
            "chunk_count": int(row["chunk_count"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def list_source_ids(self, name: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._list_source_ids_sync, name)

    def _list_source_ids_sync(self, name: str) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT source_id FROM knowledge_documents WHERE index_name = ?",
                (name,),
            ).fetchall()
            return [row["source_id"] for row in rows]
        finally:
            connection.close()

    async def sync(self, name: str, request: KnowledgeSyncRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._sync_sync, name, request)

    def _sync_sync(self, name: str, request: KnowledgeSyncRequest) -> dict[str, Any]:
        scope = f"knowledge-index-sync:{name}"
        payload = request.model_dump(mode="json")
        request_digest = _sha256(payload)
        connection = self._connect()
        try:
            index = connection.execute(
                "SELECT * FROM knowledge_indexes WHERE name = ?",
                (name,),
            ).fetchone()
            if index is None:
                # Workflows must be self-contained: a knowledge_index_sync step
                # provisions its index on first sync with default parameters.
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO knowledge_indexes(
                      name, embedding_model, chunk_size, chunk_overlap, revision,
                      content_digest, created_at, updated_at, owner_application_id
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (name, EMBEDDING_MODEL, 1_000, 120, _sha256([]), now, now,
                     request.application_id or ""),
                )
                index = connection.execute(
                    "SELECT * FROM knowledge_indexes WHERE name = ?",
                    (name,),
                ).fetchone()
            else:
                # 写隔离（读共享）：索引归属首个写入的应用；他应用写入必须
                # 显式 shared=True——两个客户项目撞了索引名，静默互踩比
                # 报错贵一万倍（缺陷 #8）。
                owner = str(
                    index["owner_application_id"]
                    if "owner_application_id" in index.keys()
                    else ""
                )
                caller = request.application_id or ""
                if owner and caller and owner != caller and not request.shared:
                    raise KnowledgeIndexConflict(
                        f"知识库「{name}」归属另一个应用（{owner[:8]}…）。"
                        "如果确实要共享写入，请在同步环节的配置里声明 shared: true；"
                        "否则请换一个索引名。"
                    )
                if not owner and caller:
                    # 旧索引补登记：第一个带身份的写入者成为属主
                    connection.execute(
                        "UPDATE knowledge_indexes SET owner_application_id=? WHERE name=?",
                        (caller, name),
                    )
            replay = connection.execute(
                """
                SELECT request_digest, response_json
                FROM knowledge_idempotency
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, request.event_id),
            ).fetchone()
            if replay is not None:
                if replay["request_digest"] != request_digest:
                    raise KnowledgeIndexConflict(
                        "sync event_id was already used for a different payload"
                    )
                response = json.loads(replay["response_json"])
                response["replayed"] = True
                return response

            inserted: list[str] = []
            updated: list[str] = []
            unchanged: list[str] = []
            deleted: list[str] = []
            for source_id in request.deleted_source_ids:
                existing = connection.execute(
                    """
                    SELECT 1 FROM knowledge_documents
                    WHERE index_name = ? AND source_id = ?
                    """,
                    (name, source_id),
                ).fetchone()
                if existing is None:
                    continue
                connection.execute(
                    "DELETE FROM knowledge_documents WHERE index_name = ? AND source_id = ?",
                    (name, source_id),
                )
                deleted.append(source_id)

            for document in request.documents:
                document_payload = document.model_dump(mode="json")
                document_digest = _sha256(document_payload)
                existing = connection.execute(
                    """
                    SELECT revision, document_digest
                    FROM knowledge_documents
                    WHERE index_name = ? AND source_id = ?
                    """,
                    (name, document.source_id),
                ).fetchone()
                if existing is not None and existing["document_digest"] == document_digest:
                    unchanged.append(document.source_id)
                    continue
                if (
                    existing is not None
                    and _revision_key(document.revision) < _revision_key(str(existing["revision"]))
                ):
                    raise KnowledgeIndexConflict(
                        f"stale revision for {document.source_id}: "
                        f"{document.revision} < {existing['revision']}"
                    )
                connection.execute(
                    """
                    INSERT INTO knowledge_documents(
                      index_name, source_id, title, content, revision, url,
                      allowed_roles_json, metadata_json, document_digest, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(index_name, source_id) DO UPDATE SET
                      title=excluded.title,
                      content=excluded.content,
                      revision=excluded.revision,
                      url=excluded.url,
                      allowed_roles_json=excluded.allowed_roles_json,
                      metadata_json=excluded.metadata_json,
                      document_digest=excluded.document_digest,
                      updated_at=excluded.updated_at
                    """,
                    (
                        name,
                        document.source_id,
                        document.title,
                        document.content,
                        document.revision,
                        document.url,
                        _canonical_json(document.allowed_roles),
                        _canonical_json(document.metadata),
                        document_digest,
                        _utc_now(),
                    ),
                )
                connection.execute(
                    "DELETE FROM knowledge_chunks WHERE index_name = ? AND source_id = ?",
                    (name, document.source_id),
                )
                for ordinal, chunk in enumerate(
                    _chunk_text(
                        document.content,
                        int(index["chunk_size"]),
                        int(index["chunk_overlap"]),
                    )
                ):
                    chunk_id = hashlib.sha256(
                        f"{name}\0{document.source_id}\0{document.revision}\0{ordinal}\0{chunk}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO knowledge_chunks(
                          index_name, source_id, chunk_id, ordinal, content,
                          tokens_json, embedding_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            name,
                            document.source_id,
                            chunk_id,
                            ordinal,
                            chunk,
                            _canonical_json(sorted(set(_tokens(chunk)))),
                            _canonical_json(_embedding(chunk)),
                        ),
                    )
                if existing is None:
                    inserted.append(document.source_id)
                else:
                    updated.append(document.source_id)

            changed = bool(inserted or updated or deleted)
            summaries = [
                {
                    "source_id": row["source_id"],
                    "revision": row["revision"],
                    "document_digest": row["document_digest"],
                }
                for row in connection.execute(
                    """
                    SELECT source_id, revision, document_digest
                    FROM knowledge_documents
                    WHERE index_name = ?
                    ORDER BY source_id
                    """,
                    (name,),
                ).fetchall()
            ]
            content_digest = _sha256(summaries)
            revision = int(index["revision"]) + (1 if changed else 0)
            now = _utc_now()
            connection.execute(
                """
                UPDATE knowledge_indexes
                SET revision = ?, content_digest = ?, updated_at = ?
                WHERE name = ?
                """,
                (revision, content_digest, now, name),
            )
            counts = connection.execute(
                """
                SELECT COUNT(DISTINCT d.source_id) AS document_count,
                       COUNT(c.chunk_id) AS chunk_count
                FROM knowledge_documents d
                LEFT JOIN knowledge_chunks c
                  ON c.index_name = d.index_name AND c.source_id = d.source_id
                WHERE d.index_name = ?
                """,
                (name,),
            ).fetchone()
            response = {
                "index_name": name,
                "event_id": request.event_id,
                "inserted": sorted(inserted),
                "updated": sorted(updated),
                "deleted": sorted(deleted),
                "unchanged": sorted(unchanged),
                "document_count": int(counts["document_count"]),
                "chunk_count": int(counts["chunk_count"]),
                "index_revision": revision,
                "index_digest": content_digest,
                "changed": changed,
                "replayed": False,
                "model_versions": {
                    "embedding": index["embedding_model"],
                    "retrieval": RETRIEVAL_MODEL,
                },
            }
            connection.execute(
                """
                INSERT INTO knowledge_idempotency(
                  scope, idempotency_key, request_digest, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    request.event_id,
                    request_digest,
                    _canonical_json(response),
                    now,
                ),
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def retrieve(self, name: str, request: KnowledgeRetrieveRequest) -> dict[str, Any]:
        return await asyncio.to_thread(self._retrieve_sync, name, request)

    def _retrieve_sync(
        self,
        name: str,
        request: KnowledgeRetrieveRequest,
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            index = connection.execute(
                "SELECT * FROM knowledge_indexes WHERE name = ?",
                (name,),
            ).fetchone()
            if index is None:
                raise KeyError(f"knowledge index not found: {name}")
            documents = connection.execute(
                """
                SELECT source_id, title, revision, url, allowed_roles_json, metadata_json
                FROM knowledge_documents
                WHERE index_name = ?
                ORDER BY source_id
                """,
                (name,),
            ).fetchall()
            principal_roles = set(request.principal_roles)
            authorized_ids: set[str] = set()
            filtered_ids: list[str] = []
            document_evidence: list[dict[str, Any]] = []
            document_details: dict[str, sqlite3.Row] = {}
            for document in documents:
                allowed_roles = set(json.loads(document["allowed_roles_json"]))
                authorized = "*" in allowed_roles or bool(allowed_roles.intersection(principal_roles))
                document_evidence.append(
                    {
                        "source_id": document["source_id"],
                        "decision": "allow" if authorized else "filter",
                        "matched_roles": sorted(allowed_roles.intersection(principal_roles)),
                    }
                )
                if authorized:
                    authorized_ids.add(str(document["source_id"]))
                    document_details[str(document["source_id"])] = document
                else:
                    filtered_ids.append(str(document["source_id"]))

            query_tokens = set(_scoring_tokens(request.query))
            query_embedding = _embedding(request.query)
            candidates: list[dict[str, Any]] = []
            if authorized_ids:
                placeholders = ",".join("?" for _ in authorized_ids)
                chunks = connection.execute(
                    f"""
                    SELECT source_id, chunk_id, ordinal, content, tokens_json, embedding_json
                    FROM knowledge_chunks
                    WHERE index_name = ? AND source_id IN ({placeholders})
                    """,
                    (name, *sorted(authorized_ids)),
                ).fetchall()
                for chunk in chunks:
                    chunk_tokens = set(json.loads(chunk["tokens_json"]))
                    overlap = query_tokens.intersection(chunk_tokens)
                    lexical = len(overlap) / max(1, len(query_tokens))
                    semantic = max(
                        0.0,
                        _cosine(query_embedding, json.loads(chunk["embedding_json"])),
                    )
                    score = min(1.0, 0.35 * semantic + 0.65 * lexical)
                    if score < request.minimum_score:
                        continue
                    document = document_details[str(chunk["source_id"])]
                    candidates.append(
                        {
                            "source_id": chunk["source_id"],
                            "title": document["title"],
                            "url": document["url"],
                            "revision": document["revision"],
                            "chunk_id": chunk["chunk_id"],
                            "ordinal": int(chunk["ordinal"]),
                            "quote": chunk["content"],
                            "score": round(score, 6),
                            "matched_terms": sorted(overlap),
                            "metadata": json.loads(document["metadata_json"]),
                        }
                    )
            candidates.sort(key=lambda item: (-item["score"], item["source_id"], item["ordinal"]))
            results = candidates[: request.top_k]
            return {
                "query": request.query,
                "index_name": name,
                "index_revision": int(index["revision"]),
                "index_digest": index["content_digest"],
                "results": results,
                "retrieved_count": len(results),
                "acl_decision": {
                    "principal_roles": request.principal_roles,
                    "evaluated_documents": len(documents),
                    "authorized_documents": len(authorized_ids),
                    "filtered_documents": len(filtered_ids),
                    "filtered_source_ids": sorted(filtered_ids),
                    "documents": document_evidence,
                },
                "forbidden_chunk_count": 0,
                "model_versions": {
                    "embedding": index["embedding_model"],
                    "retrieval": RETRIEVAL_MODEL,
                },
            }
        finally:
            connection.close()

    async def answer(self, name: str, request: GroundedAnswerRequest) -> dict[str, Any]:
        retrieval = await self.retrieve(
            name,
            KnowledgeRetrieveRequest.model_validate(
                request.model_dump(
                    mode="python",
                    exclude={"refusal_message"},
                )
            ),
        )
        return grounded_answer(
            query=request.query,
            retrieval=retrieval,
            refusal_message=request.refusal_message,
        )


def grounded_answer(
    *,
    query: str,
    retrieval: Any,
    refusal_message: str,
) -> dict[str, Any]:
    if not isinstance(retrieval, dict):
        raise ValueError("retrieval must resolve to an object")
    results = retrieval.get("results", [])
    if not isinstance(results, list):
        raise ValueError("retrieval.results must be an array")
    if not results:
        return {
            "status": "refused",
            "answer": refusal_message,
            "supported": False,
            "citations": [],
            "query": query,
            "index_name": retrieval.get("index_name"),
            "index_revision": retrieval.get("index_revision"),
            "index_digest": retrieval.get("index_digest"),
            "forbidden_chunk_count": retrieval.get("forbidden_chunk_count", 0),
            "acl_decision": retrieval.get("acl_decision", {}),
            "model_versions": {
                **dict(retrieval.get("model_versions", {})),
                "answer": ANSWER_MODEL,
            },
        }
    query_tokens = set(_tokens(query))
    top = results[0]
    quote = str(top.get("quote", "")).strip()
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？])\s*", quote)
        if item.strip()
    ] or [quote]
    sentence = max(
        sentences,
        key=lambda item: (
            len(query_tokens.intersection(_tokens(item))),
            min(len(item), 500),
        ),
    )
    citations = [
        {
            "source_id": item.get("source_id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "revision": item.get("revision"),
            "chunk_id": item.get("chunk_id"),
            "quote": item.get("quote"),
            "score": item.get("score"),
        }
        for item in results
    ]
    return {
        "status": "answered",
        "answer": sentence,
        "supported": True,
        "citations": citations,
        "query": query,
        "index_name": retrieval.get("index_name"),
        "index_revision": retrieval.get("index_revision"),
        "index_digest": retrieval.get("index_digest"),
        "forbidden_chunk_count": retrieval.get("forbidden_chunk_count", 0),
        "acl_decision": retrieval.get("acl_decision", {}),
        "model_versions": {
            **dict(retrieval.get("model_versions", {})),
            "answer": ANSWER_MODEL,
        },
    }
