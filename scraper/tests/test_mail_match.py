"""Which application is an email about? (Phase 6, step 6c.) Evidence only: a wrong match files a rejection against the wrong job."""
from datetime import datetime, timezone

import pytest

import gmail_client as G
import mail_match as M

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
VERIFIED = {"trusted": True, "dmarc": "pass", "spf": "pass", "dkim": "pass", "header_from": ""}


def msg(*, sender="Priya <priya@acme.com>", subject="Hello", text="", name=None, auth=None, id="m1"):
    from email.utils import parseaddr
    n, address = parseaddr(sender)
    domain = address.split("@", 1)[1].lower() if "@" in address else ""
    return G.Message(id=id, thread_id=id, received_at=NOW, from_name=name if name is not None else n, from_address=address.lower(),
                     from_domain=domain, subject=subject, text=text, auth=auth or {**VERIFIED, "header_from": domain})


def cand(job_id, company, title="Backend Engineer", job_status="applied", app="applied"):
    return M.Candidate(job_id, company, title, job_status, app)


# ---------------------------------------------------------------- names

@pytest.mark.parametrize("company,words", [
    ("Acme Corp", [("acme",)]), ("Acme Pvt. Ltd.", [("acme",)]), ("ACME, Inc.", [("acme",)]), ("The Acme Company", [("acme",)]),
    ("Acme Technologies Pvt Ltd", [("acme", "technologies"), ("acme",)]), ("Tata Consultancy Services", [("tata", "consultancy", "services"), ("tata", "consultancy")]),
    ("Johnson & Johnson", [("johnson", "and", "johnson")]), ("Mercedes-Benz AG", [("mercedes", "benz")]),
    ("Яндекс", [("яндекс",)]), ("腾讯", [("腾讯",)]), ("Ａｃｍｅ Corp", [("acme",)]),
    ("HP", [("hp",)]), ("3M", [("3m",)])])
def test_company_names_become_the_forms_they_can_be_written_as(company, words):
    assert [f.words for f in M.name_forms(company)] == words


@pytest.mark.parametrize("company", ["?", "??", "-", "—", "...", "", "   ", None, 5, [], "Confidential", "Company Confidential", "Stealth", "Stealth Startup", "N/A",
                                    "Unknown", "Various", "TBD", "Our Client", "Undisclosed", "Freelance", "Hiring Company", "xxx"])
def test_a_placeholder_is_not_a_company(company):
    assert M.name_forms(company) == []


def test_a_generic_word_cannot_match_free_text_and_a_short_name_must_keep_its_case():
    [glob] = M.name_forms("Global")
    assert not glob.text_ok and not M.in_text(glob, "Global is hiring a Global engineer")
    [hp] = M.name_forms("HP")
    assert hp.case_sensitive
    assert M.in_text(hp, "Thanks for applying to HP.") and not M.in_text(hp, "we use hp printers") and not M.in_text(hp, "senior PHP developer")


@pytest.mark.parametrize("company,text,hit", [
    ("Acme", "Thank you for applying to Acme!", True), ("Acme", "ACME is hiring", True), ("Acme", "thanks, the acme team", True),
    ("Acme", "Acmeology newsletter", False), ("Acme", "pacme", False), ("Acme", "Acme-Corp recruiting", True), ("Acme", "acme_corp", True),
    ("Acme Corp", "Acme  Corp", True), ("Acme Corp", "acme corporation", True), ("Stripe", "your striped shirt", False),
    ("Mercedes-Benz", "Mercedes Benz careers", True), ("3M", "We at 3M", True), ("3M", "a 3Ms budget", False), ("HP", "HP, Inc.", True),
    ("腾讯", "我们是腾讯的招聘团队", True), ("腾讯", "阿里巴巴", False), ("Acme", "", False)])
def test_whole_words_only(company, text, hit):
    forms = M.name_forms(company)
    assert any(M.in_text(f, text) for f in forms) is hit


@pytest.mark.parametrize("domain,label", [("acme.com", "acme"), ("careers.acme.com", "acme"), ("acme.co.uk", "acme"), ("jobs.acme.co.in", "acme"),
                                          ("mail.acme.com.au", "acme"), ("acme.io", "acme"), ("acme", ""), ("", ""), ("co.uk", ""), ("com.au", ""), ("a.b.c.d.acme.org", "acme")])
