"""Document versioning model.

Tracks historical snapshots of Document content, enabling:
- Version history timeline
- Point-in-time knowledge queries
- Diff between versions
"""

import uuid

from sqlalchemy import Column, ForeignKey, Integer, JSON, String, Text
from sqlalchemy_utc import UtcDateTime, utcnow

from .library import Base


class DocumentVersion(Base):
    """Snapshot of a Document's content at a point in time."""

    __tablename__ = "document_versions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)

    # Snapshot of content
    text_content = Column(Text, nullable=True)
    structured_data = Column(JSON, nullable=True)

    # What triggered this version
    trigger = Column(
        String(50), nullable=False, default="initial"
    )  # initial, refresh, manual_edit, re_research
    trigger_research_id = Column(String(36), nullable=True)

    # Diff summary (human-readable)
    changes_summary = Column(Text, nullable=True)

    created_at = Column(UtcDateTime, default=utcnow)
