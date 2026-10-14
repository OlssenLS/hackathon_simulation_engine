# Elder Care Monitoring: Backend PRD & Technical Spec

> **Audience:** an autonomous coding agent building the backend from scratch in one pass.
> **Context:** hackathon. Speed and a working live demo beat completeness. Everything must run on free tiers.
> **Frontend:** Flutter app built by a teammate. This backend is consumed over REST + WebSocket.
> **Language:** code, identifiers, and comments in English. User-facing strings (alert messages, reports, error messages shown in UI) in **Bahasa Indonesia**.

---

## 0. Instructions for the Building Agent

1. Build in the order given in **Section 17**. After each phase, run the acceptance check listed there before moving on.
2. Do not ask clarifying questions. Where the spec is silent, choose the simplest option and note it in `DECISIONS.md`.
3. Prefer fewer files and fewer abstractions. No repository pattern, no DI framework, no Alembic (use `create_all`).
4. Everything must start with `uvicorn app.main:app` and work with only the env vars in Section 15.
5. Run **exactly one** uvicorn worker (simulator and WebSocket manager live in process memory).
6. Seed data and demo accounts (Section 14) must be created automatically on first startup if the DB is empty.
7. All timestamps: UTC in DB and API (ISO 8601 with `Z`). Flutter converts to local.
8. Medical disclaimer: the system is a monitoring and alerting aid, **never diagnoses**. All generated text must reflect this.
9. At the end, produce `README.md` (run locally, deploy, demo script) and verify `/docs` shows every endpoint.

---

## 1. Product Overview

A monitoring platform for elderly people (**lansia**) and their family caregivers.

- **Lansia** wears (in the real product) a smartwatch and uses a very simple app: see medication schedule, doctor appointments, call family, press SOS.
- **Caregiver** monitors the lansia remotely: live vitals, location, medication adherence, alerts, documents, and AI-generated health reports.

**Hackathon constraint:** no real wearable. The backend includes a **Simulator** that generates realistic vitals and GPS data, so the Flutter app experiences "real-time monitoring". The simulator is a first-class feature, with scenario injection for live demos.

### Goals
- Realistic live data stream per lansia that makes the dashboard look alive.
- Anomaly detection that visibly catches injected incidents (fall, tachycardia, hypoxia, wandering, hypertensive spike, hypoglycemia).
- Auto-SOS on suspected fall.
- AI "Smart Report" in Bahasa Indonesia built from aggregated stats.
- A demo control endpoint so the presenter can trigger incidents on stage.

### Non-goals
Real device pairing, HIPAA-grade security, multi-tenancy, billing, admin panel, real phone-call infrastructure, PDF export (Markdown only).

---

## 2. Health Metrics Monitored

Metrics are split by how they would be acquired in the real product. This matters for credibility with judges: continuous data comes from a wearable, periodic data comes from a connected cuff/glucometer or manual entry.

### 2.1 Continuous (wearable, streamed every 3 s in simulation)

| Metric | Field | Unit | Normal (resting) | Notes |
|---|---|---|---|---|
| Heart rate | `hr` | bpm | 55 to 90 | Core metric |
| Blood oxygen | `spo2` | % | 95 to 99 | Core metric |
| Steps (cumulative today) | `steps` | count | n/a | Resets at local midnight (Asia/Jakarta) |
| Skin temperature | `temp` | °C | 36.0 to 37.2 | Wrist skin temp reads slightly lower than core |
| Respiratory rate | `resp_rate` | breaths/min | 12 to 20 | Optional, derived metric on newer watches |
| Motion magnitude | `accel_mag` | g | about 1.0 | Used for fall detection |
| Activity state | `activity` | enum | n/a | `sleeping`, `resting`, `walking`, `exercising` |
| GPS | `lat`, `lng` | deg | inside geofence | Separate `locations` table |

Derived (computed in analysis, not stored): sleep duration (minutes where `activity=sleeping`), resting HR (daily 10th percentile of HR while `resting`), HR variability proxy (5 min HR std dev), daily activity minutes.

### 2.2 Periodic (connected cuff / glucometer, or manual entry)

These are **not** reliably measurable by consumer smartwatches. In the product they come from a Bluetooth blood-pressure cuff, a glucometer, or manual input by the lansia/caregiver. The simulator emits them on a schedule and the API also accepts manual submission.

| Metric | Fields | Unit | Normal | Schedule (simulated, Asia/Jakarta) |
|---|---|---|---|---|
| Blood pressure | `systolic`, `diastolic` | mmHg | 90 to 130 / 60 to 85 | 08:00 and 20:00 |
| Blood glucose | `glucose`, `context` (`fasting`/`postprandial`/`random`) | mg/dL | fasting 70 to 110; 2 h post-meal under 160 | 07:00 fasting, 13:00 and 19:00 postprandial |
| Body weight (optional) | `weight` | kg | stable | weekly, only if time permits |