def test_the_registrable_label(domain, label):
    assert M.registrable_label(domain) == label


def test_the_domain_is_the_company_only_for_a_company_domain():
    for domain in ("acme.com", "careers.acme.co.uk", "acme-corp.com", "acmecorp.com", "ACME.io"):
        assert M.domain_kind("Acme Corp", domain.lower()) == "full", domain
    for domain in ("greenhouse.io", "acme.gmail.com", "gmail.com", "acme-careers.com", "notacme.com", "a-c-m-e.com", "ac-me.com", "acme.evil.example", "co.uk"):
        assert M.domain_kind("Acme Corp", domain) is None, domain
    assert M.domain_kind("Stripe Payments India", "stripe.com") == "partial", "only the distinctive first word of a longer name"
    assert M.domain_kind("Delta Dental", "delta.com") == "partial" and M.domain_kind("Delta Dental", "deltadental.com") == "full"
    assert M.domain_kind("Data Systems Inc", "data.com") is None, "a generic first word is not distinctive"
    assert M.domain_kind("Global", "global.com") == "full", "a generic name still counts through its own domain"
    assert M.domain_kind("?", "acme.com") is None and M.domain_kind(None, "acme.com") is None and M.domain_kind("Confidential", "confidential.com") is None


def test_a_first_word_domain_alone_is_not_enough_for_a_match_and_is_flagged():
    jobs = [cand(1, "Delta Dental")]
    m = M.match_message(msg(sender="x@delta.com", subject="Hi", text="Hi"), jobs)
    assert m.candidate is None, "2 points: the first word of the name is not the company"
    m = M.match_message(msg(sender="x@delta.com", subject="Interview at Delta Dental", text="Backend Engineer."), jobs)
    assert m.candidate and "domain_partial" in m.evidence and "domain_matches_first_word_only" in m.flags and m.confidence == "medium"


def test_an_unusual_domain_ending_is_flagged_and_never_high():
    for domain in ("acme.xyz", "acme.top", "acme.click"):
        m = M.match_message(msg(sender=f"x@{domain}", subject="Hi", text="Hi"), [cand(1, "Acme")])
        assert m.candidate and "unfamiliar_domain_ending" in m.flags and m.confidence == "medium", domain
    for domain in ("acme.com", "acme.io", "acme.co.uk", "acme.ai", "acme.in"):
        m = M.match_message(msg(sender=f"x@{domain}", subject="Hi", text="Hi"), [cand(1, "Acme")])
        assert "unfamiliar_domain_ending" not in m.flags and m.confidence == "high", domain


def test_the_job_title_must_be_whole_words_not_a_part_of_longer_words():
    assert not M._title_in("Data Analyst", "work with metadata analysts")
    assert not M._title_in("Software Engineer", "software engineering team")
    assert M._title_in("Data Analyst", "the Data Analyst role") and M._title_in("Software Engineer", "Senior Software Engineer, Platform")
    assert not M._title_in("Manager", "the manager said") and not M._title_in("", "anything")


@pytest.mark.parametrize("domain,ats", [("greenhouse.io", True), ("us.greenhouse-mail.io", True), ("hire.lever.co", True), ("jobs.ashbyhq.com", True), ("evil-greenhouse.io", False),
                                        ("greenhouse.io.evil.example", False), ("acme.com", False), ("myworkday.com", True)])
def test_applicant_tracking_senders(domain, ats):
    assert M.is_ats(domain) is ats


# ---------------------------------------------------------------- matching an email to an application

def test_the_company_domain_is_the_strongest_evidence():
    m = M.match_message(msg(sender="Priya <priya@acme.com>", subject="Next steps", text="Hi, let's talk."), [cand(1, "Acme Corp")])
    assert m.candidate.job_id == 1 and m.evidence == ["domain"] and m.confidence == "high" and m.sender_kind == "company" and m.flags == []


