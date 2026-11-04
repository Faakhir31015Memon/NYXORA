"""Knowledge / RAG engine: ingest -> clean -> chunk -> index -> retrieve -> cite.

V1 retriever is FTS5/BM25. The interface is built so an embedding retriever and
a hybrid (reciprocal-rank-fusion) retriever can be added without callers changing.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.safety import strip_control, wrap_untrusted
from app.db.models import Document, DocumentChunk, search_chunks

ALLOWED_UPLOAD_TYPES = {"application/pdf", "text/plain", "text/markdown"}
MAX_CHUNK_TOKENS = 800
MIN_CHUNK_TOKENS = 120
OVERLAP_RATIO = 0.12


class RetrievedChunk(BaseModel):
    chunk_id: int
    doc_id: str
    filename: str
    page: int | None = None
    chunk_idx: int
    content: str
    score: float

    @property
    def source_id(self) -> int:
        return self.chunk_id

    def as_context(self) -> str:
        label = f"{self.filename}" + (f" p.{self.page}" if self.page else "")
        return wrap_untrusted(self.content, f"document:{label}")


class Retriever(Protocol):
    def retrieve(self, query: str, limit: int) -> list[RetrievedChunk]: ...


class Fts5Retriever:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.engine = self.session.get_bind()

    def retrieve(self, query: str, limit: int = 5) -> list[RetrievedChunk]:
        rows = search_chunks(cast("Engine", self.session.get_bind()), rewrite_query(query), limit)
        return [
            RetrievedChunk(
                chunk_id=int(r["chunk_id"]),
                doc_id=str(r["doc_id"]),
                filename=str(r["filename"]),
                page=r.get("page"),
                chunk_idx=int(r.get("chunk_idx") or 0),
                content=str(r["content"]),
                score=float(r.get("score") or 0.0),
            )
            for r in rows
        ]


class StubEmbeddingRetriever:
    """Documented extension point for V1.1 (nomic-embed-text via Ollama)."""

    def retrieve(self, query: str, limit: int = 5) -> list[RetrievedChunk]:
        return []


class HybridRetriever:
    """Reciprocal rank fusion over the available retrievers. Degrades gracefully."""

    def __init__(self, retrievers: list[Retriever], k: int = 60) -> None:
        self.retrievers = retrievers
        self.k = k

    def retrieve(self, query: str, limit: int = 5) -> list[RetrievedChunk]:
        fused: dict[int, tuple[float, RetrievedChunk]] = {}
        for retriever in self.retrievers:
            for rank, chunk in enumerate(retriever.retrieve(query, limit * 2)):
                score = fused.get(chunk.chunk_id, (0.0, chunk))[0] + 1.0 / (self.k + rank + 1)
                fused[chunk.chunk_id] = (score, chunk)
        ordered = sorted(fused.values(), key=lambda p: -p[0])
        return [c for _, c in ordered[:limit]]


def rewrite_query(query: str) -> str:
    """Rule-based V1 query rewriting: drop stopwords and politeness."""
    lowered = (query or "").lower()
    lowered = re.sub(
        r"\b(according to|please|can you|could you|i want to know|tell me about"
        r"|in my documents|summarize)\b",
        " ",
        lowered,
    )
    lowered = re.sub(r"[?!.]+$", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


_HEADING_RE = re.compile(r"^(#{1,6}\s|\d+\.\s|[A-Z][A-Za-z ]{3,50}:$)")


def chunk_text(text: str, *, page: int | None = None) -> list[tuple[int, str]]:
    """~500-800 tokens, 10-15% overlap, paragraph and heading aware."""
    cleaned = strip_control(text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        content = "\n\n".join(buffer).strip()
        if content:
            chunks.append((len(chunks), content))
        # carry an overlap tail into the next window
        overlap_tokens = int(MAX_CHUNK_TOKENS * OVERLAP_RATIO)
        tail: list[str] = []
        tail_tokens = 0
        for piece in reversed(buffer):
            t = len(piece.split())
            if tail_tokens + t > overlap_tokens:
                break
            tail.insert(0, piece)
            tail_tokens += t
        buffer = tail
        buffer_tokens = tail_tokens

    for para in paragraphs:
        tokens = len(para.split())
        if _HEADING_RE.match(para) and buffer_tokens > MIN_CHUNK_TOKENS:
            flush()
        if tokens > MAX_CHUNK_TOKENS:
            if buffer:
                flush()
            words = para.split()
            step = int(MAX_CHUNK_TOKENS * (1 - OVERLAP_RATIO))
            for start in range(0, len(words), step):
                piece = " ".join(words[start : start + MAX_CHUNK_TOKENS])
                if len(piece.split()) >= MIN_CHUNK_TOKENS or start + MAX_CHUNK_TOKENS >= len(words):
                    chunks.append((len(chunks), piece))
            buffer = []
            buffer_tokens = 0
            continue
        if buffer_tokens + tokens > MAX_CHUNK_TOKENS and buffer:
            flush()
        buffer.append(para)
        buffer_tokens += tokens
    flush()
    # A chunk that is just an overlap tail of the previous one is noise.
    deduped: list[tuple[int, str]] = []
    for _, content in chunks:
        if deduped and len(content.split()) < MIN_CHUNK_TOKENS and content in deduped[-1][1]:
            continue
        deduped.append((len(deduped), content))
    return deduped


def extract_text(path: Path, content_type: str | None) -> str:
    if content_type == "application/pdf" or path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages):
            try:
                pages.append(f"\n\n[page {i + 1}]\n" + (page.extract_text() or ""))
            except Exception:  # noqa: BLE001 - a broken page must not kill ingest
                pages.append(f"\n\n[page {i + 1} unreadable]\n")
        return "".join(pages)
    return path.read_text(encoding="utf-8", errors="replace")


class IngestResult(BaseModel):
    doc_id: str
    filename: str
    chunks: int
    sha256: str
    duplicate: bool = False


class KnowledgeEngine:
    def __init__(self, session: Session, store_dir: Path) -> None:
        self.session = session
        self.store = Path(store_dir)
        self.store.mkdir(parents=True, exist_ok=True)
        self.retriever: Retriever = Fts5Retriever(session)

    def ingest(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str | None,
        page_map: list[tuple[int, str]] | None = None,
    ) -> IngestResult:
        safe_name = sanitize_filename(filename)
        digest = hashlib.sha256(data).hexdigest()
        existing = self.session.scalars(select(Document).where(Document.sha256 == digest)).first()
        if existing is not None:
            return IngestResult(
                doc_id=existing.id,
                filename=existing.filename,
                chunks=0,
                sha256=digest,
                duplicate=True,
            )
        if content_type and content_type not in ALLOWED_UPLOAD_TYPES:
            raise ValueError(f"unsupported content type {content_type!r}")

        doc = Document(
            id=str(uuid.uuid4()),
            filename=safe_name,
            content_type=content_type,
            size_bytes=len(data),
            sha256=digest,
        )
        self.session.add(doc)
        self.session.flush()
        (self.store / f"{doc.id}.bin").write_bytes(data)

        text = (
            page_map
            and "\n\n".join(f"[page {p}]\n{t}" for p, t in page_map)
            or data.decode("utf-8", errors="replace")
        )
        if content_type == "application/pdf":
            text = extract_text(self.store / f"{doc.id}.bin", content_type)

        for idx, content in chunk_text(text):
            page = _page_for(content, text)
            self.session.add(
                DocumentChunk(
                    doc_id=doc.id,
                    filename=safe_name,
                    page=page,
                    chunk_idx=idx,
                    content=content,
                    tokens=len(content.split()),
                )
            )
        self.session.flush()
        count = len(
            self.session.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == doc.id)).all()
        )
        return IngestResult(doc_id=doc.id, filename=safe_name, chunks=count, sha256=digest)

    def search(self, query: str, limit: int = 5) -> list[RetrievedChunk]:
        return self.retriever.retrieve(query, limit)

    def list_documents(self) -> list[dict[str, Any]]:
        return [
            {
                "id": d.id,
                "filename": d.filename,
                "content_type": d.content_type,
                "size_bytes": d.size_bytes,
                "created_at": d.created_at.isoformat(),
            }
            for d in self.session.scalars(select(Document)).all()
        ]

    def delete_document(self, doc_id: str) -> bool:
        doc = self.session.get(Document, doc_id)
        if doc is None:
            return False
        self.session.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == doc_id))
        self.session.delete(doc)
        path = self.store / f"{doc_id}.bin"
        if path.exists():
            path.unlink()
        self.session.flush()
        return True

    def answer_guard(self, query: str, chunks: list[RetrievedChunk]) -> str | None:
        """Honesty gate: if nothing relevant was found, say so - do not invent."""
        if not chunks:
            return (
                "I could not find anything relevant in your documents for that. "
                "I will not invent an answer. Add the document, or ask me to reason "
                "without your files if that is what you want."
            )
        weak = all(c.score < 0.5 for c in chunks)
        if weak:
            return (
                "Your documents only contain weak matches for that question, so I can't "
                "answer them confidently. Here is what was closest: "
            )
        return None


def sanitize_filename(name: str) -> str:
    base = Path(str(name)).name
    base = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip(" ._")
    return (base or "document")[:120]


def _page_for(content: str, full_text: str) -> int | None:
    match = re.findall(r"\[page (\d+)\]", full_text)
    if not match:
        return None
    idx = full_text.find(content[:40])
    if idx < 0:
        return int(match[0])
    prefix = full_text[:idx]
    seen = len(re.findall(r"\[page (\d+)\]", prefix))
    return int(match[min(seen, len(match) - 1)])