### 2.3 Alert thresholds (single source of truth, implement in `app/config/thresholds.py`)

| Metric | Warning | Critical |
|---|---|---|
| SpO2 | under 94 | under 90 |
| HR resting | above 110 or under 50 | above 130 or under 40 |
| HR exercising | above 150 | above 170 |
| Systolic BP | at or above 140, or under 100 | at or above 180, or under 85 |
| Diastolic BP | at or above 90, or under 60 | at or above 120 |
| Glucose | fasting above 126, postprandial above 180, or under 70 | above 300 or under 54 |
| Temperature (skin) | at or above 37.8 or under 35.5 | at or above 38.5 or under 34.5 |
| Resp rate | above 24 or under 10 | above 30 or under 8 |

Thresholds are **demo-grade defaults**, not clinical guidance. Per-elder overrides are out of scope; keep the constants in one file so they are easy to change.

---

## 3. User Roles & Flows

### Flow
`Onboarding -> Login / Sign Up -> choose role (Lansia | Caregiver)`

- **Lansia** registers and receives a unique 6-character `link_code` (uppercase alphanumeric, no ambiguous chars like 0/O/1/I).
- **Caregiver** registers, then links to a lansia by entering that code (`POST /elders/link`). One caregiver can link multiple lansia. One lansia has one primary caregiver in MVP (re-linking replaces it).

### Caregiver features
1. Live condition dashboard (all metrics in 2.1 and 2.2)
2. Medication scheduling (CRUD)
3. Documents: upload and list files
4. Elder GPS (live + short trail)
5. Auto-triggered SOS on suspected fall
6. Automated anomaly detection (rules + personal baseline + ML)
7. **Smart Report Generation** (LLM over aggregated stats)

### Lansia features
1. View today's medication schedule and confirm "sudah minum"
2. Call family (API returns caregiver phone; Flutter uses `tel:`)
3. View doctor appointments
4. Manual SOS button

### Authorization rules
- Caregiver may access `/elders/{id}/...` only if linked to that elder.
- Lansia may access only their own `/elders/{own_id}/...` and only: medications (read), med-log confirm, appointments (read), SOS, caregiver contact.
- Return `403` otherwise.

---

## 4. Tech Stack & Framework Decision

**Framework: FastAPI (Python 3.11).** Reasoning: the simulator, anomaly ML (scikit-learn) and LLM report all live in Python. A Node/NestJS backend would require a second runtime or service for the ML part. FastAPI also provides Swagger UI at `/docs` and `/openapi.json` automatically, which unblocks the Flutter teammate immediately.

| Concern | Choice |
|---|---|
| Web framework | FastAPI + Uvicorn (`uvicorn[standard]`) |
| ORM | SQLAlchemy 2.0 async + `asyncpg` |
| Validation / settings | Pydantic v2 + `pydantic-settings` |
| DB | Neon Postgres (free). Supabase Postgres is an acceptable alternative |
| Auth | JWT (HS256) via `python-jose`; passwords via `passlib[bcrypt]` with **`bcrypt==4.0.1` pinned** (avoids the passlib/bcrypt 4.1+ incompatibility) |
| Realtime | Native FastAPI WebSocket |
| ML | `scikit-learn` (IsolationForest), `numpy`, `joblib` |
| LLM | Provider-agnostic wrapper over **Gemini API** (free tier), with **Groq** as alternate. Use `httpx` REST calls, no vendor SDK. Model name comes from env var |
| File storage | Supabase Storage REST (if env set), else local `./uploads` (ephemeral on Render, fine for demo) |
| Push (optional) | Firebase Cloud Messaging, only if time remains. Not required |

### Requirements file (`requirements.txt`)
```
fastapi
uvicorn[standard]
sqlalchemy[asyncio]>=2.0
asyncpg
pydantic>=2
pydantic-settings
python-jose[cryptography]
passlib[bcrypt]
bcrypt==4.0.1
python-multipart
httpx
numpy
scikit-learn
joblib
tzdata
```

---

## 5. Architecture

```
Flutter ──REST / WS──► FastAPI app (single process, single worker)
                          │
                          ├─ Routers (auth, elders, vitals, meds, appts, docs, alerts, reports, sim)
                          ├─ ConnectionManager (in-memory WS rooms, keyed by elder_id)
                          ├─ Simulator (asyncio background task: one loop ticking all active elders)
                          ├─ Anomaly Engine (Layer 1 rules, Layer 2 z-score baseline, Layer 3 IsolationForest)
                          ├─ Med Scheduler (asyncio task: generates med_logs, marks missed)
                          └─ Report Service (aggregate stats -> LLM -> Markdown, with template fallback)
                          │
                       Postgres (Neon)
```