def test_an_applicant_tracking_email_is_matched_by_the_name_in_its_display_and_subject():
    m = M.match_message(msg(sender="Acme <no-reply@us.greenhouse-mail.io>", subject="Thank you for applying to Acme", text="We received your application for Backend Engineer at Acme."),
                        [cand(1, "Acme Corp"), cand(2, "Beta Ltd")])
    assert m.candidate.job_id == 1 and {"display", "subject"} <= set(m.evidence) and m.sender_kind == "ats" and m.confidence == "high" and m.flags == []


def test_a_title_in_the_text_adds_a_point():
    c = [cand(1, "Acme", title="Senior Backend Engineer")]
    with_title = M.match_message(msg(sender="x@gmail.com", name="", subject="Update on your application at Acme", text="Your Senior Backend Engineer application."), c)
    without = M.match_message(msg(sender="x@gmail.com", name="", subject="Update on your application at Acme", text="Your application."), c)
    assert "title" in with_title.evidence and with_title.candidate and without.candidate is None, "subject alone is 2 points: not enough"


def test_a_company_named_only_deep_in_the_text_is_not_evidence():
    text = "Hi there. " + "Lorem ipsum dolor sit amet. " * 40 + "Previously, at Acme we were hiring."
    assert M.match_message(msg(sender="x@example.org", name="", subject="Hello", text=text), [cand(1, "Acme")]).candidate is None


def test_an_email_that_names_no_application_matches_nothing():
    m = M.match_message(msg(sender="news@linkedin.com", subject="5 new jobs for you", text="Backend roles near you"), [cand(1, "Acme Corp"), cand(2, "Beta Ltd")])
    assert m.candidate is None and "no application" in m.reason


def test_two_companies_that_fit_equally_are_ambiguous_never_guessed():
    m = M.match_message(msg(sender="Recruiting <jobs@greenhouse.io>", name="Recruiting", subject="Your application to Acme or Beta", text="Acme and Beta both."),
                        [cand(1, "Acme"), cand(2, "Beta")])
    assert m.candidate is None and "more than one company" in m.reason and "acme" in m.reason and "beta" in m.reason


def test_a_second_company_one_point_behind_is_still_ambiguous():
    m = M.match_message(msg(sender="Acme <jobs@greenhouse.io>", subject="Update from Acme and Beta", text="Beta is mentioned here."), [cand(1, "Acme"), cand(2, "Beta")])
    assert m.candidate is None and "more than one company" in m.reason


def test_a_company_mentioned_once_in_passing_is_not_a_competitor():
    m = M.match_message(msg(sender="Acme <jobs@greenhouse.io>", subject="Update from Acme", text="Beta is mentioned here."), [cand(1, "Acme"), cand(2, "Beta")])
    assert m.candidate.job_id == 1


def test_several_applications_at_one_company_are_told_apart_by_the_role():
    jobs = [cand(1, "Acme", title="Backend Engineer"), cand(2, "Acme", title="Data Scientist")]
    m = M.match_message(msg(sender="p@acme.com", subject="Interview for the Data Scientist role", text="We'd like to talk."), jobs)
    assert m.candidate.job_id == 2 and "title" in m.evidence


def test_several_applications_at_one_company_without_a_role_pick_the_single_submitted_one():
    jobs = [cand(1, "Acme", title="Backend Engineer", job_status="shortlisted", app=None), cand(2, "Acme", title="Data Scientist")]
    assert M.match_message(msg(sender="p@acme.com", subject="Hello", text="Hi"), jobs).candidate.job_id == 2


def test_several_equal_applications_at_one_company_are_ambiguous():
    jobs = [cand(1, "Acme", title="Backend Engineer"), cand(2, "Acme", title="Data Scientist")]
    m = M.match_message(msg(sender="p@acme.com", subject="Hello", text="Hi"), jobs)
    assert m.candidate is None and "2 applications at this company" in m.reason


def test_a_short_name_does_not_match_inside_another_word():
    jobs = [cand(1, "HP")]
    assert M.match_message(msg(sender="x@example.org", name="", subject="Senior PHP developer", text="PHP, HP-UX and phpunit experience."), jobs).candidate is None
    assert M.match_message(msg(sender="x@hp.com", subject="Hello", text="Hi"), jobs).candidate.job_id == 1, "its own domain is evidence"


