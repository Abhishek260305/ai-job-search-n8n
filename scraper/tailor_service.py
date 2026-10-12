"""Runs tailoring end to end for a job: budget check, draft, audit, one revision if needed, repair,
render, verify, save. The whole loop is one service call, so Telegram (`/tailor 24`), the CLI and the
scheduled n8n workflow all share it.

Honesty is enforced in code, in layers:
  1. the prompt forbids inventing anything;
  2. tailor.audit() checks the draft against the resume (numbers, technologies, names, years claimed);
  3. a failed audit triggers ONE revision request that lists exactly what was rejected;
  4. whatever is still unsupported is removed by tailor.repair(), so the final text is always grounded,
     and the application is flagged for a human look.

Spending: every model call is priced at its worst case against the budget caps before it is made, and
its real usage is logged afterwards (budget.py). Hitting a cap pauses tailoring; it never half-runs.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Optional

import budget
import features
import render
import scoring
import tailor
from anthropic_client import AnthropicError

MAX_FILE_NAME = 40


class _Failure(Exception):
    def __init__(self, reason: str, penalise: bool = True, spent: float = 0.0):
        self.reason, self.penalise, self.spent = reason, penalise, spent     # spent: model cost so far, dollars
        super().__init__(reason)


def slug(text: str, n: int = MAX_FILE_NAME) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:n].strip("-") or "x"


def choose_variant(score: Optional[int], settings: dict) -> str:
    """'base' when the job scores at or above max_score: the resume is already close, so it is used as is."""
    mx = settings.get("max_score")
    return "base" if mx is not None and score is not None and score >= mx else "tailored"


def effective_min_score(settings: dict, search_threshold: int) -> int:
    return settings["min_score"] if settings.get("min_score") is not None else search_threshold


class TailorService:
    def __init__(self, store, ai, pdf, out_root, resume: dict, profile: dict, *, today: Optional[date] = None,
                 telegram_on: bool = True):
        self.store, self.ai, self.pdf = store, ai, pdf
        self.telegram_on = telegram_on       # with no way to reach the user, `review` falls back to `auto`
        self._job_spend = 0.0                # model cost of the job being processed, dollars (decides refunds)
        self.out_root = Path(out_root)
        self.resume, self.profile = resume, profile
        self.today = today or date.today()

    # ------------------------------------------------------------ batch

    def run_next(self, limit: int = 3) -> dict:
        """Tailor the next eligible shortlisted jobs, within the settings, the daily limit and the budget."""
        settings = self.store.tailor_settings()
        if settings["mode"] == "manual":
            return {"skipped": "manual mode: nothing is tailored until you ask (/tailor N)", "results": []}
        status = self.store.budget_status()
        if status["blocked"]:
            return {"skipped": f"spending cap reached ({status['blocked_reason']})", "results": []}
        n = limit
        if settings.get("daily_limit") is not None:
            n = min(n, max(0, settings["daily_limit"] - self.store.applications_today()))
            if n == 0:
                return {"skipped": "daily tailoring limit reached", "results": []}
        minimum = effective_min_score(settings, self.store.search_threshold())
        jobs = self.store.claim_to_tailor(n, minimum)
        results = [self._guarded(j, settings) for j in jobs]
        return {"claimed": len(jobs), "min_score": minimum, "results": results}

    def _guarded(self, job: dict, settings: dict) -> dict:
        try:
            return self.run_job(job, settings)
        except Exception as ex:  # one bad job must not stop the rest
            self.store.note_tailor_failure(job["id"])
            return {"job_id": job["id"], "status": "failed", "reason": f"{type(ex).__name__}: {ex}"[:300]}

    # ------------------------------------------------------------ one job

    def run_job(self, job: dict, settings: Optional[dict] = None) -> dict:
        settings = settings or self.store.tailor_settings()
        job_id = job["id"]
        self._job_spend = 0.0
        variant = choose_variant(job.get("score"), settings)
        cover = bool(settings["cover_letter"])
        model = settings["model"]
        spent = 0.0
        patch: Optional[dict] = None
        requirements: list = []
        audit_dict: dict = {"ok": True, "hard": [], "soft": [], "fixed": []}
        notes: list[str] = []
        attention: list[str] = []

        if variant == "tailored" or cover:
            mode = "full" if variant == "tailored" else "cover_only"
            prompt = tailor.prepare(self.resume, self.profile, job, cover_letter=cover, mode=mode,
                                    notes={"strengths": job.get("strengths") or [], "gaps": job.get("gaps") or []},
                                    today=self.today, style_notes=settings.get("style_notes"))
            worst = budget.worst_case_item_cost(self.store.pricing(model), len(prompt["system"]) + len(prompt["user"]) * 2 + 9000,
                                                prompt["max_tokens"])
            if budget.allowed_items(self.store.budget_status(), 1, worst * 2) < 1:   # a draft and a possible revision
                self.store.release_tailor_claim(job_id)        # nothing was spent: do not count this as an attempt
                return {"job_id": job_id, "status": "budget_paused", "reason": "not enough budget left for this job"}
            try:
                patch, audit_dict, requirements, spent, notes, attention = self._draft_review_repair(job, prompt, mode, cover, model, settings)
            except _Failure as f:
                if f.penalise:
                    self.store.note_tailor_failure(job_id)
                elif self._job_spend <= 0:
                    self.store.release_tailor_claim(job_id)    # the API failed before anything was billed: not the job's fault
                return {"job_id": job_id, "status": "failed" if f.penalise else "api_error", "reason": f.reason}

        view = tailor.apply_patch(self.resume, patch if variant == "tailored" else None, self.profile)
        basics = self.resume["basics"]
        expect = [basics["name"], basics["email"]]
        rendered = render.render_fit(self.pdf, lambda s: render.resume_html(view, s, settings.get("paper", "a4")), max_pages=settings["max_pages"], expect=expect)
        notes += rendered["notes"]
        if not rendered["verify"]["ok"] and rendered["pages"] <= settings["max_pages"]:
            attention += [f"resume PDF check: {p}" for p in rendered["verify"]["problems"]]
        if rendered["pages"] > settings["max_pages"]:
            attention.append(f"the resume is {rendered['pages']} pages (limit {settings['max_pages']})")

        cover_pdf = None
        cl = (patch or {}).get("cover_letter") if patch else None
        if cover and cl and len(cl["paragraphs"]) >= 2:
            cover_res = render.render_fit(
                self.pdf, lambda s: render.cover_letter_html(basics, job, cl, self.today.strftime("%-d %B %Y"), s, settings.get("paper", "a4")),
                max_pages=1, expect=[basics["name"]])
            cover_pdf = cover_res["pdf"]
            notes += [f"cover letter: {n}" for n in cover_res["notes"]]
            if cover_res["pages"] > 1:
                attention.append("the cover letter runs to more than one page")
        elif cover:
            attention.append("no cover letter could be kept (it was removed by the honesty check or not returned)")

        files_dir = self.out_root / "applications" / f"{job_id}-{slug(job.get('company'))}-{slug(job.get('title'))}"
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "resume.pdf").write_bytes(rendered["pdf"])
        if cover_pdf:
            (files_dir / "cover_letter.pdf").write_bytes(cover_pdf)
        (files_dir / "tailored_resume.json").write_text(json.dumps(view, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "patch.json").write_text(json.dumps(patch, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "audit.json").write_text(json.dumps(audit_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "posting.txt").write_text(f"{job.get('title')}\n{job.get('company')}\n{job.get('location') or ''}\n"
                                               f"{job.get('canonical_url') or ''}\n\n{job.get('description') or ''}\n", encoding="utf-8")
        (files_dir / "summary.md").write_text(_summary_md(job, variant, requirements, patch, self.resume, audit_dict, notes, attention), encoding="utf-8")

        needs = bool(attention)
        status = self.store.save_application(
            job_id, variant, str(files_dir), str(files_dir / "resume.pdf"),
            str(files_dir / "cover_letter.pdf") if cover_pdf else None, patch or {}, requirements, audit_dict, model,
            round(spent, 6), needs, "; ".join(attention) or None, features.effective_approval(settings["approval"], self.telegram_on))
        return {"job_id": job_id, "status": status, "variant": variant, "needs_attention": needs, "attention": attention,
                "cost_usd": round(spent, 6), "files_dir": str(files_dir), "has_cover_letter": bool(cover_pdf),
                "requirements": _coverage_counts(requirements), "notes": notes, "audit": audit_dict, "pages": rendered["pages"]}

    # ------------------------------------------------------------ the model loop

    def _call(self, system: str, user: str, max_tokens: int, model: str, job_id: int, note: str):
        try:
            return self.ai.messages(model=model, system=system, user=user, max_tokens=max_tokens)
        except AnthropicError as ex:
            raise _Failure(str(ex), penalise=not ex.retryable)

    def _try_parse(self, text: str, mode: str, cover: bool):
        """-> (patch | None, problems). A usable patch has passed strict validation and dash cleanup."""
        try:
            raw = scoring.parse_model_json(text)
            patch = tailor.validate_patch(raw, self.resume, cover_letter=cover, mode=mode)
        except scoring.ScoreError as e:
            return None, e.problems
        except tailor.TailorError as e:
            return None, e.problems
        patch, _ = tailor.normalise_dashes(patch)
        return patch, []

    def _log(self, job_id: int, model: str, reply, ok: bool, note: Optional[str]) -> float:
        cost = float(self.store.record_usage("anthropic", "tailor", model, job_id, budget.normalise_usage(reply.usage), ok, note) or 0)
        self._job_spend += cost
        return cost

    def _draft_review_repair(self, job, prompt, mode, cover, model, settings):
        spent = 0.0
        notes: list[str] = []
        attention: list[str] = []
        job_id = job["id"]

        r1 = self._call(prompt["system"], prompt["user"], prompt["max_tokens"], model, job_id, "draft")
        patch, problems = self._try_parse(r1.text, mode, cover)
        spent += self._log(job_id, model, r1, patch is not None, None if patch else "unusable draft")
        a1 = tailor.audit(self.resume, patch, job, self.profile) if patch else None

        if problems or (a1 and a1.hard):
            hard = [{"where": "format", "detail": p} for p in problems] + (a1.hard if a1 else [])
            soft = [v for v in (a1.soft if a1 else []) if v["kind"] == "style"]
            user2 = prompt["user"] + "\n\n---\n" + tailor.revision_user(r1.text, hard, soft)
            r2 = self._call(prompt["system"], user2, prompt["max_tokens"], model, job_id, "revision")
            patch, problems = self._try_parse(r2.text, mode, cover)
            spent += self._log(job_id, model, r2, patch is not None, "revision" if patch else "unusable revision")
            if patch is None:
                raise _Failure("the model's answer was not valid after a revision: " + "; ".join(problems[:3]))
            notes.append("one revision was needed to fix the first draft")
            final = tailor.audit(self.resume, patch, job, self.profile)
        else:
            if patch is None:  # defensive: cannot happen (problems would be set)
                raise _Failure("no usable answer")
            final = a1

        rejected: list[dict] = []
        if final.hard:
            rejected += [{"where": v["where"], "detail": v["detail"], "check": "audit"} for v in final.hard]
            patch, repair_notes = tailor.repair(patch, final)
            notes += repair_notes
            attention.append("unsupported content was removed after the honesty check: " + "; ".join(repair_notes))
            final = tailor.audit(self.resume, patch, job, self.profile)
            if final.hard:  # cannot happen if repair is correct; never let it through
                raise _Failure("content the resume does not support remained after repair")

        # Second layer: a cheap model checks the CLAIMS against the resume (the audit above only checks words).
        if settings.get("verify", True):
            patch, final, extra_spent, v_rejected, v_notes, v_attention = self._fact_check(job, patch, final, settings)
            spent += extra_spent
            rejected += v_rejected
            notes += v_notes
            attention += v_attention

        audit_dict = final.to_dict()
        audit_dict["rejected"] = rejected
        audit_dict["repairs"] = [n for n in notes if n.startswith(("kept the original", "removed", "reset"))]
        return patch, audit_dict, patch.get("requirements") or [], spent, notes, attention

    def _fact_check(self, job: dict, patch: dict, final, settings: dict):
        """One cheap model call that flags exaggerated or unsupported claims; flagged text is removed.
        If the check cannot run, the application is flagged for a human instead of passing unchecked."""
        job_id = job["id"]
        req = tailor.verify_prompt(self.resume, patch)
        if req is None:
            return patch, final, 0.0, [], [], []
        vmodel = settings.get("verify_model") or "claude-haiku-5-5"
        worst = budget.worst_case_item_cost(self.store.pricing(vmodel), len(req["system"]) + len(req["user"]), req["max_tokens"])
        if budget.allowed_items(self.store.budget_status(), 1, worst) < 1:
            return patch, final, 0.0, [], ["fact check skipped (budget)"], ["the fact check could not run (spending cap): please read the text carefully"]
        try:
            reply = self.ai.messages(model=vmodel, system=req["system"], user=req["user"], max_tokens=req["max_tokens"])
        except AnthropicError as ex:
            return patch, final, 0.0, [], [f"fact check unavailable: {ex}"], ["the fact check could not run: please read the text carefully"]
        try:
            flags = tailor.parse_verification(scoring.parse_model_json(reply.text), patch)
            ok = True
        except (scoring.ScoreError, tailor.TailorError):
            flags, ok = [], False
        spent = float(self.store.record_usage("anthropic", "tailor_verify", vmodel, job_id, budget.normalise_usage(reply.usage),
                                              ok, None if ok else "unusable fact-check reply") or 0)
        if not ok:
            return patch, final, spent, [], ["fact check reply was unusable"], ["the fact check gave an unusable answer: please read the text carefully"]
        hard_flags = [f for f in flags if f["kind"] == "claim"]
        stretches = [f for f in flags if f["kind"] == "stretch"]
        if stretches:   # a stretch is kept but shown to the human; it never blocks or deletes anything
            final.soft.extend(stretches)
        if not hard_flags:
            return patch, final, spent, [], ["fact check passed" if not stretches else "fact check found wording to double-check"], []
        flags = hard_flags
        probe = tailor.Audit(hard=flags)
        patch2, repair_notes = tailor.repair(patch, probe)
        final2 = tailor.audit(self.resume, patch2, job, self.profile)
        final2.soft.extend(stretches)
        if final2.hard:
            raise _Failure("content the resume does not support remained after the fact check")
        rejected = [{"where": v["where"], "detail": v["detail"], "check": "fact check"} for v in flags]
        return (patch2, final2, spent, rejected, repair_notes,
                ["the fact check removed claims the resume does not support: " + "; ".join(repair_notes)])


# ---------------------------------------------------------------- reporting

def _coverage_counts(requirements: list) -> dict:
    c = {"matched": 0, "bridged": 0, "gap": 0}
    for r in requirements:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def _summary_md(job, variant, requirements, patch, resume, audit_dict, notes, attention) -> str:
    idx = tailor.resume_index(resume)
    lines = [f"# {job.get('title')} at {job.get('company')}", "",
             f"- Score: {job.get('score')} ({job.get('verdict')})", f"- Variant: {variant}",
             f"- Link: {job.get('canonical_url') or ''}", ""]
    if attention:
        lines += ["## Needs your attention", ""] + [f"- {a}" for a in attention] + [""]
    if requirements:
        lines += ["## Requirement coverage", ""]
        for r in requirements:
            ev = f" (evidence: {r['evidence']})" if r.get("evidence") else ""
            lines.append(f"- **{r['status']}**: {r['requirement']}{ev}" + (f" ({r['note']})" if r.get("note") else ""))
        lines.append("")
    if patch and patch.get("work"):
        lines += ["## Changes to your resume", ""]
        for w in patch["work"]:
            for bid, new in (w.get("rewrite") or {}).items():
                lines += [f"- reworded {bid}:", f"  - was: {idx[bid]['text']}", f"  - now: {new}"]
            if w.get("order"):
                lines.append(f"- {w['id']}: showing {len(w['order'])} bullet(s), in this order: {', '.join(w['order'])}")
        lines.append("")
    if audit_dict.get("rejected"):
        lines += ["## Removed by the honesty checks", ""] + [f"- {r['where']} ({r['check']}): {r['detail']}" for r in audit_dict["rejected"]] + [""]
    if audit_dict.get("soft"):
        lines += ["## Worth checking", ""] + [f"- {v['where']}: {v['detail']}" for v in audit_dict["soft"]] + [""]
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


def review_message(result: dict, job: dict) -> str:
    """The Telegram text that accompanies the PDFs."""
    c = result.get("requirements") or {}
    lines = [f"\U0001F4DD Application ready for review: {job.get('title')} @ {job.get('company')}",
             f"Score {job.get('score')} ({job.get('verdict')}), {result['variant']} resume"
             + (", cover letter attached" if result.get("has_cover_letter") else "")]
    if c:
        lines.append(f"Requirements: {c.get('matched', 0)} matched, {c.get('bridged', 0)} bridged, {c.get('gap', 0)} gaps")
    for a in result.get("attention") or []:
        lines.append(f"⚠ {a}")
    rejected = (result.get("audit") or {}).get("rejected") or []
    if rejected:
        lines.append(f"\U0001F6E1 {len(rejected)} unsupported claim(s) removed by the honesty checks (details in summary.md)")
    soft = (result.get("audit") or {}).get("soft") or []
    if soft:
        lines.append(f"⚠ {len(soft)} thing(s) to double-check (see summary): " + "; ".join(v["detail"] for v in soft[:2]))
    if job.get("canonical_url"):
        lines.append(job["canonical_url"])
    lines += ["", f"Reply /approve {job['id']} or /skip {job['id']}   (cost ${result.get('cost_usd', 0):.3f})"]
    return "\n".join(lines)