**Data flow (live):**
`Simulator tick -> build reading -> persist -> Anomaly Engine -> (if anomaly) persist alert -> WS broadcast {vitals} and {alert} to the elder's room`.

**Lifespan startup order:** create tables -> seed if empty -> load/train IsolationForest -> start simulator -> start med scheduler.

---

## 6. Project Structure

```
elder-care-backend/
├─ app/
│  ├─ main.py                 # app, lifespan, CORS, router includes, /health
│  ├─ config.py               # Settings (pydantic-settings)
│  ├─ thresholds.py           # constants from section 2.3
│  ├─ db.py                   # async engine, session dependency, Base
│  ├─ models.py               # all SQLAlchemy models
│  ├─ schemas.py              # all Pydantic schemas
│  ├─ security.py             # hashing, JWT, get_current_user, access guards
│  ├─ ws.py                   # ConnectionManager + /ws endpoint
│  ├─ routers/
│  │   ├─ auth.py
│  │   ├─ elders.py           # link, profile, caregiver contact
│  │   ├─ vitals.py           # latest, history, measurements (bp/glucose), location
│  │   ├─ medications.py
│  │   ├─ appointments.py
│  │   ├─ documents.py
│  │   ├─ alerts.py           # list, ack, manual SOS
│  │   ├─ reports.py
│  │   └─ sim.py              # demo control
│  ├─ services/
│  │   ├─ simulator.py
│  │   ├─ anomaly.py
│  │   ├─ med_scheduler.py
│  │   ├─ report.py
│  │   ├─ llm.py
│  │   └─ storage.py
│  └─ seed.py
├─ ml/                         # model artifacts written at runtime (isoforest.joblib)
├─ requirements.txt
├─ Dockerfile
├─ render.yaml
├─ .env.example
├─ README.md
└─ DECISIONS.md
```

---

## 7. Data Model

All `id` are UUID (string) primary keys unless stated. All `ts` columns are timezone-aware UTC.

```
users
  id, name, email (unique), password_hash, role ('caregiver'|'elder'),
  phone (nullable), created_at

elder_profiles
  user_id (PK, FK users), birth_date, conditions (JSON list of strings),
  link_code (unique, 6 chars), caregiver_id (FK users, nullable),
  doctor_name (nullable), home_lat, home_lng, geofence_m (default 150),
  sim_enabled (bool, default true)

vitals                                   -- continuous stream
  id (bigserial), elder_id, ts, hr, spo2, steps, temp, resp_rate,
  accel_mag, activity
  INDEX (elder_id, ts DESC)

measurements                             -- periodic BP / glucose / weight
  id, elder_id, ts, kind ('bp'|'glucose'|'weight'),
  value1 (systolic | glucose | kg), value2 (diastolic, nullable),
  context (nullable: 'fasting'|'postprandial'|'random'), source ('sim'|'manual')
  INDEX (elder_id, kind, ts DESC)

locations
  id (bigserial), elder_id, ts, lat, lng
  INDEX (elder_id, ts DESC)

medications
  id, elder_id, name, dose (string), times (JSON list of "HH:MM" local),
  days (JSON list ints 0..6, Mon=0; empty = every day), notes, active (bool)

med_logs
  id, medication_id, elder_id, scheduled_at (UTC), status ('pending'|'taken'|'missed'),
  confirmed_at (nullable)
  UNIQUE (medication_id, scheduled_at)

doctor_appts
  id, elder_id, doctor, location, ts, notes

documents
  id, elder_id, title, file_url, mime, size_bytes, uploaded_at

alerts
  id, elder_id, type, severity ('info'|'warning'|'critical'),
  message (Bahasa Indonesia), payload (JSON), source ('rule'|'baseline'|'ml'|'sos'),
  ts, acknowledged (bool), acknowledged_at
  INDEX (elder_id, ts DESC)

reports
  id, elder_id, period_start, period_end, content_md, stats_json,
  generated_by ('llm'|'template'), created_at
```

**Alert `type` values:** `spo2_low`, `hr_high`, `hr_low`, `bp_high`, `bp_low`, `glucose_high`, `glucose_low`, `temp_high`, `temp_low`, `resp_abnormal`, `fall_suspected`, `sos_manual`, `geofence_exit`, `inactivity`, `med_missed`, `anomaly_baseline`, `anomaly_ml`.

---

## 8. REST API Contract

Base path: none (root). All endpoints except `/health`, `/auth/register`, `/auth/login` require `Authorization: Bearer <jwt>`.
Errors: `{"detail": "<message>"}` with proper status codes.

