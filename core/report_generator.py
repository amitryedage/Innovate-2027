import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import REPORTS_DIR
from core.database import (
    get_session_events, get_session_summary,
    get_connection, write_audit_log
)

# USP 1 + 2 integration (imported lazily to avoid circular imports)
def _get_risk_score(session_id: str) -> dict:
    try:
        from core.risk_scorer import RiskScorer
        import threading
        # Fetch actual session start_time from DB so shift_time component scores correctly
        conn = get_connection()
        row = conn.execute(
            "SELECT start_time FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.close()
        import time
        start_t = None
        if row and row["start_time"]:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(row["start_time"])
                start_t = dt.timestamp()
            except Exception:
                start_t = time.time() - 3600
        ss = {"session_id": session_id,
              "start_time": start_t or (time.time() - 3600),
              "alert_level": 0, "threshold_raised": 0.0}
        sl = threading.Lock()
        rs = RiskScorer(ss, sl)
        rs._last_computed_time = 0
        return rs.get_final_score()
    except Exception as e:
        return {"score": 0, "band": "N/A", "components": {}}

def _get_analytics(operator_id: str) -> dict:
    try:
        from core.analytics_engine import AnalyticsEngine
        ae = AnalyticsEngine()
        return ae.get_operator_risk_profile(operator_id, days=30)
    except Exception:
        return {}

# Try importing reportlab — graceful fallback if not installed
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table,
        TableStyle, HRFlowable
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    print("[REPORT] reportlab not installed. Run: pip install reportlab")

# Try importing matplotlib for PERCLOS chart
try:
    import matplotlib
    matplotlib.use("Agg")   # non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from io import BytesIO
    from reportlab.platypus import Image as RLImage
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


def generate_report(session_id: str, operator_name: str = "Unknown") -> str:
    """
    Generate a PDF shift safety report.

    Args:
        session_id    : UUID of the session
        operator_name : Display name of the operator

    Returns:
        filepath of generated PDF, or None on failure
    """
    if not REPORTLAB_AVAILABLE:
        print("[REPORT] Cannot generate PDF — reportlab not available.")
        return None

    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Fetch data from DB
    events  = get_session_events(session_id)
    summary = get_session_summary(session_id)
    session = _get_session(session_id)

    if not session:
        print(f"[REPORT] Session {session_id} not found.")
        return None

    # Build PDF filename
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename      = f"report_{operator_name.replace(' ','_')}_{timestamp_str}.pdf"
    filepath      = os.path.join(REPORTS_DIR, filename)

    try:
        doc = SimpleDocTemplate(
            filepath,
            pagesize     = A4,
            rightMargin  = 2*cm,
            leftMargin   = 2*cm,
            topMargin    = 2*cm,
            bottomMargin = 2*cm,
        )

        story = []
        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            "Title",
            parent    = styles["Title"],
            fontSize  = 18,
            textColor = colors.HexColor("#1E3A5F"),
            spaceAfter= 6,
        )
        h1_style = ParagraphStyle(
            "H1",
            parent    = styles["Heading1"],
            fontSize  = 13,
            textColor = colors.HexColor("#1E3A5F"),
            spaceBefore=12,
            spaceAfter= 4,
        )
        h2_style = ParagraphStyle(
            "H2",
            parent    = styles["Heading2"],
            fontSize  = 11,
            textColor = colors.HexColor("#374151"),
            spaceBefore=8,
            spaceAfter= 3,
        )
        body_style = ParagraphStyle(
            "Body",
            parent    = styles["Normal"],
            fontSize  = 10,
            textColor = colors.HexColor("#374151"),
            spaceAfter= 3,
        )
        warning_style = ParagraphStyle(
            "Warning",
            parent    = styles["Normal"],
            fontSize  = 10,
            textColor = colors.HexColor("#B91C1C"),
            spaceAfter= 3,
        )

        
        # PAGE 1 
       
        story.append(Paragraph("Operator Fatigue Detection System", title_style))
        story.append(Paragraph("End-of-Shift Safety Report", h1_style))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1E3A5F")))
        story.append(Spacer(1, 0.3*cm))

        # Session info table
        start_dt  = _parse_iso(session.get("start_time", ""))
        end_dt    = _parse_iso(session.get("end_time",   ""))
        duration  = _format_duration(start_dt, end_dt)
        machine   = session.get("machine_id", "N/A")
        demo_mode = "YES " if session.get("demo_mode") else "No"
        glasses   = "Yes" if session.get("glasses_mode") else "No"

        info_data = [
            ["Operator",    operator_name,      "Machine",      machine],
            ["Shift start", _fmt_dt(start_dt),  "Shift end",    _fmt_dt(end_dt)],
            ["Duration",    duration,            "Demo mode",    demo_mode],
            ["Glasses mode",glasses,             "Report date",  datetime.now().strftime("%d %b %Y %H:%M")],
        ]
        info_table = Table(info_data, colWidths=[3.5*cm, 6*cm, 3.5*cm, 4*cm])
        info_table.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,-1), colors.HexColor("#F8FAFC")),
            ("BACKGROUND",  (0,0), (0,-1), colors.HexColor("#EFF6FF")),
            ("BACKGROUND",  (2,0), (2,-1), colors.HexColor("#EFF6FF")),
            ("TEXTCOLOR",   (0,0), (-1,-1), colors.HexColor("#374151")),
            ("FONTNAME",    (0,0), (-1,-1), "Helvetica"),
            ("FONTSIZE",    (0,0), (-1,-1), 9),
            ("FONTNAME",    (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTNAME",    (2,0), (2,-1), "Helvetica-Bold"),
            ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E1")),
            ("PADDING",     (0,0), (-1,-1), 6),
        ]))
        story.append(info_table)
        story.append(Spacer(1, 0.5*cm))

        
        # SUMMARY STATISTICS
        
        story.append(Paragraph("Shift Summary", h1_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 0.2*cm))

        # Count events by type
        fatigue_events = [e for e in events
                          if e["event_type"].startswith("FATIGUE")]
        l1_count = sum(1 for e in fatigue_events if e["alert_level"] == 1)
        l2_count = sum(1 for e in fatigue_events if e["alert_level"] == 2)
        l3_count = sum(1 for e in fatigue_events if e["alert_level"] == 3)
        total_f  = len(fatigue_events)

        face_loss_count = sum(1 for e in events
                              if e["event_type"] == "FACE_LOSS")
        tamper_count    = sum(1 for e in events
                              if e["event_type"] == "TAMPER")

        # Ack ratio
        acked   = sum(1 for e in fatigue_events if e.get("acknowledged"))
        ack_pct = (acked / total_f * 100) if total_f > 0 else 0
        ack_times = [e["ack_time_sec"] for e in fatigue_events
                     if e.get("ack_time_sec")]
        avg_ack  = sum(ack_times)/len(ack_times) if ack_times else 0

        summary_data = [
            ["Metric",                  "Count",    "Metric",           "Value"],
            ["Total fatigue events",    str(total_f),"Ack rate",        f"{ack_pct:.0f}%"],
            ["Level 1 (mild)",          str(l1_count),"Avg ack time",   f"{avg_ack:.1f}s"],
            ["Level 2 (moderate)",      str(l2_count),"Face loss events",str(face_loss_count)],
            ["Level 3 (critical)",      str(l3_count),"Tamper events",  str(tamper_count)],
        ]
        sum_table = Table(summary_data, colWidths=[5.5*cm, 3*cm, 5.5*cm, 3*cm])
        sum_table.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#1E3A5F")),
            ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
            ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE",    (0,0), (-1,-1), 9),
            ("BACKGROUND",  (0,1), (-1,-1), colors.HexColor("#F8FAFC")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),
             [colors.HexColor("#F8FAFC"), colors.HexColor("#EFF6FF")]),
            ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E1")),
            ("PADDING",     (0,0), (-1,-1), 6),
            # Highlight L3 in red if any
            *([("TEXTCOLOR", (1,4), (1,4), colors.HexColor("#B91C1C")),
               ("FONTNAME",  (1,4), (1,4), "Helvetica-Bold")]
              if l3_count > 0 else []),
        ]))
        story.append(sum_table)
        story.append(Spacer(1, 0.4*cm))

        # Safety assessment
        if l3_count > 0:
            story.append(Paragraph(
                f"WARNING: {l3_count} critical fatigue event(s) detected. "
                "Operator safety review recommended before next shift.",
                warning_style
            ))
        elif l2_count > 2:
            story.append(Paragraph(
                f"CAUTION: {l2_count} moderate fatigue events. "
                "Consider shorter shifts or additional breaks.",
                warning_style
            ))
        else:
            story.append(Paragraph(
                "Shift completed without critical fatigue incidents.",
                body_style
            ))

        
        # PERCLOS TREND CHART
        
        if MATPLOTLIB_AVAILABLE and fatigue_events:
            story.append(Spacer(1, 0.3*cm))
            story.append(Paragraph("PERCLOS Trend During Shift", h1_style))
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#CBD5E1")))
            story.append(Spacer(1, 0.2*cm))

            chart = _build_perclos_chart(fatigue_events, session)
            if chart:
                story.append(chart)

       
        # PAGE 2 — EVENT TIMELINE
        # What happen time at which this event get happen and all get store here 
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph("Event Timeline", h1_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 0.2*cm))

        if fatigue_events:
            event_data = [["Time", "Type", "EAR", "PERCLOS", "Score", "Ack", "Ack Time"]]
            for ev in fatigue_events[:50]:   # max 50 events on report
                ev_time  = _parse_iso(ev.get("timestamp",""))
                ev_label = {1:"L1 Mild", 2:"L2 Mod", 3:"L3 CRIT"}.get(
                    ev.get("alert_level",0), "L?")
                ack_str  = "✓" if ev.get("acknowledged") else "✗"
                ack_t    = f"{ev['ack_time_sec']:.1f}s" \
                           if ev.get("ack_time_sec") else "—"
                event_data.append([
                    ev_time.strftime("%H:%M:%S") if ev_time else "?",
                    ev_label,
                    f"{ev.get('ear_value',0):.3f}" if ev.get('ear_value') else "—",
                    f"{ev.get('perclos_value',0):.1f}%" if ev.get('perclos_value') else "—",
                    f"{ev.get('fatigue_score',0):.3f}" if ev.get('fatigue_score') else "—",
                    ack_str,
                    ack_t,
                ])

            ev_table = Table(event_data,
                             colWidths=[2.2*cm,2.2*cm,1.8*cm,2.2*cm,2*cm,1.2*cm,2*cm])
            ev_style = [
                ("BACKGROUND", (0,0), (-1,0),  colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR",  (0,0), (-1,0),  colors.white),
                ("FONTNAME",   (0,0), (-1,0),  "Helvetica-Bold"),
                ("FONTNAME",   (0,1), (-1,-1), "Helvetica"),
                ("FONTSIZE",   (0,0), (-1,-1), 8),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),
                 [colors.HexColor("#F8FAFC"), colors.HexColor("#EFF6FF")]),
                ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#CBD5E1")),
                ("PADDING",    (0,0), (-1,-1), 4),
                ("ALIGN",      (2,0), (-1,-1), "CENTER"),
            ]
            # Highlight L3 rows in red
            for i, ev in enumerate(fatigue_events[:50], start=1):
                if ev.get("alert_level") == 3:
                    ev_style.append(
                        ("TEXTCOLOR", (0,i), (-1,i), colors.HexColor("#B91C1C"))
                    )
                    ev_style.append(
                        ("FONTNAME",  (0,i), (-1,i), "Helvetica-Bold")
                    )
            ev_table.setStyle(TableStyle(ev_style))
            story.append(ev_table)

            if len(fatigue_events) > 50:
                story.append(Paragraph(
                    f"  ... {len(fatigue_events)-50} more events not shown.",
                    body_style
                ))
        else:
            story.append(Paragraph(
                "No fatigue events recorded during this shift.", body_style
            ))

        
        # RISK SCORE SECTION (USP)
       
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Shift Risk Assessment", h1_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 0.2*cm))

        risk_result = _get_risk_score(session_id)
        risk_score  = risk_result.get("score", 0)
        risk_band   = risk_result.get("band", "N/A")
        risk_comps  = risk_result.get("components", {})

        band_colors = {
            "GREEN":  "#10B981", "AMBER": "#F59E0B",
            "ORANGE": "#F97316", "RED":   "#EF4444", "N/A": "#94A3B8"
        }
        band_hex = band_colors.get(risk_band, "#94A3B8")

        risk_summary_style = ParagraphStyle(
            "RiskSummary", parent=styles["Normal"],
            fontSize=22, textColor=colors.HexColor(band_hex),
            fontName="Helvetica-Bold", spaceAfter=4
        )
        story.append(Paragraph(
            f"Overall Shift Risk Score: {risk_score:.0f}/100 — {risk_band}",
            risk_summary_style
        ))

        if risk_comps:
            comp_data = [["Component", "Score", "Weight", "Contribution"]]
            weights   = {"frequency": "30%", "severity": "30%",
                         "ack_quality": "20%", "trend": "10%", "shift_time": "10%"}
            labels    = {"frequency": "Alert Frequency",
                         "severity": "Alert Severity",
                         "ack_quality": "Acknowledgement Quality",
                         "trend": "Fatigue Trend",
                         "shift_time": "Shift Time Factor"}
            for k, v in risk_comps.items():
                w = weights.get(k, "—")
                wf = float(w.strip('%')) / 100 if '%' in w else 0
                contrib = round(v * wf, 1)
                comp_data.append([labels.get(k, k), f"{v:.1f}", w, f"{contrib:.1f}"])

            comp_table = Table(comp_data, colWidths=[6*cm, 3*cm, 2.5*cm, 3*cm])
            comp_table.setStyle(TableStyle([
                ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
                ("FONTSIZE",    (0,0), (-1,-1), 9),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),
                 [colors.HexColor("#F8FAFC"), colors.HexColor("#EFF6FF")]),
                ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E1")),
                ("PADDING",     (0,0), (-1,-1), 5),
            ]))
            story.append(comp_table)

        
        # OPERATOR HISTORICAL PROFILE (USP)
        # Helpful in real word cases 
        
        analytics = _get_analytics(operator_name)
        if analytics and analytics.get("total_sessions", 0) > 1:
            story.append(Spacer(1, 0.4*cm))
            story.append(Paragraph("30-Day Operator Fatigue Profile", h1_style))
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#CBD5E1")))
            story.append(Spacer(1, 0.2*cm))

            hist_data = [
                ["Metric", "Value", "Metric", "Value"],
                ["Total shifts analysed",
                 str(analytics.get("total_sessions", "—")),
                 "Risk band (30 days)",
                 analytics.get("risk_band", "—")],
                ["Avg fatigue events/shift",
                 str(analytics.get("avg_events_per_shift", "—")),
                 "Max alert level seen",
                 str(analytics.get("max_alert_level_seen", "—"))],
                ["Avg acknowledgement time",
                 f"{analytics.get('avg_ack_time_sec','—')}s",
                 "Ack rate",
                 f"{analytics.get('ack_rate_pct','—')}%"],
                ["Trend vs history",
                 analytics.get("trend_vs_history","—").upper(),
                 "Avg shift duration",
                 f"{analytics.get('avg_shift_hours','—')}h"],
            ]
            hist_table = Table(hist_data, colWidths=[5*cm,3.5*cm,5*cm,3.5*cm])
            hist_table.setStyle(TableStyle([
                ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
                ("FONTSIZE",    (0,0), (-1,-1), 9),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),
                 [colors.HexColor("#F8FAFC"), colors.HexColor("#EFF6FF")]),
                ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E1")),
                ("PADDING",     (0,0), (-1,-1), 5),
            ]))
            story.append(hist_table)

            trend = analytics.get("trend_vs_history", "stable")
            if trend == "worsening":
                story.append(Spacer(1, 0.2*cm))
                story.append(Paragraph(
                    "ATTENTION: This operator's fatigue frequency has increased "
                    "compared to their historical baseline. Recommend fatigue counselling "
                    "and shift pattern review.",
                    warning_style
                ))
            elif trend == "improving":
                story.append(Spacer(1, 0.2*cm))
                story.append(Paragraph(
                    "POSITIVE TREND: Fatigue frequency has decreased vs historical "
                    "baseline. Current interventions appear effective.",
                    body_style
                ))

        
        # COMPLIANCE DECLARATION (USP)
     
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Safety Compliance Declaration", h1_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 0.2*cm))

        compliance_items = [
            ["Requirement", "Status", "Details"],
            ["Continuous fatigue monitoring", "COMPLIANT",
             f"Monitored for {duration}"],
            ["Operator identification", "COMPLIANT",
             f"RFID/PIN: {operator_name}"],
            ["Alert response documentation", "COMPLIANT",
             f"{acked}/{total_f} alerts acknowledged ({ack_pct:.0f}%)"],
            ["Level 3 (critical) events",
             " REVIEW REQUIRED" if l3_count > 0 else "NONE RECORDED",
             f"{l3_count} critical event(s)" if l3_count > 0 else "No critical events"],
            ["Data storage", "COMPLIANT",
             "Encrypted SQLite, offline, on-device"],
            ["Privacy compliance", "COMPLIANT",
             "No biometric data transmitted externally"],
            ["Report generation", "COMPLIANT",
             f"Auto-generated: {datetime.now().strftime('%d %b %Y %H:%M')}"],
        ]

        comp_tbl = Table(compliance_items, colWidths=[5.5*cm, 4*cm, 7.5*cm])
        comp_style_list = [
            ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#1E3A5F")),
            ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
            ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),
             [colors.HexColor("#F8FAFC"), colors.HexColor("#EFF6FF")]),
            ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#CBD5E1")),
            ("PADDING",     (0,0), (-1,-1), 5),
        ]
        # Highlight L3 row if events exist
        if l3_count > 0:
            comp_style_list.append(
                ("TEXTCOLOR", (1,4), (1,4), colors.HexColor("#B91C1C"))
            )
        comp_tbl.setStyle(TableStyle(comp_style_list))
        story.append(comp_tbl)

        # Signature block
        story.append(Spacer(1, 0.5*cm))
        sig_data = [
            ["Site Safety Officer", "System (Auto-generated)", "Operator"],
            ["", "", ""],
            ["_____________________", "_____________________", "_____________________"],
            ["Signature / Stamp", "Fatigue Detection System v1.0", operator_name],
        ]
        sig_table = Table(sig_data, colWidths=[5.5*cm, 6*cm, 5.5*cm])
        sig_table.setStyle(TableStyle([
            ("FONTNAME",  (0,0), (-1,-1), "Helvetica"),
            ("FONTSIZE",  (0,0), (-1,-1), 8),
            ("TEXTCOLOR", (0,0), (-1,-1), colors.HexColor("#374151")),
            ("ALIGN",     (0,0), (-1,-1), "CENTER"),
            ("TOPPADDING",(0,1), (-1,1),  20),
        ]))
        story.append(sig_table)

        
        # FOOTER NOTES
        # can be changed as per the requirement 
        story.append(Spacer(1, 0.5*cm))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 0.2*cm))
        story.append(Paragraph(
            "This report was generated automatically by the Operator Fatigue "
            "Detection System. All data is stored locally on the edge device. "
            "No data was transmitted externally.",
            ParagraphStyle("Footer", parent=styles["Normal"],
                           fontSize=7, textColor=colors.HexColor("#9CA3AF"))
        ))

        # Build PDF
        doc.build(story)
        size = os.path.getsize(filepath)
        print(f"[REPORT]PDF generated: {filename} ({size:,} bytes)")

        write_audit_log("REPORT_EXPORT", "SYSTEM", session_id,
                        f"PDF: {filename}")
        return filepath

    except Exception as e:
        import traceback
        print(f"[REPORT]PDF generation error: {e}")
        traceback.print_exc()
        return None