def test_a_placeholder_company_matches_nothing_even_when_the_text_is_full_of_it():
    jobs = [cand(1, "?"), cand(2, "Confidential"), cand(3, "-")]
    assert M.match_message(msg(sender="x@example.org", name="?", subject="Is this confidential? ? ?", text="Confidential - ? - Stealth"), jobs).candidate is None


def test_a_generic_company_name_counts_only_through_the_domain():
    jobs = [cand(1, "Global")]
    assert M.match_message(msg(sender="x@example.org", name="Global", subject="Global update", text="Global hiring."), jobs).candidate is None
    assert M.match_message(msg(sender="x@global.com", subject="Hi", text="Hi"), jobs).candidate.job_id == 1


def test_an_email_without_a_usable_sender_matches_nothing():
    m = M.match_message(msg(sender="garbage", subject="Acme", text="Acme"), [cand(1, "Acme")])
    assert m.candidate is None and "no usable address" in m.reason


def test_a_company_named_by_free_mail_or_a_lookalike_domain_is_matched_but_flagged():
    jobs = [cand(1, "Acme", title="Backend Engineer")]
    free = M.match_message(msg(sender="Acme Recruiting <acme.hr@gmail.com>", subject="Interview with Acme", text="Backend Engineer at Acme."), jobs)
    assert free.candidate and "free_mail_sender" in free.flags and "display_name_only" in free.flags and free.sender_kind == "other"
    look = M.match_message(msg(sender="Careers <hr@acme-careers.com>", name="Careers", subject="Interview at Acme", text="Backend Engineer at Acme."), jobs)
    assert look.candidate and "sender_domain_not_the_company" in look.flags and look.sender_kind == "other" and "domain" not in look.evidence


def test_a_company_domain_with_a_subdomain_and_a_second_level_ending_is_the_company():
    m = M.match_message(msg(sender="x@careers.acme.co.uk", subject="Hello", text="Hi"), [cand(1, "Acme Ltd")])
    assert m.candidate.job_id == 1 and m.sender_kind == "company"


def test_an_applicant_tracking_domain_never_counts_as_the_company_even_for_a_company_named_like_it():
    assert M.match_message(msg(sender="x@greenhouse.io", name="", subject="Hi", text="Hi"), [cand(1, "Greenhouse")]).candidate is None


def test_no_candidates_means_no_match():
    assert M.match_message(msg(), []).candidate is None


def test_a_candidate_is_built_from_a_database_row():
    c = M.Candidate.from_row({"job_id": "7", "company": None, "title": None, "job_status": "applied", "app_status": None})
    assert (c.job_id, c.company, c.title, c.app_status, c.submitted) == (7, "", "", None, False)
    assert M.Candidate.from_row({"job_id": 1, "company": "A", "title": "T", "job_status": "applied", "app_status": "rejected"}).submitted


def test_a_sender_that_is_neither_the_company_nor_its_recruiting_system_is_never_high_confidence():
    jobs = [cand(1, "Acme", title="Backend Engineer")]
    text = "We would like to schedule an interview for Backend Engineer at Acme."
    free = M.match_message(msg(sender="Acme HR <acme.hr@gmail.com>", subject="Interview with Acme", text=text), jobs)
    other = M.match_message(msg(sender="Acme HR <hr@some-agency.example>", subject="Interview with Acme", text=text), jobs)
    assert free.candidate and other.candidate and free.confidence == other.confidence == "medium"
    ats = M.match_message(msg(sender="Acme <no-reply@us.greenhouse-mail.io>", subject="Interview with Acme", text=text), jobs)
    assert ats.confidence == "high" and M.match_message(msg(sender="x@acme.com", subject="Hi", text="Hi"), jobs).confidence == "high"


def test_a_mention_beyond_the_first_part_of_the_text_adds_nothing_even_to_other_evidence():
    """display (2) + a mention far down (would be 1 more) must stay at 2: not enough."""
    deep = "Filler sentence here. " * 60 + "We also work with Acme on other things."
    assert M.match_message(msg(sender="Acme <hr@some-agency.example>", subject="Hello", text=deep), [cand(1, "Acme")]).candidate is None
    near = "We also work with Acme on other things. " + "Filler sentence here. " * 60
    assert M.match_message(msg(sender="Acme <hr@some-agency.example>", subject="Hello", text=near), [cand(1, "Acme")]).candidate.job_id == 1


