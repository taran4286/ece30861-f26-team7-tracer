"""Tables and enums for Phase 3 (see AGENTS.md, "Data model and storage")."""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id(prefix: str) -> str:
    """Opaque, globally unique ID, e.g. inc_3f2a...; prefixes: inc, ev, aud."""
    return f"{prefix}_{uuid.uuid4().hex}"


# --- Enums -------------------------------------------------------------------


class State(StrEnum):
    UNDER_ASSESSMENT = "UnderAssessment"
    DETERMINED_MATERIAL = "DeterminedMaterial"
    DETERMINED_NOT_MATERIAL = "DeterminedNotMaterial"
    DISCLOSURE_DELAYED = "DisclosureDelayed"
    DISCLOSURE_RECORDED = "DisclosureRecorded"
    AMENDMENT_PENDING = "AmendmentPending"
    CLOSED = "Closed"


class FormType(StrEnum):
    FORM_8K_ITEM_105 = "8-K Item 1.05"
    FORM_8K_ITEM_801 = "8-K Item 8.01"
    FORM_6K = "6-K"
    FORM_8KA = "8-K/A"


class ImpactCategory(StrEnum):
    OPERATIONAL = "operational"
    FINANCIAL = "financial"
    LEGAL_REGULATORY = "legal_regulatory"
    REPUTATIONAL = "reputational"
    DATA_COMPROMISED = "data_compromised"


class AuditAction(StrEnum):
    CREATE_INCIDENT = "create_incident"
    IMPORT_EVIDENCE = "import_evidence"
    RECORD_DETERMINATION = "record_determination"
    TRANSITION = "transition"


class Decision(StrEnum):
    MATERIAL = "Material"
    NOT_MATERIAL = "NotMaterial"


class EvidenceSource(StrEnum):
    TICKETING = "ticketing"
    SIEM = "siem"


class SiemSeverity(StrEnum):
    """Allowed severity on SIEM import items (validated, not stored as a column)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def _db_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # Store the enum's values ("8-K Item 1.05"), not its member names (FORM_8K_ITEM_105).
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


# All timestamps are UTC.
UtcDateTime = DateTime(timezone=True)


# --- Tables ------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("inc"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    discovery_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[State] = mapped_column(
        _db_enum(State, "state"), nullable=False, default=State.UNDER_ASSESSMENT
    )
    # Null until a Material determination.
    required_form: Mapped[FormType | None] = mapped_column(_db_enum(FormType, "form_type"))
    # Always null in Phase 3.
    due_date: Mapped[date | None] = mapped_column(Date)
    group_id: Mapped[str | None] = mapped_column(Text)
    # Starts at 1; +1 on each determination and transition.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Determination(Base):
    __tablename__ = "determinations"

    # One per incident in Phase 3.
    incident_id: Mapped[str] = mapped_column(
        Text, ForeignKey("incidents.incident_id"), primary_key=True
    )
    decision: Mapped[Decision] = mapped_column(_db_enum(Decision, "decision"), nullable=False)
    determination_date: Mapped[date] = mapped_column(Date, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    voluntary_disclosure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Evidence(Base):
    __tablename__ = "evidence"

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ev"))
    incident_id: Mapped[str] = mapped_column(
        Text, ForeignKey("incidents.incident_id"), nullable=False, index=True
    )
    source: Mapped[EvidenceSource] = mapped_column(
        _db_enum(EvidenceSource, "evidence_source"), nullable=False
    )
    # From ticket_id or alert_id.
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # From summary or rule_name.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # From created_at or detected_at.
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    imported_by: Mapped[str] = mapped_column(Text, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    impact_categories: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # The original source item, kept for traceability.
    raw_item: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    # Sets "oldest first" order.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    entry_id: Mapped[str] = mapped_column(
        Text, unique=True, nullable=False, default=lambda: new_id("aud")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        _db_enum(AuditAction, "audit_action"), nullable=False
    )
    # The incident ID.
    target_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # none_as_null: a Python None is stored as SQL NULL, not JSON null.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
