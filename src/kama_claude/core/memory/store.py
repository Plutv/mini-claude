from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def project_memory_scope(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/").lower()
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return f"project:{digest}"


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) == 1:
            words.add(chunk)
        else:
            words.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return words


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: str
    content: str
    tags: tuple[str, ...]
    importance: float
    created_at: str
    updated_at: str
    access_count: int
    score: float = 0.0


class MemoryStore:
    """Durable scoped memories with deterministic selective retrieval."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    importance REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope, content_hash)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)"
            )

    def save(
        self,
        *,
        scope: str,
        content: str,
        tags: list[str] | None = None,
        importance: float = 0.5,
        source_run_id: str = "",
    ) -> MemoryRecord:
        del source_run_id  # reserved for a future provenance table
        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content must not be empty")
        normalized_tags = sorted({tag.strip().lower() for tag in tags or [] if tag.strip()})
        content_hash = hashlib.sha256(normalized.encode()).hexdigest()
        timestamp = _now()
        importance = min(1.0, max(0.0, float(importance)))
        memory_id = f"mem-{uuid.uuid4().hex[:16]}"
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM memories WHERE scope = ? AND content_hash = ?",
                (scope, content_hash),
            ).fetchone()
            if existing is not None:
                merged_tags = sorted(set(json.loads(existing["tags_json"])) | set(normalized_tags))
                connection.execute(
                    """
                    UPDATE memories SET tags_json = ?, importance = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(merged_tags, ensure_ascii=False),
                        max(float(existing["importance"]), importance),
                        timestamp,
                        existing["id"],
                    ),
                )
                memory_id = str(existing["id"])
            else:
                connection.execute(
                    """
                    INSERT INTO memories (
                        id, scope, content, content_hash, tags_json, importance,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        scope,
                        normalized,
                        content_hash,
                        json.dumps(normalized_tags, ensure_ascii=False),
                        importance,
                        timestamp,
                        timestamp,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    def recall(
        self,
        query: str,
        *,
        scopes: list[str],
        limit: int = 6,
        min_score: float = 0.15,
        max_chars: int = 6_000,
    ) -> list[MemoryRecord]:
        query_terms = _terms(query)
        if not query_terms or not scopes or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in scopes)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memories WHERE scope IN ({placeholders})",  # noqa: S608
                scopes,
            ).fetchall()
            scored: list[MemoryRecord] = []
            for row in rows:
                memory_terms = _terms(str(row["content"])) | set(json.loads(row["tags_json"]))
                overlap = query_terms & memory_terms
                if not overlap:
                    continue
                coverage = len(overlap) / len(query_terms)
                precision = len(overlap) / max(1, len(memory_terms))
                importance = float(row["importance"])
                access_bonus = min(0.03, math.log1p(int(row["access_count"])) * 0.01)
                scope_bonus = 0.03 if str(row["scope"]).startswith("session:") else 0.015
                score = coverage * 0.65 + precision * 0.10 + importance * 0.20
                score += access_bonus + scope_bonus
                if score >= min_score:
                    scored.append(self._record(row, score=score))

            selected: list[MemoryRecord] = []
            used_chars = 0
            for record in sorted(
                scored,
                key=lambda item: (item.score, item.importance, item.updated_at),
                reverse=True,
            ):
                if len(selected) >= limit:
                    break
                if selected and used_chars + len(record.content) > max_chars:
                    continue
                selected.append(record)
                used_chars += len(record.content)

            if selected:
                timestamp = _now()
                connection.executemany(
                    """
                    UPDATE memories
                    SET access_count = access_count + 1, last_accessed_at = ?
                    WHERE id = ?
                    """,
                    [(timestamp, item.id) for item in selected],
                )
        return selected

    @staticmethod
    def render(records: list[MemoryRecord]) -> str:
        lines = []
        for record in records:
            tags = f" tags={','.join(record.tags)}" if record.tags else ""
            lines.append(f"- [{record.id} scope={record.scope}{tags}] {record.content}")
        return "\n".join(lines)

    @staticmethod
    def _record(row: sqlite3.Row, *, score: float = 0.0) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            scope=str(row["scope"]),
            content=str(row["content"]),
            tags=tuple(str(tag) for tag in json.loads(row["tags_json"])),
            importance=float(row["importance"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            access_count=int(row["access_count"]),
            score=score,
        )