def test_a_company_that_shares_its_name_with_a_free_mail_host_is_not_its_domain():
    assert M.domain_matches("Outlook", "outlook.com") is False
    assert M.match_message(msg(sender="x@gmail.com", name="", subject="Hi", text="Hi"), [cand(1, "Gmail")]).candidate is None
    assert M.domain_kind("Gmail", "gmail.com") is None


# ---------------------------------------------------------------- the role the email names

def role_flag(subject, title="Software Engineer", text="Hello", company="Cloudflare", sender="Cloudflare Recruiting <no-reply@cloudflare.com>"):
    m = M.match_message(msg(sender=sender, subject=subject, text=text), [cand(1, company, title=title, job_status="shortlisted", app=None)])
    assert m.candidate, m.reason
    return "role_mismatch" in m.flags


def test_a_role_that_is_more_specific_than_the_matched_job_is_flagged():
    # the real case: the reply was about "Software Engineer, Rulesets", the only shortlisted Cloudflare job was another Software Engineer
    assert role_flag("Cloudflare Recruiting | Application Received - Software Engineer, Rulesets")
    assert role_flag("Application Update | Software Engineer, Rulesets")


def test_a_different_role_is_flagged_and_the_same_role_is_not():
    assert role_flag("Your application for Data Scientist at Cloudflare", title="Software Engineer")
    assert not role_flag("Application Received - Software Engineer")
    assert not role_flag("Thank you for applying to Cloudflare: Software Engineer")
    assert not role_flag("Your application for Software Engineer at Cloudflare")


def test_an_email_may_shorten_a_title_but_not_extend_it():
    assert not role_flag("Application received: Backend Engineer", title="Senior Backend Engineer")
    assert role_flag("Application received: Senior Backend Engineer", title="Backend Engineer")
    assert role_flag("Application received: Software Engineer II", title="Software Engineer")


def test_an_email_that_names_no_role_is_not_flagged():
    assert not role_flag("Application Received")
    assert not role_flag("Update on your application", text="Thank you for your interest in Cloudflare.")


def test_full_time_and_process_words_are_not_part_of_the_role():
    assert not role_flag("Interview invitation: Software Engineer (Full-Time)")
    assert not role_flag("Next steps for your Software Engineer application")
    assert not role_flag("Phone screening - Software Engineer - Remote")


def test_the_text_names_the_role_when_the_subject_does_not():
    body = "Hi Sam, we have received your application for the Software Engineer, Rulesets position! Our team will review it."
    assert role_flag("Application Received", text=body)
    assert not role_flag("Application Received", text=body.replace(", Rulesets", ""))
    assert not role_flag("Application Received", text="Thanks for applying for the next position that opens.")


def test_the_subject_decides_when_it_names_a_role_even_if_the_text_names_another():
    assert not role_flag("Application Received - Software Engineer", text="We received your application for the Data Scientist position.")


def test_one_matching_segment_is_enough():
    assert not role_flag("Acme Careers | Software Engineer", title="Software Engineer", company="Acme", sender="Acme <jobs@acme.com>")
    assert not role_flag("Re: Software Engineer", title="Software Engineer", company="Acme", sender="Acme <jobs@acme.com>")


def test_the_company_name_in_the_role_segment_is_not_an_extra_word():
    assert not role_flag("Cloudflare Software Engineer application")


def test_a_mismatch_caps_the_confidence_at_low():
    import mail_rules as R
    m = M.match_message(msg(sender="no-reply@cloudflare.com", subject="Application Received - Software Engineer, Rulesets", text="Hello"),
                        [cand(1, "Cloudflare", title="Software Engineer", job_status="shortlisted", app=None)])
    assert m.confidence == "high" and "role_mismatch" in m.flags
    assert R.combined_confidence(m.confidence, "high", m.flags) == "low"


# the cases an independent review found: noise around a correct role, spelling variants, and qualifiers that were missed

