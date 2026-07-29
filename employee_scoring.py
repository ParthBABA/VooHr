"""Rule-based wellness / attrition-risk scoring for employees.

This intentionally starts as a transparent, explainable weighted model —
not a black-box ML/"psychological AI" score — built from HR-observable
signals that already exist in most orgs (attendance, overtime, review
deltas, deadlines, engagement surveys). It's meant to be replaceable
later with a learned model, but every score should stay explainable:
`score_employee()` returns *why* a score is what it is, not just the
number, so the dashboard/alerts can show real reasons instead of
inventing them.

Input signals (all optional; missing signals just don't penalize):
- overtime_hours_last_3w: float, hours of overtime in the trailing 3 weeks
- absences_last_30d: int, days absent in the trailing 30 days
- performance_delta_pct: float, % change in performance rating vs. last
  review period (negative = decline)
- missed_deadlines_last_30d: int
- engagement_survey_score: int 0-100, most recent pulse-survey result

Output:
- wellness_score: int 0-100 (higher is better)
- status: "healthy" | "watch" | "at_risk"
- attrition_risk_pct: int 0-100 (rough, derived from wellness_score)
- reasons: list of {code, message, severity} — the specific factors that
  moved the score, used to populate the alert feed instead of hardcoded text
"""

from dataclasses import dataclass, field


STATUS_THRESHOLDS = (
    (60, "healthy"),
    (40, "watch"),
    (0, "at_risk"),
)

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass
class ScoreResult:
    wellness_score: int
    status: str
    attrition_risk_pct: int
    reasons: list = field(default_factory=list)


def _status_for(score: int) -> str:
    for floor, status in STATUS_THRESHOLDS:
        if score >= floor:
            return status
    return "at_risk"


def score_employee(
    *,
    overtime_hours_last_3w: float = 0,
    absences_last_30d: int = 0,
    performance_delta_pct: float = 0,
    missed_deadlines_last_30d: int = 0,
    engagement_survey_score: int | None = None,
) -> ScoreResult:
    score = 100.0
    reasons = []

    # --- Burnout signal: sustained overtime ---
    if overtime_hours_last_3w >= 25:
        score -= 48
        reasons.append({
            "code": "burnout_overtime",
            "message": f"{int(overtime_hours_last_3w)}h overtime over the last 3 weeks — sustained burnout signal",
            "severity": SEVERITY_CRITICAL,
        })
    elif overtime_hours_last_3w >= 12:
        score -= 18
        reasons.append({
            "code": "elevated_overtime",
            "message": f"{int(overtime_hours_last_3w)}h overtime over the last 3 weeks — worth monitoring",
            "severity": SEVERITY_WARNING,
        })

    # --- Performance trend ---
    if performance_delta_pct <= -30:
        score -= 45
        reasons.append({
            "code": "performance_drop",
            "message": f"Performance dropped {abs(performance_delta_pct):.0f}% vs last review period",
            "severity": SEVERITY_CRITICAL,
        })
    elif performance_delta_pct <= -10:
        score -= 15
        reasons.append({
            "code": "performance_dip",
            "message": f"Performance down {abs(performance_delta_pct):.0f}% vs last review period",
            "severity": SEVERITY_WARNING,
        })

    # --- Attendance pattern ---
    if absences_last_30d >= 4:
        score -= 25
        reasons.append({
            "code": "absence_pattern",
            "message": f"Absent {absences_last_30d} days this month — pattern emerging",
            "severity": SEVERITY_WARNING,
        })
    elif absences_last_30d >= 2:
        score -= 10
        reasons.append({
            "code": "absence_notice",
            "message": f"Absent {absences_last_30d} days this month",
            "severity": SEVERITY_INFO,
        })

    # --- Missed deadlines ---
    if missed_deadlines_last_30d >= 2:
        score -= 20
        reasons.append({
            "code": "missed_deadlines",
            "message": f"Missed {missed_deadlines_last_30d} consecutive deadlines",
            "severity": SEVERITY_WARNING,
        })
    elif missed_deadlines_last_30d == 1:
        score -= 6
        reasons.append({
            "code": "missed_deadline",
            "message": "Missed 1 deadline this month",
            "severity": SEVERITY_INFO,
        })

    # --- Engagement survey: small additive nudge, not a rescue blend ---
    # (deliberately NOT averaged in at a high weight — a good survey score
    # shouldn't be able to fully cancel out a burnout/performance signal)
    if engagement_survey_score is not None:
        if engagement_survey_score < 50:
            score -= 10
            reasons.append({
                "code": "low_engagement",
                "message": "Wellness survey flagged low engagement",
                "severity": SEVERITY_INFO,
            })
        elif engagement_survey_score >= 80:
            score += 5

    score = max(0, min(100, round(score)))
    status = _status_for(score)
    attrition_risk_pct = max(0, min(100, 100 - score))

    # Sort reasons worst-first so the dashboard can show the most urgent one
    severity_rank = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    reasons.sort(key=lambda r: severity_rank.get(r["severity"], 3))

    return ScoreResult(
        wellness_score=score,
        status=status,
        attrition_risk_pct=attrition_risk_pct,
        reasons=reasons,
    )
