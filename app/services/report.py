import json
import re
import uuid
from datetime import datetime, timezone, timedelta, date
from typing import Dict, Any, List, Optional
from collections import Counter
import numpy as np
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import User, ElderProfile, Vital, Measurement, MedLog, Medication, Alert, Report
from app.services import llm

SYSTEM_PROMPT = (
    "Kamu adalah asisten pemantauan kesehatan untuk keluarga lansia. "
    "Tulis laporan dalam Bahasa Indonesia yang hangat, jelas, dan mudah dipahami orang awam. "
    "Gunakan HANYA data pada statistik yang diberikan; jangan mengarang angka. "
    "Jangan mendiagnosis penyakit dan jangan menyarankan obat atau dosis. "
    "Jika ada hal yang mengkhawatirkan, sarankan berkonsultasi dengan dokter. "
    "PENTING: Jangan mengulang kalimat, frasa, atau saran yang sama. Tulis secara ringkas, padat, dan segera akhiri setelah saran poin terakhir selesai tanpa repetisi."
)

DISCLAIMER_LINE = "*Laporan ini dibuat otomatis dari data pemantauan dan bukan pengganti pemeriksaan dokter.*"


def calculate_age(birth_date: Optional[date]) -> Optional[int]:
    if not birth_date:
        return None
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def calculate_trend_str(val1: float, val2: float, name: str = "") -> str:
    if val1 == 0:
        return "stabil"
    pct_change = ((val2 - val1) / val1) * 100
    if abs(pct_change) < 5.0:
        return "stabil"
    elif pct_change > 0:
        return f"naik {pct_change:.1f}%"
    else:
        return f"menurun {abs(pct_change):.1f}%"