@pytest.mark.parametrize("subject,text,title", [
    ("Application Received", "Thank you for your interest in the Software Engineer position at Acme.", "Software Engineer"),
    ("Application Received", "Thanks for taking the time to apply for the Software Engineer position.", "Software Engineer"),
    ("Application Received", "We would like to move forward with your application for the Software Engineer role.", "Software Engineer"),
    ("Application Received", "Thank you for applying for the Software Engineer - Backend position.", "Software Engineer - Backend"),
    ("Your application for Software Engineer was successfully submitted", "", "Software Engineer"),
    ("Software Engineer interview scheduled", "", "Software Engineer"),
    ("Your interview for Software Engineer is scheduled", "", "Software Engineer"),
    ("We're reviewing your application for Software Engineer", "", "Software Engineer"),
    ("Thanks for your interest in Software Engineer at Acme", "", "Software Engineer"),
    ("Thanks for applying! Your Software Engineer application was received", "", "Software Engineer"),
    ("Unfortunately, Software Engineer position has been filled", "", "Software Engineer"),
    ("Job Application: Software Engineer 2026 Batch", "", "Software Engineer"),
    ("Sam, your application to Software Engineer at Acme", "", "Software Engineer"),
    ("Application Received - Software Engineer, Bengaluru", "", "Software Engineer"),
    ("Application Received - Software Engineer, Bengaluru, India", "", "Software Engineer"),
    ("Application Received - Software Engineer (Bangalore)", "", "Software Engineer"),
    ("Application Received - Software Engineer L5", "", "Software Engineer"),
    ("Application Received - Software Engineer, New Grad 2026", "", "Software Engineer"),
    ("Application Received - Sr. Software Engineer", "", "Senior Software Engineer"),
    ("Application Received - Senior Software Engineer", "", "Sr Software Engineer"),
    ("Application Received - SDE-2", "", "Software Development Engineer II"),
    ("Application Received - SDE II", "", "Software Development Engineer 2"),
    ("Application Received - Software Engineer II", "", "Software Engineer 2"),
    ("Application Received - Fullstack Developer", "", "Full Stack Developer"),
    ("Application Received - Front-End Engineer", "", "Frontend Engineer"),
    ("Application Received - Software Engineering Intern", "", "Software Engineer Intern"),
    ("Application Received - ML Engineer", "", "Machine Learning Engineer"),
    ("Application Received - QA Engineer", "", "Quality Assurance Engineer"),
    ("Message from Priya, Recruiter at Acme", "", "Software Engineer"),
    ("Interview with the Hiring Manager", "", "Software Engineer"),
    ("Software Engineer interview - coding test", "", "Software Engineer"),
    ("Software Engineer - Application Review", "", "Software Engineer"),
    ("Application received - Software Engineer - Acme Corp", "", "Software Engineer"),
])
def test_correct_matches_with_ordinary_wording_are_not_flagged(subject, text, title):
    assert not role_flag(subject, title=title, text=text, company="Acme", sender="Acme <jobs@acme.com>")


@pytest.mark.parametrize("subject,text,title", [
    ("Application received: Software Engineer - Rulesets", "", "Software Engineer"),
    ("Application received: Software Engineer (Rulesets)", "", "Software Engineer"),
    ("Application Received - Software Engineer (Intern)", "", "Software Engineer"),
    ("Application Received - Software Engineer - Backend", "", "Software Engineer"),
    ("Application Received - Test Engineer", "", "Software Engineer"),
    ("Application Received - Contract Engineer", "", "Software Engineer"),
    ("Application Received - SRE", "", "Software Engineer"),
    ("Application Received - DevOps Engineer", "", "Software Engineer"),
    ("Application Received", "We received your application for Software Engineer, Rulesets.", "Software Engineer"),
    ("Application Received", "Thank you for your interest in the Data Scientist position.", "Software Engineer"),
    ("Application Received - Senior Software Engineer", "", "Software Engineer"),
    ("Application Received - Software Engineer 3", "", "Software Engineer 2"),
])
def test_a_different_role_is_flagged_in_every_common_shape(subject, text, title):
    assert role_flag(subject, title=title, text=text, company="Acme", sender="Acme <jobs@acme.com>")