### 8.1 Auth
- `POST /auth/register` body `{name, email, password, role, phone?, birth_date?}` returns `{access_token, token_type, user}`. If `role=elder`, also create `elder_profiles` with a generated `link_code` and default home coordinates (seed default: Jakarta area, configurable).
- `POST /auth/login` body `{email, password}` returns same shape as register.
- `GET /me` returns user. For elders include `{elder: {id, link_code, caregiver: {id, name, phone} | null}}`. For caregivers include `{elders: [{id, name, link_code, last_alert_severity, online}]}`.

JWT lifetime: 24 h in dev, configurable.

### 8.2 Linking
- `POST /elders/link` body `{link_code}` (caregiver only) sets `caregiver_id`; returns elder summary. `404` for bad code.
- `GET /elders` (caregiver) returns linked elders.
- `GET /elders/{id}` returns profile (name, age, conditions, phone, doctor_name, home coords, geofence).
- `GET /elders/{id}/caregiver` returns `{name, phone}` for the "Panggil Keluarga" button.

### 8.3 Monitoring
- `GET /elders/{id}/vitals/latest` returns the most recent vitals row plus latest BP and glucose measurement:
  ```json
  {"vitals": {"ts": "...", "hr": 72, "spo2": 97, "steps": 3120, "temp": 36.6,
              "resp_rate": 16, "accel_mag": 1.02, "activity": "resting"},
   "bp": {"ts": "...", "systolic": 128, "diastolic": 82},
   "glucose": {"ts": "...", "value": 104, "context": "fasting"},
   "status": "normal"}
  ```
  `status` is the worst severity among unacknowledged alerts in the last 30 min (`normal|warning|critical`).
- `GET /elders/{id}/vitals?from=&to=&interval=raw|1m|5m|1h&metrics=hr,spo2` returns downsampled time series `[{ts, hr, spo2, ...}]`. Default: last 24 h, `5m`. Cap at 2000 points.
- `GET /elders/{id}/measurements?kind=bp|glucose&days=30` returns history.
- `POST /elders/{id}/measurements` manual entry `{kind, value1, value2?, context?}`. Runs through the same threshold check and may raise an alert.
- `GET /elders/{id}/location/latest` returns `{lat, lng, ts, distance_from_home_m, inside_geofence}`.
- `GET /elders/{id}/location/trail?minutes=60` returns points.

### 8.4 Medications (caregiver full CRUD; elder read-only)
- `GET /elders/{id}/medications`
- `POST /elders/{id}/medications` body `{name, dose, times:["08:00","20:00"], days?:[], notes?}`
- `PUT /elders/{id}/medications/{med_id}`
- `DELETE /elders/{id}/medications/{med_id}` (soft delete: `active=false`)
- `GET /elders/{id}/med-logs?date=YYYY-MM-DD` returns today's doses with status, ordered by time. This is the elder's main screen.
- `POST /med-logs/{log_id}/confirm` (elder or linked caregiver) sets `taken`, `confirmed_at=now`.
- `GET /elders/{id}/medications/adherence?days=7` returns `{taken, missed, pending, rate}`.

### 8.5 Appointments
- `GET|POST /elders/{id}/appointments`, `PUT|DELETE /elders/{id}/appointments/{appt_id}`. `GET` supports `?upcoming=true`.

### 8.6 Documents
- `POST /elders/{id}/documents` multipart (`title`, `file`), max 5 MB, allow pdf/png/jpg. Returns document row with `file_url`.
- `GET /elders/{id}/documents`
- `DELETE /elders/{id}/documents/{doc_id}`

### 8.7 Alerts & SOS
- `GET /elders/{id}/alerts?severity=&acknowledged=&limit=50`
- `POST /alerts/{id}/ack`
- `POST /elders/{id}/sos` (elder or caregiver) creates `sos_manual` critical alert, broadcasts over WS. Body optional `{note}`.
- Fall-triggered SOS is created automatically by the anomaly engine (Section 10) as `fall_suspected`, critical.

### 8.8 Reports
- `POST /elders/{id}/reports/generate?days=7` (caregiver). Synchronous; target under 15 s. Returns the report.
- `GET /elders/{id}/reports` list (metadata only), `GET /reports/{id}` full content.

