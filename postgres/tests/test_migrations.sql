-- Behaviour tests for migration 001. Run by postgres/tests/run.sh against a throwaway database.
-- Every block raises on failure; everything is rolled back at the end.
BEGIN;

-- Unicode-safe normalisation ------------------------------------------------
DO $$
BEGIN
  ASSERT norm_company('Яндекс Ltd.') = 'яндекс', 'cyrillic company should survive: ' || norm_company('Яндекс Ltd.');
  ASSERT norm_title('技术 工程师 (Remote)') = '技术 工程师', 'cjk title should survive: ' || norm_title('技术 工程师 (Remote)');
  ASSERT norm_city('Москва, Россия') = 'москва', 'cyrillic city should survive';
  ASSERT norm_company('Acme Pvt. Ltd.') = 'acme';
  ASSERT norm_company('!!!') = '' AND safe_norm_company('!!!') <> '', 'symbol-only name needs a guard value';
  ASSERT safe_norm_company('!!!') <> safe_norm_company('???'), 'guard values must differ per name';
END $$;

-- Dedupe still works, and non-Latin companies no longer merge ----------------
DO $$
DECLARE a record; b record; c record; d record;
BEGIN
  SELECT * INTO a FROM upsert_job('linkedin','L1','u1',NULL,'Sr. Software Engineer (Remote)','Acme Pvt. Ltd.',
       'Pune, IN',false,now(),'Build things',NULL,NULL,NULL,'{}'::jsonb);
  SELECT * INTO b FROM upsert_job('indeed','I1','u2','https://acme.com/c/1','Sr Software Engineer','ACME Private Limited',
       'Pune, India',false,now(),'Build things and more',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT a.is_new AND NOT b.is_new AND a.job_id = b.job_id, 'cross-source duplicate should merge';
  ASSERT (SELECT canonical_url FROM jobs WHERE id = a.job_id) = 'https://acme.com/c/1', 'direct apply url should win';

  SELECT * INTO c FROM upsert_job('naukri','N1','u3',NULL,'Инженер','Яндекс','Москва',false,now(),'a',NULL,NULL,NULL,'{}'::jsonb);
  SELECT * INTO d FROM upsert_job('naukri','N2','u4',NULL,'Инженер','Сбербанк','Москва',false,now(),'b',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT c.is_new AND d.is_new AND c.job_id <> d.job_id, 'different non-Latin companies must not merge';
  ASSERT (SELECT count(*) FROM jobs) = 3;
END $$;

-- Old call shape (14 args) keeps working; new fields default sensibly ---------
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM upsert_job('indeed','X1','u','a','Data Analyst','Globex','Delhi',false,now(),'d',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT (SELECT status FROM jobs WHERE id = r.job_id) = 'new';
  ASSERT (SELECT scam_risk FROM jobs WHERE id = r.job_id) IS NULL;
  ASSERT (SELECT scam_flags FROM jobs WHERE id = r.job_id) = '[]'::jsonb;
END $$;

-- Scam handling ---------------------------------------------------------------
DO $$
DECLARE r record; r2 record;
BEGIN
  SELECT * INTO r FROM upsert_job('naukri','S1','u','a','Typing Job','Quick Cash Co','Remote',true,now(),'pay a fee',
       NULL,NULL,NULL,'{}'::jsonb, NULL, 'high', 7, '[{"code":"FEE_REQUEST","severity":3}]'::jsonb);
  ASSERT (SELECT status FROM jobs WHERE id = r.job_id) = 'scam_blocked', 'high risk must be blocked on insert';
  ASSERT (SELECT filter_reason FROM jobs WHERE id = r.job_id) = 'scam risk: high';
  ASSERT (SELECT scam_flags->0->>'code' FROM jobs WHERE id = r.job_id) = 'FEE_REQUEST';

  -- a known job later assessed as high risk is blocked too
  SELECT * INTO r2 FROM upsert_job('indeed','X1','u','a','Data Analyst','Globex','Delhi',false,now(),'d',
       NULL,NULL,NULL,'{}'::jsonb, NULL, 'high', 6, '[]'::jsonb);
  ASSERT (SELECT status FROM jobs WHERE id = r2.job_id) = 'scam_blocked', 'new high-risk assessment must block an open job';

  -- but never overrides an application already in motion
  UPDATE jobs SET status = 'applied' WHERE id = r.job_id;
  PERFORM upsert_job('naukri','S1','u','a','Typing Job','Quick Cash Co','Remote',true,now(),'pay a fee',
       NULL,NULL,NULL,'{}'::jsonb, NULL, 'high', 7, '[]'::jsonb);
  ASSERT (SELECT status FROM jobs WHERE id = r.job_id) = 'applied', 'applied jobs must not be changed by a rescan';
END $$;

-- Deadline sweep + shortlist view ---------------------------------------------
DO $$
DECLARE r1 record; r2 record; r3 record; n int;
BEGIN
  SELECT * INTO r1 FROM upsert_job('indeed','D1','u','a','Role One','Alpha','Pune',false,now(),'x',NULL,NULL,NULL,'{}'::jsonb,
       current_date - 1);
  SELECT * INTO r2 FROM upsert_job('indeed','D2','u','a','Role Two','Beta','Pune',false,now(),'y',NULL,NULL,NULL,'{}'::jsonb,
       current_date + 3);
  SELECT * INTO r3 FROM upsert_job('indeed','D3','u','a','Role Three','Gamma','Pune',false,now() - interval '40 days','z',NULL,NULL,NULL,'{}'::jsonb);

  n := sweep_expired();
  ASSERT n = 1, 'exactly the past-deadline job should expire, got ' || n;
  ASSERT (SELECT status FROM jobs WHERE id = r1.job_id) = 'expired';
  ASSERT (SELECT status FROM jobs WHERE id = r3.job_id) = 'new', 'no deadline means never swept';

  UPDATE jobs SET status = 'scored', score = 80, verdict = 'strong' WHERE id IN (r2.job_id, r3.job_id);
  ASSERT (SELECT closing_soon FROM v_shortlist WHERE id = r2.job_id), 'deadline within 7 days is closing soon';
  ASSERT NOT (SELECT closing_soon FROM v_shortlist WHERE id = r3.job_id);
  ASSERT (SELECT stale FROM v_shortlist WHERE id = r3.job_id), 'posting over 30 days old is stale';

  UPDATE jobs SET verdict = 'excluded' WHERE id = r3.job_id;
  ASSERT NOT EXISTS (SELECT 1 FROM v_shortlist WHERE id = r3.job_id), 'excluded jobs stay out of the shortlist';
END $$;

-- Constraints -----------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    UPDATE jobs SET scam_risk = 'banana' WHERE id = (SELECT min(id) FROM jobs);
    RAISE EXCEPTION 'bad scam_risk was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    UPDATE jobs SET status = 'nonsense' WHERE id = (SELECT min(id) FROM jobs);
    RAISE EXCEPTION 'bad status was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END $$;

-- record_score ----------------------------------------------------------------
DO $$
DECLARE r record; st text;
BEGIN
  SELECT * INTO r FROM upsert_job('indeed','R1','u','a','Scored Role','Delta','Pune',false,now(),'d',NULL,NULL,NULL,'{}'::jsonb);

  st := record_score(r.job_id, '{"score":78,"verdict":"strong","scores":{"technical":80,"experience":70,"behavioral":60,"career":90},
        "gates":{"eligibility":{"verdict":"PASS","note":""}},"strengths":["a"],"gaps":["b"],"score_reason":"good",
        "deadline":"2026-12-31","posting_language":"English","yoe_min":3,"yoe_max":6,
        "next_status":"shortlisted","filter_reason":null}'::jsonb);
  ASSERT st = 'shortlisted', 'status should follow next_status, got ' || st;
  ASSERT (SELECT score FROM jobs WHERE id = r.job_id) = 78;
  ASSERT (SELECT verdict FROM jobs WHERE id = r.job_id) = 'strong';
  ASSERT (SELECT scores->>'career' FROM jobs WHERE id = r.job_id) = '90';
  ASSERT (SELECT deadline FROM jobs WHERE id = r.job_id) = date '2026-12-31';
  ASSERT (SELECT yoe_min FROM jobs WHERE id = r.job_id) = 3;
  ASSERT (SELECT scored_at FROM jobs WHERE id = r.job_id) IS NOT NULL;
  ASSERT EXISTS (SELECT 1 FROM v_shortlist WHERE id = r.job_id), 'shortlisted job appears in the view';

  -- a failed gate sends it to filtered_out with a reason
  st := record_score(r.job_id, '{"score":78,"verdict":"excluded","scores":{},"gates":{},"strengths":[],"gaps":[],
        "score_reason":"x","next_status":"filtered_out","filter_reason":"gate failed: language: needs German"}'::jsonb);
  ASSERT st = 'filtered_out' AND (SELECT filter_reason FROM jobs WHERE id = r.job_id) LIKE '%German%';
  ASSERT NOT EXISTS (SELECT 1 FROM v_shortlist WHERE id = r.job_id);

  -- jobs already in motion, or blocked as scams, are never moved by a score
  UPDATE jobs SET status = 'applied' WHERE id = r.job_id;
  st := record_score(r.job_id, '{"score":10,"verdict":"poor","next_status":"scored","filter_reason":null}'::jsonb);
  ASSERT st = 'applied', 'applied job must keep its status';
  UPDATE jobs SET status = 'scam_blocked' WHERE id = r.job_id;
  st := record_score(r.job_id, '{"score":99,"verdict":"strong","next_status":"shortlisted","filter_reason":null}'::jsonb);
  ASSERT st = 'scam_blocked', 'scam-blocked job must stay blocked';

  BEGIN
    PERFORM record_score(-1, '{"score":1,"next_status":"scored"}'::jsonb);
    RAISE EXCEPTION 'unknown job id was accepted';
  EXCEPTION WHEN raise_exception THEN
    ASSERT SQLERRM LIKE 'record_score: job -1 not found', 'wrong error: ' || SQLERRM;
  END;
END $$;

-- pipeline plumbing (003) -------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; n int; r record; st text;
BEGIN
  -- collection rotation wraps and advances by the batch size
  ASSERT next_collect_slice('t', 10, 4) = 0;
  ASSERT next_collect_slice('t', 10, 4) = 4;
  ASSERT next_collect_slice('t', 10, 4) = 8;
  ASSERT next_collect_slice('t', 10, 4) = 2, 'wraps around';
  ASSERT next_collect_slice('t', 3, 4) = 0, 'cursor (now 6) is re-wrapped modulo the smaller total';
  ASSERT next_collect_slice('t', 3, 4) = 1, 'and continues from there';
  ASSERT next_collect_slice('empty', 0, 4) = 0;

  -- scoring claims
  DELETE FROM jobs;
  SELECT job_id INTO a FROM upsert_job('indeed','C1','u','a','Claim A','Co A','Pune',false,now() - interval '1 day','d',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO b FROM upsert_job('indeed','C2','u','a','Claim B','Co B','Pune',false,now(),'d',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO c FROM upsert_job('indeed','C3','u','a','Claim C','Co C','Pune',false,now(),'d',NULL,NULL,NULL,'{}'::jsonb,
         NULL, 'high', 7, '[]'::jsonb);   -- scam_blocked: never claimed
  UPDATE jobs SET status = 'filtered_out' WHERE id = b AND false;  -- (kept new)

  SELECT count(*) INTO n FROM claim_jobs_to_score(10);
  ASSERT n = 2, 'claims the two eligible jobs, not the scam-blocked one, got ' || n;
  SELECT count(*) INTO n FROM claim_jobs_to_score(10);
  ASSERT n = 0, 'a claimed job is not handed out again within its lease';

  UPDATE jobs SET scoring_claimed_at = now() - interval '31 minutes' WHERE id = a;
  SELECT count(*) INTO n FROM claim_jobs_to_score(10);
  ASSERT n = 1, 'an expired lease makes the job claimable again';

  -- newest posting first, and the limit is respected
  UPDATE jobs SET scoring_claimed_at = NULL;
  SELECT id INTO d FROM claim_jobs_to_score(1);
  ASSERT d = b, 'newest posting is claimed first';

  -- invalid replies are counted, released, and stop after the attempt cap
  UPDATE jobs SET scoring_claimed_at = NULL;
  ASSERT note_score_failure(a) = 1;
  ASSERT note_score_failure(a) = 2;
  ASSERT note_score_failure(a) = 3;
  SELECT count(*) INTO n FROM claim_jobs_to_score(10) WHERE id = a;
  ASSERT n = 0, 'jobs that failed 3 times are not claimed again';

  -- recording a score releases the claim
  UPDATE jobs SET scoring_claimed_at = now() WHERE id = b;
  st := record_score(b, '{"score":80,"verdict":"strong","next_status":"shortlisted","filter_reason":null}'::jsonb);
  ASSERT st = 'shortlisted' AND (SELECT scoring_claimed_at FROM jobs WHERE id = b) IS NULL;
END $$;

-- location-aware dedupe (004) ---------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; g bigint;
BEGIN
  -- city keys
  ASSERT norm_city('Pune Division, Maharashtra, India') = 'pune', norm_city('Pune Division, Maharashtra, India');
  ASSERT norm_city('Pune City, Maharashtra, India') = 'pune' AND norm_city('Pune District, Maharashtra') = 'pune';
  ASSERT norm_city('Greater Bengaluru Area') = 'bengaluru' AND norm_city('Bengaluru East, Karnataka, India') = 'bengaluru';
  ASSERT norm_city('Pune, Hyderabad, Bengaluru, India') = 'pune', 'multi-city lists use the first city';
  ASSERT norm_city('West Bengal, India') = 'bengal' OR norm_city('West Bengal, India') <> '', 'no crash on region names';
  ASSERT is_coarse_city('mh') AND is_coarse_city('ka') AND is_coarse_city('india') AND is_coarse_city('remote') AND is_coarse_city('');
  ASSERT NOT is_coarse_city('pune') AND NOT is_coarse_city('bengaluru');

  DELETE FROM jobs;
  -- same job: Indeed state code vs LinkedIn city
  SELECT job_id INTO a FROM upsert_job('linkedin','L1','u','a','Software Engineer II','Mastercard','Pune Division, Maharashtra, India',false,now(),'Build payments APIs in Java and Go.',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO b FROM upsert_job('indeed','I1','u','a','Software Engineer II','Mastercard','MH, India',false,now(),'Totally different formatting of the text',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT a = b, 'coarse state code must match the city';

  -- same remote posting listed under two cities with identical description
  SELECT job_id INTO c FROM upsert_job('linkedin','L2','u','a','Senior Golang Engineer - Remote','Kake','Pune District, Maharashtra, India',true,now(),
         'We are hiring a remote Golang engineer to build distributed systems with Redis and Postgres. Great team.',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO d FROM upsert_job('linkedin','L3','u','a','Senior Golang Engineer - Remote','Kake','Bengaluru, Karnataka, India',true,now(),
         'We are hiring a remote Golang engineer to build distributed systems with Redis and Postgres. Great team.',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT c = d, 'identical descriptions across two cities are the same posting';

  -- same title at two named cities with different descriptions: two real jobs
  SELECT job_id INTO e FROM upsert_job('linkedin','L4','u','a','Senior Software Engineer','Virtusa','Pune Division, Maharashtra, India',false,now(),
         'Mainframe modernisation for a banking client using COBOL and Java on premise.',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO f FROM upsert_job('linkedin','L5','u','a','Senior Software Engineer','Virtusa','Bengaluru, Karnataka, India',false,now(),
         'Cloud native microservices on AWS for a retail platform using Kotlin and Kubernetes.',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT e <> f, 'same title in two real offices with different descriptions must stay separate';

  -- different title, same company, same city: separate
  SELECT job_id INTO g FROM upsert_job('linkedin','L6','u','a','Data Analyst','Mastercard','Pune Division, Maharashtra, India',false,now(),'Dashboards.',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT g <> a;
  ASSERT (SELECT count(*) FROM job_sources WHERE job_id = a) = 2, 'both listings are kept as sources';
END $$;

-- merge_duplicate_jobs cleans up pre-existing duplicates and protects jobs in motion
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; n int;
BEGIN
  DELETE FROM jobs;
  -- insert rows the way the OLD code would have (distinct fingerprints), bypassing upsert's matching
  INSERT INTO jobs (fingerprint, title, company, location, norm_title, norm_company, norm_city, description, status)
  VALUES ('x1','Backend Engineer','Acme','MH, India','backend engineer','acme','mh','Same text about APIs.','new') RETURNING id INTO a;
  INSERT INTO jobs (fingerprint, title, company, location, norm_title, norm_company, norm_city, description, status)
  VALUES ('x2','Backend Engineer','Acme','Pune, India','backend engineer','acme','pune','Longer same text about APIs and more.','new') RETURNING id INTO b;
  INSERT INTO job_sources (job_id, source, source_job_id) VALUES (a, 'indeed', 'm1'), (b, 'linkedin', 'm2');
  INSERT INTO jobs (fingerprint, title, company, location, norm_title, norm_company, norm_city, description, status)
  VALUES ('x3','Platform Engineer','Globex','Pune','platform engineer','globex','pune','text','applied') RETURNING id INTO c;
  INSERT INTO jobs (fingerprint, title, company, location, norm_title, norm_company, norm_city, description, status)
  VALUES ('x4','Platform Engineer','Globex','MH, India','platform engineer','globex','mh','text','new') RETURNING id INTO d;

  n := merge_duplicate_jobs();
  ASSERT n = 1, 'only the Acme pair merges; the applied Globex job is protected, got ' || n;
  ASSERT (SELECT count(*) FROM jobs WHERE company = 'Acme') = 1;
  ASSERT (SELECT count(*) FROM job_sources WHERE job_id = a) = 2, 'sources move to the kept job';
  ASSERT (SELECT description FROM jobs WHERE id = a) LIKE 'Longer%', 'the longer description wins';
  ASSERT (SELECT count(*) FROM jobs WHERE company = 'Globex') = 2, 'jobs in motion are untouched';
END $$;

-- budget + usage logging (005) --------------------------------------------------
DO $$
DECLARE c numeric; b record; j bigint;
BEGIN
  -- cost = tokens x price per million; haiku 5.5: $0.10 in, $0.50 out, $0.125 cache write, $0.01 cache read
  ASSERT usage_cost('claude-haiku-5-5', 1000000, 0, 0, 0) = 0.10;
  ASSERT usage_cost('claude-haiku-5-5', 0, 1000000, 0, 0) = 0.50;
  ASSERT usage_cost('claude-haiku-5-5', 0, 0, 1000000, 1000000) = 0.135;
  ASSERT usage_cost('claude-haiku-5-5', 2500, 350, 0, 0) = 0.000425, usage_cost('claude-haiku-5-5', 2500, 350, 0, 0)::text;
  ASSERT usage_cost('claude-sonnet-5-5', 1000000, 1000000, 0, 0) = 12.00;
  ASSERT usage_cost('some-future-model', 1000000, 0, 0, 0) = 4.00, 'unknown models are priced at the most expensive known input rate';
  ASSERT usage_cost('claude-haiku-5-5', NULL, NULL, NULL, NULL) = 0, 'nulls count as zero';

  DELETE FROM llm_usage;
  UPDATE budget_settings SET monthly_cap_usd = 10, daily_cap_usd = 1.5, warn_pct = 80, timezone = 'Asia/Kolkata';
  SELECT * INTO b FROM budget_status();
  ASSERT b.month_spent = 0 AND b.day_spent = 0 AND NOT b.blocked AND NOT b.warn AND b.month_remaining = 10 AND b.day_remaining = 1.5;

  -- logging
  c := record_usage('anthropic', 'score', 'claude-haiku-5-5', 1, 2500, 350, 0, 0);
  ASSERT c = 0.000425;
  ASSERT record_usage('anthropic', 'score', 'claude-haiku-5-5', 2, 100, 10, 1000, 2000, false, 'invalid reply') > 0;
  ASSERT (SELECT count(*) FROM llm_usage) = 2 AND (SELECT ok FROM llm_usage WHERE job_id = 2) = false;
  ASSERT (SELECT note FROM llm_usage WHERE job_id = 1) IS NULL;
  PERFORM record_usage('anthropic', 'score', 'claude-typo-9', 3, 1000, 0);
  ASSERT (SELECT note FROM llm_usage WHERE job_id = 3) LIKE '%unknown model%';

  -- only anthropic spend counts toward the caps; apify is logged separately with an override
  PERFORM record_usage('apify', 'search', 'cheap_scraper', NULL, 0, 0, 0, 0, true, 'estimated', 0.13);
  SELECT * INTO b FROM budget_status();
  ASSERT b.month_spent < 0.01, 'apify estimate must not count: ' || b.month_spent;
  ASSERT (SELECT sum(cost_usd) FROM llm_usage WHERE provider = 'apify') = 0.13;

  -- warn at 80% of the month, block at the monthly cap
  UPDATE budget_settings SET monthly_cap_usd = 0.004, daily_cap_usd = 100;   -- spend so far is about 0.0046
  SELECT * INTO b FROM budget_status();
  ASSERT b.warn AND b.blocked AND b.blocked_reason = 'monthly cap reached', coalesce(b.blocked_reason, 'null');
  ASSERT b.month_remaining = 0;

  -- the daily cap blocks on its own, even with plenty of month left
  UPDATE budget_settings SET monthly_cap_usd = 1000, daily_cap_usd = 0.001;
  SELECT * INTO b FROM budget_status();
  ASSERT b.blocked AND b.blocked_reason = 'daily cap reached' AND NOT b.warn;

  -- usage from before this month, or before today, is outside the windows
  UPDATE budget_settings SET monthly_cap_usd = 1000, daily_cap_usd = 1000;
  UPDATE llm_usage SET at = now() - interval '40 days';
  SELECT * INTO b FROM budget_status();
  ASSERT b.month_spent = 0 AND NOT b.blocked, 'old usage is outside the month window';
  UPDATE llm_usage SET at = date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata' - interval '1 hour'
   WHERE provider = 'anthropic';   -- yesterday evening IST
  SELECT * INTO b FROM budget_status();
  ASSERT b.day_spent = 0, 'yesterday is outside the day window';

  -- the spend view groups by day, provider, purpose and model
  UPDATE llm_usage SET at = now();
  ASSERT (SELECT sum(calls) FROM v_spend_month WHERE provider = 'anthropic') = 3;

  -- constraints
  BEGIN UPDATE budget_settings SET monthly_cap_usd = -1; RAISE EXCEPTION 'negative cap accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO budget_settings (id) VALUES (2); RAISE EXCEPTION 'second settings row accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
END $$;

-- tailoring (006) -------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; n int; st text; r record;
BEGIN
  DELETE FROM applications; DELETE FROM jobs;
  -- defaults
  SELECT * INTO r FROM tailor_settings;
  ASSERT r.mode = 'auto' AND r.approval = 'review' AND r.model = 'claude-sonnet-5-5' AND r.min_score IS NULL
     AND r.max_score IS NULL AND r.daily_limit IS NULL AND r.cover_letter AND r.max_pages = 1;
  ASSERT r.verify AND r.verify_model = 'claude-haiku-5-5', 'the fact-check layer is on by default';
  BEGIN UPDATE tailor_settings SET min_score = 80, max_score = 70; RAISE EXCEPTION 'inverted band accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET mode = 'sometimes'; RAISE EXCEPTION 'bad mode accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;

  SELECT job_id INTO a FROM upsert_job('indeed','T1','u','a','Backend Engineer','Alpha','Pune',false,now(),'d1',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO b FROM upsert_job('indeed','T2','u','a','Platform Engineer','Beta','Pune',false,now(),'d2',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO c FROM upsert_job('indeed','T3','u','a','Data Engineer','Gamma','Pune',false,now(),'d3',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO d FROM upsert_job('indeed','T4','u','a','Scam Role','Delta','Pune',false,now(),'d4',NULL,NULL,NULL,'{}'::jsonb);
  UPDATE jobs SET status = 'shortlisted', score = 85 WHERE id = a;
  UPDATE jobs SET status = 'shortlisted', score = 72 WHERE id = b;
  UPDATE jobs SET status = 'shortlisted', score = 60 WHERE id = c;          -- below the minimum
  UPDATE jobs SET status = 'shortlisted', score = 90, scam_risk = 'high' WHERE id = d;

  -- claim: at or above the minimum, best first, never scam-flagged, never the same job twice
  SELECT count(*) INTO n FROM claim_jobs_to_tailor(10, 70);
  ASSERT n = 2, 'claims a and b only, got ' || n;
  ASSERT (SELECT count(*) FROM claim_jobs_to_tailor(10, 70)) = 0, 'a claimed job is leased';
  UPDATE jobs SET tailoring_claimed_at = NULL;
  ASSERT (SELECT id FROM claim_jobs_to_tailor(1, 70)) = a, 'highest score first';
  UPDATE jobs SET tailoring_claimed_at = NULL;
  ASSERT note_tailor_failure(a) = 1 AND note_tailor_failure(a) = 2;
  ASSERT (SELECT count(*) FROM claim_jobs_to_tailor(10, 70) WHERE id = a) = 0, 'two failures stop retries';

  -- review mode: the job waits as tailored, the application as drafted
  st := save_application(b, 'tailored', '/output/x', '/output/x/resume.pdf', '/output/x/cover.pdf',
          '{"a":1}'::jsonb, '[{"requirement":"Go","status":"matched"}]'::jsonb, '{"ok":true}'::jsonb, 'claude-sonnet-5-5', 0.03, false, NULL, 'review');
  ASSERT st = 'drafted' AND (SELECT status FROM jobs WHERE id = b) = 'tailored';
  ASSERT (SELECT count(*) FROM claim_jobs_to_tailor(10, 0) WHERE id = b) = 0, 'a job with an application is never claimed again';
  ASSERT applications_today() = 1;
  ASSERT (SELECT count(*) FROM v_review_queue) = 1;

  -- auto-approval stores it as approved, unless the audit flagged it
  UPDATE jobs SET status = 'shortlisted' WHERE id = c;
  st := save_application(c, 'base', '/o/c', '/o/c/r.pdf', NULL, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, 'claude-sonnet-5-5', 0, false, NULL, 'auto');
  ASSERT st = 'approved' AND (SELECT status FROM jobs WHERE id = c) = 'approved';
  UPDATE jobs SET status = 'shortlisted' WHERE id = a;
  st := save_application(a, 'tailored', '/o/a', '/o/a/r.pdf', '/o/a/c.pdf', '{}'::jsonb, '[]'::jsonb, '{"ok":false}'::jsonb, 'claude-sonnet-5-5', 0.04, true, 'ungrounded claim', 'auto');
  ASSERT st = 'drafted', 'auto mode still asks a human when the audit flagged the draft';
  ASSERT (SELECT needs_attention FROM applications WHERE job_id = a);

  -- review actions
  ASSERT review_application(b, 'approve', 'looks good') = 'approved';
  ASSERT (SELECT status FROM jobs WHERE id = b) = 'approved' AND (SELECT review_note FROM applications WHERE job_id = b) = 'looks good';
  ASSERT review_application(b, 'applied') = 'applied';
  ASSERT (SELECT applied_at FROM applications WHERE job_id = b) IS NOT NULL AND (SELECT status FROM jobs WHERE id = b) = 'applied';
  BEGIN PERFORM review_application(b, 'skip'); RAISE EXCEPTION 'skipped an applied application';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM LIKE 'cannot skip an application that is applied', SQLERRM; END;
  BEGIN PERFORM review_application(b, 'frobnicate'); RAISE EXCEPTION 'bad action accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM LIKE 'unknown action%', SQLERRM; END;
  BEGIN PERFORM review_application(-1, 'approve'); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM LIKE 'no job -1', SQLERRM; END;
  st := review_application(a, 'skip');   -- (separate statement: a subquery in the same expression would not see the update)
  ASSERT st = 'skipped' AND (SELECT status FROM jobs WHERE id = a) = 'skipped';
  st := review_application(a, 'approve');
  ASSERT st = 'approved', 'a skipped application can be reconsidered';

  -- re-tailoring never overwrites an application the user already acted on
  UPDATE jobs SET status = 'shortlisted' WHERE id = b;
  PERFORM save_application(b, 'tailored', '/new', '/new/r.pdf', NULL, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, 'm', 0.1, false, NULL, 'review');
  ASSERT (SELECT status FROM applications WHERE job_id = b) = 'applied' AND (SELECT files_dir FROM applications WHERE job_id = b) = '/output/x';

  ASSERT (SELECT update_offset FROM telegram_state) = 0;
END $$;

-- Optional parts (008): feature switches, approval 'cli', manual status editing ------------------
DO $$
DECLARE j1 bigint; j2 bigint; j3 bigint; st text;
BEGIN
  -- every optional stage is on by default, and the row cannot be duplicated
  ASSERT (SELECT tailoring AND telegram AND digest FROM feature_settings WHERE id = 1), 'features default to on';
  BEGIN INSERT INTO feature_settings (id) VALUES (2); RAISE EXCEPTION 'second settings row accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE feature_settings SET tailoring = false, telegram = false WHERE id = 1;
  ASSERT NOT (SELECT tailoring FROM feature_settings), 'a switch can be turned off';

  -- the CLI-only approval choice is accepted, and anything else still is not
  UPDATE tailor_settings SET approval = 'cli';
  ASSERT (SELECT approval FROM tailor_settings) = 'cli';
  BEGIN UPDATE tailor_settings SET approval = 'maybe'; RAISE EXCEPTION 'bad approval accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE tailor_settings SET approval = 'review';

  -- stop-at-scoring path: a job that was never tailored can still be decided on, by hand
  SELECT job_id INTO j1 FROM upsert_job('indeed','M1','u','a','Backend Engineer','ManualCo','Pune',false,now(),'d1',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO j2 FROM upsert_job('indeed','M2','u','a','Platform Engineer','HandCo','Delhi',false,now(),'d2',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO j3 FROM upsert_job('indeed','M3','u','a','Data Engineer','ScamCo','Goa',false,now(),'d3',NULL,NULL,NULL,'{}'::jsonb);
  UPDATE jobs SET status = 'shortlisted', score = 80 WHERE id IN (j1, j2);

  st := review_application(j1, 'applied', 'applied on the company site');
  ASSERT st = 'applied', 'applied works without a tailored application';
  ASSERT (SELECT variant FROM applications WHERE job_id = j1) = 'base', 'a manual decision is recorded on a base row';
  ASSERT (SELECT files_dir FROM applications WHERE job_id = j1) IS NULL, 'a manual row has no files';
  ASSERT (SELECT applied_at FROM applications WHERE job_id = j1) IS NOT NULL;
  ASSERT (SELECT status FROM jobs WHERE id = j1) = 'applied';
  ASSERT (SELECT review_note FROM applications WHERE job_id = j1) = 'applied on the company site';
  st := review_application(j2, 'skip');
  ASSERT st = 'skipped' AND (SELECT status FROM jobs WHERE id = j2) = 'skipped', 'skip works on a job never tailored';
  st := review_application(j2, 'approve');
  ASSERT st = 'approved', 'a skipped decision can be reconsidered';
  BEGIN PERFORM review_application(-5, 'skip'); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM = 'no job -5', SQLERRM; END;

  -- without Gmail tracking the later stages are edited by hand, from any state
  ASSERT set_application_status(j1, 'interview', 'phone screen Tuesday') = 'interview';
  ASSERT (SELECT status FROM applications WHERE job_id = j1) = 'interview';
  ASSERT (SELECT status FROM jobs WHERE id = j1) = 'applied', 'the job stays applied; the application holds the stage';
  ASSERT (SELECT review_note FROM applications WHERE job_id = j1) = 'phone screen Tuesday';
  PERFORM set_application_status(j1, 'offer');
  ASSERT (SELECT review_note FROM applications WHERE job_id = j1) = 'phone screen Tuesday', 'an empty note keeps the old one';
  PERFORM set_application_status(j1, 'rejected', 'they went another way');
  ASSERT (SELECT status FROM applications WHERE job_id = j1) = 'rejected';
  ASSERT (SELECT applied_at FROM applications WHERE job_id = j1) <= now(), 'applied_at is kept, not reset';
  -- marking a job you found yourself straight as in progress records when you applied
  PERFORM set_application_status(j2, 'in_progress');
  ASSERT (SELECT applied_at FROM applications WHERE job_id = j2) IS NOT NULL;
  BEGIN PERFORM set_application_status(j1, 'hired'); RAISE EXCEPTION 'bad status accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM = 'unknown status hired', SQLERRM; END;
  BEGIN PERFORM set_application_status(-1, 'applied'); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM = 'no job -1', SQLERRM; END;

  -- a scam-blocked job keeps its blocked state even if someone marks it
  UPDATE jobs SET status = 'scam_blocked' WHERE id = j3;
  PERFORM set_application_status(j3, 'withdrawn');
  ASSERT (SELECT status FROM jobs WHERE id = j3) = 'scam_blocked';

  -- the review queue only holds real drafts, never manual rows
  ASSERT (SELECT count(*) FROM v_review_queue WHERE job_id IN (j1, j2, j3)) = 0;
END $$;

-- Schedules (009): defaults, constraints, atomic claiming, intervals, daily once-per-day -----------
DO $$
DECLARE r record; n int; nxt timestamptz;
BEGIN
  -- defaults: collect every 6 hours, 18 combinations per run
  SELECT * INTO r FROM schedule_settings WHERE name = 'collect';
  ASSERT r.every_minutes = 360 AND r.batch = 18 AND r.enabled AND r.at_time IS NULL, 'collect defaults to every 6 hours';
  ASSERT (SELECT every_minutes FROM schedule_settings WHERE name = 'score') = 30 AND (SELECT model FROM schedule_settings WHERE name = 'score') = 'claude-haiku-5-5';
  ASSERT (SELECT batch FROM schedule_settings WHERE name = 'score') = 25, 'score defaults to 25 jobs per run (011)';
  ASSERT (SELECT every_minutes FROM schedule_settings WHERE name = 'tailor') = 15 AND (SELECT batch FROM schedule_settings WHERE name = 'tailor') = 3;
  ASSERT (SELECT at_time FROM schedule_settings WHERE name = 'digest') = '08:00' AND (SELECT every_minutes FROM schedule_settings WHERE name = 'digest') IS NULL;

  -- the settings cannot be put in a shape the scheduler cannot read
  BEGIN UPDATE schedule_settings SET every_minutes = 10 WHERE name = 'collect'; RAISE EXCEPTION 'collect every 10 minutes accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;                                    -- boards would be hammered
  BEGIN UPDATE schedule_settings SET every_minutes = 4 WHERE name = 'score'; RAISE EXCEPTION 'every 4 minutes accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET every_minutes = 10081 WHERE name = 'score'; RAISE EXCEPTION 'more than 7 days accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET every_minutes = 60 WHERE name = 'digest'; RAISE EXCEPTION 'digest as interval + time accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET at_time = '09:00' WHERE name = 'score'; RAISE EXCEPTION 'interval + daily time accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET batch = 0 WHERE name = 'score'; RAISE EXCEPTION 'batch 0 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET batch = 51 WHERE name = 'score'; RAISE EXCEPTION 'batch 51 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET model = 'x' WHERE name = 'collect'; RAISE EXCEPTION 'a model on collect accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET model = 'bad model!' WHERE name = 'score'; RAISE EXCEPTION 'a malformed model name accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO schedule_settings (name, every_minutes, batch) VALUES ('mystery', 60, 1); RAISE EXCEPTION 'an unknown schedule accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;

  -- first run: a fresh install claims everything that is due at once. Make the daily time always passed.
  UPDATE schedule_settings SET at_time = '00:00' WHERE name = 'digest';
  UPDATE schedule_settings SET last_run_at = NULL, run_now = false, enabled = true;
  SELECT count(*) INTO n FROM claim_due_schedules();
  ASSERT n = 4, 'a fresh install claims collect, score, tailor and digest: ' || n;
  ASSERT (SELECT count(*) FROM claim_due_schedules()) = 0, 'a second tick right after claims nothing (no double starts)';
  ASSERT (SELECT every_minutes FROM schedule_settings WHERE name = 'collect') = 360;

  -- the claim hands back what the workflow needs
  UPDATE schedule_settings SET last_run_at = now() - interval '2 hours' WHERE name = 'score';
  UPDATE schedule_settings SET last_run_at = now() WHERE name <> 'score';
  SELECT * INTO r FROM claim_due_schedules();
  ASSERT r.name = 'score' AND r.batch = 25 AND r.model = 'claude-haiku-5-5', 'score is due after 2 hours, and brings its batch and model';

  -- intervals are honoured to the minute
  UPDATE schedule_settings SET last_run_at = now() - interval '359 minutes' WHERE name = 'collect';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'collect') = 0, 'collect is not due before 6 hours';
  UPDATE schedule_settings SET last_run_at = now() - interval '361 minutes' WHERE name = 'collect';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'collect') = 1, 'collect is due after 6 hours';
  UPDATE schedule_settings SET every_minutes = 60 WHERE name = 'collect';          -- a changed schedule applies to the next tick
  UPDATE schedule_settings SET last_run_at = now() - interval '61 minutes' WHERE name = 'collect';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'collect') = 1;
  UPDATE schedule_settings SET every_minutes = 360 WHERE name = 'collect';

  -- disabled schedules never run on their own, but "run now" is an explicit request
  UPDATE schedule_settings SET last_run_at = now() - interval '1 day', enabled = false WHERE name IN ('collect', 'score');
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name IN ('collect', 'score')) = 0, 'disabled means disabled';
  UPDATE schedule_settings SET run_now = true WHERE name = 'collect';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'collect') = 1, 'run now overrides a disabled schedule, once';
  ASSERT NOT (SELECT run_now FROM schedule_settings WHERE name = 'collect'), 'run now is cleared when claimed';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'collect') = 0, 'and only runs once';
  UPDATE schedule_settings SET enabled = true;

  -- schedules whose feature is off are left alone, and fire promptly once it is back on
  UPDATE schedule_settings SET last_run_at = now() - interval '1 day' WHERE name = 'tailor';
  ASSERT NOT EXISTS (SELECT 1 FROM claim_due_schedules(ARRAY['tailor']) WHERE name = 'tailor'), 'a skipped schedule is not claimed';
  ASSERT (SELECT last_run_at FROM schedule_settings WHERE name = 'tailor') < now() - interval '23 hours', 'a skipped schedule keeps its old last_run_at';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'tailor') = 1, 'and is claimed as soon as it is no longer skipped';

  -- daily: once per local day, even if the exact minute was missed
  UPDATE schedule_settings SET last_run_at = now() - interval '2 days' WHERE name = 'digest';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 1, 'the digest is due when it has not run today';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 0, 'and not twice in a day';
  UPDATE schedule_settings SET at_time = '23:59:59' WHERE name = 'digest';
  UPDATE schedule_settings SET last_run_at = now() - interval '2 days' WHERE name = 'digest';
  IF (now() AT TIME ZONE (SELECT timezone FROM budget_settings))::time < '23:59:59' THEN
    ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 0, 'the digest waits for its time';
  END IF;
  UPDATE schedule_settings SET at_time = '00:00' WHERE name = 'digest';

  -- the daily time follows the configured timezone
  UPDATE budget_settings SET timezone = 'Pacific/Kiritimati';       -- UTC+14
  UPDATE schedule_settings SET at_time = ((now() AT TIME ZONE 'Pacific/Kiritimati')::time - interval '1 minute')::time,
         last_run_at = now() - interval '2 days' WHERE name = 'digest';
  IF (now() AT TIME ZONE 'Pacific/Kiritimati')::time > '00:01' THEN
    ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 1, 'due one minute after its time in the configured zone';
  END IF;
  UPDATE budget_settings SET timezone = 'Asia/Kolkata';
  UPDATE schedule_settings SET at_time = '00:00' WHERE name = 'digest';

  -- schedule_status: next run
  UPDATE schedule_settings SET enabled = true, last_run_at = now() - interval '1 hour' WHERE name = 'collect';
  SELECT next_run_at INTO nxt FROM schedule_status() WHERE name = 'collect';
  ASSERT abs(extract(epoch FROM nxt - (now() - interval '1 hour' + interval '360 minutes'))) < 2, 'next collect = last + 6 hours';
  UPDATE schedule_settings SET enabled = false WHERE name = 'score';
  ASSERT (SELECT next_run_at FROM schedule_status() WHERE name = 'score') IS NULL AND NOT (SELECT enabled FROM schedule_status() WHERE name = 'score');
  UPDATE schedule_settings SET last_run_at = now() WHERE name = 'digest';
  ASSERT (SELECT next_run_at FROM schedule_status() WHERE name = 'digest') > now(), 'after today''s digest the next one is in the future';
  ASSERT (SELECT array_agg(name) FROM schedule_status()) = ARRAY['collect', 'score', 'tailor', 'digest'], 'a stable order';

  -- the digest may be daily at a time OR every N hours (011), never both and never neither
  UPDATE schedule_settings SET every_minutes = 360, at_time = NULL WHERE name = 'digest';
  ASSERT (SELECT every_minutes FROM schedule_settings WHERE name = 'digest') = 360, 'the digest can run every 6 hours';
  UPDATE schedule_settings SET last_run_at = now() - interval '361 minutes' WHERE name = 'digest';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 1, 'an interval digest is due after its interval';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'digest') = 0;
  BEGIN UPDATE schedule_settings SET every_minutes = 30 WHERE name = 'digest'; RAISE EXCEPTION 'a digest more often than hourly accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET at_time = '08:00' WHERE name = 'digest'; RAISE EXCEPTION 'digest with both accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET every_minutes = NULL WHERE name = 'digest'; RAISE EXCEPTION 'digest with neither accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET every_minutes = NULL, at_time = '08:00' WHERE name = 'score'; RAISE EXCEPTION 'a daily score schedule accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE schedule_settings SET every_minutes = NULL, at_time = '00:00' WHERE name = 'digest';

  -- a schedule that has never run is due at the next tick, and says so
  UPDATE schedule_settings SET last_run_at = NULL, enabled = true WHERE name = 'collect';
  ASSERT (SELECT next_run_at FROM schedule_status() WHERE name = 'collect') <= now() + interval '2 seconds', 'never run means due now';

  -- tailoring options
  ASSERT (SELECT paper FROM tailor_settings) = 'a4' AND (SELECT style_notes FROM tailor_settings) IS NULL;
  UPDATE tailor_settings SET paper = 'letter', style_notes = 'Keep the tone warm but formal.';
  BEGIN UPDATE tailor_settings SET paper = 'a5'; RAISE EXCEPTION 'unknown paper accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET style_notes = repeat('x', 401); RAISE EXCEPTION 'overlong style notes accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE tailor_settings SET paper = 'a4', style_notes = NULL;
END $$;

-- Runaway guards (012): counted claims, refunds, parking, Apify caps, global pause ---------------
DO $$
DECLARE a bigint; b bigint; c bigint; n int; i int; r record;
BEGIN
  SELECT job_id INTO a FROM upsert_job('indeed','G1','u','x','Loop Test A','LoopCo','Pune',false,now(),'d a',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO b FROM upsert_job('indeed','G2','u','x','Loop Test B','LoopCo','Delhi',false,now(),'d b',NULL,NULL,NULL,'{}'::jsonb);
  SELECT job_id INTO c FROM upsert_job('indeed','G3','u','x','Loop Test C','LoopCo','Goa',false,now(),'d c',NULL,NULL,NULL,'{}'::jsonb);
  UPDATE jobs SET status = 'filtered_out' WHERE status = 'new' AND id NOT IN (a, b, c);     -- only our three can be claimed

  -- every claim is counted, so a job whose reply never comes back cannot be re-paid for forever
  FOR i IN 1..6 LOOP
    SELECT count(*) INTO n FROM claim_jobs_to_score(10) WHERE id = a;
    ASSERT n = 1, 'claim ' || i || ' should succeed';
    UPDATE jobs SET scoring_claimed_at = now() - interval '31 minutes' WHERE id = a;      -- the lease runs out, nothing was recorded
  END LOOP;
  ASSERT (SELECT score_claims FROM jobs WHERE id = a) = 6;
  ASSERT (SELECT count(*) FROM claim_jobs_to_score(10) WHERE id = a) = 0, 'after 6 unfinished claims the job is parked, not re-claimed';
  ASSERT (SELECT count(*) FROM v_parked_jobs WHERE stage = 'score' AND job_id = a) = 1, 'and it shows up as parked';
  UPDATE jobs SET scoring_claimed_at = NULL, score_claims = 0 WHERE id IN (b, c);           -- the loop above claimed them once as well
  ASSERT (SELECT count(*) FROM claim_jobs_to_score(10) WHERE id = b) = 1, 'other jobs are unaffected';

  -- an attempt that cost nothing is refunded, so an outage parks nobody
  UPDATE jobs SET score_claims = 0, scoring_claimed_at = NULL WHERE id = c;                 -- (the claim for b above claimed c too)
  FOR i IN 1..20 LOOP
    UPDATE jobs SET scoring_claimed_at = now() - interval '31 minutes' WHERE id = c;
    PERFORM 1 FROM claim_jobs_to_score(10) WHERE id = c;
    PERFORM release_score_claim(c);                                                       -- "the API was down"
  END LOOP;
  n := (SELECT score_claims FROM jobs WHERE id = c);
  ASSERT n = 0, '20 outages in a row left ' || n || ' claims counted on a healthy job';
  n := (SELECT count(*) FROM claim_jobs_to_score(10) WHERE id = c);
  ASSERT n = 1, 'a job refunded 20 times must still be claimable, got ' || n || ' (claims ' || (SELECT score_claims FROM jobs WHERE id = c) || ', status ' || (SELECT status FROM jobs WHERE id = c) || ')';
  ASSERT release_score_claim(c) = 0, 'a refund never goes below zero';

  -- parked jobs can be put back, one at a time or all of a stage
  ASSERT requeue_jobs('score', c) = 0, 'a job that is not parked is left alone';
  n := requeue_jobs('score', a);
  ASSERT n = 1, 'requeue puts the parked job back';
  ASSERT (SELECT score_claims FROM jobs WHERE id = a) = 0, 'and clears its claims';
  ASSERT (SELECT count(*) FROM claim_jobs_to_score(10) WHERE id = a) = 1, 'a requeued job is claimed again';
  UPDATE jobs SET score_attempts = 3 WHERE id = b;                                         -- three unusable replies parks it too
  ASSERT (SELECT count(*) FROM v_parked_jobs WHERE stage = 'score' AND job_id = b) = 1;
  n := requeue_jobs('score');
  ASSERT n >= 1, 'requeue with no id resets every parked job of the stage';
  ASSERT (SELECT score_attempts FROM jobs WHERE id = b) = 0, 'including its bad-reply strikes';
  BEGIN PERFORM requeue_jobs('nonsense'); RAISE EXCEPTION 'unknown stage accepted';
  EXCEPTION WHEN raise_exception THEN ASSERT SQLERRM = 'stage must be score or tailor', SQLERRM; END;

  -- a scam-blocked job is never "parked" (it is never processed in the first place)
  UPDATE jobs SET status = 'new', scam_risk = 'high', score_claims = 9 WHERE id = c;
  ASSERT (SELECT count(*) FROM v_parked_jobs WHERE job_id = c) = 0;
  UPDATE jobs SET scam_risk = NULL, score_claims = 0 WHERE id = c;

  -- tailoring: at most 3 claims, with refunds for attempts that cost nothing
  UPDATE jobs SET status = 'shortlisted', score = 90, tailor_claims = 0, tailor_attempts = 0, tailoring_claimed_at = NULL WHERE id = a;
  FOR i IN 1..3 LOOP
    ASSERT (SELECT count(*) FROM claim_jobs_to_tailor(10, 0) WHERE id = a) = 1, 'tailor claim ' || i;
    UPDATE jobs SET tailoring_claimed_at = now() - interval '21 minutes' WHERE id = a;
  END LOOP;
  ASSERT (SELECT count(*) FROM claim_jobs_to_tailor(10, 0) WHERE id = a) = 0, 'a job whose drafts keep failing is claimed 3 times, then parked';
  ASSERT (SELECT count(*) FROM v_parked_jobs WHERE stage = 'tailor' AND job_id = a) = 1;
  n := requeue_jobs('tailor', a);
  ASSERT n = 1, 'a parked tailor job can be requeued';
  n := (SELECT count(*) FROM claim_jobs_to_tailor(10, 0) WHERE id = a);
  ASSERT n = 1, 'and is claimed again';
  PERFORM release_tailor_claim(a);
  ASSERT (SELECT tailor_claims FROM jobs WHERE id = a) = 0, 'a refunded tailor claim is not counted';
  UPDATE jobs SET status = 'new' WHERE id = a;

  -- Apify has its own caps, separate from Claude's
  DELETE FROM llm_usage WHERE provider = 'apify';                                          -- (earlier blocks logged some)
  SELECT * INTO r FROM apify_budget_status();
  ASSERT r.monthly_cap = 15 AND r.daily_cap = 0.5 AND NOT r.blocked, 'Apify defaults: $15 a month, $0.50 a day';
  PERFORM record_usage('anthropic', 'score', 'claude-haiku-5-5', NULL, 1000, 100);
  ASSERT (SELECT month_spent FROM apify_budget_status()) = 0, 'Claude spend does not count against Apify';
  PERFORM record_usage('apify', 'search', 'curious', NULL, 0, 0, 0, 0, true, 'x', 0.30);
  SELECT * INTO r FROM apify_budget_status();
  ASSERT r.month_spent = 0.30 AND r.day_remaining = 0.20 AND NOT r.blocked;
  PERFORM record_usage('apify', 'search', 'curious', NULL, 0, 0, 0, 0, true, 'x', 0.25);
  SELECT * INTO r FROM apify_budget_status();
  ASSERT r.blocked AND r.blocked_reason = 'Apify daily cap reached' AND r.day_remaining = 0;
  ASSERT NOT (SELECT blocked FROM budget_status()), 'and Claude is not blocked by Apify spend';
  UPDATE budget_settings SET apify_daily_cap_usd = 100, apify_monthly_cap_usd = 0.5;
  ASSERT (SELECT blocked_reason FROM apify_budget_status()) = 'Apify monthly cap reached';
  BEGIN UPDATE budget_settings SET apify_monthly_cap_usd = -1; RAISE EXCEPTION 'negative cap accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE budget_settings SET apify_daily_cap_usd = 0.5, apify_monthly_cap_usd = 15;

  -- the global pause stops everything, including "run now"
  UPDATE schedule_settings SET last_run_at = NULL, run_now = false, enabled = true;
  UPDATE schedule_settings SET at_time = '00:00', every_minutes = NULL WHERE name = 'digest';
  UPDATE schedule_control SET paused = true, paused_at = now(), reason = 'testing';
  UPDATE schedule_settings SET run_now = true WHERE name = 'collect';
  ASSERT (SELECT count(*) FROM claim_due_schedules()) = 0, 'paused: nothing starts, not even a run-now request';
  ASSERT (SELECT run_now FROM schedule_settings WHERE name = 'collect'), 'the request waits until resumed';
  UPDATE schedule_control SET paused = false;
  ASSERT (SELECT count(*) FROM claim_due_schedules()) = 4, 'resumed: everything due starts again';
END $$;

-- Company boards and remote feeds (013): settings columns, fetch_state, claim_fetch, record_fetch ---------------
DO $$
DECLARE r record; a record; b record; n int; ok boolean; backdated timestamptz;
BEGIN
  -- new settings columns have empty defaults
  SELECT boards, board_title_words INTO r FROM search_profile WHERE name = 'default';
  ASSERT r.boards = '{}'::jsonb, 'boards defaults to an empty object';
  ASSERT r.board_title_words = '{}'::text[], 'board_title_words defaults to an empty array';
  ASSERT (SELECT board_batch FROM schedule_settings WHERE name = 'collect') = 30, 'board_batch defaults to 30';
  BEGIN UPDATE schedule_settings SET board_batch = 0; RAISE EXCEPTION 'board_batch 0 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET board_batch = 201; RAISE EXCEPTION 'board_batch 201 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE schedule_settings SET board_batch = 200 WHERE name = 'collect';
  UPDATE schedule_settings SET board_batch = 1 WHERE name = 'collect';
  UPDATE schedule_settings SET board_batch = 30 WHERE name = 'collect';

  -- claim_fetch: the first caller wins, the second is told to wait, and it opens again after the interval
  ok := claim_fetch('remotive', '', interval '6 hours');
  ASSERT ok, 'the first claim of a never-fetched source succeeds';
  ok := claim_fetch('remotive', '', interval '6 hours');
  ASSERT NOT ok, 'a second claim inside the interval is refused';
  ASSERT (SELECT count(*) FROM fetch_state WHERE source = 'remotive' AND key = '') = 1, 'one row per (source, key)';
  UPDATE fetch_state SET last_fetched_at = now() - interval '7 hours' WHERE source = 'remotive' AND key = '';
  ok := claim_fetch('remotive', '', interval '6 hours');
  ASSERT ok, 'after the interval has passed the source can be claimed again';
  ok := claim_fetch('remotive', '', interval '6 hours');
  ASSERT NOT ok, 'and the claim restarts the clock';
  ok := claim_fetch('greenhouse', 'acme', interval '6 hours');
  ASSERT ok, 'a different key is independent';
  ok := claim_fetch('remotive', 'other', interval '6 hours');
  ASSERT ok, 'a different source/key pair is independent';
  UPDATE fetch_state SET last_fetched_at = NULL WHERE source = 'greenhouse' AND key = 'acme';
  ok := claim_fetch('greenhouse', 'acme', interval '6 hours');
  ASSERT ok, 'a row that was never fetched (NULL) can be claimed';

  -- a REFUSED claim must leave the clock alone: a fetch that is an hour old stays an hour old
  PERFORM claim_fetch('lever', 'slow', interval '6 hours');
  backdated := now() - interval '1 hour';
  UPDATE fetch_state SET last_fetched_at = backdated WHERE source = 'lever' AND key = 'slow';
  ok := claim_fetch('lever', 'slow', interval '6 hours');
  ASSERT NOT ok, 'a claim 1 hour after the last fetch is refused when the interval is 6 hours';
  ASSERT (SELECT last_fetched_at FROM fetch_state WHERE source = 'lever' AND key = 'slow') = backdated,
         'a refused claim must not move last_fetched_at';
  -- interval 0: always open, and each claim restarts the clock
  ok := claim_fetch('lever', 'slow', interval '0 seconds');
  ASSERT ok, 'a zero interval is always claimable';
  ASSERT (SELECT last_fetched_at FROM fetch_state WHERE source = 'lever' AND key = 'slow') > backdated,
         'a successful claim moves last_fetched_at forward';
  ok := claim_fetch('lever', 'slow', interval '0 seconds');
  ASSERT ok, 'a zero interval stays claimable, however often it is asked';

  -- record_fetch: failures count up, one success resets them, the last good result is kept across failures
  PERFORM record_fetch('remotive', '', false, NULL, 'HTTP 500');
  PERFORM record_fetch('remotive', '', false, NULL, 'HTTP 503');
  SELECT * INTO r FROM fetch_state WHERE source = 'remotive' AND key = '';
  ASSERT r.failures = 2, 'two failures in a row, got ' || r.failures;
  ASSERT r.last_error = 'HTTP 503', 'the latest error is kept';
  ASSERT r.last_ok_at IS NULL, 'no success yet';
  PERFORM record_fetch('remotive', '', true, 42, NULL);
  SELECT * INTO r FROM fetch_state WHERE source = 'remotive' AND key = '';
  ASSERT r.failures = 0, 'a success resets the failure count';
  ASSERT r.last_ok_at IS NOT NULL AND r.last_count = 42, 'a success records when and how many';
  PERFORM record_fetch('remotive', '', false, NULL, 'HTTP 502');
  SELECT * INTO r FROM fetch_state WHERE source = 'remotive' AND key = '';
  ASSERT r.failures = 1 AND r.last_error = 'HTTP 502', 'a failure after a success starts at 1';
  ASSERT r.last_ok_at IS NOT NULL AND r.last_count = 42, 'a failure keeps the previous success';
  PERFORM record_fetch('remotive', '', false, NULL, repeat('x', 1000));
  ASSERT (SELECT length(last_error) FROM fetch_state WHERE source = 'remotive' AND key = '') = 300, 'errors are cut to 300 characters';
  PERFORM record_fetch('lever', 'newco', true, 7, NULL);
  ASSERT (SELECT failures = 0 AND last_count = 7 AND last_error IS NULL FROM fetch_state WHERE source = 'lever' AND key = 'newco'),
         'record_fetch creates the row when the source was never claimed';

  -- the same job on a company board and on LinkedIn is ONE job with two sources
  SELECT * INTO a FROM upsert_job('greenhouse','acme:1','https://boards.greenhouse.io/acme/jobs/1',NULL,'Backend Engineer','Acme Corp',
       'Pune, India',false,now(),'Build APIs for Acme.',NULL,NULL,NULL,'{}'::jsonb);
  SELECT * INTO b FROM upsert_job('linkedin','999','https://linkedin.com/jobs/view/999',NULL,'Backend Engineer','ACME Corp.',
       'Pune, India',false,now(),'Build APIs for Acme.',NULL,NULL,NULL,'{}'::jsonb);
  ASSERT a.is_new AND NOT b.is_new, 'the board listing created the job; the aggregator listing only joined it';
  ASSERT a.job_id = b.job_id, 'board and aggregator listings of one job are one job';
  ASSERT (SELECT count(*) FROM job_sources WHERE job_id = a.job_id) = 2, 'with both listings kept as sources';
  ASSERT (SELECT count(DISTINCT source) FROM job_sources WHERE job_id = a.job_id) = 2;
END $$;

-- Dashboard (014): views, summary, the guarded status change, file paths, the low-privilege role ----------------
-- A fixed data set: one job in every pipeline state the dashboard shows. Earlier blocks' jobs are cleared first
-- (the whole file is rolled back at the end).
CREATE FUNCTION pg_temp.dash_job(p_n int, p_status text, p_score int) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_id bigint;
BEGIN
  SELECT job_id INTO v_id FROM upsert_job('indeed', 'D' || p_n, 'https://example.com/jobs/' || p_n, NULL,
       'Dashboard Role ' || p_n, 'DashCo ' || p_n, 'City ' || p_n, false, now(), 'description ' || p_n,
       NULL, NULL, NULL, '{}'::jsonb);
  UPDATE jobs SET status = p_status, score = p_score WHERE id = v_id;
  RETURN v_id;
END $$;

CREATE TEMP TABLE dash_ids (name text PRIMARY KEY, id bigint NOT NULL);

DO $$
DECLARE r record; s jsonb; f jsonb; jt bigint; jsb bigint; jback bigint;
BEGIN
  TRUNCATE jobs CASCADE;
  UPDATE search_profile SET score_threshold = 60 WHERE name = 'default';

  INSERT INTO dash_ids VALUES ('new',          pg_temp.dash_job(1,  'new',          NULL)),
                              ('filtered',     pg_temp.dash_job(2,  'filtered_out', 74)),   -- the scorer also scores jobs the filters exclude
                              ('scam',         pg_temp.dash_job(3,  'scam_blocked', NULL)),
                              ('scored',       pg_temp.dash_job(4,  'scored',       40)),
                              ('short_new',    pg_temp.dash_job(5,  'shortlisted',  80)),
                              ('short_old',    pg_temp.dash_job(6,  'shortlisted',  65)),
                              ('tailored',     pg_temp.dash_job(7,  'tailored',     90)),
                              ('applied',      pg_temp.dash_job(8,  'shortlisted',  70)),
                              ('interview',    pg_temp.dash_job(9,  'shortlisted',  75)),
                              ('offer',        pg_temp.dash_job(10, 'shortlisted',  85)),
                              ('rejected',     pg_temp.dash_job(11, 'shortlisted',  61)),
                              ('withdrawn',    pg_temp.dash_job(12, 'shortlisted',  62)),
                              ('in_progress',  pg_temp.dash_job(13, 'shortlisted',  63)),
                              ('skipped',      pg_temp.dash_job(14, 'shortlisted',  64)),
                              ('moved_back',   pg_temp.dash_job(15, 'shortlisted',  66));

  SELECT id INTO jt FROM dash_ids WHERE name = 'tailored';
  SELECT id INTO jsb FROM dash_ids WHERE name = 'scam';
  SELECT id INTO jback FROM dash_ids WHERE name = 'moved_back';

  INSERT INTO applications (job_id, status, resume_path) VALUES (jt, 'drafted', '/output/applications/7-dashco/resume.pdf');
  PERFORM set_application_status(id, name) FROM dash_ids
    WHERE name IN ('applied', 'interview', 'offer', 'rejected', 'withdrawn', 'in_progress', 'skipped');
  PERFORM set_application_status(jback, 'applied');
  PERFORM set_application_status(jback, 'approved');           -- moved back: applied_at is kept
  PERFORM set_application_status(jsb, 'withdrawn');            -- recorded on a job that is scam-blocked

  UPDATE jobs SET first_seen = now() - interval '10 days' WHERE id = (SELECT id FROM dash_ids WHERE name = 'short_old');
  UPDATE jobs SET deadline = current_date + 3  WHERE id = (SELECT id FROM dash_ids WHERE name = 'short_new');
  UPDATE jobs SET deadline = current_date + 30 WHERE id = (SELECT id FROM dash_ids WHERE name = 'short_old');
  UPDATE jobs SET deadline = current_date - 1  WHERE id = (SELECT id FROM dash_ids WHERE name = 'scored');

  -- the combined status: the application's when there is one, scam-blocked always wins
  FOR r IN SELECT d.name, v.pipeline_status FROM dash_ids d JOIN v_dashboard_jobs v ON v.id = d.id LOOP
    ASSERT r.pipeline_status = CASE r.name
             WHEN 'new' THEN 'new' WHEN 'filtered' THEN 'filtered_out' WHEN 'scam' THEN 'scam_blocked'
             WHEN 'scored' THEN 'scored' WHEN 'short_new' THEN 'shortlisted' WHEN 'short_old' THEN 'shortlisted'
             WHEN 'tailored' THEN 'drafted' WHEN 'applied' THEN 'applied' WHEN 'interview' THEN 'interview'
             WHEN 'offer' THEN 'offer' WHEN 'rejected' THEN 'rejected' WHEN 'withdrawn' THEN 'withdrawn'
             WHEN 'in_progress' THEN 'in_progress' WHEN 'skipped' THEN 'skipped' WHEN 'moved_back' THEN 'approved'
           END, format('pipeline_status of %s is %s', r.name, r.pipeline_status);
  END LOOP;
  ASSERT (SELECT count(*) FROM v_dashboard_jobs) = 15, 'one row per job';
  ASSERT (SELECT job_status FROM v_dashboard_jobs WHERE id = (SELECT id FROM dash_ids WHERE name = 'interview')) = 'applied',
         'jobs.status alone cannot tell an interview from a rejection, which is why the view exists';

  -- files, flags and dates
  SELECT * INTO r FROM v_dashboard_jobs WHERE id = jt;
  ASSERT r.has_resume AND NOT r.has_cover_letter AND NOT r.needs_attention, 'file flags follow the application';
  ASSERT NOT (SELECT has_resume FROM v_dashboard_jobs WHERE id = (SELECT id FROM dash_ids WHERE name = 'new')),
         'no application, no files';
  ASSERT (SELECT closing_soon FROM v_dashboard_jobs WHERE id = (SELECT id FROM dash_ids WHERE name = 'short_new')), 'deadline in 3 days is closing soon';
  ASSERT NOT (SELECT closing_soon FROM v_dashboard_jobs WHERE id = (SELECT id FROM dash_ids WHERE name = 'short_old')), 'deadline in 30 days is not';
  ASSERT NOT (SELECT closing_soon FROM v_dashboard_jobs WHERE id = (SELECT id FROM dash_ids WHERE name = 'scored')), 'a passed deadline is not';

  -- the summary, against a hand count of the data set above
  s := dashboard_summary();
  ASSERT (s->>'threshold')::int = 60, 'threshold of the default profile';
  ASSERT s->'cards' = '{"found": 15, "shortlisted": 2, "new_this_week": 1, "applied": 8, "in_progress": 1,
                        "interview": 1, "offer": 1, "rejected": 1, "withdrawn": 2}'::jsonb, 'cards: ' || (s->'cards')::text;
  ASSERT s->'by_status' = '{"applied": 1, "in_progress": 1, "interview": 1, "offer": 1, "rejected": 1,
                            "withdrawn": 2, "not_applied": 2}'::jsonb, 'by_status: ' || (s->'by_status')::text;
  -- nested steps: the filtered-out job (score 74) and the scam-blocked one (a withdrawn application) count in
  -- "found" only, so "applied" is 7 here although the card says 8, and no step is ever larger than the one above
  ASSERT s->'funnel' = '[{"step": "found", "count": 15}, {"step": "passed_filters", "count": 13},
                         {"step": "scored", "count": 12}, {"step": "shortlisted", "count": 11},
                         {"step": "applied", "count": 7}, {"step": "interview", "count": 2},
                         {"step": "offer", "count": 1}]'::jsonb, 'funnel: ' || (s->'funnel')::text;
  ASSERT (s->>'generated_at')::timestamptz <= now() + interval '1 second', 'generated_at is a timestamp';

  -- the funnel's shortlisted step follows today's threshold; no profile means no shortlisted step
  UPDATE search_profile SET score_threshold = 70 WHERE name = 'default';
  f := dashboard_summary()->'funnel';
  ASSERT (f->3->>'count')::int = 10, 'at threshold 70: the scores 80, 90, 70, 75 and 85, plus the five jobs with an application below it, got ' || (f->3)::text;
  UPDATE search_profile SET name = 'renamed' WHERE name = 'default';
  s := dashboard_summary();
  ASSERT s->'threshold' = 'null'::jsonb AND (s->'funnel'->3->>'count')::int = 9, 'no default profile: only jobs with an application, got ' || (s->'funnel'->3)::text;
  UPDATE search_profile SET name = 'default', score_threshold = 60 WHERE name = 'renamed';
END $$;

-- Apply links: canonical_url when it is http(s), else the earliest listing's http(s) link, else nothing
DO $$
DECLARE jn bigint; js bigint; jo bigint;
BEGIN
  SELECT id INTO jn FROM dash_ids WHERE name = 'short_new';
  SELECT id INTO js FROM dash_ids WHERE name = 'short_old';
  SELECT id INTO jo FROM dash_ids WHERE name = 'new';
  ASSERT (SELECT apply_url FROM v_dashboard_jobs WHERE id = js) = (SELECT canonical_url FROM jobs WHERE id = js),
         'an http canonical_url is the apply link';

  UPDATE jobs SET canonical_url = 'javascript:alert(1)' WHERE id = jn;
  INSERT INTO job_sources (job_id, source, source_job_id, url, apply_url, first_seen)
  VALUES (jn, 'lever', 'dash-lever-1', 'ftp://example.com/x', 'mailto:jobs@example.com', now() - interval '1 day');
  ASSERT (SELECT apply_url FROM v_dashboard_jobs WHERE id = jn) = 'https://example.com/jobs/5',
         'a non-http canonical_url and non-http listing links are skipped, got ' || coalesce((SELECT apply_url FROM v_dashboard_jobs WHERE id = jn), 'NULL');
  ASSERT (SELECT sources FROM v_dashboard_jobs WHERE id = jn) = '{indeed,lever}'::text[], 'sources are listed once each, sorted';

  UPDATE jobs SET canonical_url = 'data:text/html,x' WHERE id = jo;
  UPDATE job_sources SET url = 'javascript:void(0)', apply_url = NULL WHERE job_id = jo;
  ASSERT (SELECT apply_url FROM v_dashboard_jobs WHERE id = jo) IS NULL, 'no http(s) link anywhere: no apply link';
  ASSERT (SELECT sources FROM v_dashboard_jobs WHERE id = jo) = '{indeed}'::text[];

  -- the detail views add fields but never the stored file paths or the raw scraped payload
  ASSERT (SELECT description FROM v_dashboard_job_detail WHERE id = jn) = 'description 5';
  ASSERT (SELECT variant FROM v_dashboard_job_detail WHERE id = (SELECT id FROM dash_ids WHERE name = 'applied')) = 'base';
  ASSERT (SELECT count(*) FROM v_dashboard_job_sources WHERE job_id = jn) = 2;
  ASSERT NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name IN ('v_dashboard_jobs', 'v_dashboard_job_detail', 'v_dashboard_job_sources')
                        AND column_name IN ('resume_path', 'cover_letter_path', 'files_dir', 'raw_json', 'tailor_patch')),
         'no view exposes file paths or raw payloads';
END $$;

-- dashboard_set_status: the check, the write, and every refusal
DO $$
DECLARE js bigint; jt bigint; jsb bigint; ji bigint; st text; d text;
BEGIN
  SELECT id INTO js FROM dash_ids WHERE name = 'short_old';
  SELECT id INTO jt FROM dash_ids WHERE name = 'tailored';
  SELECT id INTO jsb FROM dash_ids WHERE name = 'scam';
  SELECT id INTO ji FROM dash_ids WHERE name = 'interview';

  st := dashboard_set_status(js, 'applied', 'shortlisted', 'applied on the company site');
  ASSERT st = 'applied', 'returns the new status';
  ASSERT (SELECT status FROM applications WHERE job_id = js) = 'applied' AND (SELECT status FROM jobs WHERE id = js) = 'applied',
         'both tables are written, as by set_application_status';
  ASSERT (SELECT applied_at IS NOT NULL AND review_note = 'applied on the company site' FROM applications WHERE job_id = js);
  ASSERT (SELECT pipeline_status FROM v_dashboard_jobs WHERE id = js) = 'applied';

  -- stale: the browser still shows "shortlisted"; nothing changes and the current status comes back
  BEGIN
    PERFORM dashboard_set_status(js, 'interview', 'shortlisted');
    RAISE EXCEPTION 'a stale expected status was accepted';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN
    GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL;
    ASSERT d = 'applied', 'DETAIL carries the current status, got ' || coalesce(d, 'NULL');
  END;
  ASSERT (SELECT status FROM applications WHERE job_id = js) = 'applied', 'a refused change writes nothing';
  BEGIN PERFORM dashboard_set_status(js, 'interview', NULL); RAISE EXCEPTION 'a missing expected status was accepted';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;

  -- a draft compares against the application's status, not the job's
  ASSERT dashboard_set_status(jt, 'approved', 'drafted') = 'approved';
  BEGIN PERFORM dashboard_set_status(jt, 'applied', 'tailored'); RAISE EXCEPTION 'the job status was used as expected';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;

  -- notes: trimmed, blank keeps the old one, too long is refused
  PERFORM dashboard_set_status(ji, 'offer', 'interview', '  final round done  ');
  ASSERT (SELECT review_note FROM applications WHERE job_id = ji) = 'final round done', 'the note is trimmed';
  PERFORM dashboard_set_status(ji, 'rejected', 'offer', '   ');
  ASSERT (SELECT review_note FROM applications WHERE job_id = ji) = 'final round done', 'a blank note keeps the old one';
  BEGIN PERFORM dashboard_set_status(ji, 'withdrawn', 'rejected', repeat('x', 501)); RAISE EXCEPTION 'a 501-character note was accepted';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  PERFORM dashboard_set_status(ji, 'withdrawn', 'rejected', repeat('x', 500));

  -- refusals
  BEGIN PERFORM dashboard_set_status(jsb, 'applied', 'scam_blocked'); RAISE EXCEPTION 'a scam-blocked job was changed';
  EXCEPTION WHEN SQLSTATE 'JD403' THEN NULL; END;
  BEGIN PERFORM dashboard_set_status(jsb, 'applied', 'withdrawn'); RAISE EXCEPTION 'a scam-blocked job was changed';
  EXCEPTION WHEN SQLSTATE 'JD403' THEN NULL; END;
  ASSERT (SELECT status FROM applications WHERE job_id = jsb) = 'withdrawn';
  BEGIN PERFORM dashboard_set_status(js, 'hired', 'applied'); RAISE EXCEPTION 'unknown status accepted';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_set_status(js, 'drafted', 'applied'); RAISE EXCEPTION 'drafted can only come from tailoring';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_set_status(js, NULL, 'applied'); RAISE EXCEPTION 'NULL status accepted';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_set_status(-1, 'applied', 'new'); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;

  -- file paths, only through the function
  ASSERT dashboard_application_file(jt, 'resume') = '/output/applications/7-dashco/resume.pdf';
  ASSERT dashboard_application_file(jt, 'cover_letter') IS NULL, 'no cover letter stored';
  ASSERT dashboard_application_file(js, 'resume') IS NULL, 'a manual application has no files';
  ASSERT dashboard_application_file((SELECT id FROM dash_ids WHERE name = 'new'), 'resume') IS NULL, 'no application at all';
  BEGIN PERFORM dashboard_application_file(jt, '../../etc/passwd'); RAISE EXCEPTION 'unknown file kind accepted';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
END $$;

-- The role: the three views and the three functions work; the tables, and the functions behind them, do not
DO $$
BEGIN
  ASSERT (SELECT 'statement_timeout=10s' = ANY(rolconfig) FROM pg_roles WHERE rolname = 'jobsearch_dashboard'),
         'the role has a statement timeout';
  ASSERT has_database_privilege('jobsearch_dashboard', current_database(), 'CONNECT');
  ASSERT NOT has_table_privilege('jobsearch_dashboard', 'jobs', 'SELECT');
  ASSERT NOT has_table_privilege('jobsearch_dashboard', 'v_dashboard_jobs', 'UPDATE');
  ASSERT NOT has_function_privilege('public', 'dashboard_set_status(bigint,text,text,text)', 'EXECUTE'),
         'other roles cannot run the dashboard functions';
  ASSERT NOT has_function_privilege('public', 'dashboard_summary()', 'EXECUTE');
  ASSERT NOT has_function_privilege('public', 'dashboard_application_file(bigint,text)', 'EXECUTE');
END $$;

SET LOCAL ROLE jobsearch_dashboard;
DO $$
DECLARE n int; js bigint;
BEGIN
  SELECT count(*) INTO n FROM v_dashboard_jobs;
  ASSERT n = 15, 'the role reads the list view';
  PERFORM 1 FROM v_dashboard_job_detail LIMIT 1;
  PERFORM 1 FROM v_dashboard_job_sources LIMIT 1;
  ASSERT (dashboard_summary()->'cards'->>'found')::int = 15, 'the role runs the summary';
  SELECT id INTO js FROM v_dashboard_jobs WHERE pipeline_status = 'shortlisted' LIMIT 1;
  ASSERT dashboard_set_status(js, 'skipped', 'shortlisted') = 'skipped', 'the role changes a status through the guarded function';
  PERFORM dashboard_application_file(js, 'resume');

  BEGIN PERFORM 1 FROM jobs LIMIT 1; RAISE EXCEPTION 'the role read jobs';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM applications LIMIT 1; RAISE EXCEPTION 'the role read applications';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM job_sources LIMIT 1; RAISE EXCEPTION 'the role read job_sources';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM llm_usage LIMIT 1; RAISE EXCEPTION 'the role read llm_usage';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM set_application_status(js, 'applied'); RAISE EXCEPTION 'the role bypassed the guarded function';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE budget_settings SET monthly_cap_usd = 1000; RAISE EXCEPTION 'the role changed the budget';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN DELETE FROM v_dashboard_job_sources; RAISE EXCEPTION 'the role deleted through a view';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET ROLE;

-- An empty database: zeros everywhere, nothing breaks
DO $$
DECLARE s jsonb;
BEGIN
  TRUNCATE jobs CASCADE;
  s := dashboard_summary();
  ASSERT (s->'cards'->>'found')::int = 0 AND (s->'cards'->>'applied')::int = 0, 'empty: zero cards';
  ASSERT (SELECT bool_and((e->>'count')::int = 0) FROM jsonb_array_elements(s->'funnel') e), 'empty: zero funnel';
  ASSERT (SELECT count(*) FROM v_dashboard_jobs) = 0;
END $$;

-- 015: re-shortlisting after the threshold was lowered -------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; n int;
BEGIN
  TRUNCATE jobs CASCADE;
  UPDATE search_profile SET score_threshold = 60 WHERE name = 'default';
  a := pg_temp.dash_job(101, 'scored', 65);          -- scored under an older, higher threshold: promoted
  b := pg_temp.dash_job(102, 'scored', 59);          -- below the threshold: stays
  c := pg_temp.dash_job(103, 'filtered_out', 74);    -- a gate excluded it: stays out whatever its score
  d := pg_temp.dash_job(104, 'scored', 70);          -- high scam risk: stays
  UPDATE jobs SET scam_risk = 'high' WHERE id = d;
  e := pg_temp.dash_job(105, 'scored', 80);          -- already has an application: left alone
  INSERT INTO applications (job_id, status) VALUES (e, 'skipped');
  f := pg_temp.dash_job(106, 'shortlisted', 90);     -- already shortlisted: not touched
  ASSERT (SELECT array_agg(id) FROM v_reshortlist) = ARRAY[a], 'only the plain scored job above the threshold';
  n := reshortlist();
  ASSERT n = 1, 'one job promoted, got ' || n;
  ASSERT (SELECT status FROM jobs WHERE id = a) = 'shortlisted';
  ASSERT (SELECT status FROM jobs WHERE id = b) = 'scored' AND (SELECT status FROM jobs WHERE id = c) = 'filtered_out'
     AND (SELECT status FROM jobs WHERE id = d) = 'scored' AND (SELECT status FROM jobs WHERE id = e) = 'scored', 'the others are untouched';
  ASSERT reshortlist() = 0, 'running it again changes nothing';
  UPDATE search_profile SET score_threshold = 50 WHERE name = 'default';
  ASSERT (SELECT array_agg(id ORDER BY id) FROM v_reshortlist) = ARRAY[b], 'a lower threshold lists the 59';
  UPDATE search_profile SET score_threshold = 60 WHERE name = 'default';
END $$;

-- 016: the dashboard's health and spend panels, and the pause switch --------------------------------------------------
DO $$
DECLARE h jsonb; s jsonb; r jsonb; j bigint; jj bigint; codes text[];
BEGIN
  TRUNCATE jobs CASCADE;
  DELETE FROM llm_usage;
  DELETE FROM fetch_state;
  UPDATE schedule_control SET paused = false, paused_at = NULL, reason = NULL;
  UPDATE budget_settings SET monthly_cap_usd = 10, daily_cap_usd = 1.5, warn_pct = 80, apify_monthly_cap_usd = 15, apify_daily_cap_usd = 0.5;
  UPDATE schedule_settings SET enabled = true, last_run_at = now();      -- everything ran a moment ago
  UPDATE feature_settings SET tailoring = true, telegram = true, digest = true;

  -- a quiet system: no alerts, all four schedules listed in their usual order, nothing waiting
  h := dashboard_health();
  ASSERT h->'alerts' = '[]'::jsonb, 'quiet system has no alerts, got ' || (h->'alerts')::text;
  ASSERT (SELECT array_agg(e->>'name') FROM jsonb_array_elements(h->'schedules') e) = ARRAY['collect', 'score', 'tailor', 'digest'];
  ASSERT (h->'paused'->>'paused')::boolean = false AND (h->'queue'->>'waiting_to_score')::int = 0;
  ASSERT (h->'parked'->>'total')::int = 0 AND (h->'sources'->>'tracked')::int = 0 AND (h->>'timezone') = 'Asia/Kolkata';

  -- pause and resume: the same one row the scraper's switch uses
  r := dashboard_set_pause(true, '  maintenance  ');
  ASSERT (r->>'paused')::boolean AND r->>'reason' = 'maintenance' AND r->>'paused_at' IS NOT NULL, 'paused: ' || r::text;
  ASSERT (SELECT paused AND reason = 'maintenance' FROM schedule_control WHERE id = 1), 'the shared row changed';
  h := dashboard_health();
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['paused'], 'a pause is an alert';
  ASSERT (SELECT e->>'level' FROM jsonb_array_elements(h->'alerts') e LIMIT 1) = 'bad';
  ASSERT (SELECT count(*) FROM claim_due_schedules()) = 0, 'while paused nothing is claimed (existing behaviour)';
  r := dashboard_set_pause(true, repeat('x', 300));
  ASSERT length(r->>'reason') = 200, 'the reason is cut to 200 characters';
  r := dashboard_set_pause(true, '   ');
  ASSERT r->'reason' = 'null'::jsonb, 'a blank reason is none';
  r := dashboard_set_pause(false, 'ignored on resume');
  ASSERT NOT (r->>'paused')::boolean AND r->'paused_at' = 'null'::jsonb AND r->'reason' = 'null'::jsonb, 'resumed: ' || r::text;
  BEGIN PERFORM dashboard_set_pause(NULL, 'x'); RAISE EXCEPTION 'NULL accepted';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;

  -- overdue schedules: only when enabled, not paused, and over 30 minutes late
  UPDATE schedule_settings SET every_minutes = 10, at_time = NULL, last_run_at = now() - interval '2 hours' WHERE name = 'score';
  h := dashboard_health();
  ASSERT (SELECT (e->>'overdue')::boolean FROM jsonb_array_elements(h->'schedules') e WHERE e->>'name' = 'score'), 'score is overdue';
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['schedule_overdue'];
  UPDATE schedule_settings SET last_run_at = now() - interval '20 minutes' WHERE name = 'score';
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'ten minutes late is not overdue';
  UPDATE schedule_settings SET last_run_at = now() - interval '2 hours' WHERE name = 'score';
  UPDATE schedule_settings SET enabled = false WHERE name = 'score';
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'a disabled schedule is never overdue';
  UPDATE schedule_settings SET enabled = true WHERE name = 'score';
  ASSERT (SELECT e->>'message' FROM jsonb_array_elements(dashboard_health()->'alerts') e) LIKE '%Asia/Kolkata%',
         'the overdue message names the configured time zone';
  PERFORM dashboard_set_pause(true, NULL);
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(dashboard_health()->'alerts') e) = ARRAY['paused'], 'paused: late is expected';
  PERFORM dashboard_set_pause(false, NULL);
  UPDATE schedule_settings SET last_run_at = now();

  -- the scheduler skips tailor and digest while their feature is off (scraper/app.py schedule_tick), so a stale last
  -- run there is normal: flagged as feature_off, never an alert; only collect and score can be overdue
  UPDATE schedule_settings SET last_run_at = now() - interval '26 hours' WHERE name IN ('tailor', 'digest');
  UPDATE schedule_settings SET enabled = true WHERE name IN ('tailor', 'digest');
  UPDATE feature_settings SET tailoring = false, digest = true, telegram = true;
  h := dashboard_health();
  ASSERT h->'alerts' = '[]'::jsonb, 'stale tailor and digest are not alerts: ' || (h->'alerts')::text;
  ASSERT (SELECT (e->>'feature_off')::boolean FROM jsonb_array_elements(h->'schedules') e WHERE e->>'name' = 'tailor'), 'tailoring off';
  ASSERT NOT (SELECT (e->>'feature_off')::boolean FROM jsonb_array_elements(h->'schedules') e WHERE e->>'name' = 'digest'), 'digest is on';
  UPDATE feature_settings SET tailoring = true, digest = false;
  ASSERT (SELECT (e->>'feature_off')::boolean FROM jsonb_array_elements(h->'schedules') e WHERE e->>'name' = 'tailor') IS NOT NULL;
  h := dashboard_health();
  ASSERT (SELECT (e->>'feature_off')::boolean FROM jsonb_array_elements(h->'schedules') e WHERE e->>'name' = 'digest'), 'digest switched off';
  UPDATE feature_settings SET digest = true, telegram = false;
  ASSERT (SELECT (e->>'feature_off')::boolean FROM jsonb_array_elements(dashboard_health()->'schedules') e WHERE e->>'name' = 'digest'), 'the digest needs Telegram';
  UPDATE feature_settings SET tailoring = true, telegram = true, digest = true;
  UPDATE schedule_settings SET last_run_at = now();

  -- a schedule that never ran is overdue once it has been set up for over 30 minutes
  UPDATE schedule_settings SET last_run_at = NULL, updated_at = now() - interval '2 hours' WHERE name = 'score';
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(dashboard_health()->'alerts') e) = ARRAY['schedule_overdue'], 'never ran, set up long ago';
  UPDATE schedule_settings SET updated_at = now() WHERE name = 'score';
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'just set up: its first tick is still to come';
  UPDATE schedule_settings SET last_run_at = now() WHERE name = 'score';

  -- the scoring queue: the doctor's rule (10+ waiting over 3 hours and nothing scored for 3 hours)
  FOR i IN 1..10 LOOP
    j := pg_temp.dash_job(200 + i, 'new', NULL);
    UPDATE jobs SET first_seen = now() - interval '4 hours' WHERE id = j;
  END LOOP;
  h := dashboard_health();
  ASSERT (h->'queue'->>'waiting_to_score')::int = 10 AND (h->'queue'->>'waiting_over_3h')::int = 10;
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['scoring_stalled'], 'stalled: ' || (h->'alerts')::text;
  UPDATE jobs SET scored_at = now() - interval '10 minutes' WHERE id = j;
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'something scored recently: not stalled';
  UPDATE jobs SET scored_at = NULL WHERE id = j;
  DELETE FROM jobs WHERE status = 'new' AND id <> j;
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'fewer than 10 waiting: not stalled';

  -- parked jobs
  UPDATE jobs SET score_claims = 6 WHERE id = j;
  ASSERT (dashboard_health()->'queue'->>'waiting_to_score')::int = 0, 'a parked job is not waiting to be scored';
  h := dashboard_health();
  ASSERT (h->'parked'->>'total')::int = 1 AND h->'parked'->'items'->0->>'stage' = 'score'
     AND (h->'parked'->'items'->0->>'job_id')::bigint = j AND (h->'parked'->'items'->0->>'claims')::int = 6, 'parked: ' || (h->'parked')::text;
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['parked'];
  UPDATE jobs SET score_claims = 0 WHERE id = j;
  -- ten or more parked jobs must not also raise "scoring stalled": they have their own alert
  FOR i IN 1..12 LOOP
    jj := pg_temp.dash_job(300 + i, 'new', NULL);
    UPDATE jobs SET score_claims = 6, first_seen = now() - interval '5 hours' WHERE id = jj;
  END LOOP;
  h := dashboard_health();
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['parked'], 'parked only: ' || (h->'alerts')::text;
  DELETE FROM jobs WHERE id <> j;

  -- boards and feeds: any failure is listed, three in a row is an alert
  PERFORM record_fetch('greenhouse', 'acme', true, 12, NULL);
  PERFORM record_fetch('lever', 'newco', false, NULL, 'HTTP 404');
  h := dashboard_health();
  ASSERT (h->'sources'->>'tracked')::int = 2 AND (h->'sources'->>'failing')::int = 1, 'sources: ' || (h->'sources')::text;
  ASSERT h->'sources'->'items'->0->>'key' = 'newco' AND h->'sources'->'items'->0->>'last_error' = 'HTTP 404';
  ASSERT h->'alerts' = '[]'::jsonb, 'one failure is listed but is not an alert';
  PERFORM record_fetch('lever', 'newco', false, NULL, 'HTTP 404');
  PERFORM record_fetch('lever', 'newco', false, NULL, 'HTTP 404');
  PERFORM record_fetch('remoteok', '', false, NULL, 'timeout');
  h := dashboard_health();
  ASSERT h->'sources'->'items'->0->>'key' = 'newco' AND (h->'sources'->'items'->0->>'failures')::int = 3, 'worst first';
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['sources_failing'];
  PERFORM record_fetch('lever', 'newco', true, 3, NULL);
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'a success clears the streak';

  -- spend: Claude and Apify against their caps, history by day and purpose, this month by model
  INSERT INTO llm_usage (at, provider, purpose, model, input_tokens, output_tokens, cost_usd) VALUES
    (now(), 'anthropic', 'score',  'claude-haiku-5-5',  1000, 200, 0.40),
    (now(), 'anthropic', 'score',  'claude-haiku-5-5',  1000, 200, 0.40),
    (now(), 'anthropic', 'tailor', 'claude-sonnet-5-5', 5000, 900, 0.50),
    (now() - interval '3 days', 'anthropic', 'score', 'claude-haiku-5-5', 1000, 200, 0.10),
    (now() - interval '45 days', 'anthropic', 'score', 'claude-haiku-5-5', 1000, 200, 9.00),
    (now(), 'apify', 'search', NULL, 0, 0, 0.55);
  s := dashboard_spend();
  ASSERT (s->'claude'->>'day_spent')::numeric = 1.30 AND (s->'claude'->>'daily_cap')::numeric = 1.5, 'claude today: ' || (s->'claude')::text;
  ASSERT NOT (s->'claude'->>'blocked')::boolean, 'under both caps';
  ASSERT (s->'apify'->>'blocked')::boolean AND s->'apify'->>'blocked_reason' = 'Apify daily cap reached', 'apify: ' || (s->'apify')::text;
  ASSERT (SELECT count(*) FROM jsonb_array_elements(s->'daily') e) >= 3, 'rows per day, provider and purpose';
  ASSERT NOT EXISTS (SELECT 1 FROM jsonb_array_elements(s->'daily') e WHERE (e->>'cost_usd')::numeric = 9.00), 'older than 30 days is not in the history';
  ASSERT (SELECT sum((e->>'cost_usd')::numeric) FROM jsonb_array_elements(s->'daily') e WHERE e->>'provider' = 'anthropic' AND e->>'purpose' = 'score'
           AND (e->>'day')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date) = 0.80, 'two score calls today add up';
  ASSERT (SELECT array_agg(e->>'model') FROM jsonb_array_elements(s->'models') e WHERE e->>'provider' = 'anthropic') IS NOT NULL;
  ASSERT (s->'apify'->>'blocked')::boolean, 'the Apify daily cap is reached';
  h := dashboard_health();
  ASSERT h->'alerts' = '[]'::jsonb, 'the Apify DAILY cap is routine: a meter, not an alert, got ' || (h->'alerts')::text;
  UPDATE budget_settings SET apify_monthly_cap_usd = 0.5;
  h := dashboard_health();
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['apify_blocked'], 'the monthly cap is an alert: ' || (h->'alerts')::text;
  UPDATE budget_settings SET apify_monthly_cap_usd = 15;
  INSERT INTO llm_usage (provider, purpose, model, cost_usd) VALUES ('anthropic', 'score', 'claude-haiku-5-5', 1.00);
  h := dashboard_health();
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['claude_blocked'], 'daily cap reached: ' || (h->'alerts')::text;
  DELETE FROM llm_usage WHERE cost_usd = 1.00;
  UPDATE budget_settings SET monthly_cap_usd = round((SELECT month_spent FROM budget_status()) / 0.9, 2);   -- spent is 90% of the cap
  h := dashboard_health();
  ASSERT (SELECT array_agg(e->>'code') FROM jsonb_array_elements(h->'alerts') e) = ARRAY['claude_warn'], 'warn: ' || (h->'alerts')::text;
  UPDATE budget_settings SET monthly_cap_usd = 10;

  -- an empty usage table: zeros and empty lists, no nulls where the screen expects a number
  DELETE FROM llm_usage;
  s := dashboard_spend();
  ASSERT s->'daily' = '[]'::jsonb AND s->'models' = '[]'::jsonb AND (s->'claude'->>'month_spent')::numeric = 0;
END $$;

-- the low-privilege role can run exactly the three new functions and still read no table
DO $$
DECLARE h jsonb;
BEGIN
  SET LOCAL ROLE jobsearch_dashboard;
  h := dashboard_health();
  ASSERT h ? 'alerts' AND dashboard_spend() ? 'claude';
  ASSERT (dashboard_set_pause(true, 'role test')->>'paused')::boolean;
  PERFORM dashboard_set_pause(false, NULL);
  BEGIN PERFORM 1 FROM schedule_control; RAISE EXCEPTION 'the role read schedule_control';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM fetch_state; RAISE EXCEPTION 'the role read fetch_state';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM llm_usage; RAISE EXCEPTION 'the role read llm_usage';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE schedule_control SET paused = true; RAISE EXCEPTION 'the role wrote schedule_control directly';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM requeue_jobs('score'); RAISE EXCEPTION 'the role ran requeue_jobs';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM schedule_status(); RAISE EXCEPTION 'the role ran schedule_status';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
END $$;

-- 017: who last wrote the search profile ------------------------------------------------------------------------------
DO $$
BEGIN
  ASSERT (SELECT edited_by FROM search_profile WHERE name = 'default') = 'cli', 'an existing profile counts as written by the CLI';
  INSERT INTO search_profile (name, search_terms, locations) VALUES ('t17', ARRAY['x'], ARRAY['y']);
  ASSERT (SELECT edited_by FROM search_profile WHERE name = 't17') = 'cli', 'the default is the CLI';
  UPDATE search_profile SET edited_by = 'dashboard' WHERE name = 't17';
  ASSERT (SELECT edited_by FROM search_profile WHERE name = 't17') = 'dashboard';
  BEGIN UPDATE search_profile SET edited_by = 'telegram' WHERE name = 't17'; RAISE EXCEPTION 'an unknown writer was accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE search_profile SET edited_by = NULL WHERE name = 't17'; RAISE EXCEPTION 'NULL was accepted';
  EXCEPTION WHEN not_null_violation THEN NULL; END;
  DELETE FROM search_profile WHERE name = 't17';
END $$;

ROLLBACK;
\echo ALL SQL TESTS PASSED
