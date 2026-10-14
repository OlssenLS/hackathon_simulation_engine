import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Boolean,
    Float,
    Integer,
    BigInteger,
    DateTime,
    Date,
    JSON,
    ForeignKey,
    Index,
    UniqueConstraint,
    Text,
)
from sqlalchemy.orm import relationship

from app.db import Base


def get_utc_now():
    return datetime.now(timezone.utc)


def generate_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False)  # 'caregiver' | 'elder'
    phone = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)

    elder_profile = relationship("ElderProfile", back_populates="user", uselist=False, foreign_keys="ElderProfile.user_id")


class ElderProfile(Base):
    __tablename__ = "elder_profiles"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    birth_date = Column(Date, nullable=True)
    conditions = Column(JSON, default=list, nullable=False)  # list of strings
    link_code = Column(String(6), unique=True, index=True, nullable=False)
    caregiver_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    doctor_name = Column(String(255), nullable=True)
    home_lat = Column(Float, nullable=False, default=-6.2349)
    home_lng = Column(Float, nullable=False, default=106.9896)
    geofence_m = Column(Float, nullable=False, default=150.0)
    sim_enabled = Column(Boolean, nullable=False, default=True)

    user = relationship("User", foreign_keys=[user_id], back_populates="elder_profile")
    caregiver = relationship("User", foreign_keys=[caregiver_id])


class Vital(Base):
    __tablename__ = "vitals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    hr = Column(Float, nullable=False)
    spo2 = Column(Float, nullable=False)
    steps = Column(Integer, nullable=False)
    temp = Column(Float, nullable=False)
    resp_rate = Column(Float, nullable=False)
    accel_mag = Column(Float, nullable=False)
    activity = Column(String(50), nullable=False)  # sleeping, resting, walking, exercising

    __table_args__ = (
        Index("ix_vitals_elder_ts_desc", "elder_id", ts.desc()),
    )


class Measurement(Base):
    __tablename__ = "measurements"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    kind = Column(String(50), nullable=False)  # 'bp' | 'glucose' | 'weight'
    value1 = Column(Float, nullable=False)     # systolic | glucose | kg
    value2 = Column(Float, nullable=True)      # diastolic
    context = Column(String(50), nullable=True)  # 'fasting' | 'postprandial' | 'random'
    source = Column(String(50), nullable=False, default="sim")  # 'sim' | 'manual'

    __table_args__ = (
        Index("ix_measurements_elder_kind_ts_desc", "elder_id", "kind", ts.desc()),
    )


class Location(Base):
    __tablename__ = "locations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_locations_elder_ts_desc", "elder_id", ts.desc()),
    )


class Medication(Base):
    __tablename__ = "medications"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    dose = Column(String(100), nullable=False)
    times = Column(JSON, default=list, nullable=False)  # ["08:00", "20:00"]
    days = Column(JSON, default=list, nullable=False)   # [0, 1, ...], empty = all days
    notes = Column(Text, nullable=True)
    active = Column(Boolean, default=True, nullable=False)


class MedLog(Base):
    __tablename__ = "med_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    medication_id = Column(String(36), ForeignKey("medications.id", ondelete="CASCADE"), nullable=False, index=True)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(50), default="pending", nullable=False)  # 'pending' | 'taken' | 'missed'
    confirmed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("medication_id", "scheduled_at", name="uq_med_log_med_scheduled"),
    )

    medication = relationship("Medication", foreign_keys=[medication_id])


class DoctorAppt(Base):
    __tablename__ = "doctor_appts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    notes = Column(Text, nullable=True)


class Document(Base):
    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    file_url = Column(String(1024), nullable=False)
    mime = Column(String(100), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(100), nullable=False)
    severity = Column(String(50), nullable=False)  # 'info' | 'warning' | 'critical'
    message = Column(Text, nullable=False)          # Bahasa Indonesia
    payload = Column(JSON, nullable=True)
    source = Column(String(50), nullable=False)    # 'rule' | 'baseline' | 'ml' | 'sos'
    ts = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_alerts_elder_ts_desc", "elder_id", ts.desc()),
    )


class Report(Base):
    __tablename__ = "reports"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elder_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    content_md = Column(Text, nullable=False)
    stats_json = Column(JSON, nullable=False)
    generated_by = Column(String(50), nullable=False)  # 'llm' | 'template'
    created_at = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)
