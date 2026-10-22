"""Knowledge base with fake hashing-trick embeddings and cosine search."""

from __future__ import annotations

from app.models import KnowledgeChunk, KnowledgeDocument
from app.providers.embedding import FakeEmbeddingProvider
from app.services.speaker import cosine
from app.web.extensions import db


class KnowledgeService:
    def __init__(self, embedder: FakeEmbeddingProvider | None = None):
        self.embedder = embedder or FakeEmbeddingProvider()

    def create_document(
        self,
        title: str,
        content: str,
        project_id: str | None = None,
        created_by: str | None = None,
        source_meeting_id: str | None = None,
    ) -> KnowledgeDocument:
        doc = KnowledgeDocument(
            title=title,
            content=content,
            project_id=project_id,
            created_by=created_by,
            source_meeting_id=source_meeting_id,
            status="完成",
        )
        db.session.add(doc)
        db.session.flush()
        chunks = self._split(content)
        for i, text in enumerate(chunks):
            db.session.add(
                KnowledgeChunk(
                    document_id=doc.id,
                    ordinal=i,
                    text=text,
                    embedding=self.embedder.embed(text),
                )
            )
        db.session.commit()
        return doc

    def _split(self, content: str, size: int = 180) -> list[str]:
        content = (content or "").strip()
        if not content:
            return [""]
        return [content[i : i + size] for i in range(0, len(content), size)]

    def search(self, query: str, k: int = 5) -> list[dict]:
        qv = self.embedder.embed(query)
        scored = []
        for ch in KnowledgeChunk.query.all():
            scored.append(
                {
                    "id": ch.id,
                    "document_id": ch.document_id,
                    "text": ch.text,
                    "score": cosine(qv, ch.embedding or []),
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]

    def get(self, doc_id: str) -> KnowledgeDocument | None:
        return db.session.get(KnowledgeDocument, doc_id)

    def list_documents(self) -> list[KnowledgeDocument]:
        return KnowledgeDocument.query.order_by(KnowledgeDocument.created_at.desc()).all()
