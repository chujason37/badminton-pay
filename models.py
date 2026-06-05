from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from sqlalchemy.types import JSON

from config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Participant(Base):
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True)
    chinese_name = Column(String, nullable=False, unique=True)
    pinyin = Column(String, default="")
    # List of reference strings this person has used (learned over time)
    known_references = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class GameSession(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    date = Column(String, nullable=False)
    location = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    entries = relationship("SessionEntry", back_populates="game_session", lazy="select")


class SessionEntry(Base):
    __tablename__ = "session_entries"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    participant_id = Column(Integer, ForeignKey("participants.id"), nullable=False)
    amount_owed = Column(Float, nullable=False)
    paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True)

    game_session = relationship("GameSession", back_populates="entries")
    participant = relationship("Participant")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    # NULL allowed — so duplicate-checking only applies when ID is present
    wise_transaction_id = Column(String, unique=True, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="GBP")
    reference = Column(String, default="")
    sender_name = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow)
    # pending | confirmed | unmatched
    status = Column(String, default="pending")
    matched_participant_id = Column(Integer, ForeignKey("participants.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
