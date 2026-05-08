from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Complaint(Base):
    __tablename__ = "complaints"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    author_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    author_role: Mapped[str] = mapped_column(String, index=True)
    target_type: Mapped[str] = mapped_column(String, index=True)
    target_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    target_pet_id: Mapped[str | None] = mapped_column(String, ForeignKey("pets.id"), nullable=True, index=True)
    walk_id: Mapped[str | None] = mapped_column(String, ForeignKey("walks.id"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="baixa", index=True)
    status: Mapped[str] = mapped_column(String, default="aberta", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[float] = mapped_column(Float, default=0)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=True)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    suggested_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    applied_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_admin_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)

    evidences = relationship("ComplaintEvidence", back_populates="complaint", cascade="all, delete-orphan")
    decisions = relationship("ComplaintDecision", back_populates="complaint", cascade="all, delete-orphan")
    history = relationship("ComplaintStatusHistory", back_populates="complaint", cascade="all, delete-orphan")


class ComplaintEvidence(Base):
    __tablename__ = "complaint_evidences"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    complaint_id: Mapped[str] = mapped_column(String, ForeignKey("complaints.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(String, default="note")
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    complaint = relationship("Complaint", back_populates="evidences")


class ComplaintDecision(Base):
    __tablename__ = "complaint_decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    complaint_id: Mapped[str] = mapped_column(String, ForeignKey("complaints.id"), index=True)
    decision_type: Mapped[str] = mapped_column(String, index=True)
    decision_status: Mapped[str] = mapped_column(String, default="suggested", index=True)
    severity_snapshot: Mapped[str] = mapped_column(String, default="baixa")
    reason: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String, default="decision_engine")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    complaint = relationship("Complaint", back_populates="decisions")


class ComplaintStatusHistory(Base):
    __tablename__ = "complaint_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    complaint_id: Mapped[str] = mapped_column(String, ForeignKey("complaints.id"), index=True)
    from_status: Mapped[str] = mapped_column(String, default="")
    to_status: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str] = mapped_column(String, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    complaint = relationship("Complaint", back_populates="history")


class RiskScore(Base):
    __tablename__ = "risk_scores"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String, index=True)
    subject_id: Mapped[str] = mapped_column(String, index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    severity: Mapped[str] = mapped_column(String, default="normal", index=True)
    complaints_count: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    shared_walk_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