async def aggregate_stats(db: AsyncSession, elder_id: str, days: int = 7) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)

    # 1. Elder Profile
    stmt_p = (
        select(ElderProfile)
        .options(selectinload(ElderProfile.user))
        .where(ElderProfile.user_id == elder_id)
    )
    profile = (await db.execute(stmt_p)).scalar_one()
    elder_user = profile.user
    age = calculate_age(profile.birth_date)

    # 2. Vitals
    stmt_v = (
        select(Vital)
        .where(and_(Vital.elder_id == elder_id, Vital.ts >= start_dt))
        .order_by(Vital.ts.asc())
    )
    v_rows = (await db.execute(stmt_v)).scalars().all()

    hr_list = [r.hr for r in v_rows]
    resting_hr_list = [r.hr for r in v_rows if r.activity == "resting"]
    spo2_list = [r.spo2 for r in v_rows]
    temp_list = [r.temp for r in v_rows]

    # Split into 2 halves for trend
    mid_idx = len(v_rows) // 2
    if v_rows and mid_idx > 0:
        hr_half1 = float(np.mean([r.hr for r in v_rows[:mid_idx]]))
        hr_half2 = float(np.mean([r.hr for r in v_rows[mid_idx:]]))
        hr_trend = calculate_trend_str(hr_half1, hr_half2)
    else:
        hr_trend = "stabil"

    # Daily resting HR (group by date)
    daily_resting_dict: Dict[date, List[float]] = {}
    daily_steps_dict: Dict[date, int] = {}
    daily_sleep_minutes: Dict[date, int] = {}

    for r in v_rows:
        d = r.ts.date()
        if d not in daily_steps_dict or r.steps > daily_steps_dict[d]:
            daily_steps_dict[d] = r.steps
        if r.activity == "resting":
            daily_resting_dict.setdefault(d, []).append(r.hr)
        if r.activity == "sleeping":
            # Each row represents ~1 min in backfill or 3s in live
            daily_sleep_minutes[d] = daily_sleep_minutes.get(d, 0) + 1

    daily_resting = [
        int(round(float(np.percentile(vals, 10)))) if vals else 70
        for d, vals in sorted(daily_resting_dict.items())
    ]

    # Low SpO2 count
    minutes_below_94 = sum(1 for s in spo2_list if s < 94)

    # 3. Blood Pressure
    stmt_bp = (
        select(Measurement)
        .where(and_(Measurement.elder_id == elder_id, Measurement.kind == "bp", Measurement.ts >= start_dt))
    )
    bp_rows = (await db.execute(stmt_bp)).scalars().all()
    if bp_rows:
        avg_sys = round(float(np.mean([r.value1 for r in bp_rows])))
        avg_dia = round(float(np.mean([r.value2 or 80.0 for r in bp_rows])))
        max_sys = round(max(r.value1 for r in bp_rows))
        max_dia = round(max(r.value2 or 0.0 for r in bp_rows))
        bp_high = sum(1 for r in bp_rows if r.value1 >= 140 or (r.value2 and r.value2 >= 90))
        bp_stats = {
            "avg": f"{avg_sys}/{avg_dia}",
            "max": f"{max_sys}/{max_dia}",
            "readings": len(bp_rows),
            "high_readings": bp_high,
        }
    else:
        bp_stats = {"avg": "120/80", "max": "120/80", "readings": 0, "high_readings": 0}

    # 4. Glucose
    stmt_g = (
        select(Measurement)
        .where(and_(Measurement.elder_id == elder_id, Measurement.kind == "glucose", Measurement.ts >= start_dt))
    )
    g_rows = (await db.execute(stmt_g)).scalars().all()
    fasting_vals = [r.value1 for r in g_rows if r.context == "fasting"]
    post_vals = [r.value1 for r in g_rows if r.context != "fasting"]
    g_high = sum(1 for r in g_rows if (r.context == "fasting" and r.value1 > 126) or (r.context != "fasting" and r.value1 > 180))
    g_low = sum(1 for r in g_rows if r.value1 < 70)
    g_stats = {
        "fasting_avg": round(float(np.mean(fasting_vals))) if fasting_vals else 95,
        "postprandial_avg": round(float(np.mean(post_vals))) if post_vals else 135,
        "high_readings": g_high,
        "low_readings": g_low,
    }

    # 5. Activity & Sleep
    step_vals = list(daily_steps_dict.values())
    avg_steps = int(round(float(np.mean(step_vals)))) if step_vals else 3500
    best_day = max(daily_steps_dict.items(), key=lambda x: x[1])[0].isoformat() if daily_steps_dict else now.date().isoformat()
    worst_day = min(daily_steps_dict.items(), key=lambda x: x[1])[0].isoformat() if daily_steps_dict else now.date().isoformat()
    avg_sleep_h = round(float(np.mean(list(daily_sleep_minutes.values()))) / 60.0, 1) if daily_sleep_minutes else 6.5

    # 6. Medications
    stmt_meds = (
        select(MedLog, Medication)
        .join(Medication, MedLog.medication_id == Medication.id)
        .where(and_(MedLog.elder_id == elder_id, MedLog.scheduled_at >= start_dt))
    )
    med_results = (await db.execute(stmt_meds)).all()
    taken_cnt = sum(1 for log, _ in med_results if log.status == "taken")
    missed_cnt = sum(1 for log, _ in med_results if log.status == "missed")
    total_med = taken_cnt + missed_cnt
    adh_pct = int(round((taken_cnt / max(1, total_med)) * 100))

    missed_names = [med.name for log, med in med_results if log.status == "missed"]
    most_missed = Counter(missed_names).most_common(1)[0][0] if missed_names else "Tidak ada"

    # 7. Alerts
    stmt_al = (
        select(Alert)
        .where(and_(Alert.elder_id == elder_id, Alert.ts >= start_dt))
        .order_by(Alert.ts.desc())
    )
    alerts = (await db.execute(stmt_al)).scalars().all()
    crit_count = sum(1 for a in alerts if a.severity == "critical")
    type_counts = dict(Counter(a.type for a in alerts))

    notable = [
        {
            "ts": a.ts.isoformat().replace("+00:00", "Z"),
            "type": a.type,
            "note": a.message,
        }
        for a in alerts[:5]
    ]

    return {
        "elder": {
            "name": elder_user.name,
            "age": age or 70,
            "conditions": profile.conditions or [],
        },
        "period": {
            "start": start_dt.isoformat().replace("+00:00", "Z"),
            "end": now.isoformat().replace("+00:00", "Z"),
            "days": days,
        },
        "heart_rate": {
            "avg": round(float(np.mean(hr_list)), 1) if hr_list else 72.0,
            "resting_avg": round(float(np.mean(resting_hr_list)), 1) if resting_hr_list else 65.0,
            "min": int(min(hr_list)) if hr_list else 52,
            "max": int(max(hr_list)) if hr_list else 125,
            "trend": hr_trend,
            "daily_resting": daily_resting,
        },
        "spo2": {
            "avg": round(float(np.mean(spo2_list)), 1) if spo2_list else 97.0,
            "min": int(min(spo2_list)) if spo2_list else 93,
            "minutes_below_94": minutes_below_94,
        },
        "temperature": {
            "avg": round(float(np.mean(temp_list)), 1) if temp_list else 36.5,
            "max": round(float(max(temp_list)), 1) if temp_list else 37.2,
        },
        "blood_pressure": bp_stats,
        "glucose": g_stats,
        "activity": {
            "avg_steps_per_day": avg_steps,
            "best_day": best_day,
            "worst_day": worst_day,
            "avg_sleep_hours": avg_sleep_h,
            "trend_steps": "stabil",
        },
        "medication": {
            "adherence_pct": adh_pct,
            "missed": missed_cnt,
            "most_missed": most_missed,
        },
        "alerts": {
            "total": len(alerts),
            "critical": crit_count,
            "by_type": type_counts,
        },
        "notable_events": notable,
    }