### 8.9 Demo control (no role restriction beyond being authenticated; keep enabled in all envs for the hackathon)
- `POST /sim/{elder_id}/trigger` body `{"scenario": "...", "duration_s": 30}`

  | scenario | Effect |
  |---|---|
  | `normal` | Clear any active scenario |
  | `fall` | accel spike over 3 g for 1 tick, then accel about 0 and `activity=resting`, HR +25 for `duration_s`. Must cause `fall_suspected` SOS within 20 s |
  | `tachycardia` | HR ramps to 135 and holds |
  | `bradycardia` | HR ramps down to 38 |
  | `hypoxia` | SpO2 ramps down to 87 |
  | `fever` | temp ramps to 38.6 |
  | `wander` | GPS drifts steadily away from home beyond geofence |
  | `hypertensive` | immediately inserts BP measurement 192/124 |
  | `hypoglycemia` | immediately inserts glucose 48 mg/dL |
  | `inactivity` | steps stop, activity `resting` and HR flat for `duration_s` during waking hours (triggers `inactivity` if configured threshold is shortened for demo, see 10.1) |
- `GET /sim/{elder_id}/status` returns current scenario, activity state, and seconds remaining.
- `POST /sim/{elder_id}/reseed` regenerates 7 days of history.

### 8.10 Health
- `GET /health` returns `{"status":"ok","time":"...","simulator":{"active_elders":N}}`. No auth. Used by uptime pingers.

---

## 9. WebSocket Contract

**Endpoint:** `WS /ws/{elder_id}?token=<jwt>`
Reject with close code 4401 if token invalid, 4403 if the user has no access.

Server pushes JSON messages, one envelope shape:
```json
{"type": "vitals" | "location" | "measurement" | "alert" | "med" | "scenario", "elder_id": "...", "ts": "...", "data": { ... }}
```

| type | when | data |
|---|---|---|
| `vitals` | every simulator tick (3 s) | same object as `vitals` in latest endpoint |
| `location` | every tick | `{lat, lng, distance_from_home_m, inside_geofence}` |
| `measurement` | BP/glucose emitted | `{kind, value1, value2?, context?}` |
| `alert` | alert created | full alert object |
| `med` | dose becomes due / marked missed / confirmed | `{log_id, name, dose, status, scheduled_at}` |
| `scenario` | demo scenario starts/ends | `{scenario, active}` |

Client may send `{"type":"ping"}`; server replies `{"type":"pong"}`. Server also sends a ping every 25 s to survive proxy idle timeouts.

**Caregiver with multiple elders:** Flutter opens one socket per elder, or call `WS /ws/all?token=` which multiplexes all linked elders (nice to have; implement if trivial).

Fallback for the app: all data also available via REST polling every 3 s.

---

## 10. Anomaly Detection

Run on every new vitals row (Layer 1 and 2 inline, Layer 3 every tick but cheap). Deduplicate: do not raise the same `type` for the same elder more than once per **2 minutes** (per-type cooldown, in-memory dict). Escalation from warning to critical bypasses cooldown.

### 10.1 Layer 1: Rules (instant)
Use thresholds from Section 2.3. Condition must hold for **2 consecutive ticks** to avoid noise (except fall and measurement-based alerts, which fire immediately).

Additional rules:
- **Fall detection (auto-SOS):** `accel_mag > 2.8 g` on any tick, then within the next 20 s: mean `accel_mag` under 0.25 g (or std dev under 0.05, meaning no movement) **and** `activity` not `sleeping`. Raise `fall_suspected`, severity `critical`, message "Kemungkinan jatuh terdeteksi. Segera periksa kondisi {name}.", `source='sos'`. Payload includes peak g, location, latest HR.
- **Geofence:** distance from home above `geofence_m` for 2 ticks -> `geofence_exit` warning. Above 3x geofence -> critical.
- **Inactivity:** awake hours (07:00 to 21:00 local), no steps change and activity `resting` for more than `INACTIVITY_MIN` minutes (env, default 90; set to 1 for demo) -> `inactivity` warning.
- **Missed medication:** from the med scheduler, dose still `pending` 30 min after schedule -> `med_missed` warning and log status `missed`.

### 10.2 Layer 2: Personal baseline (z-score)
- Baseline per elder, per **hour of day**, for `hr` (and `spo2`, `temp` if enough data): mean and std dev computed from the last 7 days of vitals where `activity` equals the current activity.
- Recompute every 30 minutes, cache in memory.
- Flag when `|z| > 3` for 3 consecutive ticks. Type `anomaly_baseline`, severity `warning`.
- Payload and message must be explainable: `{"metric":"hr","value":41,"baseline_mean":71.2,"baseline_std":6.4,"z":-4.7,"reason":"HR 41 bpm turun lebih dari 3σ dari kebiasaan pukul 14:00 (rata-rata 71)"}`.

### 10.3 Layer 3: Isolation Forest
- Features per tick: `[hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m]`.
- Train at startup on the seeded 7-day **normal** history (contamination 0.01, `n_estimators=100`, `random_state=42`). Save to `ml/isoforest.joblib`; load if present.
- Score each tick; if the decision score is below the threshold for 3 consecutive ticks -> `anomaly_ml` warning with payload `{score, top_features}` (the 2 features with greatest absolute deviation from the training mean, in units of std dev).
- Must **not** fire during normal simulation. Tune the threshold on seeded data so the false alert rate is under 1 per elder per hour.

