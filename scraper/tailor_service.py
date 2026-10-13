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
import company_notes as CN
import features
import render
import scoring
import tailor
import tailor_v2 as V2
from anthropic_client import AnthropicError

MAX_FILE_NAME = 40
CUT_BELOW_SCALE = 0.92       # v2: a layout that needs to shrink below this first loses its least relevant older bullets
MAX_CUTS = 3


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
                 telegram_on: bool = True, proof_points: Optional[list] = None, company_fetch=None):
        self.store, self.ai, self.pdf = store, ai, pdf
        self.proof_points = proof_points or []          # config/proof_points.json, validated (tailor_v2); used by the v2 engine only
        self.company_fetch = company_fetch or CN.fetch_page
        self.telegram_on = telegram_on       # with no way to reach the user, `review` falls back to `auto`
        self._job_spend = 0.0                # model cost of the job being processed, dollars (decides refunds)
        self._purpose = "tailor"             # the usage purpose the current run is logged under ("tailor_compare" for a comparison)
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

    def run_job(self, job: dict, settings: Optional[dict] = None, *, track: bool = True, dry_dir: Optional[Path] = None,
                purpose: str = "tailor") -> dict:
        """Tailor one job. `track=False` (a run the user asked for) leaves the attempt counters alone: it never claimed the job,
        so it must not hand back or add a claim that belongs to the schedule.

        `dry_dir` makes it a comparison run (`compare_job`): the files go to that folder and NOTHING else is written: no application
        is saved, no status changes, no claim is touched, and the Telegram message is not this method's business. The model is
        paid for as always (budget guard and caps included), under the usage purpose `purpose`."""
        settings = settings or self.store.tailor_settings()
        engine, gap_policy = tailor.engine_options(settings)
        opts = tailor.style_options(settings)
        proof_points = self.proof_points if engine == "v2" else None
        ctx = {"engine": engine, "opts": opts, "proof_points": proof_points, "company_facts": None}
        if dry_dir is not None:
            track = False
        self._purpose = purpose
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
            def make_prompt(facts=None):
                return tailor.prepare(self.resume, self.profile, job, cover_letter=cover, mode=mode,
                                      notes={"strengths": job.get("strengths") or [], "gaps": job.get("gaps") or []},
                                      today=self.today, style_notes=settings.get("style_notes"), engine=engine, gap_policy=gap_policy,
                                      proof_points=proof_points, cover_style=opts["cover_style"], tone=opts["tone"], company_facts=facts)
            prompt = make_prompt()
            worst = budget.worst_case_item_cost(self.store.pricing(model), len(prompt["system"]) + len(prompt["user"]) * 2 + 9000,
                                                prompt["max_tokens"])
            if budget.allowed_items(self.store.budget_status(), 1, worst * 2) < 1:   # a draft and a possible revision
                if track:
                    self.store.release_tailor_claim(job_id)    # nothing was spent: do not count this as an attempt
                return {"job_id": job_id, "status": "budget_paused", "reason": "not enough budget left for this job"}
            company_spent, company_notes_ = 0.0, []
            if engine == "v2" and opts["company_research"] and cover:
                facts, company_spent, company_notes_ = self._company_facts(job, settings)
                if facts:
                    ctx["company_facts"] = facts
                    prompt = make_prompt(facts)
            try:
                patch, audit_dict, requirements, spent, notes, attention = self._draft_review_repair(job, prompt, mode, cover, model, settings, ctx)
                spent += company_spent
                notes = company_notes_ + notes
            except _Failure as f:
                if track and f.penalise:
                    self.store.note_tailor_failure(job_id)
                elif track and self._job_spend <= 0:
                    self.store.release_tailor_claim(job_id)    # the API failed before anything was billed: not the job's fault
                # what was billed before the failure is reported too: a failed run that cost money must not look free
                return {"job_id": job_id, "status": "failed" if f.penalise else "api_error", "reason": f.reason,
                        "cost_usd": round(self._job_spend, 6)}

        view = tailor.apply_patch(self.resume, patch if variant == "tailored" else None, self.profile)
        basics = self.resume["basics"]
        expect = [basics["name"], basics["email"]]
        rendered = render.render_fit(self.pdf, lambda s: render.resume_html(view, s, settings.get("paper", "a4")), max_pages=settings["max_pages"], expect=expect)
        notes += rendered["notes"]
        if engine == "v2" and variant == "tailored" and patch and rendered["pages"] <= settings["max_pages"] and rendered["scale"] < CUT_BELOW_SCALE:
            # shrinking the type is the last resort: first take the least relevant bullets out of the older roles
            current, cut_ids, kept = patch, [], rendered
            for _ in range(MAX_CUTS):
                candidate, bid = tailor.cut_lowest(self.resume, current, job)
                if bid is None:
                    break
                view_c = tailor.apply_patch(self.resume, candidate, self.profile)
                kept = render.render_fit(self.pdf, lambda s_, v=view_c: render.resume_html(v, s_, settings.get("paper", "a4")),
                                         max_pages=settings["max_pages"], expect=expect)
                current, view, cut_ids = candidate, view_c, cut_ids + [bid]
                if kept["scale"] >= CUT_BELOW_SCALE:
                    break
            if cut_ids:
                patch, rendered = current, kept
                notes.append(f"left out {len(cut_ids)} less relevant bullet(s) from older roles ({', '.join(cut_ids)}) instead of shrinking the layout further")
                notes += [n for n in kept["notes"] if n not in notes]
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

        files_dir = dry_dir if dry_dir is not None else self.out_root / "applications" / f"{job_id}-{slug(job.get('company'))}-{slug(job.get('title'))}"
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "resume.pdf").write_bytes(rendered["pdf"])
        if cover_pdf:
            (files_dir / "cover_letter.pdf").write_bytes(cover_pdf)
        else:
            (files_dir / "cover_letter.pdf").unlink(missing_ok=True)     # no letter this time: an old one must not be sent by mistake
        (files_dir / "tailored_resume.json").write_text(json.dumps(view, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "patch.json").write_text(json.dumps(patch, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "audit.json").write_text(json.dumps(audit_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        (files_dir / "posting.txt").write_text(f"{job.get('title')}\n{job.get('company')}\n{job.get('location') or ''}\n"
                                               f"{job.get('canonical_url') or ''}\n\n{job.get('description') or ''}\n", encoding="utf-8")
        (files_dir / "summary.md").write_text(_summary_md(job, variant, requirements, patch, self.resume, audit_dict, notes, attention, engine), encoding="utf-8")

        needs = bool(attention)
        if dry_dir is not None:      # a comparison: the files are the whole result
            return {"job_id": job_id, "status": "compared", "engine": engine, "variant": variant, "needs_attention": needs,
                    "attention": attention, "cost_usd": round(spent, 6), "files_dir": str(files_dir),
                    "has_cover_letter": bool(cover_pdf), "requirements": _coverage_counts(requirements), "notes": notes,
                    "audit": audit_dict, "pages": rendered["pages"], "patch": patch, "requirement_list": requirements}
        status = self.store.save_application(
            job_id, variant, str(files_dir), str(files_dir / "resume.pdf"),
            str(files_dir / "cover_letter.pdf") if cover_pdf else None, patch or {}, requirements, audit_dict, model,
            round(spent, 6), needs, "; ".join(attention) or None, features.effective_approval(settings["approval"], self.telegram_on))
        return {"job_id": job_id, "status": status, "variant": variant, "needs_attention": needs, "attention": attention,
                "cost_usd": round(spent, 6), "files_dir": str(files_dir), "has_cover_letter": bool(cover_pdf),
                "requirements": _coverage_counts(requirements), "notes": notes, "audit": audit_dict, "pages": rendered["pages"]}

    # ------------------------------------------------------------ comparing engines

    def compare_job(self, job: dict, engines=("v1", "v2"), settings: Optional[dict] = None) -> dict:
        """Tailor one job with each engine into output/compare/<job>/<engine>/ and write compare.md next to them. Nothing else is
        written: no application, no status, no claim, no message. The model is paid for (twice for two engines) under the usage
        purpose `tailor_compare`, inside the same budget guard and caps. Returns the per-engine results and the folder."""
        engines = list(dict.fromkeys(engines))
        if not engines or any(e not in tailor.ENGINES for e in engines):
            raise ValueError(f"engines must be some of {', '.join(tailor.ENGINES)}")
        base = settings or self.store.tailor_settings()
        job_id = job["id"]
        root = self.out_root / "compare" / f"{job_id}-{slug(job.get('company'))}-{slug(job.get('title'))}"
        runs: dict = {}
        for engine in engines:
            try:
                res = self.run_job(job, dict(base, engine=engine), dry_dir=root / engine, purpose="tailor_compare")
            except Exception as ex:      # a render or disk failure after the model was paid: keep what was learned and what it cost
                res = {"job_id": job_id, "status": "failed", "reason": f"{type(ex).__name__}: {ex}"[:300],
                       "cost_usd": round(self._job_spend, 6)}
            runs[engine] = res
            if res.get("status") != "compared":
                break                   # a budget pause or a failure: the next engine would fare the same, and must not spend more
        wrote = None
        if any(r.get("status") == "compared" for r in runs.values()):
            root.mkdir(parents=True, exist_ok=True)
            (root / "compare.md").write_text(_compare_md(job, runs, self.resume), encoding="utf-8")
            wrote = str(root / "compare.md")
        slim = {e: {k: v for k, v in r.items() if k not in ("patch", "requirement_list")} for e, r in runs.items()}
        return {"job_id": job_id, "folder": str(root), "compare_md": wrote, "runs": slim,
                "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in runs.values()), 6)}

    # ------------------------------------------------------------ the model loop

    def _call(self, system: str, user: str, max_tokens: int, model: str, job_id: int, note: str):
        try:
            return self.ai.messages(model=model, system=system, user=user, max_tokens=max_tokens)
        except AnthropicError as ex:
            raise _Failure(str(ex), penalise=not ex.retryable)

    def _try_parse(self, text: str, mode: str, cover: bool, cover_style: str = "classic"):
        """-> (patch | None, problems). A usable patch has passed strict validation and dash cleanup."""
        try:
            raw = scoring.parse_model_json(text)
            patch = tailor.validate_patch(raw, self.resume, cover_letter=cover, mode=mode, cover_style=cover_style)
        except scoring.ScoreError as e:
            return None, e.problems
        except tailor.TailorError as e:
            return None, e.problems
        patch, _ = tailor.normalise_dashes(patch)
        return patch, []

    def _log(self, job_id: int, model: str, reply, ok: bool, note: Optional[str]) -> float:
        cost = float(self.store.record_usage("anthropic", self._purpose, model, job_id, budget.normalise_usage(reply.usage), ok, note) or 0)
        self._job_spend += cost
        return cost

    def _draft_review_repair(self, job, prompt, mode, cover, model, settings, ctx):
        spent = 0.0
        notes: list[str] = []
        attention: list[str] = []
        job_id = job["id"]
        engine, opts = ctx["engine"], ctx["opts"]
        style = opts["cover_style"] if engine == "v2" else "classic"
        audit_args = {"engine": engine, "proof_points": ctx["proof_points"], "company_facts": ctx["company_facts"]}

        r1 = self._call(prompt["system"], prompt["user"], prompt["max_tokens"], model, job_id, "draft")
        patch, problems = self._try_parse(r1.text, mode, cover, style)
        spent += self._log(job_id, model, r1, patch is not None, None if patch else "unusable draft")
        a1 = tailor.audit(self.resume, patch, job, self.profile, **audit_args) if patch else None

        if problems or (a1 and a1.hard):
            hard = [{"where": "format", "detail": p} for p in problems] + (a1.hard if a1 else [])
            soft = [v for v in (a1.soft if a1 else []) if v["kind"] == "style"]
            user2 = prompt["user"] + "\n\n---\n" + tailor.revision_user(r1.text, hard, soft)
            r2 = self._call(prompt["system"], user2, prompt["max_tokens"], model, job_id, "revision")
            patch, problems = self._try_parse(r2.text, mode, cover, style)
            spent += self._log(job_id, model, r2, patch is not None, "revision" if patch else "unusable revision")
            if patch is None:
                raise _Failure("the model's answer was not valid after a revision: " + "; ".join(problems[:3]))
            notes.append("one revision was needed to fix the first draft")
            final = tailor.audit(self.resume, patch, job, self.profile, **audit_args)
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
            final = tailor.audit(self.resume, patch, job, self.profile, **audit_args)
            if final.hard:  # cannot happen if repair is correct; never let it through
                raise _Failure("content the resume does not support remained after repair")

        extras: dict = {}
        if engine == "v2" and opts["reviewer"] and (mode == "full" or cover):
            patch, final, r_spent, r_notes, review = self._review(job, patch, final, settings, ctx, audit_args, mode)
            spent += r_spent
            notes += r_notes
            extras["review"] = review
        if engine == "v2" and mode == "full":
            report = tailor.keyword_report(self.resume, patch, job)
            patch, added = tailor.emphasise_low_keywords(patch, report)
            if added:
                notes.append("emphasised skills the posting asks for that the resume already has: " + ", ".join(added))
            extras["keywords"] = report

        # Second layer: a cheap model checks the CLAIMS against the resume (the audit above only checks words).
        if settings.get("verify", True):
            patch, final, extra_spent, v_rejected, v_notes, v_attention = self._fact_check(job, patch, final, settings, ctx, audit_args)
            spent += extra_spent
            rejected += v_rejected
            notes += v_notes
            attention += v_attention

        audit_dict = final.to_dict()
        audit_dict["rejected"] = rejected
        audit_dict["repairs"] = [n for n in notes if n.startswith(("kept the original", "removed", "reset"))]
        audit_dict.update(extras)
        return patch, audit_dict, patch.get("requirements") or [], spent, notes, attention

    def _shown_bullets(self, patch: dict) -> list:
        """[(bullet id, text)] of the work bullets the tailored resume shows (the reviewer reads these)."""
        idx = tailor.resume_index(self.resume)
        specs = {w["id"]: w for w in patch.get("work", []) or []}
        out = []
        for role in self.resume.get("work", []) or []:
            spec = specs.get(role["id"], {})
            if spec.get("show") is False:
                continue
            for bid in (spec.get("order") or [h["id"] for h in role["highlights"]]):
                if bid in idx:
                    out.append((bid, (spec.get("rewrite") or {}).get(bid, idx[bid]["text"])))
        return out[:14]

    def _review(self, job: dict, patch: dict, final, settings: dict, ctx: dict, audit_args: dict, mode: str = "full"):
        """One cheap model call that reads the draft the way a hiring manager would and proposes edits. THE CODE decides: each edit is
        applied only if the result still passes the whole audit, so the reviewer can rephrase and nothing else. Never raises: a reviewer
        that cannot run changes nothing. -> (patch, audit, spent, notes, review dict)"""
        job_id = job["id"]
        review = {"applied": [], "dropped": [], "missed_keywords": []}
        cl = patch.get("cover_letter")
        full = mode == "full"       # a resume used as written has nothing to edit: the reviewer sees and may change only the letter
        req = V2.review_prompt(job, patch.get("headline") if full else None, patch.get("summary") if full else None,
                               self._shown_bullets(patch) if full else [], list(cl["paragraphs"]) if cl else [])
        rmodel = ctx["opts"]["reviewer_model"]
        worst = budget.worst_case_item_cost(self.store.pricing(rmodel), len(req["system"]) + len(req["user"]), req["max_tokens"])
        if budget.allowed_items(self.store.budget_status(), 1, worst) < 1:
            return patch, final, 0.0, ["reviewer skipped (budget)"], review
        try:
            reply = self.ai.messages(model=rmodel, system=req["system"], user=req["user"], max_tokens=req["max_tokens"])
        except AnthropicError as ex:
            return patch, final, 0.0, [f"reviewer unavailable: {ex}"], review
        try:
            edits, missed = V2.parse_review(scoring.parse_model_json(reply.text))
            ok = True
        except (scoring.ScoreError, tailor.TailorError):
            edits, missed, ok = [], [], False
        spent = float(self.store.record_usage("anthropic", self._purpose + "_review", rmodel, job_id, budget.normalise_usage(reply.usage),
                                              ok, None if ok else "unusable reviewer reply") or 0)
        self._job_spend += spent
        if not ok:
            return patch, final, spent, ["the reviewer's reply was unusable: nothing changed"], review
        review["missed_keywords"] = missed
        new_patch, applied, dropped = tailor.apply_review(self.resume, patch, job, self.profile, edits, engine="v2", proof_points=ctx["proof_points"],
                                                          company_facts=ctx["company_facts"], allow_bullets=full)
        review["applied"], review["dropped"] = applied, dropped
        notes = []
        if applied:
            final2 = tailor.audit(self.resume, new_patch, job, self.profile, **audit_args)
            if final2.hard:                       # cannot happen (every edit was audited); never let it through
                review["dropped"] += [dict(e, why="the result failed the audit") for e in applied]
                review["applied"] = []
                return patch, final, spent, ["the reviewer's edits were set aside (they failed the audit)"], review
            patch, final = new_patch, final2
            notes.append(f"the reviewer's {len(applied)} edit(s) were applied" + (f" ({len(dropped)} set aside)" if dropped else ""))
        else:
            notes.append("the reviewer had no edit that passed the audit" if edits else "the reviewer had no edits")
        return patch, final, spent, notes, review

    def _company_facts(self, job: dict, settings: dict):
        """(facts, spent, notes): up to three plain facts from the employer's OWN website, cached for 30 days. Never raises, never
        blocks tailoring: any problem means no company angle. The website comes from the job's own links only."""
        get = getattr(self.store, "company_notes_get", None)
        save = getattr(self.store, "company_notes_save", None)
        if get is None or save is None:
            return [], 0.0, []
        key = CN.company_key(job.get("company"))
        if not key:
            return [], 0.0, []
        try:
            cached = get(key, CN.FRESH_DAYS)
            if cached is not None:
                return list(cached.get("facts") or []), 0.0, ["company note: from the cache"] if cached.get("facts") else []
            links = list(getattr(self.store, "job_links", lambda _id: [])(job["id"]) or []) + [job.get("canonical_url")]
            domain = CN.own_domain(job.get("company"), links)
            if not domain:
                return [], 0.0, ["no company website among the job's own links, so no company angle"]
            text = self.company_fetch(domain)
            if not text:
                save(key, [], f"https://{domain}/")
                return [], 0.0, [f"could not read {domain}, so no company angle"]
            req = CN.extract_prompt(job.get("company") or "", text)
            vmodel = settings.get("verify_model") or "claude-haiku-5-5"
            worst = budget.worst_case_item_cost(self.store.pricing(vmodel), len(req["system"]) + len(req["user"]), req["max_tokens"])
            if budget.allowed_items(self.store.budget_status(), 1, worst) < 1:
                return [], 0.0, ["company note skipped (budget)"]
            try:
                reply = self.ai.messages(model=vmodel, system=req["system"], user=req["user"], max_tokens=req["max_tokens"])
            except AnthropicError as ex:
                return [], 0.0, [f"company note unavailable: {ex}"]
            try:
                facts = CN.parse_facts(scoring.parse_model_json(reply.text), text)
                ok = True
            except (scoring.ScoreError, tailor.TailorError):
                facts, ok = [], False
            spent = float(self.store.record_usage("anthropic", self._purpose + "_company", vmodel, job["id"], budget.normalise_usage(reply.usage),
                                                  ok, None if ok else "unusable company-note reply") or 0)
            self._job_spend += spent
            if ok:
                save(key, facts, f"https://{domain}/")
            return facts, spent, [f"company note: {len(facts)} fact(s) from {domain}" if facts else f"company note: nothing usable on {domain}"]
        except Exception as ex:               # a company note is a nicety: whatever went wrong, tailoring goes on without it
            return [], 0.0, [f"company note failed: {type(ex).__name__}"]

    def _fact_check(self, job: dict, patch: dict, final, settings: dict, ctx: dict, audit_args: dict):
        """One cheap model call that flags exaggerated or unsupported claims; flagged text is removed.
        If the check cannot run, the application is flagged for a human instead of passing unchecked."""
        job_id = job["id"]
        req = tailor.verify_prompt(self.resume, patch, V2.points_prompt(ctx["proof_points"]) if ctx["proof_points"] else "")
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
        spent = float(self.store.record_usage("anthropic", self._purpose + "_verify", vmodel, job_id, budget.normalise_usage(reply.usage),
                                              ok, None if ok else "unusable fact-check reply") or 0)
        self._job_spend += spent
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
        final2 = tailor.audit(self.resume, patch2, job, self.profile, **audit_args)
        final2.soft.extend(stretches)
        if final2.hard:
            raise _Failure("content the resume does not support remained after the fact check")
        rejected = [{"where": v["where"], "detail": v["detail"], "check": "fact check"} for v in flags]
        return (patch2, final2, spent, rejected, repair_notes,
                ["the fact check removed claims the resume does not support: " + "; ".join(repair_notes)])


# ---------------------------------------------------------------- reporting

def _letter_text(patch: Optional[dict]) -> str:
    cl = (patch or {}).get("cover_letter")
    if not cl:
        return "(no cover letter)"
    return "\n\n".join([cl.get("greeting", "")] + list(cl.get("paragraphs", [])) + [cl.get("closing", "")])


def _letter_words(patch: Optional[dict]) -> int:
    cl = (patch or {}).get("cover_letter")
    return len(" ".join(list(cl.get("paragraphs", []))).split()) if cl else 0


def _fenced(text: str) -> list:
    """A code block that the text inside it cannot close: the fence is longer than the longest run of backticks in the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence, text, fence]


def _compare_md(job: dict, runs: dict, resume: dict) -> str:
    """The side-by-side the user reads to decide between engines. Plain markdown, one section per thing worth comparing."""
    idx = tailor.resume_index(resume)
    done = {e: r for e, r in runs.items() if r.get("status") == "compared"}
    engines = list(done)
    rule = "|" + "---|" * (len(engines) + 1)
    lines = [f"# Compare: {job.get('title')} at {job.get('company')} (job {job.get('id')})", "",
             f"- Score: {job.get('score') if job.get('score') is not None else 'not scored'} ({job.get('verdict') or 'no verdict'})",
             "- Nothing here was saved as an application: these are test runs of the same job with different engines.",
             f"- Engines in this run: {', '.join(runs)} (a folder of another engine next to this file is from an earlier run).", ""]
    variants = {r.get("variant") for r in done.values()}
    if variants == {"base"}:
        lines += ["- This job scores at or above your `max_score`, so your base resume is used as written and only a cover letter is written: "
                  "the resume part will be the same in every engine.", ""]
    missing = [e for e, r in runs.items() if r.get("status") != "compared"]
    if missing:
        lines += [f"- {e} did not finish: {runs[e].get('status')}: {runs[e].get('reason')}" for e in missing] + [""]

    def row(label, fn):
        return "| " + label + " | " + " | ".join(str(fn(done[e])) for e in engines) + " |"

    lines += ["## At a glance", "", "| | " + " | ".join(engines) + " |", rule,
              row("cost", lambda r: f"${r['cost_usd']:.3f}"),
              row("requirements", lambda r: "{matched} matched / {bridged} bridged / {gap} gaps".format(**{"matched": 0, "bridged": 0, "gap": 0, **(r["requirements"] or {})})),
              row("cover letter words", lambda r: _letter_words(r.get("patch"))),
              row("rewritten bullets", lambda r: sum(len((w.get("rewrite") or {})) for w in (r.get("patch") or {}).get("work", []) or [])),
              row("removed by the honesty checks", lambda r: len((r.get("audit") or {}).get("rejected") or [])),
              row("things to double-check", lambda r: len((r.get("audit") or {}).get("soft") or [])),
              row("reviewer edits applied / set aside", lambda r: "{} / {}".format(len(((r.get("audit") or {}).get("review") or {}).get("applied") or []),
                                                                                    len(((r.get("audit") or {}).get("review") or {}).get("dropped") or []))),
              row("needs attention", lambda r: "yes" if r.get("needs_attention") else "no"), ""]
    lines += ["## Cover letter", ""]
    for e in engines:
        lines += [f"### {e}", ""] + _fenced(_letter_text(done[e].get("patch"))) + [""]
    lines += ["## Headline and summary", ""]
    for e in engines:
        pt = done[e].get("patch") or {}
        lines += [f"- **{e}**: headline: {pt.get('headline') or '(unchanged)'}; summary: {pt.get('summary') or '(none)'}"]
    lines += ["", "## Reworded bullets", ""]
    for e in engines:
        lines.append(f"### {e}")
        rewrote = False
        for w in (done[e].get("patch") or {}).get("work", []) or []:
            for bid, new in (w.get("rewrite") or {}).items():
                rewrote = True
                lines += [f"- {bid}:", f"  - was: {idx[bid]['text']}", f"  - now: {new}"]
        lines += ([] if rewrote else ["- (none)"]) + [""]
    lines += ["## Requirement coverage (private notes)", ""]
    for e in engines:
        lines.append(f"### {e}")
        for r in done[e].get("requirement_list") or []:
            lines.append(f"- **{r['status']}**: {r['requirement']}" + (f" ({r['note']})" if r.get("note") else ""))
        lines.append("")
    lines += ["## Keyword coverage", ""]
    for e in engines:
        kw = (done[e].get("audit") or {}).get("keywords")
        lines.append(f"- **{e}**: " + (f"said: {', '.join(kw['shown']) or '(none)'}; low (now emphasised): {', '.join(kw['low']) or '(none)'}; "
                                       f"missing from the resume: {', '.join(kw['missing']) or '(none)'}" if kw else "(only the v2 engine reports this)"))
    lines += ["", "## What the checks found", ""]
    for e in engines:
        lines.append(f"### {e}")
        a = done[e].get("audit") or {}
        items = [f"- removed ({v['check']}): {v['where']}: {v['detail']}" for v in a.get("rejected") or []] + \
                [f"- check: {v['where']}: {v['detail']}" for v in a.get("soft") or []] + \
                [f"- note: {n}" for n in done[e].get("notes") or []] + [f"- attention: {x}" for x in done[e].get("attention") or []]
        lines += (items or ["- nothing"]) + [""]
    lines += ["Each folder next to this file holds the PDFs and the summary of that engine."]
    return "\n".join(lines) + "\n"


def _coverage_counts(requirements: list) -> dict:
    c = {"matched": 0, "bridged": 0, "gap": 0}
    for r in requirements:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def _summary_md(job, variant, requirements, patch, resume, audit_dict, notes, attention, engine: str = "v1") -> str:
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
    if engine == "v2" and requirements:
        # v2 keeps gaps out of the letter and the summary: they are for you (and for an interview), never for the employer
        private = [r for r in requirements if r["status"] in ("gap", "bridged")]
        if private:
            lines += ["## Gaps and bridges (private notes: none of this is in the letter or the summary)", ""]
            for r in private:
                what = "gap" if r["status"] == "gap" else "closest real work"
                lines.append(f"- **{what}**: {r['requirement']}" + (f" ({r['note']})" if r.get("note") else ""))
            lines.append("")
    if patch and patch.get("work"):
        lines += ["## Changes to your resume", ""]
        for w in patch["work"]:
            for bid, new in (w.get("rewrite") or {}).items():
                lines += [f"- reworded {bid}:", f"  - was: {idx[bid]['text']}", f"  - now: {new}"]
            if w.get("order"):
                lines.append(f"- {w['id']}: showing {len(w['order'])} bullet(s), in this order: {', '.join(w['order'])}")
        lines.append("")
    kw = audit_dict.get("keywords")
    if kw:
        lines += ["## Keyword coverage (the posting's terms)", "",
                  f"- in your resume and said in the tailored text: {', '.join(kw.get('shown') or []) or '(none)'}",
                  f"- in your resume, but only in a list or a hidden bullet (now emphasised in the skills): {', '.join(kw.get('low') or []) or '(none)'}",
                  f"- technical words in the posting that your resume does not have (gaps: never stuffed in): {', '.join(kw.get('missing') or []) or '(none)'}", ""]
    rv = audit_dict.get("review")
    if rv and (rv.get("applied") or rv.get("dropped")):
        lines += ["## Reviewer", ""]
        for e in rv.get("applied") or []:
            lines += [f"- applied ({e['where']}): {e['reason']}", f"  - was: {e['old']}", f"  - now: {e['new']}"]
        for e in rv.get("dropped") or []:
            lines.append(f"- set aside ({e['where']}): {e['why']}")
        if rv.get("missed_keywords"):
            lines.append(f"- keywords the reviewer thought were missing: {', '.join(rv['missed_keywords'])}")
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
