from datetime import datetime, date
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, EmailStr, Field


# ===================== Auth Schemas =====================

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = Field(..., pattern="^(caregiver|elder)$")
    phone: Optional[str] = None
    birth_date: Optional[date] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    phone: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class CaregiverContact(BaseModel):
    id: Optional[str] = None
    name: str
    phone: Optional[str] = None


class ElderSummaryForCaregiver(BaseModel):
    id: str
    name: str
    link_code: str
    last_alert_severity: Optional[str] = None
    online: bool = True


class ElderMeInfo(BaseModel):
    id: str
    link_code: str
    caregiver: Optional[CaregiverContact] = None


class MeResponse(BaseModel):
    user: UserOut
    elder: Optional[ElderMeInfo] = None
    elders: Optional[List[ElderSummaryForCaregiver]] = None


# ===================== Elder Profile Schemas =====================

class LinkElderRequest(BaseModel):
    link_code: str


class ElderProfileOut(BaseModel):
    id: str
    name: str
    age: Optional[int] = None
    birth_date: Optional[date] = None
    conditions: List[str] = []
    phone: Optional[str] = None
    doctor_name: Optional[str] = None
    home_lat: float
    home_lng: float
    geofence_m: float
    link_code: str
    caregiver_id: Optional[str] = None


# ===================== Vitals & Measurements =====================

class VitalsPoint(BaseModel):
    ts: datetime
    hr: float
    spo2: float
    steps: int
    temp: float
    resp_rate: float
    accel_mag: float
    activity: str


class BPData(BaseModel):
    ts: datetime
    systolic: float
    diastolic: float


class GlucoseData(BaseModel):
    ts: datetime
    value: float
    context: Optional[str] = None


class LatestVitalsResponse(BaseModel):
    vitals: Optional[VitalsPoint] = None
    bp: Optional[BPData] = None
    glucose: Optional[GlucoseData] = None
    status: str = "normal"  # normal | warning | critical


class MeasurementCreate(BaseModel):
    kind: str = Field(..., pattern="^(bp|glucose|weight)$")
    value1: float
    value2: Optional[float] = None
    context: Optional[str] = None


class MeasurementOut(BaseModel):
    id: str
    elder_id: str
    ts: datetime
    kind: str
    value1: float
    value2: Optional[float] = None
    context: Optional[str] = None
    source: str

    class Config:
        from_attributes = True


class LocationOut(BaseModel):
    lat: float
    lng: float
    ts: datetime
    distance_from_home_m: float
    inside_geofence: bool


class LocationTrailPoint(BaseModel):
    lat: float
    lng: float
    ts: datetime


# ===================== Medications =====================

class MedicationCreate(BaseModel):
    name: str
    dose: str
    times: List[str]  # e.g. ["08:00", "20:00"]
    days: Optional[List[int]] = []  # 0..6 (0=Mon), empty=all days
    notes: Optional[str] = None


class MedicationUpdate(BaseModel):
    name: Optional[str] = None
    dose: Optional[str] = None
    times: Optional[List[str]] = None
    days: Optional[List[int]] = None
    notes: Optional[str] = None
    active: Optional[bool] = None


class MedicationOut(BaseModel):
    id: str
    elder_id: str
    name: str
    dose: str
    times: List[str]
    days: List[int]
    notes: Optional[str] = None
    active: bool

    class Config:
        from_attributes = True


class MedLogOut(BaseModel):
    id: str
    medication_id: str
    elder_id: str
    name: str
    dose: str
    scheduled_at: datetime
    status: str  # pending | taken | missed
    confirmed_at: Optional[datetime] = None


class AdherenceResponse(BaseModel):
    taken: int
    missed: int
    pending: int
    rate: float


# ===================== Doctor Appointments =====================

class AppointmentCreate(BaseModel):
    doctor: str
    location: str
    ts: datetime
    notes: Optional[str] = None


class AppointmentUpdate(BaseModel):
    doctor: Optional[str] = None
    location: Optional[str] = None
    ts: Optional[datetime] = None
    notes: Optional[str] = None


class AppointmentOut(BaseModel):
    id: str
    elder_id: str
    doctor: str
    location: str
    ts: datetime
    notes: Optional[str] = None

    class Config:
        from_attributes = True


# ===================== Documents =====================

class DocumentOut(BaseModel):
    id: str
    elder_id: str
    title: str
    file_url: str
    mime: str
    size_bytes: int
    uploaded_at: datetime

    class Config:
        from_attributes = True


# ===================== Alerts & SOS =====================

class AlertOut(BaseModel):
    id: str
    elder_id: str
    type: str
    severity: str  # info | warning | critical
    message: str
    payload: Optional[Dict[str, Any]] = None
    source: str   # rule | baseline | ml | sos
    ts: datetime
    acknowledged: bool
    acknowledged_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SOSRequest(BaseModel):
    note: Optional[str] = None


# ===================== Reports =====================

class ReportMetadata(BaseModel):
    id: str
    elder_id: str
    period_start: datetime
    period_end: datetime
    generated_by: str
    created_at: datetime

    class Config:
        from_attributes = True


class ReportOut(ReportMetadata):
    content_md: str
    stats_json: Dict[str, Any]


# ===================== Demo Sim =====================

class SimTriggerRequest(BaseModel):
    scenario: str = Field(..., pattern="^(normal|fall|tachycardia|bradycardia|hypoxia|fever|wander|hypertensive|hypoglycemia|inactivity)$")
    duration_s: int = 30


class SimStatusResponse(BaseModel):
    scenario: str
    activity: str
    duration_remaining_s: int


# ===================== Health =====================

class HealthSimulatorStatus(BaseModel):
    active_elders: int


class HealthResponse(BaseModel):
    status: str = "ok"
    time: str
    simulator: HealthSimulatorStatus