### 10.4 Alert message style (Bahasa Indonesia, short, actionable)
Examples: "Kadar oksigen rendah (SpO2 88%). Periksa kondisi segera.", "Tekanan darah tinggi 192/124 mmHg.", "Gula darah rendah 48 mg/dL. Berikan asupan gula dan hubungi dokter.", "{name} keluar dari area aman (±420 m dari rumah)."

---

## 11. Simulator Specification

### 11.1 Loop
- A single asyncio task iterates all elders with `sim_enabled=true` every **3 s**. Per-elder state is stored in a dict: `{activity, activity_until, hr_drift, steps_today, pos, scenario, scenario_until, last_tick}`.
- Each tick: compute reading -> insert `vitals` (batched flush per tick with one commit) and `locations` -> run anomaly engine -> broadcast WS.

### 11.2 Activity state machine
States: `sleeping`, `resting`, `walking`, `exercising`.
- Circadian prior by **local time (Asia/Jakarta)**: 22:00 to 05:00 sleeping 95%; 05:00 to 07:00 mix of resting/walking; 07:00 to 21:00 mostly resting with 15 to 25% walking bouts (2 to 10 min) and rare light exercise (under 3% of the day); 21:00 to 22:00 resting.
- Transition: when `activity_until` passes, sample next state from the prior; sample duration from the table above.

### 11.3 Generators
| Metric | Rule |
|---|---|
| HR | base: sleeping 56, resting 70, walking 92, exercising 112. Plus slow drift (random walk, clamp ±6), Gaussian noise σ=2. Ease toward target (low-pass, α=0.3) so transitions are smooth. Add a mild per-elder offset from conditions (hypertension +4) |
| SpO2 | 97 plus noise σ=0.6, clamp 94 to 99. Sleeping: −0.5 |
| Temp (skin) | 36.4 plus tiny drift (σ 0.03) plus circadian ±0.3 (lower at night) |
| Resp rate | sleeping 13, resting 16, walking 19, exercising 24, plus noise σ=1 |
| Steps | walking: +1.5 to 2 steps/s per tick, exercising: +2.5/s, else 0. Cumulative; reset at local midnight |
| Accel mag | resting/sleeping 1.0 ±0.02; walking 1.15 ±0.15; exercising 1.4 ±0.3 |
| GPS | home at `(home_lat, home_lng)`. While `walking`, random walk step 0.5 to 1.5 m per tick with a bias back toward home when beyond 80 m; resting keeps position. Convert meters to degrees (111,320 m/deg lat; scale lng by cos(lat)) |

### 11.3.1 Periodic measurements
- At scheduled local times (Section 2.2), insert BP and glucose with realistic noise around per-elder baselines (BP baseline from conditions: hypertension -> 138/86 else 120/78; diabetes -> glucose fasting 118 and postprandial 165 else 92/128). Do this once per scheduled slot (track last emitted slot to prevent duplicates after restarts).
- Broadcast as `measurement` event.

### 11.4 Backfill
- On seed and on `reseed`: generate **7 days** of history at **1 sample/minute** using the same generators (no WS). Include periodic measurements for those days, and realistic med logs (about 85% taken, 10% late, 5% missed). Include 2 to 3 mild historical alerts (e.g. one `bp_high`, one `geofence_exit`) so reports and the alert list are not empty.
- On startup after downtime: backfill the gap from `last_ts` to now at 1 sample/minute (cap at 24 h) so charts have no holes. This handles Render free-tier sleep.
- Bulk insert in chunks of 2000 rows with `insert().values([...])`. Backfill for 3 demo elders must finish under 30 s.

### 11.5 Scenario injection
Scenarios override the generator for `duration_s` (default 30), then ramp back to normal over about 10 s. Mechanics per Section 8.9. Broadcast a `scenario` WS event at start and end. For `fall`, the sequence is:
1. Tick N: `accel_mag = 3.4`.
2. Ticks N+1 to end: `accel_mag = 0.05..0.1`, `activity=resting`, HR +25.
3. The fall rule fires, creating the SOS alert within about 6 to 15 s.

---

## 12. Medication Scheduler
- Every minute: for each active medication, for each time in `times` that applies today (local time), ensure a `med_logs` row exists for today's `scheduled_at` (UNIQUE constraint makes this idempotent). Create today's rows at startup and at 00:05 local.
- When `now >= scheduled_at` and status is `pending`, emit one `med` WS event "due".
- When `now >= scheduled_at + 30 min` and status is still `pending`: set `missed`, raise `med_missed` alert, emit `med` event.
- Timezone: medication `times` are local `Asia/Jakarta`. Use `zoneinfo.ZoneInfo("Asia/Jakarta")`.

