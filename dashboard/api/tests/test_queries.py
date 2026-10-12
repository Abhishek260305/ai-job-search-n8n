"""The jobs-table query builder: every tab, filter, sort and page, and that request values only ever travel as
parameters."""
from __future__ import annotations

import pytest

import queries
from models import JobsQuery


def build(**kw):
    return queries.jobs_page(JobsQuery.model_validate(kw))


def test_the_default_query():
    sql, params = build()
    assert sql == (f"SELECT {queries.LIST_COLUMNS} FROM v_dashboard_jobs"
                   " WHERE pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved')"
                   " ORDER BY score DESC NULLS LAST, id DESC LIMIT %s OFFSET %s")
    assert params == [50, 0]


@pytest.mark.parametrize("tab, condition", [
    ("shortlisted", "pipeline_status IN ('shortlisted', 'tailored', 'drafted', 'approved')"),
    ("applied", "applied_at IS NOT NULL"),
    ("scored", "score IS NOT NULL"),
    ("filtered", "job_status = 'filtered_out'"),
    ("scam", "job_status = 'scam_blocked'"),
])
def test_each_tab(tab, condition):
    sql, _ = build(tab=tab)
    assert f" WHERE {condition} ORDER BY" in sql


def test_the_all_tab_has_no_condition():
    sql, params = build(tab="all")
    assert " WHERE " not in sql and params == [50, 0]


def test_every_filter_together_in_order():
    sql, params = build(tab="all", q="acme", status=["offer", "interview", "offer"], source=["lever", "greenhouse"],
                        min_score=60, remote=True, posted_days=14)
    assert (" WHERE (title ILIKE %s ESCAPE '\\' OR company ILIKE %s ESCAPE '\\')"
            " AND pipeline_status = ANY(%s) AND sources && %s::text[] AND score >= %s AND is_remote IS TRUE"
            " AND coalesce(posted_at, first_seen) >= now() - make_interval(days => %s) ORDER BY") in sql
    assert params == ["%acme%", "%acme%", ["interview", "offer"], ["greenhouse", "lever"], 60, 14, 50, 0]


def test_search_text_is_a_parameter_and_wildcards_are_literal():
    sql, params = build(tab="all", q="  100%_off\\deal  ")
    assert "100" not in sql and "off" not in sql
    assert params[:2] == ["%100\\%\\_off\\\\deal%"] * 2


def test_blank_search_adds_nothing():
    sql, params = build(tab="all", q="   ")
    assert "ILIKE" not in sql and params == [50, 0]


def test_a_hostile_search_never_reaches_the_sql_text():
    hostile = "x'); DELETE FROM jobs; --"
    sql, params = build(q=hostile)
    assert hostile not in sql and "DELETE" not in sql
    assert params[0] == f"%{hostile}%"


def test_remote_false_does_not_filter():
    assert "is_remote" not in queries.where_clause(JobsQuery(remote=False))[0]
    assert "is_remote IS TRUE" in queries.where_clause(JobsQuery(remote=True))[0]


def test_min_score_zero_still_filters():
    sql, params = build(tab="all", min_score=0)
    assert "score >= %s" in sql and params[0] == 0


@pytest.mark.parametrize("sort, expr", [
    ("score", "score"), ("posted", "coalesce(posted_at, first_seen)"), ("first_seen", "first_seen"),
    ("company", "lower(company)"), ("title", "lower(title)"), ("status", "pipeline_status"),
])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_every_sort_and_order(sort, expr, order):
    sql, _ = build(sort=sort, order=order)
    assert sql.endswith(f" ORDER BY {expr} {order.upper()} NULLS LAST, id DESC LIMIT %s OFFSET %s")


@pytest.mark.parametrize("page, size, offset", [(1, 25, 0), (2, 25, 25), (3, 50, 100), (10, 100, 900)])
def test_paging(page, size, offset):
    assert build(page=page, page_size=size)[1][-2:] == [size, offset]


def test_the_count_uses_the_same_filters_without_paging_or_order():
    q = JobsQuery(tab="applied", q="acme", min_score=70)
    count_sql, count_params = queries.jobs_count(q)
    page_sql, page_params = queries.jobs_page(q)
    assert count_sql.startswith("SELECT count(*) AS total FROM v_dashboard_jobs WHERE applied_at IS NOT NULL AND")
    assert "ORDER BY" not in count_sql and "LIMIT" not in count_sql
    assert count_params == page_params[:-2]
    assert queries.where_clause(q)[0] in page_sql


def test_the_export_uses_the_same_filters_and_order_with_a_cap():
    q = JobsQuery(tab="scored", sort="company", order="asc", page=7)
    sql, params = queries.jobs_export(q)
    assert sql.endswith(" WHERE score IS NOT NULL ORDER BY lower(company) ASC NULLS LAST, id DESC LIMIT %s")
    assert "OFFSET" not in sql, "the export ignores the page"
    assert params == [queries.EXPORT_LIMIT] and queries.EXPORT_LIMIT == 5000


def test_like_pattern():
    assert queries.like_pattern("abc") == "%abc%"
    assert queries.like_pattern("a%b_c\\d") == "%a\\%b\\_c\\\\d%"


def test_no_percent_signs_in_any_generated_sql_except_placeholders():
    """psycopg reads a lone % in the SQL text as a placeholder; the builder must never emit one."""
    for tab in ("shortlisted", "applied", "scored", "filtered", "scam", "all"):
        for sort in queries.SORTS:
            sql, _ = build(tab=tab, sort=sort, q="50%", min_score=1, posted_days=1, remote=True,
                           status=["applied"], source=["indeed"])
            assert sql.replace("%s", "").count("%") == 0, sql
