# analytics_engine.py — USP Cross-shift fatigue analytics
# Reads historical session data already stored in SQLite and
# produces actionable intelligence for site managers:
# Per-operator fatigue profile (which operators are highest risk)
#  Peak risk time windows (e.g. "2pm-4pm is high-risk for this site")
# Shift-duration fatigue correlation
# Operator improvement/deterioration trend over days
# Recommended interventions (break schedule, shift rotation)
# Every alert device logs data. Nobody in the Indian market is
# ANALYSING it. This turns your system from a sensor into an
# intelligence platform. Site managers can justify the system
# cost with data: "Since installing this, fatigue incidents
# dropped 40% after we adjusted the 2pm shift pattern."
# REQUIRES: No new hardware. Pure SQL queries on existing DB.
import sys, os
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.database import get_connection


class AnalyticsEngine:
    def get_operator_risk_profile(self, operator_id: str,
                                  days: int = 30) -> dict:
        """
        Aggregates all sessions for an operator over the last N days.
        Returns their personal fatigue fingerprint.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = get_connection()

        try:
            # All sessions for this operator
            sessions = conn.execute("""
                SELECT s.session_id, s.start_time, s.end_time,
                       s.glasses_mode, s.demo_mode,
                       COUNT(e.event_id) as total_events,
                       MAX(e.alert_level) as max_level,
                       AVG(e.fatigue_score) as avg_score,
                       SUM(CASE WHEN e.acknowledged=1 THEN 1 ELSE 0 END) as acked,
                       AVG(e.ack_time_sec) as avg_ack_time
                FROM sessions s
                LEFT JOIN events e
                    ON s.session_id = e.session_id
                    AND e.event_type LIKE 'FATIGUE%'
                WHERE s.operator_id=?
                AND s.start_time >= ?
                AND s.status IN ('CLOSED','CRASHED')
                GROUP BY s.session_id
                ORDER BY s.start_time DESC
            """, (operator_id, since)).fetchall()

            if not sessions:
                return {"operator_id": operator_id, "days": days,
                        "sessions": 0, "message": "No historical data"}

            sessions = [dict(s) for s in sessions]

            # Compute shift durations
            durations = []
            for s in sessions:
                if s.get("start_time") and s.get("end_time"):
                    try:
                        t0 = datetime.fromisoformat(s["start_time"])
                        t1 = datetime.fromisoformat(s["end_time"])
                        durations.append((t1 - t0).total_seconds() / 3600)
                    except Exception:
                        pass

            # Aggregate metrics
            total_events    = sum(s.get("total_events", 0) or 0 for s in sessions)
            avg_events_sess = total_events / len(sessions) if sessions else 0
            max_level_seen  = max((s.get("max_level") or 0 for s in sessions), default=0)
            avg_score       = (sum(s.get("avg_score") or 0 for s in sessions)
                               / len(sessions)) if sessions else 0
            total_acked     = sum(s.get("acked") or 0 for s in sessions)
            ack_rate        = total_acked / total_events if total_events > 0 else 1.0
            avg_ack_times   = [s.get("avg_ack_time") for s in sessions
                               if s.get("avg_ack_time")]
            avg_ack_time    = sum(avg_ack_times)/len(avg_ack_times) if avg_ack_times else None

            # Risk trend: compare first half vs second half of sessions
            mid = len(sessions) // 2
            recent_events = sum(s.get("total_events", 0) or 0
                               for s in sessions[:mid]) if mid else 0
            older_events  = sum(s.get("total_events", 0) or 0
                               for s in sessions[mid:]) if mid else 0
            trend = "improving" if recent_events < older_events else \
                    "worsening" if recent_events > older_events else "stable"

            # Peak alert level band
            if max_level_seen >= 3:
                risk_band = "HIGH"
            elif max_level_seen >= 2 or avg_events_sess >= 3:
                risk_band = "MEDIUM"
            else:
                risk_band = "LOW"

            return {
                "operator_id":       operator_id,
                "analysis_days":     days,
                "total_sessions":    len(sessions),
                "avg_shift_hours":   round(sum(durations)/len(durations), 1) if durations else None,
                "total_fatigue_events": total_events,
                "avg_events_per_shift": round(avg_events_sess, 1),
                "max_alert_level_seen": max_level_seen,
                "avg_fatigue_score":    round(avg_score, 3),
                "ack_rate_pct":         round(ack_rate * 100, 1),
                "avg_ack_time_sec":     round(avg_ack_time, 1) if avg_ack_time else None,
                "trend_vs_history":     trend,
                "risk_band":            risk_band,
                "sessions":             sessions[:10],  # last 10 for detail table
            }

        finally:
            conn.close()

    # Find the feak of risk hours 
    # SITE PEAK RISK HOURS


    def get_peak_risk_hours(self, days: int = 14) -> dict:
        """
        Identifies which hours of day generate the most fatigue events
        across ALL operators. Used to recommend shift pattern changes.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = get_connection()

        try:
            rows = conn.execute("""
                SELECT strftime('%H', e.timestamp) as hour,
                       COUNT(*) as event_count,
                       AVG(e.alert_level) as avg_level,
                       AVG(e.fatigue_score) as avg_score
                FROM events e
                JOIN sessions s ON e.session_id = s.session_id
                WHERE e.event_type LIKE 'FATIGUE%'
                AND e.timestamp >= ?
                GROUP BY hour
                ORDER BY event_count DESC
            """, (since,)).fetchall()

            if not rows:
                return {"message": "Insufficient data", "days": days}

            hours_data = [dict(r) for r in rows]

            # Find peak hours (top 3)
            peak_hours = hours_data[:3]

            # Find safest hours (bottom 3)
            safe_hours = sorted(hours_data, key=lambda x: x["event_count"])[:3]

            # Generate recommendation
            peak_hr_nums = [int(h["hour"]) for h in peak_hours]
            recs = []
            for hr in peak_hr_nums:
                if 13 <= hr <= 16:
                    recs.append(f"Schedule mandatory 15-min break at {hr:02d}:00")
                elif 3 <= hr <= 6:
                    recs.append(f"Night shift risk window at {hr:02d}:00 — "
                                f"consider crew rotation")
                else:
                    recs.append(f"High fatigue at {hr:02d}:00 — review workload")

            return {
                "analysis_days":  days,
                "hourly_data":    hours_data,
                "peak_hours":     peak_hours,
                "safest_hours":   safe_hours,
                "recommendations": recs,
            }

        finally:
            conn.close()

    # SITE-WIDE OPERATOR RANKING
    # Rank the different opreator based on there health condition

    def get_operator_ranking(self, days: int = 7) -> list:
        """
        Ranks all operators by fatigue risk over the last N days.
        Used for the manager's daily morning briefing view.
        Returns list of operators sorted highest risk first.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = get_connection()

        try:
            rows = conn.execute("""
                SELECT s.operator_id,
                       o.name,
                       COUNT(DISTINCT s.session_id) as sessions,
                       COUNT(e.event_id) as total_events,
                       MAX(e.alert_level) as max_level,
                       SUM(CASE WHEN e.alert_level=3 THEN 1 ELSE 0 END) as l3_count,
                       AVG(e.fatigue_score) as avg_score,
                       SUM(CASE WHEN e.acknowledged=0 THEN 1 ELSE 0 END) as unacked
                FROM sessions s
                JOIN operators o ON s.operator_id = o.operator_id
                LEFT JOIN events e
                    ON s.session_id = e.session_id
                    AND e.event_type LIKE 'FATIGUE%'
                WHERE s.start_time >= ?
                AND s.status IN ('CLOSED','CRASHED')
                GROUP BY s.operator_id
                ORDER BY l3_count DESC, total_events DESC
            """, (since,)).fetchall()

            if not rows:
                return []

            result = []
            for r in rows:
                row = dict(r)
                # Simple risk score for ranking
                risk = (
                    (row.get("l3_count") or 0) * 10 +
                    (row.get("total_events") or 0) * 1 +
                    (row.get("unacked") or 0) * 5
                )
                sessions = row.get("sessions") or 1
                row["risk_index"]    = round(risk / sessions, 1)
                row["avg_score"]     = round(row.get("avg_score") or 0, 3)
                row["risk_band"]     = (
                    "RED"    if risk/sessions >= 10 else
                    "ORANGE" if risk/sessions >= 5  else
                    "AMBER"  if risk/sessions >= 2  else
                    "GREEN"
                )
                result.append(row)

            result.sort(key=lambda x: x["risk_index"], reverse=True)
            return result

        finally:
            conn.close()

    def get_site_summary(self, days: int = 14) -> dict:
        """
        Provides a site-wide fatigue risk summary and safety recommendations.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = get_connection()
        try:
            row = conn.execute("""
                SELECT COUNT(*) as total_events,
                       MAX(e.alert_level) as max_level
                FROM events e
                JOIN sessions s ON e.session_id = s.session_id
                WHERE e.event_type LIKE 'FATIGUE%'
                AND e.timestamp >= ?
            """, (since,)).fetchone()
            
            sess_count = conn.execute("""
                SELECT COUNT(*) as total_sessions
                FROM sessions
                WHERE start_time >= ?
                AND status IN ('CLOSED','CRASHED')
            """, (since,)).fetchone()["total_sessions"] or 1
            
            total_events = row["total_events"] or 0
            max_level = row["max_level"] or 0
            avg_events = total_events / sess_count if sess_count > 0 else 0
            
            if max_level >= 3:
                site_risk_level = "HIGH"
            elif max_level >= 2 or avg_events >= 3:
                site_risk_level = "MEDIUM"
            else:
                site_risk_level = "LOW"

            recs = []
            if max_level >= 3:
                recs.append("Critical fatigue events detected — schedule immediate operator safety reviews")
            if avg_events >= 2:
                recs.append("Elevated fatigue frequency per shift — introduce mid-shift recovery breaks")
            
            unacked = conn.execute("""
                SELECT COUNT(*) as count
                FROM events e
                JOIN sessions s ON e.session_id = s.session_id
                WHERE e.event_type LIKE 'FATIGUE%'
                AND e.timestamp >= ?
                AND e.acknowledged = 0
            """, (since,)).fetchone()["count"] or 0
            if unacked > 0:
                recs.append(f"Found {unacked} unacknowledged alert(s) — reinforce operator training on alert acknowledgment")
                
            if not recs:
                recs.append("No significant fatigue risks detected. Maintain current operating protocols.")

            return {
                "site_risk_level": site_risk_level,
                "recommendations": recs,
                "total_events": total_events,
                "max_level": max_level,
            }
        finally:
            conn.close()

    def get_duration_fatigue_correlation(self, days: int = 30) -> dict:
        """
        Analyses the relationship between shift duration and fatigue events.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT s.session_id, s.start_time, s.end_time,
                       COUNT(e.event_id) as total_events
                FROM sessions s
                LEFT JOIN events e
                    ON s.session_id = e.session_id
                    AND e.event_type LIKE 'FATIGUE%'
                WHERE s.start_time >= ?
                AND s.status IN ('CLOSED', 'CRASHED')
                GROUP BY s.session_id
            """, (since,)).fetchall()

            data_points = []
            durations = []
            event_counts = []

            for r in rows:
                row = dict(r)
                if row.get("start_time") and row.get("end_time"):
                    try:
                        t0 = datetime.fromisoformat(row["start_time"])
                        t1 = datetime.fromisoformat(row["end_time"])
                        dur_hours = (t1 - t0).total_seconds() / 3600
                        events = row["total_events"] or 0
                        data_points.append({
                            "session_id": row["session_id"],
                            "duration_hours": round(dur_hours, 2),
                            "events": events
                        })
                        durations.append(dur_hours)
                        event_counts.append(events)
                    except Exception:
                        pass

            correlation = 0.0
            n = len(data_points)
            if n >= 2:
                import numpy as np
                try:
                    corr_matrix = np.corrcoef(durations, event_counts)
                    correlation = float(corr_matrix[0, 1])
                    if np.isnan(correlation):
                        correlation = 0.0
                except Exception:
                    mean_x = sum(durations) / n
                    mean_y = sum(event_counts) / n
                    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(durations, event_counts))
                    den_x = sum((x - mean_x) ** 2 for x in durations)
                    den_y = sum((y - mean_y) ** 2 for y in event_counts)
                    if den_x > 0 and den_y > 0:
                        correlation = num / ((den_x * den_y) ** 0.5)

            buckets = defaultdict(list)
            for dp in data_points:
                dur = dp["duration_hours"]
                if dur <= 2.0:
                    bucket_name = "0-2 hours"
                elif dur <= 4.0:
                    bucket_name = "2-4 hours"
                elif dur <= 6.0:
                    bucket_name = "4-6 hours"
                elif dur <= 8.0:
                    bucket_name = "6-8 hours"
                else:
                    bucket_name = "8+ hours"
                buckets[bucket_name].append(dp["events"])

            bucket_summary = {}
            for name, counts in buckets.items():
                bucket_summary[name] = {
                    "sessions": len(counts),
                    "avg_events": round(sum(counts) / len(counts), 1) if counts else 0
                }

            return {
                "analysis_days": days,
                "data_points": data_points,
                "correlation_coefficient": round(correlation, 3),
                "bucket_summary": bucket_summary,
                "total_sessions": n
            }
        finally:
            conn.close()


    