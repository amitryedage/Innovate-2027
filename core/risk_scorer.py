# risk_scorer.py — USP  Shift-level fatigue risk scoring
# Converts all the raw fatigue signals into a single number (0-100)
# that a site manager can read at a glance.
# A manager with 20 operators doesn't read 20 PDFs.
# They need: green / amber / red per operator, ranked by risk.
# This is the feature that makes your system a management tool,
# not just an alert device — a completely different budget category.
# SCORING COMPONENTS (weighted):
#   30% — Alert frequency    (how many L1/L2/L3 events per hour)
#   30% — Alert severity     (weighted by level: L1=1, L2=3, L3=10)
#   20% — Acknowledgement    (slow/no ack = higher risk)
#   10% — Predictive trend   (rising slope adds to score)
#   10% — Shift time factor  (hour 6+ of shift = multiplier)
#
# OUTPUT:
#   0-25   → GREEN  — Low risk, operator alert
#   26-50  → AMBER  — Moderate risk, monitor closely
#   51-75  → ORANGE — High risk, consider break
#   76-100 → RED    — Critical risk, stop work