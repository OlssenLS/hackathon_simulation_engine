import html
import re
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Report, User, ElderProfile
from app.schemas import ReportOut, ReportMetadata
from app.security import get_current_user, require_caregiver, verify_elder_access
from app.services.report import generate_smart_report

router = APIRouter(tags=["reports"])


@router.post("/elders/{elder_id}/reports/generate", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def generate_report(
    elder_id: str,
    days: int = Query(7, ge=1, le=30),
    caregiver: User = Depends(require_caregiver),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, caregiver, db, allow_elder_self=False)
    report = await generate_smart_report(db, elder_id, days)
    return report


@router.get("/elders/{elder_id}/reports", response_model=List[ReportMetadata])
async def list_reports(
    elder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await verify_elder_access(elder_id, user, db, allow_elder_self=True)
    stmt = (
        select(Report)
        .where(Report.elder_id == elder_id)
        .order_by(Report.created_at.desc())
    )
    results = await db.execute(stmt)
    return results.scalars().all()


@router.get("/reports/{report_id}", response_model=ReportOut)
async def get_report_detail(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Report).where(Report.id == report_id)
    report = (await db.execute(stmt)).scalar_one_or_none()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Laporan tidak ditemukan"
        )

    await verify_elder_access(report.elder_id, user, db, allow_elder_self=True)
    return report


@router.get("/reports/{report_id}/download")
async def download_report_doc(
    report_id: str,
    format: str = Query("html", pattern="^(html|md|txt)$"),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Report).where(Report.id == report_id)
    report = (await db.execute(stmt)).scalar_one_or_none()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Laporan tidak ditemukan"
        )

    if format in ("md", "txt"):
        return PlainTextResponse(
            report.content_md,
            headers={
                "Content-Disposition": f'attachment; filename="laporan-lansia-{report.created_at.strftime("%Y%m%d")}.{format}"'
            }
        )

    stats = report.stats_json or {}
    elder_info = stats.get("elder", {})
    elder_name = elder_info.get("name", "Lansia")
    elder_age = elder_info.get("age", "-")
    elder_conditions = ", ".join(elder_info.get("conditions", [])) or "Tidak ada riwayat khusus"
    period_days = stats.get("period", {}).get("days", 7)

    hr = stats.get("heart_rate", {})
    bp = stats.get("blood_pressure", {})
    med = stats.get("medication", {})

    # Simple markdown to HTML converter for sections
    raw_md = report.content_md or ""
    lines = raw_md.split("\n")
    body_html_parts = []
    in_list = False

    for line in lines:
        line_s = line.strip()
        if not line_s:
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            continue

        if line_s.startswith("### "):
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            body_html_parts.append(f"<h3>{html.escape(line_s[4:])}</h3>")
        elif line_s.startswith("## "):
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            body_html_parts.append(f"<h2>{html.escape(line_s[3:])}</h2>")
        elif line_s.startswith("# "):
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            body_html_parts.append(f"<h2>{html.escape(line_s[2:])}</h2>")
        elif line_s.startswith("- ") or line_s.startswith("* "):
            if not in_list:
                body_html_parts.append("<ul>")
                in_list = True
            item_text = line_s[2:]
            item_html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html.escape(item_text))
            body_html_parts.append(f"<li>{item_html}</li>")
        elif re.match(r"^\d+\.\s", line_s):
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            item_text = re.sub(r"^\d+\.\s*", "", line_s)
            item_html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html.escape(item_text))
            body_html_parts.append(f"<p>{item_html}</p>")
        else:
            if in_list:
                body_html_parts.append("</ul>")
                in_list = False
            p_html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html.escape(line_s))
            body_html_parts.append(f"<p>{p_html}</p>")

    if in_list:
        body_html_parts.append("</ul>")

    body_html = "\n".join(body_html_parts)
    created_str = report.created_at.strftime("%d %B %Y, %H:%M WIB")

    html_doc = f"""<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Laporan Medis LanSIAP - {html.escape(elder_name)}</title>
  <style>
    @page {{ size: A4; margin: 15mm; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; color: #1e293b; line-height: 1.6; margin: 0; padding: 24px; background: #f1f5f9; }}
    .btn-bar {{ max-width: 820px; margin: 0 auto 16px auto; display: flex; justify-content: flex-end; gap: 10px; }}
    .btn {{ background: #0284c7; color: #ffffff; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 13px; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; box-shadow: 0 2px 6px rgba(2,132,199,0.3); }}
    .btn:hover {{ background: #0369a1; }}
    .btn-sec {{ background: #ffffff; color: #475569; border: 1px solid #cbd5e1; box-shadow: none; }}
    .btn-sec:hover {{ background: #f8fafc; }}
    .page {{ max-width: 820px; margin: 0 auto; background: #ffffff; padding: 40px 48px; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); }}
    .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #0284c7; padding-bottom: 20px; margin-bottom: 24px; }}
    .brand-title {{ font-size: 24px; font-weight: 900; color: #0284c7; letter-spacing: -0.5px; margin: 0; }}
    .brand-subtitle {{ font-size: 12px; color: #64748b; margin: 3px 0 0 0; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }}
    .doc-meta {{ text-align: right; }}
    .badge {{ background: #e0f2fe; color: #0369a1; padding: 5px 12px; border-radius: 12px; font-size: 11px; font-weight: 800; display: inline-block; margin-bottom: 6px; }}
    .doc-date {{ font-size: 12px; color: #64748b; margin: 0; }}
    .info-card {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; }}
    .info-item {{ font-size: 13px; }}
    .info-label {{ color: #64748b; font-weight: 600; margin-bottom: 2px; }}
    .info-value {{ color: #0f172a; font-weight: 700; }}
    .metrics-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 28px; }}
    .metric-box {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; text-align: center; border-top: 3px solid #0284c7; }}
    .metric-val {{ font-size: 20px; font-weight: 900; color: #0f172a; margin-bottom: 2px; }}
    .metric-lbl {{ font-size: 11px; color: #64748b; font-weight: 600; text-transform: uppercase; }}
    .content h2 {{ font-size: 15px; color: #0f172a; border-left: 4px solid #0284c7; padding-left: 10px; margin-top: 24px; margin-bottom: 12px; font-weight: 800; }}
    .content h3 {{ font-size: 14px; color: #1e293b; margin-top: 16px; margin-bottom: 8px; font-weight: 700; }}
    .content p {{ font-size: 13.5px; color: #334155; margin: 8px 0; }}
    .content ul {{ padding-left: 20px; margin: 8px 0; }}
    .content li {{ font-size: 13.5px; color: #334155; margin-bottom: 6px; }}
    .disclaimer {{ background: #fffbeb; border: 1px solid #fef3c7; color: #92400e; padding: 14px; border-radius: 10px; font-size: 11.5px; font-style: italic; margin-top: 32px; }}
    .footer {{ display: flex; justify-content: space-between; align-items: flex-end; margin-top: 40px; padding-top: 24px; border-top: 1px solid #e2e8f0; }}
    .sig-box {{ text-align: center; min-width: 180px; }}
    .sig-line {{ border-bottom: 1px solid #0f172a; height: 50px; margin-bottom: 6px; }}
    .sig-name {{ font-size: 12px; font-weight: 700; color: #0f172a; margin: 0; }}
    .sig-sub {{ font-size: 11px; color: #64748b; margin: 2px 0 0 0; }}
    @media print {{
      body {{ background: #ffffff; padding: 0; }}
      .btn-bar {{ display: none !important; }}
      .page {{ box-shadow: none; border-radius: 0; padding: 0; max-width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="btn-bar">
    <a href="/reports/{report.id}/download?format=md" class="btn btn-sec">⬇ Unduh Markdown</a>
    <button onclick="window.print()" class="btn">🖨️ Cetak / Simpan PDF</button>
  </div>
  <div class="page">
    <div class="header">
      <div>
        <h1 class="brand-title">LanSIAP Healthcare</h1>
        <p class="brand-subtitle">Sistem Pemantauan Lansia Terpadu Berbasis AI</p>
      </div>
      <div class="doc-meta">
        <span class="badge">Dokumen Resmi Medis</span>
        <p class="doc-date">Dibuat: {created_str}</p>
        <p class="doc-date">ID: {report.id[:8].upper()}</p>
      </div>
    </div>

    <div class="info-card">
      <div class="info-item">
        <div class="info-label">Nama Pasien Lansia</div>
        <div class="info-value">{html.escape(elder_name)} ({elder_age} tahun)</div>
      </div>
      <div class="info-item">
        <div class="info-label">Periode Pemantauan</div>
        <div class="info-value">{period_days} Hari Terakhir</div>
      </div>
      <div class="info-item">
        <div class="info-label">Kondisi / Komorbiditas</div>
        <div class="info-value">{html.escape(elder_conditions)}</div>
      </div>
      <div class="info-item">
        <div class="info-label">Metode Analisis</div>
        <div class="info-value">Groq AI Engine ({report.generated_by.upper()})</div>
      </div>
    </div>

    <div class="metrics-row">
      <div class="metric-box">
        <div class="metric-val">{hr.get("avg", "-")} <span style="font-size:12px">bpm</span></div>
        <div class="metric-lbl">Rata-rata HR</div>
      </div>
      <div class="metric-box">
        <div class="metric-val">{bp.get("avg", "-")} <span style="font-size:12px">mmHg</span></div>
        <div class="metric-lbl">Rata-rata Tensi</div>
      </div>
      <div class="metric-box">
        <div class="metric-val">{med.get("adherence_pct", 0)}%</div>
        <div class="metric-lbl">Kepatuhan Obat</div>
      </div>
      <div class="metric-box">
        <div class="metric-val">{stats.get("spo2", {}).get("avg", 97)}%</div>
        <div class="metric-lbl">Oksigen SpO2</div>
      </div>
    </div>

    <div class="content">
      {body_html}
    </div>

    <div class="disclaimer">
      *Laporan ini dibuat secara otomatis oleh modul AI LanSIAP berdasarkan analisis data sensor IoT dan telemetri pemantauan. Laporan ini merupakan instrumen pendukung skrining dan bukan pengganti diagnosis klinis resmi oleh dokter.*
    </div>

    <div class="footer">
      <div>
        <p style="font-size:11px;color:#64748b;margin:0">LanSIAP Autonomous Elderly Care System</p>
        <p style="font-size:10px;color:#94a3b8;margin:2px 0 0 0">Verifikasi Dokumen: {report.id}</p>
      </div>
      <div class="sig-box">
        <div class="sig-line"></div>
        <p class="sig-name">Penanggung Jawab / Caregiver</p>
        <p class="sig-sub">Keluarga Pendamping</p>
      </div>
    </div>
  </div>
</body>
</html>"""

    return HTMLResponse(content=html_doc)