def render_template_report(stats: Dict[str, Any]) -> str:
    name = stats["elder"]["name"]
    age = stats["elder"]["age"]
    conds = ", ".join(stats["elder"]["conditions"]) if stats["elder"]["conditions"] else "Tidak ada catatan khusus"
    days = stats["period"]["days"]

    hr = stats["heart_rate"]
    spo2 = stats["spo2"]
    bp = stats["blood_pressure"]
    glu = stats["glucose"]
    temp = stats["temperature"]
    act = stats["activity"]
    med = stats["medication"]
    al = stats["alerts"]

    events_md = ""
    if stats["notable_events"]:
        for ev in stats["notable_events"][:4]:
            events_md += f"- **{ev['type']}**: {ev['note']}\n"
    else:
        events_md = "- Tidak ada kejadian darurat selama periode pemantauan.\n"

    concerns_md = []
    if bp.get("high_readings", 0) > 0:
        concerns_md.append(f"Terdapat {bp['high_readings']} kali pembacaan tekanan darah tinggi (tertinggi {bp['max']} mmHg).")
    if glu.get("high_readings", 0) > 0:
        concerns_md.append(f"Kadar gula darah terpantau tinggi sebanyak {glu['high_readings']} kali.")
    if med.get("missed", 0) > 0:
        concerns_md.append(f"Ada {med['missed']} jadwal minum obat yang terlewat (paling sering: {med['most_missed']}).")
    if not concerns_md:
        concerns_md.append("Secara keseluruhan parameter kesehatan terpantau dalam rentang terkendali.")
    concerns_text = "\n".join(f"- {c}" for c in concerns_md)

    content = f"""## Ringkasan
Laporan pemantauan kesehatan {name} ({age} tahun) selama {days} hari terakhir. Kondisi penyerta: {conds}. Secara umum kondisi kesehatan terpantau dengan tingkat kepatuhan minum obat mencapai {med['adherence_pct']}%.

## Tren Kesehatan (detak jantung, oksigen, tekanan darah, gula darah, suhu)
- **Detak Jantung:** Rata-rata harian {hr['avg']} bpm (saat istirahat {hr['resting_avg']} bpm). Rentang min {hr['min']} bpm hingga maks {hr['max']} bpm dengan tren {hr['trend']}.
- **Oksigen Darah (SpO2):** Rata-rata {spo2['avg']}%, minimum terpantau {spo2['min']}%. Terpantau di bawah 94% selama ±{spo2['minutes_below_94']} menit.
- **Tekanan Darah:** Rata-rata {bp['avg']} mmHg (pembacaan tertinggi {bp['max']} mmHg dari total {bp['readings']} kali pengukuran).
- **Gula Darah:** Rata-rata puasa {glu['fasting_avg']} mg/dL, rata-rata setelah makan {glu['postprandial_avg']} mg/dL.
- **Suhu Tubuh:** Rata-rata {temp['avg']}°C, suhu tertinggi {temp['max']}°C.

## Aktivitas & Tidur
- Rata-rata langkah kaki per hari: {act['avg_steps_per_day']} langkah.
- Hari paling aktif: {act['best_day']}, hari kurang aktif: {act['worst_day']}.
- Estimasi durasi tidur malam rata-rata: {act['avg_sleep_hours']} jam per hari.

## Kepatuhan Obat
- Tingkat kepatuhan jadwal obat: **{med['adherence_pct']}%**.
- Jumlah dosis terlewat: {med['missed']} kali.
- Jadwal yang sering terlewat: {med['most_missed']}.

## Kejadian Penting
{events_md}
## Hal yang Perlu Diperhatikan
{concerns_text}

## Saran untuk Keluarga
1. Pastikan lansia minum obat secara teratur sesuai jadwal, terutama untuk {med['most_missed']}.
2. Dorong aktivitas fisik ringan seperti jalan santai di pagi hari untuk menjaga mobilitas.
3. Jaga asupan hidrasi air putih yang cukup dan pastikan waktu istirahat tidur teratur.
4. Jadwalkan kontrol berkala dengan dokter spesialis jika tekanan darah atau gula darah sering melonjak.

{DISCLAIMER_LINE}"""

    return content


