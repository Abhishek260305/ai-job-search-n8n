"""Builds the jobs-table queries over v_dashboard_jobs. Pure: no database, fully unit-tested.

The SQL text is assembled only from the fixed fragments in this module, picked by values JobsQuery has already
checked against fixed lists. Every value that comes from the request (search text, statuses, sources, numbers) is a
%s parameter. Nothing from a request is ever placed into the SQL text itself.
"""
from __future__ import annotations

from models import JobsQuery

LIST_COLUMNS = (
    "id, title, company, location, is_remote, salary_min, salary_max, salary_currency, score, verdict, scores, "
    "scam_risk, job_status, filter_reason, posted_at, first_seen, last_seen, deadline, closing_soon, "
    "application_status, applied_at, needs_attention, has_resume, has_cover_letter, pipeline_status, sources, "
    "apply_url"
)

TABS = {
    "shortlisted": "pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved')",
    "applied": "applied_at IS NOT NULL",
    "scored": "score IS NOT NULL",
    "filtered": "job_status = 'filtered_out'",
    "scam": "job_status = 'scam_blocked'",
    "all": None,
}

SORTS = {
    "score": "score",
    "posted": "coalesce(posted_at, first_seen)",
    "first_seen": "first_seen",
    "company": "lower(company)",
    "title": "lower(title)",
    "status": "pipeline_status",
}

EXPORT_LIMIT = 5000


def like_pattern(text: str) -> str:
    """A substring pattern for ILIKE ... ESCAPE '\\': the user's % and _ are matched literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def where_clause(q: JobsQuery) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    if TABS[q.tab]:
        conditions.append(TABS[q.tab])
    text = (q.q or "").strip()
    if text:
        conditions.append("(title ILIKE %s ESCAPE '\\' OR company ILIKE %s ESCAPE '\\')")
        params += [like_pattern(text), like_pattern(text)]
    if q.status:
        conditions.append("pipeline_status = ANY(%s)")
        params.append(sorted(set(q.status)))
    if q.source:
        conditions.append("sources && %s::text[]")
        params.append(sorted(set(q.source)))
    if q.min_score is not None:
        conditions.append("score >= %s")
        params.append(q.min_score)
    if q.remote:
        conditions.append("is_remote IS TRUE")
    if q.posted_days is not None:
        conditions.append("coalesce(posted_at, first_seen) >= now() - make_interval(days => %s)")
        params.append(q.posted_days)
    return (" WHERE " + " AND ".join(conditions) if conditions else ""), params


def order_clause(q: JobsQuery) -> str:
    direction = "ASC" if q.order == "asc" else "DESC"
    # id DESC last, so rows with equal sort values keep a stable order across pages
    return f" ORDER BY {SORTS[q.sort]} {direction} NULLS LAST, id DESC"


def jobs_page(q: JobsQuery) -> tuple[str, list]:
    where, params = where_clause(q)
    sql = f"SELECT {LIST_COLUMNS} FROM v_dashboard_jobs{where}{order_clause(q)} LIMIT %s OFFSET %s"
    return sql, params + [q.page_size, (q.page - 1) * q.page_size]


def jobs_count(q: JobsQuery) -> tuple[str, list]:
    where, params = where_clause(q)
    return f"SELECT count(*) AS total FROM v_dashboard_jobs{where}", params


def jobs_export(q: JobsQuery) -> tuple[str, list]:
    where, params = where_clause(q)
    return f"SELECT {LIST_COLUMNS} FROM v_dashboard_jobs{where}{order_clause(q)} LIMIT %s", params + [EXPORT_LIMIT]
