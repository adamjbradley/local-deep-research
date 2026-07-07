"""
Research History Indexer Service

Enables semantic search over research history by:
- Converting ResearchHistory reports into indexable Documents
- Linking documents to the Research History collection
- Triggering RAG indexing via LibraryRAGService
"""

import hashlib
import uuid
from datetime import datetime, UTC
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy.exc import IntegrityError

from ....constants import ResearchStatus
from ....database.library_init import ensure_research_history_collection
from ....database.models.library import (
    Document,
    DocumentCollection,
    DocumentStatus,
    SourceType,
)
from ....database.models.research import ResearchHistory
from ....database.session_context import get_user_db_session


class ResearchHistoryIndexer:
    """
    Service for indexing research history into a searchable collection.

    Converts research reports into Documents that can be indexed for
    semantic search using the existing RAG infrastructure.
    """

    # Source type names used in the database
    SOURCE_TYPE_REPORT = "research_report"
    SOURCE_TYPE_STRUCTURED = "structured_research"
    SOURCE_TYPE_STRUCTURED_SUMMARY = "structured_research_summary"
    COLLECTION_TYPE = "research_history"

    def __init__(self, username: str, db_password: Optional[str] = None):
        """
        Initialize the indexer for a user.

        Args:
            username: Username for database access
            db_password: Optional database password for encrypted DB access
        """
        self.username = username
        self.db_password = db_password

    def get_or_create_collection(self) -> str:
        """
        Get or create the Research History collection for this user.

        Returns:
            UUID of the Research History collection
        """
        return ensure_research_history_collection(
            self.username, self.db_password
        )

    def index_research(
        self,
        research_id: str,
        collection_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Convert a single research entry into a Document and add it to a
        collection.  Idempotent — safe to call multiple times.

        Args:
            research_id: UUID of the research to index
            collection_id: Target collection UUID (defaults to Research History)

        Returns:
            Dict with status and document count
        """
        if collection_id is None:
            collection_id = self.get_or_create_collection()

        with get_user_db_session(self.username, self.db_password) as session:
            research = (
                session.query(ResearchHistory)
                .filter(ResearchHistory.id == research_id)
                .first()
            )

            if not research:
                return {"status": "error", "error": "Research not found"}

            if research.status != ResearchStatus.COMPLETED:
                return {
                    "status": "error",
                    "error": "Research is not yet completed",
                }

            if not research.report_content:
                return {
                    "status": "error",
                    "error": "Research has no report content",
                }

            # Check if this is structured research
            is_structured = research.mode == "structured"
            meta = research.research_meta or {}
            structured_data = meta.get("structured_data") if is_structured else None

            try:
                if is_structured and structured_data:
                    docs_added = self._index_structured_research(
                        research, structured_data, collection_id, session
                    )
                else:
                    report_doc = self._create_document_from_report(
                        research, collection_id, session
                    )
                    if report_doc is None:
                        return {
                            "status": "error",
                            "error": "SourceType not found. Run library initialization.",
                        }
                    docs_added = 1

                logger.info(
                    f"Created/found {docs_added} document(s) for research: {research_id[:8]}"
                )
            except Exception:
                logger.exception("Error creating documents")
                return {
                    "status": "error",
                    "error": "Failed to create documents",
                }

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.info(
                    f"DocumentCollection already exists for research "
                    f"{research_id[:8]} (concurrent insert)"
                )

            return {
                "status": "success",
                "research_id": research_id,
                "collection_id": collection_id,
                "documents_added": docs_added,
            }

    def convert_all_research(self, force: bool = False) -> Dict[str, Any]:
        """
        Convert all completed research entries into Documents (without RAG indexing).

        Unlike index_all_research / index_research, this method operates within a
        single DB session and calls the private helpers directly, avoiding the
        nested-session issues that arise on SQLite when index_research opens its
        own session inside a loop.

        Args:
            force: If True, process all entries even if already converted.
                   If False (default), skip entries that already have a report
                   Document.

        Returns:
            Dict with:
                - converted: Number of research entries successfully converted
                - skipped:   Number of entries skipped (already converted)
                - failed:    Number of entries that raised an exception
                - collection_id: UUID of the Research History collection

        Only report Documents are created; source documents are not indexed.
        """
        collection_id = self.get_or_create_collection()

        with get_user_db_session(self.username, self.db_password) as session:
            # Resolve the report SourceType — required to create report Documents
            report_type = (
                session.query(SourceType)
                .filter_by(name=self.SOURCE_TYPE_REPORT)
                .first()
            )
            if report_type is None:
                logger.warning(
                    f"SourceType '{self.SOURCE_TYPE_REPORT}' not found. "
                    "Run library initialization to seed source types before "
                    "converting research history."
                )
                return {
                    "converted": 0,
                    "skipped": 0,
                    "failed": 0,
                    "collection_id": collection_id,
                }

            # Build subquery of research IDs that already have a report Document
            already_converted_subquery = (
                session.query(Document.research_id)
                .filter(Document.source_type_id == report_type.id)
                .filter(Document.research_id.isnot(None))
                .distinct()
                .subquery()
            )

            # Count total eligible research entries (before filtering)
            total_eligible = (
                session.query(ResearchHistory)
                .filter(ResearchHistory.status == ResearchStatus.COMPLETED)
                .filter(ResearchHistory.report_content.isnot(None))
                .filter(ResearchHistory.report_content != "")
                .count()
            )

            # Fetch candidates — optionally excluding already-converted entries
            query = (
                session.query(ResearchHistory)
                .filter(ResearchHistory.status == ResearchStatus.COMPLETED)
                .filter(ResearchHistory.report_content.isnot(None))
                .filter(ResearchHistory.report_content != "")
                .order_by(ResearchHistory.created_at.desc())
            )
            if not force:
                query = query.filter(
                    ResearchHistory.id.notin_(
                        already_converted_subquery.select()
                    )
                )

            research_entries = query.all()

            converted = 0
            skipped = total_eligible - len(research_entries) if not force else 0
            failed = 0

            for research in research_entries:
                try:
                    # Create (or reuse) report Document
                    report_doc = self._create_document_from_report(
                        research,
                        collection_id,
                        session,
                        report_type_id=report_type.id,
                    )
                    if report_doc is None:
                        # SourceType missing inside helper (already warned)
                        failed += 1
                        continue

                    # Commit each entry individually so a rollback on failure
                    # only loses the failing entry, not the whole batch.
                    session.commit()
                    converted += 1

                except Exception:
                    logger.exception(f"Error converting research {research.id}")
                    session.rollback()
                    failed += 1

        logger.info(
            f"convert_all_research complete — converted={converted}, "
            f"skipped={skipped}, failed={failed}"
        )
        return {
            "converted": converted,
            "skipped": skipped,
            "failed": failed,
            "collection_id": collection_id,
        }

    def _index_structured_research(
        self,
        research: ResearchHistory,
        structured_data: Dict[str, Any],
        collection_id: str,
        session,
    ) -> int:
        """Index structured research as Documents in the library.

        Creates:
        1. One parent Document (source_type: structured_research) with the
           markdown summary as text_content and structured metadata in notes.
        2. One Document per dimension summary (source_type: structured_research_summary)
           with the narrative content — these get RAG-indexed for semantic search.

        Returns:
            Number of documents created.
        """
        import json as _json

        docs_created = 0

        # Resolve source types
        structured_type = (
            session.query(SourceType)
            .filter_by(name=self.SOURCE_TYPE_STRUCTURED)
            .first()
        )
        summary_type = (
            session.query(SourceType)
            .filter_by(name=self.SOURCE_TYPE_STRUCTURED_SUMMARY)
            .first()
        )

        if not structured_type:
            logger.warning(
                f"SourceType '{self.SOURCE_TYPE_STRUCTURED}' not found. "
                "Run library initialization."
            )
            # Fall back to regular report indexing
            self._create_document_from_report(research, collection_id, session)
            return 1

        # Build tags from dimensions
        tags = []
        cells = structured_data.get("cells", [])
        schema = structured_data.get("schema", {})
        for cell in cells:
            for k, v in cell.get("dimension_values", {}).items():
                tag = f"{k}:{v}"
                if tag not in tags:
                    tags.append(tag)

        # Compact structured metadata for notes field
        compact_meta = {
            "cells_count": len(cells),
            "items_count": sum(len(c.get("items", [])) for c in cells),
            "sources_count": len(structured_data.get("sources", [])),
            "dimensions": [d.get("name") for d in schema.get("dimensions", [])],
            "fields": [f.get("name") for f in schema.get("fields", [])],
        }

        # 1. Parent Document
        content = research.report_content or ""
        doc_hash = hashlib.sha256(
            f"structured:{research.id}:{content[:100]}".encode()
        ).hexdigest()

        existing = (
            session.query(Document)
            .filter_by(research_id=research.id, source_type_id=structured_type.id)
            .first()
        )

        if not existing:
            parent_doc = Document(
                id=str(uuid.uuid4()),
                source_type_id=structured_type.id,
                research_id=research.id,
                document_hash=doc_hash,
                file_size=len(content.encode("utf-8")),
                file_type="markdown",
                mime_type="text/markdown",
                title=research.title or (research.query[:100] if research.query else "Untitled"),
                text_content=content,
                tags=tags,
                notes=_json.dumps(compact_meta),
                status=DocumentStatus.COMPLETED,
                processed_at=datetime.now(UTC),
                character_count=len(content),
                word_count=len(content.split()),
            )
            session.add(parent_doc)
            session.flush()
            self._ensure_in_collection(parent_doc.id, collection_id, session)
            docs_created += 1

        # 2. Summary Documents (one per dimension summary)
        summaries = structured_data.get("summaries", [])
        if summary_type and summaries:
            for s in summaries:
                dim_value = s.get("dimension_value", "")
                summary_content = s.get("content", "")
                if not summary_content:
                    continue

                s_hash = hashlib.sha256(
                    f"summary:{research.id}:{dim_value}:{summary_content[:50]}".encode()
                ).hexdigest()

                existing_summary = (
                    session.query(Document)
                    .filter_by(document_hash=s_hash)
                    .first()
                )

                if not existing_summary:
                    dim_name = s.get("dimension_name", "")
                    summary_doc = Document(
                        id=str(uuid.uuid4()),
                        source_type_id=summary_type.id,
                        research_id=research.id,
                        document_hash=s_hash,
                        file_size=len(summary_content.encode("utf-8")),
                        file_type="markdown",
                        mime_type="text/markdown",
                        title=s.get("title", f"{dim_value} Summary"),
                        text_content=summary_content,
                        tags=[f"{dim_name}:{dim_value}"] if dim_name else [dim_value],
                        status=DocumentStatus.COMPLETED,
                        processed_at=datetime.now(UTC),
                        character_count=len(summary_content),
                        word_count=len(summary_content.split()),
                    )
                    session.add(summary_doc)
                    session.flush()
                    self._ensure_in_collection(summary_doc.id, collection_id, session)
                    docs_created += 1

        logger.info(
            f"Indexed structured research {research.id[:8]}: "
            f"{docs_created} docs (1 parent + {len(summaries)} summaries)"
        )
        return docs_created

    def _create_document_from_report(
        self,
        research: ResearchHistory,
        collection_id: str,
        session,
        report_type_id: Optional[str] = None,
    ) -> Optional[Document]:
        """
        Create a Document from a research report.

        Args:
            research: ResearchHistory entry
            collection_id: Target collection UUID
            session: Database session
            report_type_id: Pre-resolved SourceType ID (avoids N+1 queries
                when called in a loop from convert_all_research)

        Returns:
            Created Document or None if skipped
        """
        # Resolve report SourceType (cached ID avoids per-entry query)
        if report_type_id is None:
            report_type = (
                session.query(SourceType)
                .filter_by(name=self.SOURCE_TYPE_REPORT)
                .first()
            )
            if report_type is None:
                logger.warning(
                    f"SourceType '{self.SOURCE_TYPE_REPORT}' not found for research "
                    f"{research.id}. Cannot create document — run library initialization "
                    "to seed source types."
                )
                return None
            report_type_id = report_type.id
        existing_doc = (
            session.query(Document)
            .filter(Document.research_id == research.id)
            .filter(Document.source_type_id == report_type_id)
            .first()
        )

        if existing_doc:
            # Ensure it's in the collection
            self._ensure_in_collection(existing_doc.id, collection_id, session)
            return existing_doc

        # Create document or reuse existing one with same content hash
        # (document_hash has a unique constraint, so identical content
        # must share a Document row — research_id points to the first creator)
        content = research.report_content
        doc_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        document = (
            session.query(Document)
            .filter(Document.document_hash == doc_hash)
            .first()
        )

        if document is None:
            doc_id = str(uuid.uuid4())
            document = Document(
                id=doc_id,
                source_type_id=report_type_id,
                research_id=research.id,
                document_hash=doc_hash,
                file_size=len(content.encode("utf-8")),
                file_type="markdown",
                mime_type="text/markdown",
                title=research.title
                or (research.query[:100] if research.query else "Untitled"),
                text_content=content,
                status=DocumentStatus.COMPLETED,
                processed_at=datetime.now(UTC),
                character_count=len(content),
                word_count=len(content.split()),
            )
            session.add(document)
            session.flush()

        self._ensure_in_collection(document.id, collection_id, session)
        return document

    def _ensure_in_collection(
        self, document_id: str, collection_id: str, session
    ) -> None:
        """Add document to collection if not already there."""
        existing = (
            session.query(DocumentCollection)
            .filter_by(document_id=document_id, collection_id=collection_id)
            .first()
        )
        if not existing:
            session.add(
                DocumentCollection(
                    document_id=document_id,
                    collection_id=collection_id,
                    indexed=False,
                )
            )


def auto_convert_research(
    username: str, research_id: str, db_password: str | None = None
):
    """Auto-convert a completed research entry to a document in the History collection.

    Safe to call from any context — exceptions are caught and logged.
    """
    try:
        indexer = ResearchHistoryIndexer(username, db_password=db_password)
        result = indexer.index_research(research_id)
        logger.info(
            f"Auto-converted research {research_id} for user {username}: "
            f"{result.get('status')}"
        )
    except Exception:
        logger.exception(
            f"Failed to auto-convert research {research_id} for user {username}"
        )