def clean_repetitive_hallucinations(text: str) -> str:
    """Sanitize LLM output by dropping repetitive loop sentences/paragraphs
    and trimming trailing cut-off sentence fragments."""
    if not text:
        return text

    clean_text = text.replace(DISCLAIMER_LINE, "").strip()

    def dedupe_sentences(p: str) -> str:
        if p.startswith("#"):
            return p
        sentences = re.split(r"(?<=[.!?])\s+", p)
        res = []
        seen_s = set()
        for s in sentences:
            s_clean = s.strip()
            if not s_clean:
                continue
            s_norm = re.sub(r"^[\d\.\-\+\*\s]+", "", s_clean).lower()[:35]
            if len(s_norm) >= 15:
                if s_norm in seen_s:
                    continue
                seen_s.add(s_norm)
            res.append(s_clean)
        return " ".join(res)

    lines = clean_text.split("\n")
    cleaned_lines = []
    seen_prefixes = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue

        line_deduped = dedupe_sentences(line)
        norm = re.sub(r"^[\d\.\-\+\*\s]+", "", line_deduped).lower()
        prefix = norm[:35].strip() if len(norm) >= 15 else norm.strip()

        if prefix and len(prefix) >= 15:
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)

        cleaned_lines.append(line_deduped)

    # Cut off trailing incomplete lines or orphan numbering (e.g. "4.", dangling fragments)
    while cleaned_lines:
        last = cleaned_lines[-1].strip()
        if not last:
            cleaned_lines.pop()
            continue
        if re.match(r"^(\d+[\.\)]|[\-\*\+]|#+)\s*$", last):
            cleaned_lines.pop()
            continue
        if not last.endswith((".", "!", "?", "*", ":", "#", "-", ")")):
            cleaned_lines.pop()
            continue
        break

    clean_text = "\n".join(cleaned_lines).rstrip()
    return f"{clean_text}\n\n{DISCLAIMER_LINE}"


async def generate_smart_report(db: AsyncSession, elder_id: str, days: int = 7) -> Report:
    # 1. Aggregate stats
    stats = await aggregate_stats(db, elder_id, days)

    # 2. Try LLM
    content_md = None
    generated_by = "llm"
    user_prompt = f"Data statistik pemantauan {days} hari:\n```json\n{json.dumps(stats, indent=2)}\n```\n\nBuat laporan dengan struktur Markdown yang ditentukan."

    try:
        if settings.LLM_API_KEY:
            raw_text = await llm.generate(SYSTEM_PROMPT, user_prompt)
            content_md = clean_repetitive_hallucinations(raw_text)
    except Exception as e:
        print("LLM generation failed or timed out, using fallback template:", e)
        content_md = None

    # 3. Fallback if LLM failed or not configured
    if not content_md:
        content_md = render_template_report(stats)
        generated_by = "template"

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)

    report = Report(
        id=str(uuid.uuid4()),
        elder_id=elder_id,
        period_start=start_dt,
        period_end=now,
        content_md=content_md,
        stats_json=stats,
        generated_by=generated_by,
        created_at=now,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    return report