def test_odd_values_never_crash_the_role_check():
    c = M.Candidate(1, "Acme", 5, "shortlisted", None)                      # a title that is not text
    assert M.role_mismatch(c, "Software Engineer", "") is False
    assert M.role_mismatch(M.Candidate(1, "Acme", "Software Engineer", "x", None), None, None) is False
    assert M.role_mismatch(M.Candidate(1, "Acme", "Software Engineer", "x", None), " - " * 300000, "x" * 1_000_000) is False


# ---------------------------------------------------------------- a vendor's recruiting system is not the vendor's own mail

def test_mail_from_oracles_recruiting_system_for_another_company_is_not_oracles():
    # the real case: JPMorgan's acknowledgement came from workflow.mail.us2.cloud.oracle.com and was matched to a shortlisted Oracle job
    m = msg(sender="JPMorganChase <donotreply@workflow.mail.us2.cloud.oracle.com>", subject="We received your job application (Job Number: 210784627)",
            text="Sam, We're glad that you're interested in a career at JPMorganChase! Your application for the position listed below was successfully submitted.")
    assert M.match_message(m, [cand(611, "Oracle", title="Senior Core Infrastructure Engineer", job_status="shortlisted", app=None)]).candidate is None
    assert M.is_ats("workflow.mail.us2.cloud.oracle.com") and M.domain_kind("Oracle", "workflow.mail.us2.cloud.oracle.com") is None


def test_that_mail_matches_the_company_the_email_names():
    m = msg(sender="JPMorganChase <donotreply@workflow.mail.us2.cloud.oracle.com>", subject="JPMorganChase: we received your application",
            text="We're glad that you're interested in a career at JPMorganChase!")
    jobs = [cand(611, "Oracle", title="Senior Core Infrastructure Engineer", job_status="shortlisted", app=None),
            cand(641, "JPMorganChase", title="Software Engineer III", job_status="shortlisted", app=None)]
    hit = M.match_message(m, jobs)
    assert hit.candidate and hit.candidate.job_id == 641 and hit.sender_kind == "ats"


def test_oracles_own_domain_is_still_oracles():
    m = msg(sender="Recruiting <careers@oracle.com>", subject="Your application to Oracle", text="Thank you for applying.")
    hit = M.match_message(m, [cand(611, "Oracle", title="Senior Core Infrastructure Engineer", job_status="shortlisted", app=None)])
    assert hit.candidate and "domain" in hit.evidence


# ---------------------------------------------------------------- one name, two spellings

@pytest.mark.parametrize("company,text,hit", [
    ("JPMorganChase", "JPMorgan Chase & Co. Human Resources", True),
    ("JPMorgan Chase", "JPMorganChase Careers", True),
    ("JPMorgan Chase & Co.", "your application to JPMorganChase", True),
    ("Mercedes-Benz AG", "Mercedes Benz Group recruiting", True),
    ("Thoughtworks", "Thought Works recruiting", True),
    ("Sunrise", "a sun rise day", False),                            # seven letters: too short for the run-together rule
    ("Samsung Research", "SamsungResearch", True),
    ("JPMorganChase", "JPMorgan and Chase", False),                  # an extra word in between is not the name
    ("JPMorganChase", "jpmorgan", False),                            # half of it is not it
    ("Acme Corp", "AcmeCorp", False),                                # the legal suffix is no part of the name (a company domain is judged apart)
])
def test_a_long_name_matches_when_its_words_are_run_together_or_spaced(company, text, hit):
    forms = M.name_forms(company)
    assert any(M.in_text(f, text) for f in forms) is hit


def test_the_jpmorgan_acknowledgement_matches_the_jpmorgan_job_through_the_display_name_and_the_text():
    m = msg(sender="JPMorgan Chase & Co. Human Resources <donotreply@workflow.mail.us2.cloud.oracle.com>",
            subject="We received your job application (Job Number: 210784627)",
            text="Sam, We're glad that you're interested in a career at JPMorganChase! Your application for the position was received.")
    jobs = [cand(611, "Oracle", title="Senior Core Infrastructure Engineer", job_status="shortlisted", app=None),
            cand(641, "JPMorganChase", title="Software Engineer III - Terraform, AWS, Golang", job_status="shortlisted", app=None)]
    hit = M.match_message(m, jobs)
    assert hit.candidate and hit.candidate.job_id == 641 and hit.sender_kind == "ats" and "display" in hit.evidence