---

## 13. Smart Report Generation

### 13.1 Pipeline
1. `aggregate_stats(elder_id, period)` in pure Python/SQL. **Never send raw rows to the LLM.** Output JSON, roughly under 3 KB:
   ```json
   {
     "elder": {"name":"...", "age":71, "conditions":["Hipertensi","Diabetes tipe 2"]},
     "period": {"start":"...","end":"...","days":7},
     "heart_rate": {"avg":74,"resting_avg":63,"min":48,"max":131,"trend":"stabil",
                    "daily_resting":[62,63,65,64,61,66,63]},
     "spo2": {"avg":97.1,"min":91,"minutes_below_94":14},
     "temperature": {"avg":36.5,"max":37.4},
     "blood_pressure": {"avg":"131/83","max":"158/94","readings":14,"high_readings":3},
     "glucose": {"fasting_avg":119,"postprandial_avg":171,"high_readings":4,"low_readings":0},
     "activity": {"avg_steps_per_day":3850,"best_day":"...","worst_day":"...",
                  "avg_sleep_hours":6.4,"trend_steps":"menurun 12%"},
     "medication": {"adherence_pct":86,"missed":6,"most_missed":"Metformin 20:00"},
     "alerts": {"total":9,"critical":1,"by_type":{"bp_high":3,"geofence_exit":2}},
     "notable_events":[{"ts":"...","type":"fall_suspected","note":"..."}]
   }
   ```
   `trend` = compare first half vs. second half of the period (percent change, label `naik/turun/stabil` using ±5% as stable band).
2. Prompt the LLM (Section 13.2).
3. Store `content_md`, `stats_json`, `generated_by`.
4. **Fallback:** if the LLM call fails, times out (12 s), or no API key exists, render a deterministic template from the same stats in Bahasa Indonesia and set `generated_by='template'`. The demo must never break because of the LLM.

### 13.2 Prompt
System:
> Kamu adalah asisten pemantauan kesehatan untuk keluarga lansia. Tulis laporan dalam Bahasa Indonesia yang hangat, jelas, dan mudah dipahami orang awam. Gunakan HANYA data pada statistik yang diberikan; jangan mengarang angka. Jangan mendiagnosis penyakit dan jangan menyarankan obat atau dosis. Jika ada hal yang mengkhawatirkan, sarankan berkonsultasi dengan dokter.

User: the stats JSON plus the required Markdown structure:
```
## Ringkasan
## Tren Kesehatan (detak jantung, oksigen, tekanan darah, gula darah, suhu)
## Aktivitas & Tidur
## Kepatuhan Obat
## Kejadian Penting
## Hal yang Perlu Diperhatikan
## Saran untuk Keluarga
```
End every report with the line: "*Laporan ini dibuat otomatis dari data pemantauan dan bukan pengganti pemeriksaan dokter.*"

