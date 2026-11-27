"""Business metrics: manual vs automated handling time, accuracy, cost.

Everything is measured from production data, not estimated:

* Time to triage = ticket received -> final classification stored.
* Human touch time = reviewer opened the task -> submitted it (the session that
  submitted). Abandoned opens are excluded.
* The manual baseline is the random ``manual_baseline_rate`` sample triaged by
  people without seeing AI output. The AI also ran on those tickets in shadow
  mode, so comparing its answer with the person's gives unbiased accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ticket_triage.pipeline import REASON_BASELINE

MIN_BASELINE_SAMPLE = 5


@dataclass(frozen=True)
class PathStats:
    path: str
    tickets: int
    median_time_to_triage_s: float | None
    p90_time_to_triage_s: float | None
    median_touch_s: float | None
    total_touch_s: float
    llm_cost_usd: Decimal


@dataclass(frozen=True)
class Accuracy:
    sample: int
    category: float | None
    priority: float | None


@dataclass(frozen=True)
class Report:
    since: datetime
    received: int
    open_reviews: int
    paths: dict[str, PathStats]
    shadow_accuracy: Accuracy
    review_agreement: Accuracy
    median_ai_latency_ms: float | None
    llm_cost_usd: Decimal
    llm_cost_per_ticket_usd: Decimal | None
    automation_rate: float | None
    baseline_ready: bool
    manual_touch_s: float | None
    hours_saved: float | None
    triage_speedup: float | None
    status_counts: dict[str, int]


_PATH_SQL = text(
    """
    WITH touch AS (
        SELECT rt.ticket_id,
               SUM(EXTRACT(EPOCH FROM rs.submitted_at - rs.opened_at)) AS touch_s
        FROM review_tasks rt
        JOIN review_sessions rs ON rs.task_id = rt.id AND rs.submitted_at IS NOT NULL
        WHERE rt.status = 'done' AND NOT (:reason_failed = ANY(rt.reasons))
        GROUP BY rt.ticket_id
    ), cost AS (
        SELECT ticket_id, SUM(cost_usd) AS cost FROM triage_runs GROUP BY ticket_id
    )
    SELECT t.processing_path::text AS path,
           COUNT(*) AS tickets,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM t.triaged_at - t.received_at)) AS median_ttt,
           percentile_cont(0.9) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM t.triaged_at - t.received_at)) AS p90_ttt,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY touch.touch_s) AS median_touch,
           COALESCE(SUM(touch.touch_s), 0) AS total_touch,
           COALESCE(SUM(cost.cost), 0) AS cost
    FROM tickets t
    LEFT JOIN touch ON touch.ticket_id = t.id
    LEFT JOIN cost ON cost.ticket_id = t.id
    WHERE t.triaged_at IS NOT NULL AND t.received_at >= :since
      AND NOT EXISTS (
          SELECT 1 FROM review_tasks f
          WHERE f.ticket_id = t.id AND :reason_failed = ANY(f.reasons))
    GROUP BY t.processing_path
    """
)

_SHADOW_ACCURACY_SQL = text(
    """
    SELECT COUNT(*) AS n,
           AVG((tr.output->>'category' = rt.decision->>'category')::int) AS category,
           AVG((tr.output->>'priority' = rt.decision->>'priority')::int) AS priority
    FROM review_tasks rt
    JOIN tickets t ON t.id = rt.ticket_id
    JOIN LATERAL (
        SELECT output FROM triage_runs
        WHERE ticket_id = rt.ticket_id AND shadow AND output IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
    ) tr ON TRUE
    WHERE rt.status = 'done' AND :reason_baseline = ANY(rt.reasons) AND t.received_at >= :since
    """
)

_AGREEMENT_SQL = text(
    """
    SELECT COUNT(*) AS n,
           AVG((NOT ('category' = ANY(rt.changed_fields)))::int) AS category,
           AVG((NOT ('priority' = ANY(rt.changed_fields)))::int) AS priority
    FROM review_tasks rt
    JOIN tickets t ON t.id = rt.ticket_id
    WHERE rt.status = 'done' AND rt.kind = 'verify_ai' AND t.received_at >= :since
    """
)


def build_report(session: Session, days: int = 30, now: datetime | None = None) -> Report:
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    params = {"since": since, "reason_failed": "ai_failed", "reason_baseline": REASON_BASELINE}

    paths: dict[str, PathStats] = {}
    for row in session.execute(_PATH_SQL, params).mappings():
        paths[row["path"]] = PathStats(
            path=row["path"],
            tickets=int(row["tickets"]),
            median_time_to_triage_s=_f(row["median_ttt"]),
            p90_time_to_triage_s=_f(row["p90_ttt"]),
            median_touch_s=_f(row["median_touch"]),
            total_touch_s=float(row["total_touch"]),
            llm_cost_usd=Decimal(row["cost"]),
        )

    shadow = session.execute(_SHADOW_ACCURACY_SQL, params).mappings().one()
    agreement = session.execute(_AGREEMENT_SQL, params).mappings().one()
    received = session.execute(
        text("SELECT COUNT(*) FROM tickets WHERE received_at >= :since"), params
    ).scalar_one()
    open_reviews = session.execute(
        text("SELECT COUNT(*) FROM review_tasks WHERE status = 'open'")
    ).scalar_one()
    status_counts = {
        r.status: int(r.n)
        for r in session.execute(
            text(
                "SELECT status::text AS status, COUNT(*) AS n FROM tickets "
                "WHERE received_at >= :since GROUP BY status"
            ),
            params,
        )
    }
    llm = (
        session.execute(
            text(
                """
            SELECT COALESCE(SUM(r.cost_usd), 0) AS cost,
                   COUNT(DISTINCT r.ticket_id) AS tickets,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms)
                       FILTER (WHERE r.output IS NOT NULL) AS latency
            FROM triage_runs r JOIN tickets t ON t.id = r.ticket_id
            WHERE t.received_at >= :since
            """
            ),
            params,
        )
        .mappings()
        .one()
    )

    auto = paths.get("ai_auto")
    reviewed = paths.get("ai_reviewed")
    manual = paths.get("manual")
    n_auto = auto.tickets if auto else 0
    n_reviewed = reviewed.tickets if reviewed else 0
    # Tickets the AI routed to a person, decided or still waiting.
    sent_to_review = session.execute(
        text(
            "SELECT COUNT(*) FROM review_tasks rt JOIN tickets t ON t.id = rt.ticket_id "
            "WHERE rt.kind = 'verify_ai' AND t.received_at >= :since"
        ),
        params,
    ).scalar_one()
    ai_handled = n_auto + int(sent_to_review)

    baseline_ready = bool(manual and manual.tickets >= MIN_BASELINE_SAMPLE)
    manual_touch = manual.median_touch_s if manual and baseline_ready else None
    hours_saved = None
    if manual_touch is not None:
        review_touch = reviewed.median_touch_s if reviewed and reviewed.median_touch_s else 0.0
        saved_s = n_auto * manual_touch + n_reviewed * max(manual_touch - review_touch, 0.0)
        hours_saved = saved_s / 3600
    speedup = None
    if (
        baseline_ready
        and manual
        and manual.median_time_to_triage_s
        and auto
        and auto.median_time_to_triage_s
    ):
        speedup = manual.median_time_to_triage_s / auto.median_time_to_triage_s

    llm_tickets = int(llm["tickets"])
    return Report(
        since=since,
        received=int(received),
        open_reviews=int(open_reviews),
        paths=paths,
        shadow_accuracy=Accuracy(int(shadow["n"]), _f(shadow["category"]), _f(shadow["priority"])),
        review_agreement=Accuracy(
            int(agreement["n"]), _f(agreement["category"]), _f(agreement["priority"])
        ),
        median_ai_latency_ms=_f(llm["latency"]),
        llm_cost_usd=Decimal(llm["cost"]),
        llm_cost_per_ticket_usd=(Decimal(llm["cost"]) / llm_tickets) if llm_tickets else None,
        automation_rate=(n_auto / ai_handled) if ai_handled else None,
        baseline_ready=baseline_ready,
        manual_touch_s=manual_touch,
        hours_saved=hours_saved,
        triage_speedup=speedup,
        status_counts=status_counts,
    )


def _f(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]
