"""Postgres access for the pipeline (psycopg 3). Thin on purpose: the logic is in SQL functions
(postgres/migrations) and pipeline.py. Verified against a real database; unit tests use a fake store.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Json

SETTINGS_COLUMNS = ("search_terms", "locations", "sources", "allow_remote", "min_yoe", "max_yoe",
                    "include_keywords", "exclude_keywords", "exclude_companies", "max_age_days", "score_threshold",
                    "boards", "board_title_words")
# Columns added later (migration 013) and what a database that does not have them yet means by them.
# Every other settings column stays strict: a missing one is a real error.
NEW_SETTINGS_DEFAULTS = {"boards": {}, "board_title_words": []}


def conninfo_from_env(env=os.environ) -> str:
    return make_conninfo(host=env.get("DB_HOST", "postgres"), port=env.get("DB_PORT", "5432"),
                         dbname=env.get("DB_NAME", "jobsearch"), user=env.get("DB_USER", "jobs"),
                         password=env.get("DB_PASSWORD", ""), connect_timeout="10")


class PgStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    @contextmanager
    def cursor(self):
        # commits on normal exit, rolls back on an exception
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                yield cur

    def search_profile_row(self, name: str = "default") -> Optional[dict]:
        """The whole search_profile row (with `updated_at`, the version the dashboard sends back, and `edited_by`)."""
        with self.cursor() as cur:
            cur.execute("SELECT * FROM search_profile WHERE name = %s", (name,))
            return cur.fetchone()

    def save_search_profile(self, name: str, settings: dict, expected_version: str, edited_by: str = "dashboard") -> Optional[dict]:
        """Replace the profile, but only if nobody wrote it since `expected_version` (its updated_at as the dashboard
        last read it). Returns the new row, or None when the profile is missing or was changed in between."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE search_profile SET search_terms = %s, locations = %s, sources = %s, allow_remote = %s, min_yoe = %s, "
                "max_yoe = %s, include_keywords = %s, exclude_keywords = %s, exclude_companies = %s, max_age_days = %s, "
                "score_threshold = %s, boards = %s, board_title_words = %s, edited_by = %s, updated_at = now() "
                "WHERE name = %s AND updated_at = %s::timestamptz RETURNING *",
                (settings["search_terms"], settings["locations"], settings["sources"], settings["allow_remote"],
                 settings["min_yoe"], settings["max_yoe"], settings["include_keywords"], settings["exclude_keywords"],
                 settings["exclude_companies"], settings["max_age_days"], settings["score_threshold"],
                 Json(settings["boards"]), settings["board_title_words"], edited_by, name, expected_version))
            return cur.fetchone()

    def search_settings(self, profile: str = "default") -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM search_profile WHERE name = %s", (profile,))
            row = cur.fetchone()
        if not row:
            raise LookupError(f"search profile '{profile}' not found (run: scripts/jobsearch search-profile apply)")
        out = {k: row[k] for k in SETTINGS_COLUMNS if k not in NEW_SETTINGS_DEFAULTS}
        # the two Phase 7 columns come from migration 013: a database that missed it behaves like the old code
        for k, default in NEW_SETTINGS_DEFAULTS.items():
            out[k] = row.get(k, default)
        return out

    def next_slice(self, profile: str, total: int, batch: int) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT next_collect_slice(%s, %s, %s) AS s", (profile, total, batch))
            return cur.fetchone()["s"]

    def upsert(self, j: dict, assessment: dict) -> tuple[int, bool]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT job_id, is_new FROM upsert_job(%s,%s,%s,%s,%s,%s,%s,%s::boolean,%s::timestamptz,%s,"
                "%s::numeric,%s::numeric,%s,%s::jsonb,%s::date,%s,%s::int,%s::jsonb,%s)",
                (j.get("source"), str(j.get("source_job_id")), j.get("url"), j.get("apply_url"), j.get("title"),
                 j.get("company"), j.get("location"), j.get("is_remote"), j.get("posted_at"), j.get("description"),
                 j.get("salary_min"), j.get("salary_max"), j.get("salary_currency"), Json(j.get("raw") or {}),
                 None, assessment["risk"], assessment["score"], Json(assessment["flags"]), None))
            r = cur.fetchone()
            return r["job_id"], r["is_new"]

    def status_of(self, job_id: int) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
            return cur.fetchone()["status"]

    def set_yoe(self, job_id: int, lo, hi) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE jobs SET yoe_min = coalesce(%s::int, yoe_min), yoe_max = coalesce(%s::int, yoe_max) "
                        "WHERE id = %s", (lo, hi, job_id))

    def mark_filtered(self, job_id: int, reason: str, lo, hi) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE jobs SET status = 'filtered_out', filter_reason = %s, "
                        "yoe_min = coalesce(%s::int, yoe_min), yoe_max = coalesce(%s::int, yoe_max) "
                        "WHERE id = %s AND status = 'new'", (reason, lo, hi, job_id))

    def claim(self, limit: int) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM claim_jobs_to_score(%s)", (limit,))
            return cur.fetchall()

    def note_failure(self, job_id: int) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT note_score_failure(%s) AS n", (job_id,))
            return cur.fetchone()["n"]

    def record_score(self, job_id: int, result: dict) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT record_score(%s::bigint, %s::jsonb) AS s", (job_id, Json(result)))
            return cur.fetchone()["s"]

    def digest_rows(self, limit: int) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, title, company, location, score, verdict, scam_risk, scam_flags, deadline, gates, "
                "strengths, gaps, canonical_url, posted_at FROM jobs "
                "WHERE status = 'shortlisted' AND notified_at IS NULL AND coalesce(scam_risk, 'low') <> 'high' "
                "ORDER BY score DESC NULLS LAST, id LIMIT %s", (limit,))
            return cur.fetchall()

    def mark_notified(self, ids: list[int]) -> int:
        with self.cursor() as cur:
            cur.execute("UPDATE jobs SET notified_at = now() WHERE id = ANY(%s::bigint[]) AND notified_at IS NULL", (ids,))
            return cur.rowcount

    # ---- budget and usage -------------------------------------------------------------
    def budget_status(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM budget_status()")
            return cur.fetchone()

    def pricing(self, model: str) -> dict:
        """Per-million-token rates for a model; an unknown model gets the most expensive known rates."""
        with self.cursor() as cur:
            cur.execute("SELECT input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok "
                        "FROM model_pricing WHERE model = %s", (model,))
            row = cur.fetchone()
            if row:
                return row
            cur.execute("SELECT max(input_per_mtok) AS input_per_mtok, max(output_per_mtok) AS output_per_mtok, "
                        "max(cache_write_per_mtok) AS cache_write_per_mtok, max(cache_read_per_mtok) AS cache_read_per_mtok "
                        "FROM model_pricing")
            return cur.fetchone()

    def record_usage(self, provider: str, purpose: str, model, job_id, usage: dict, ok: bool = True,
                     note=None, cost_override=None) -> float:
        with self.cursor() as cur:
            cur.execute("SELECT record_usage(%s, %s, %s, %s::bigint, %s, %s, %s, %s, %s, %s, %s::numeric) AS c",
                        (provider, purpose, model, job_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                         usage.get("cache_write_tokens", 0), usage.get("cache_read_tokens", 0), ok, note, cost_override))
            return float(cur.fetchone()["c"])

    def budget_settings(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT monthly_cap_usd, daily_cap_usd, warn_pct, timezone, apify_monthly_cap_usd, apify_daily_cap_usd "
                        "FROM budget_settings WHERE id = 1")
            return cur.fetchone()

    def update_budget_settings(self, **fields) -> dict:
        allowed = {"monthly_cap_usd", "daily_cap_usd", "warn_pct", "timezone", "apify_monthly_cap_usd", "apify_daily_cap_usd"}
        cols = [k for k in fields if k in allowed and fields[k] is not None]
        if cols:
            with self.cursor() as cur:
                cur.execute("UPDATE budget_settings SET " + ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now() WHERE id = 1",
                            [fields[c] for c in cols])
        return self.budget_settings()

    def spend_summary(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT provider, purpose, model, sum(calls)::int AS calls, sum(input_tokens)::bigint AS input_tokens, "
                        "sum(output_tokens)::bigint AS output_tokens, sum(cache_read_tokens)::bigint AS cache_read_tokens, "
                        "round(sum(cost_usd), 4) AS cost_usd FROM v_spend_month GROUP BY 1, 2, 3 ORDER BY 8 DESC")
            return cur.fetchall()

    # ---- tailoring, review, telegram -----------------------------------------------------
    def tailor_settings(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT mode, min_score, max_score, daily_limit, approval, model, cover_letter, max_pages, verify, verify_model, paper, style_notes "
                        "FROM tailor_settings WHERE id = 1")
            return cur.fetchone()

    def update_tailor_settings(self, **fields) -> dict:
        allowed = {"mode", "min_score", "max_score", "daily_limit", "approval", "model", "cover_letter", "max_pages", "verify", "verify_model", "paper", "style_notes"}
        cols = [k for k in fields if k in allowed]
        if cols:
            with self.cursor() as cur:
                cur.execute("UPDATE tailor_settings SET " + ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now() WHERE id = 1",
                            [fields[c] for c in cols])
        return self.tailor_settings()

    # ---- optional parts (feature switches) and manual status editing ---------------------
    def features(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT tailoring, telegram, digest FROM feature_settings WHERE id = 1")
            return cur.fetchone()

    def update_features(self, **flags) -> dict:
        cols = [k for k in flags if k in ("tailoring", "telegram", "digest")]
        if cols:
            with self.cursor() as cur:
                cur.execute("UPDATE feature_settings SET " + ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now() WHERE id = 1",
                            [bool(flags[c]) for c in cols])
        return self.features()

    def set_application_status(self, job_id: int, status: str, note=None) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT set_application_status(%s::bigint, %s, %s) AS s", (job_id, status, note))
            return cur.fetchone()["s"]

    # ---- runaway guards: refunds for attempts that cost nothing, Apify caps, parked jobs, global pause ----
    def release_score_claim(self, job_id: int) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT release_score_claim(%s::bigint) AS n", (job_id,))
            row = cur.fetchone()
            return row["n"] if row else 0

    def release_tailor_claim(self, job_id: int) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT release_tailor_claim(%s::bigint) AS n", (job_id,))
            row = cur.fetchone()
            return row["n"] if row else 0

    def apify_budget_status(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM apify_budget_status()")
            return cur.fetchone()

    def schedule_pause(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT paused, paused_at, reason FROM schedule_control WHERE id = 1")
            return cur.fetchone()

    def set_schedule_paused(self, paused: bool, reason: Optional[str] = None) -> dict:
        with self.cursor() as cur:
            cur.execute("UPDATE schedule_control SET paused = %s, paused_at = CASE WHEN %s THEN now() END, reason = %s WHERE id = 1",
                        (paused, paused, reason if paused else None))
        return self.schedule_pause()

    # ---- schedules (when each stage runs, and how much it does) --------------------------
    def schedule_status(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM schedule_status()")
            return cur.fetchall()

    def schedule_row(self, name: str) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT name, enabled, every_minutes, at_time, batch, model FROM schedule_settings WHERE name = %s", (name,))
            row = cur.fetchone()
        if not row:
            raise LookupError(f"no schedule '{name}'")
        return row

    def update_schedule(self, name: str, **fields) -> None:
        cols = [k for k in fields if k in ("enabled", "every_minutes", "at_time", "batch", "board_batch", "model")]
        if cols:
            with self.cursor() as cur:
                cur.execute("UPDATE schedule_settings SET " + ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now() WHERE name = %s",
                            [fields[c] for c in cols] + [name])

    def board_batch(self) -> int:
        """How many company boards one collect run visits (a long list is rotated through)."""
        try:
            with self.cursor() as cur:
                cur.execute("SELECT board_batch FROM schedule_settings WHERE name = 'collect'")
                row = cur.fetchone()
        except psycopg.errors.UndefinedColumn:
            # migration 013 is not applied yet. The failed statement aborted that transaction; leaving the
            # `with` above already rolled it back and closed the connection, so this opens nothing new.
            return 30
        return row["board_batch"] if row else 30

    # ---- per-feed politeness and health (fetch_state) --------------------------------------
    def claim_fetch(self, source: str, key: str, min_seconds: int) -> bool:
        """Atomically claim a (source, key) fetch; False when it was fetched less than min_seconds ago."""
        with self.cursor() as cur:
            cur.execute("SELECT claim_fetch(%s, %s, make_interval(secs => %s::double precision)) AS ok", (source, key, min_seconds))
            row = cur.fetchone()
            return bool(row and row["ok"])

    def record_fetch(self, source: str, key: str, ok: bool, count: Optional[int], error: Optional[str]) -> None:
        with self.cursor() as cur:
            cur.execute("SELECT record_fetch(%s, %s, %s, %s::int, %s)", (source, key, ok, count, error))

    def fetch_failures(self, min_failures: int = 5) -> list[dict]:
        """Feeds that failed at least min_failures times in a row, worst first (for the doctor)."""
        with self.cursor() as cur:
            cur.execute("SELECT source, key, failures, last_error, last_ok_at FROM fetch_state "
                        "WHERE failures >= %s ORDER BY failures DESC, source, key", (min_failures,))
            return cur.fetchall()

    def request_run(self, name: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE schedule_settings SET run_now = true, updated_at = now() WHERE name = %s", (name,))
            if cur.rowcount != 1:
                raise LookupError(f"no schedule '{name}'")

    def claim_due_schedules(self, skip: list[str]) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT name, batch, model FROM claim_due_schedules(%s::text[])", (skip,))
            return cur.fetchall()

    def valid_timezone(self, tz: str) -> bool:
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_timezone_names WHERE name = %s", (tz,))
            return cur.fetchone() is not None

    def search_threshold(self, profile: str = "default") -> int:
        return int(self.search_settings(profile)["score_threshold"])

    def claim_to_tailor(self, limit: int, min_score: int) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM claim_jobs_to_tailor(%s, %s)", (limit, min_score))
            return cur.fetchall()

    def note_tailor_failure(self, job_id: int) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT note_tailor_failure(%s::bigint) AS n", (job_id,))
            return cur.fetchone()["n"]

    def applications_today(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT applications_today() AS n")
            return cur.fetchone()["n"]

    def save_application(self, job_id, variant, files_dir, resume_path, cover_path, patch, requirements, audit,
                         model, cost, needs_attention, reason, approval) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT save_application(%s::bigint, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::numeric, %s, %s, %s) AS s",
                        (job_id, variant, files_dir, resume_path, cover_path, Json(patch), Json(requirements), Json(audit),
                         model, cost, needs_attention, reason, approval))
            return cur.fetchone()["s"]

    def review_application(self, job_id: int, action: str, note=None) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT review_application(%s::bigint, %s, %s) AS s", (job_id, action, note))
            return cur.fetchone()["s"]

    def review_queue(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM v_review_queue ORDER BY created_at DESC")
            return cur.fetchall()

    def get_job(self, job_id: int):
        with self.cursor() as cur:
            cur.execute("SELECT id, title, company, location, description, canonical_url, score, verdict, strengths, gaps, "
                        "gates, posted_at, status FROM jobs WHERE id = %s", (job_id,))
            return cur.fetchone()

    def application(self, job_id: int):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM applications WHERE job_id = %s", (job_id,))
            return cur.fetchone()

    def status_counts(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT status, count(*)::int AS n FROM jobs GROUP BY status ORDER BY n DESC")
            return {r["status"]: r["n"] for r in cur.fetchall()}

    def telegram_offset(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT update_offset FROM telegram_state WHERE id = 1")
            return int(cur.fetchone()["update_offset"])

    def set_telegram_offset(self, n: int) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE telegram_state SET update_offset = %s, updated_at = now() WHERE id = 1", (n,))

    def blocked_rows(self) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT id, title, company, location, description, canonical_url AS url, posted_at, is_remote "
                        "FROM jobs WHERE status = 'scam_blocked'")
            return cur.fetchall()

    def set_safety(self, job_id: int, assessment: dict, unblock: bool) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE jobs SET scam_risk = %s, scam_score = %s, scam_flags = %s::jsonb, "
                        "status = CASE WHEN %s AND status = 'scam_blocked' THEN 'new' ELSE status END, "
                        "filter_reason = CASE WHEN %s AND status = 'scam_blocked' THEN NULL ELSE filter_reason END "
                        "WHERE id = %s", (assessment["risk"], assessment["score"], Json(assessment["flags"]),
                                          unblock, unblock, job_id))

    def sweep(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT sweep_expired() AS n")
            return cur.fetchone()["n"]