### 13.3 LLM client (`services/llm.py`)
- `async def generate(system: str, user: str) -> str`, `httpx.AsyncClient`, timeout 12 s, one retry.
- Provider by env `LLM_PROVIDER` (`gemini` | `groq`); API key by `LLM_API_KEY`; model by `LLM_MODEL` (**do not hardcode model names**; free-tier model availability changes, document in README to check the provider's current free model).
- Temperature 0.4, max output about 900 tokens.

---

## 14. Seed Data & Demo Accounts

Seed only when `users` is empty. Password for all: `demo1234`.

| Role | Name | Email | Notes |
|---|---|---|---|
| caregiver | Rina (anak) | `caregiver@demo.id` | phone `+6281200000001`, linked to both elders below |
| elder | Pak Budi, 72 | `budi@demo.id` | conditions: Hipertensi; link_code `BUDI23` |
| elder | Bu Sari, 68 | `sari@demo.id` | conditions: Diabetes tipe 2; link_code `SARI45` |

Seed per elder: 7-day backfill, 2 to 3 medications (Pak Budi: Amlodipin 5 mg 08:00; Candesartan 8 mg 20:00. Bu Sari: Metformin 500 mg 07:30 and 19:30; Vitamin D 09:00), 2 upcoming doctor appointments, 1 placeholder document row. Home coords: Bekasi area (`-6.2349, 106.9896`) for Pak Budi, nearby offset for Bu Sari. Both default `sim_enabled=true`.

---

## 15. Configuration (env vars)

```
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
JWT_SECRET=change-me
JWT_EXPIRE_MINUTES=1440
CORS_ORIGINS=*
TZ_LOCAL=Asia/Jakarta
SIM_TICK_SECONDS=3
INACTIVITY_MIN=90
LLM_PROVIDER=gemini
LLM_API_KEY=
LLM_MODEL=
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
SUPABASE_BUCKET=documents
```
**Neon + asyncpg gotcha:** convert the URL scheme to `postgresql+asyncpg://` in code, and strip `sslmode=require` / `channel_binding` query params, then pass `connect_args={"ssl": True}` to `create_async_engine` (asyncpg does not understand `sslmode`). Use `pool_pre_ping=True` and a small pool (`pool_size=5, max_overflow=2`) because Neon free closes idle connections.

---

## 16. Deployment (fully free)

| Piece | Service |
|---|---|
| API | **Render** free Web Service (Docker or native Python). Alternatives: Koyeb free, Hugging Face Spaces (Docker, port 7860) |
| DB | **Neon** free Postgres (or Supabase) |
| Files | Supabase Storage free bucket (public read for simplicity) |
| Keep-alive | **UptimeRobot** or cron-job.org pinging `GET /health` every 5 min |
| LLM | Gemini API free tier or Groq free tier |
| Emergency fallback | run locally + `cloudflared tunnel --url http://localhost:8000` (or ngrok) |

**Dockerfile**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
```

**Notes**
- Render free tier sleeps after about 15 min idle and cold-starts slowly (tens of seconds). The pinger reduces this, but **warm it manually 5 min before the demo**. The startup backfill (11.4) fixes any data gap.
- Render free filesystem is ephemeral: the IsolationForest is retrained at startup if the `.joblib` is missing (must take under 5 s), and local `./uploads` files vanish on redeploy, so prefer Supabase Storage for documents.
- WebSockets work on Render free. Keep a 25 s server ping.
- Free-tier limits change. Check each provider's pricing page before the event.

---

## 17. Build Order & Acceptance Criteria

| # | Phase | Acceptance check |
|---|---|---|
| 1 | Skeleton: config, db, models, `/health`, auth (register/login/me), role guards, CORS | `POST /auth/register` + login + `/me` work in Swagger |
| 2 | Link flow, elder endpoints, seed script, **7-day backfill** | Fresh DB boots into seeded demo accounts; `/elders/{id}/vitals?interval=1h` returns 7 days |
| 3 | WebSocket manager + simulator loop + vitals/location/measurement endpoints | `wscat` receives `vitals` every 3 s; `/vitals/latest` matches |
| 4 | Threshold rules + alerts API + manual SOS + demo trigger endpoint + fall detector | `trigger fall` produces a `fall_suspected` alert over WS within 20 s; `hypoxia`, `tachycardia`, `hypertensive`, `hypoglycemia`, `wander` each create the right alert |
| 5 | Medications, med_logs, scheduler, adherence, appointments | A med scheduled 2 minutes ahead shows `pending`, emits `due`, confirm works, unconfirmed becomes `missed` plus alert |
| 6 | Z-score baseline + IsolationForest | No false alerts in 30 min of normal run; `anomaly_*` appear on abnormal injections with an explainable `reason` |
| 7 | Smart Report (aggregation, LLM, template fallback) | Report generates in under 15 s with LLM key; with the key unset it still returns a template report |
| 8 | Documents upload (Supabase or local) | Upload and list works; file_url opens |
| 9 | Dockerfile, `render.yaml`, README, DECISIONS.md, gap-backfill on startup | Container runs locally; README demo script works end to end |
| 10 | (Optional) FCM push for critical alerts, `/ws/all` multiplex | Only if everything above is green |

---

## 18. Demo Script (include in README)

1. Open Flutter caregiver dashboard (Pak Budi): live HR / SpO2 / steps tick every 3 s, BP and glucose cards show latest, map shows him at home.
2. `POST /sim/{id}/trigger {"scenario":"hypoxia"}` -> SpO2 falls, critical alert pops up.
3. `{"scenario":"fall"}` -> SOS banner on caregiver app, elder screen shows alert.
4. `{"scenario":"wander"}` -> marker leaves geofence, warning alert.
5. `{"scenario":"hypertensive"}` -> BP card turns red.
6. Show medication adherence, then **Generate Report** -> AI report in Bahasa Indonesia.
7. Explain the anomaly engine: rules, personal baseline, Isolation Forest, with the alert `reason` text.

---

## 19. Definition of Done
- All endpoints in Section 8 present in `/docs` and returning the documented shapes.
- WS contract in Section 9 matches exactly (the Flutter teammate depends on it).
- Cold start to seeded, streaming state with only env vars set.
- No unhandled exceptions in a 10-minute run with scenarios triggered.
- `README.md` and `DECISIONS.md` written.
- Disclaimer language present in reports and alert messages (no diagnoses).
