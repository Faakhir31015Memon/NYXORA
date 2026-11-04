"""Persistence tests for the parts that used to fail silently.

The FTS triggers were once installed from a connect event, before document_chunks
existed, so every CREATE TRIGGER raised "no such table" into a contextlib.suppress
and BM25 retrieval returned nothing in production while unit tests stayed green.
These tests assert the wiring itself, not just the query text.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentChunk,
    fts_ready,
    init_db,
    make_engine,
    search_chunks,
)

REQUIRED_KEYS = {"chunk_id", "doc_id", "filename", "page", "chunk_idx", "content", "score"}


def add_document(session: Session, filename: str, *chunks: str) -> str:
    doc = Document(id=str(uuid.uuid4()), filename=filename, sha256=uuid.uuid4().hex)
    session.add(doc)
    session.flush()
    for idx, content in enumerate(chunks):
        session.add(
            DocumentChunk(
                doc_id=doc.id,
                filename=filename,
                chunk_idx=idx,
                content=content,
                tokens=len(content.split()),
            )
        )
    session.flush()
    return doc.id


def test_init_db_actually_installs_the_fts_triggers(engine: Engine) -> None:
    """A virtual table alone is not an index: the triggers are what fill it."""
    with engine.connect() as conn:
        names = set(
            conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars()
        )
    assert {"chunks_ai", "chunks_ad"} <= names
    assert fts_ready(engine)


def test_bm25_finds_terms_split_across_a_chunk(engine: Engine) -> None:
    """Multi-word queries must not become phrase searches."""
    with Session(engine) as session:
        add_document(
            session, "runbook.md", "The staging gateway rotates its TLS certificate every 90 days."
        )
        session.commit()
    for query in ("TLS certificate renew", "staging", "certificate rotation"):
        assert search_chunks(engine, query), f"{query!r} found nothing in its own document"


def test_bm25_outranks_the_less_relevant_chunk(engine: Engine) -> None:
    with Session(engine) as session:
        add_document(
            session,
            "a.md",
            "Parking is on level two of the building.",
            "TLS certificate renewal for the staging gateway requires a restart.",
        )
        session.commit()
    hits = search_chunks(engine, "TLS certificate staging")
    assert hits[0]["content"].startswith("TLS")


def test_score_is_comparable_confidence_not_raw_bm25(engine: Engine) -> None:
    """The honesty gate thresholds on 0..1, so bm25() itself cannot be the score."""
    with Session(engine) as session:
        add_document(session, "a.md", "staging gateway TLS certificate")
        session.commit()
    strong = search_chunks(engine, "staging gateway TLS certificate")
    weak = search_chunks(engine, "staging quantum tunnelling")
    assert strong[0]["score"] == 1.0
    assert weak[0]["score"] < 0.5, "one term of three must not read as a confident match"


def test_deleting_a_document_removes_it_from_the_index(engine: Engine) -> None:
    with Session(engine) as session:
        doc_id = add_document(session, "temp.md", "Ephemeral token vault notes")
        session.commit()
        assert search_chunks(engine, "vault")
        session.execute(text("DELETE FROM document_chunks WHERE doc_id = :d"), {"d": doc_id})
        session.commit()
    assert search_chunks(engine, "vault") == []


def test_like_fallback_returns_the_same_shape(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without FTS5 the caller still gets results and a 0..1 score."""
    with Session(engine) as session:
        add_document(session, "a.md", "The staging gateway rotates its TLS certificate.")
        session.commit()
    monkeypatch.setattr("app.db.models.fts_ready", lambda _engine: False)
    hits = search_chunks(engine, "staging gateway")
    assert hits
    assert {k for k in hits[0]} == REQUIRED_KEYS
    assert 0.0 <= hits[0]["score"] <= 1.0


@pytest.mark.parametrize(
    "hostile",
    ['NEAR(a b) OR "exact phrase"', "{column} > 2", "a AND NOT b", 'prefix: "c"', "a* OR b^2"],
)
def test_query_operators_cannot_be_injected_through_search(
    engine: Engine, hostile: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Document text is untrusted: FTS operator syntax must never reach MATCH."""
    with Session(engine) as session:
        add_document(session, "a.md", "The staging gateway rotates its TLS certificate.")
        session.commit()
    hits = search_chunks(engine, hostile)
    for hit in hits:
        assert set(hit) == REQUIRED_KEYS
    assert "failed" not in caplog.text, "a hostile query must degrade quietly, not raise"


def test_empty_and_stopword_only_queries_return_nothing(engine: Engine) -> None:
    with Session(engine) as session:
        add_document(session, "a.md", "Some content about nothing in particular.")
        session.commit()
    assert search_chunks(engine, "") == []
    assert search_chunks(engine, "a an of") == []


def test_reingest_repairs_an_index_left_empty_by_the_old_bug(tmp_root: Path) -> None:
    """Old databases have content rows and no index entries; init_db() must fix them."""
    eng = make_engine(tmp_root / "legacy.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE documents (id TEXT PRIMARY KEY, filename TEXT NOT NULL, "
                "content_type VARCHAR(120), size_bytes INTEGER NOT NULL, sha256 VARCHAR(64), "
                "created_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE document_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "doc_id TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', page INTEGER, "
                "chunk_idx INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL, "
                "tokens INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO document_chunks (doc_id, filename, content) "
                "VALUES ('legacy-doc', 'legacy.md', 'staging TLS gateway')"
            )
        )
    init_db(eng)
    assert fts_ready(eng)
    assert search_chunks(eng, "staging TLS"), "rebuild must index rows written before the triggers"


def test_search_survives_a_missing_index_without_lying(tmp_root: Path) -> None:
    """No FTS triggers and no rows: retrieval returns empty, it does not raise."""
    eng: Engine = make_engine(tmp_root / "empty.db")
    init_db(eng)
    assert search_chunks(eng, "anything at all") == []
    assert isinstance(search_chunks(eng, "anything"), list) is True


def test_engine_never_leaves_fts_half_installed(tmp_root: Path) -> None:
    """fts_ready() is the only signal callers get; it must mean fully wired."""
    eng = make_engine(tmp_root / "partial.db")
    init_db(eng)
    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER chunks_ai"))
    assert not fts_ready(eng), "a missing insert trigger means the index is silently stale"
    init_db(eng)
    assert fts_ready(eng)


def test_chunk_rows_are_the_source_of_truth_for_content(engine: Engine) -> None:
    """External-content FTS stores no text; content must come from document_chunks."""
    with Session(engine) as session:
        doc_id = add_document(session, "a.md", "Staging gateway TLS certificate details here")
        session.commit()
    hit = search_chunks(engine, "staging TLS")[0]
    assert hit["doc_id"] == doc_id
    assert isinstance(hit["chunk_id"], int)
    assert "Staging gateway" in hit["content"]