# PERCLOS CHART


def _build_perclos_chart(events: list, session: dict):
    """Build a matplotlib PERCLOS trend chart embedded in PDF."""
    try:
        times   = []
        perclos = []
        scores  = []

        for ev in events:
            if ev.get("perclos_value") is not None:
                dt = _parse_iso(ev.get("timestamp",""))
                if dt:
                    times.append(dt)
                    perclos.append(ev["perclos_value"])
                    scores.append(ev.get("fatigue_score", 0) * 100)

        if len(times) < 2:
            return None

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 5), dpi=100)
        fig.patch.set_facecolor("#F8FAFC")

        # PERCLOS plot
        ax1.plot(times, perclos, color="#1D4ED8", linewidth=1.5, label="PERCLOS %")
        ax1.axhline(y=15, color="#F59E0B", linestyle="--",
                    linewidth=1, alpha=0.7, label="L1 baseline")
        ax1.axhline(y=25, color="#EF4444", linestyle="--",
                    linewidth=1, alpha=0.7, label="L2 baseline")
        ax1.set_ylabel("PERCLOS (%)", fontsize=9)
        ax1.set_title("PERCLOS over shift", fontsize=10, fontweight="bold")
        ax1.legend(fontsize=7, loc="upper left")
        ax1.set_facecolor("#F8FAFC")
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        # Fatigue score plot
        ax2.fill_between(times, scores, alpha=0.3, color="#7C3AED")
        ax2.plot(times, scores, color="#7C3AED", linewidth=1.5, label="Fatigue score %")
        ax2.set_ylabel("Score above baseline (%)", fontsize=9)
        ax2.set_title("Fatigue score (% above personal baseline)", fontsize=10)
        ax2.set_facecolor("#F8FAFC")
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        plt.tight_layout(pad=1.5)

        # Save to BytesIO and embed in PDF
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=100,
                    bbox_inches="tight", facecolor="#F8FAFC")
        buf.seek(0)
        plt.close(fig)

        return RLImage(buf, width=16*cm, height=6*cm)

    except Exception as e:
        print(f"[REPORT] Chart error: {e}")
        return None



# HELPERS


def _get_session(session_id: str) -> dict:
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _fmt_dt(dt) -> str:
    if not dt:
        return "N/A"
    return dt.strftime("%d %b %Y  %H:%M:%S")


def _format_duration(start, end) -> str:
    if not start or not end:
        return "N/A"
    delta = end - start
    hours   = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    return f"{hours}h {minutes}m"