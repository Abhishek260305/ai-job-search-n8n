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
  ASSERT n = 5, 'a fresh install claims collect, score, tailor, digest and mail: ' || n;
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
  ASSERT (SELECT array_agg(name) FROM schedule_status()) = ARRAY['collect', 'score', 'tailor', 'digest', 'mail'], 'a stable order';

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
  ASSERT (SELECT count(*) FROM claim_due_schedules()) = 5, 'resumed: everything due starts again';
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

  -- a quiet system: no alerts, all five schedules listed in their usual order, nothing waiting
  h := dashboard_health();
  ASSERT h->'alerts' = '[]'::jsonb, 'quiet system has no alerts, got ' || (h->'alerts')::text;
  ASSERT (SELECT array_agg(e->>'name') FROM jsonb_array_elements(h->'schedules') e) = ARRAY['collect', 'score', 'tailor', 'digest', 'mail'];
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

-- 018: application history, timeline, board and stats -----------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; bd jsonb; st jsonb; ev jsonb; n int; r record;
BEGIN
  TRUNCATE jobs CASCADE;
  a := pg_temp.dash_job(101, 'shortlisted', 80);
  b := pg_temp.dash_job(102, 'shortlisted', 70);
  c := pg_temp.dash_job(103, 'shortlisted', 90);
  d := pg_temp.dash_job(104, 'shortlisted', 50);
  e := pg_temp.dash_job(105, 'shortlisted', 75);

  -- every way of changing a status is recorded, whoever makes the change
  INSERT INTO applications (job_id, status) VALUES (a, 'drafted');                  -- what tailoring does
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = 1;
  SELECT * INTO r FROM application_events WHERE job_id = a;
  ASSERT r.from_status IS NULL AND r.to_status = 'drafted' AND r.source IS NULL, 'the first event has no from, no source';

  PERFORM set_application_status(a, 'applied', 'sent my CV');                      -- the CLI or Telegram
  SELECT * INTO r FROM application_events WHERE job_id = a ORDER BY id DESC LIMIT 1;
  ASSERT r.from_status = 'drafted' AND r.to_status = 'applied' AND r.note = 'sent my CV' AND r.source IS NULL;

  PERFORM dashboard_set_status(a, 'interview', 'applied');                         -- the dashboard
  SELECT * INTO r FROM application_events WHERE job_id = a ORDER BY id DESC LIMIT 1;
  ASSERT r.from_status = 'applied' AND r.to_status = 'interview' AND r.source = 'dashboard' AND r.note IS NULL,
         'the old note is not repeated on a later event';
  ASSERT coalesce(current_setting('jobsearch.source', true), '') = '', 'the marker is cleared after the change';

  PERFORM set_application_status(a, 'offer');                                      -- afterwards a CLI change is not marked
  SELECT * INTO r FROM application_events WHERE job_id = a ORDER BY id DESC LIMIT 1;
  ASSERT r.to_status = 'offer' AND r.source IS NULL;

  n := (SELECT count(*) FROM application_events WHERE job_id = a);
  PERFORM set_application_status(a, 'offer');                                      -- nothing changed: nothing recorded
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = n, 'a repeat is not an event';
  PERFORM set_application_status(a, 'offer', 'verbal offer');                      -- a note alone is an event
  SELECT * INTO r FROM application_events WHERE job_id = a ORDER BY id DESC LIMIT 1;
  ASSERT r.from_status = 'offer' AND r.to_status = 'offer' AND r.note = 'verbal offer';
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = n + 1;

  -- the timeline: oldest first, with the failures the API maps
  ev := dashboard_job_events(a);
  ASSERT jsonb_array_length(ev) = n + 1;
  ASSERT ev->0->>'to_status' = 'drafted' AND ev->-1->>'note' = 'verbal offer';
  ASSERT dashboard_job_events(c) = '[]'::jsonb, 'a job with no application has an empty timeline';
  BEGIN PERFORM dashboard_job_events(-1); RAISE EXCEPTION 'an unknown job was accepted';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;

  -- the board: not-yet-applied jobs by score, the rest by when they last moved; counts are the true totals
  PERFORM set_application_status(b, 'applied');
  PERFORM set_application_status(d, 'withdrawn');
  PERFORM set_application_status(e, 'skipped');
  bd := dashboard_board();
  ASSERT jsonb_array_length(bd->'columns') = 6;
  ASSERT (SELECT array_agg(x->>'key') FROM jsonb_array_elements(bd->'columns') x)
         = ARRAY['not_applied', 'applied', 'in_progress', 'interview', 'offer', 'rejected'];
  ASSERT (bd->'columns'->0->>'total')::int = 1 AND (bd->'columns'->0->'items'->0->>'id')::bigint = c, 'only c is not applied';
  ASSERT (bd->'columns'->1->>'total')::int = 1 AND (bd->'columns'->1->'items'->0->>'id')::bigint = b;
  ASSERT (bd->'columns'->4->>'total')::int = 1 AND (bd->'columns'->4->'items'->0->>'id')::bigint = a;
  ASSERT (bd->'closed'->>'withdrawn')::int = 1 AND (bd->'closed'->>'skipped')::int = 1, 'withdrawn and skipped are counted, not shown';
  ASSERT NOT (bd::text LIKE '%' || (SELECT description FROM jobs WHERE id = a) || '%'), 'cards carry no description';
  ASSERT (bd->'columns'->4->'items'->0->>'status_since') IS NOT NULL;

  FOR n IN 1..45 LOOP PERFORM pg_temp.dash_job(200 + n, 'shortlisted', n); END LOOP;
  bd := dashboard_board();
  ASSERT (bd->'columns'->0->>'total')::int = 46, 'the total counts every job';
  ASSERT jsonb_array_length(bd->'columns'->0->'items') = 40, 'a column shows at most 40 cards';
  ASSERT (bd->'columns'->0->'items'->0->>'id')::bigint = c AND (bd->'columns'->0->'items'->1->>'score')::int = 45, 'best score first';
  UPDATE jobs SET status = 'scam_blocked' WHERE id = c;
  ASSERT (dashboard_board()->'columns'->0->>'total')::int = 45, 'a scam-blocked job is not on the board';

  -- the numbers: replies are measured from real events only
  TRUNCATE jobs CASCADE;
  a := pg_temp.dash_job(301, 'shortlisted', 80);
  b := pg_temp.dash_job(302, 'shortlisted', 80);
  c := pg_temp.dash_job(303, 'shortlisted', 80);
  d := pg_temp.dash_job(304, 'shortlisted', 80);
  PERFORM set_application_status(a, 'applied');
  PERFORM set_application_status(a, 'interview');
  PERFORM set_application_status(b, 'applied');
  PERFORM set_application_status(b, 'rejected');
  PERFORM set_application_status(c, 'applied');                                    -- never answered
  PERFORM set_application_status(d, 'applied');
  PERFORM set_application_status(d, 'offer');
  UPDATE application_events SET at = now() - interval '20 days' WHERE job_id = a AND to_status = 'applied';
  UPDATE application_events SET at = now() - interval '14 days' WHERE job_id = a AND to_status = 'interview';   -- 6 days
  UPDATE application_events SET at = now() - interval '20 days' WHERE job_id = b AND to_status = 'applied';
  UPDATE application_events SET at = now() - interval '10 days' WHERE job_id = b AND to_status = 'rejected';    -- 10 days
  UPDATE application_events SET at = now() - interval '20 days' WHERE job_id = d AND to_status = 'applied';
  UPDATE application_events SET at = now() - interval '19 days', source = 'backfill' WHERE job_id = d AND to_status = 'offer';  -- not measured
  st := dashboard_stats();
  ASSERT (st->>'applied')::int = 4 AND (st->>'responded')::int = 3, 'four applied, three answered, got ' || st::text;
  ASSERT (st->>'reached_interview')::int = 2 AND (st->>'reached_offer')::int = 1, 'interview or offer: a and d';
  ASSERT (st->>'avg_days_to_reply')::numeric = 8.0 AND (st->>'replies_measured')::int = 2, 'mean of 6 and 10 days, backfill left out, got ' || st::text;
  ASSERT jsonb_array_length(st->'weekly') = 12;
  ASSERT (SELECT sum((w->>'applied')::int) FROM jsonb_array_elements(st->'weekly') w) = 4, 'every application is in some week';
  ASSERT (st->'weekly'->-1->>'week_start')::date = (date_trunc('week', now() AT TIME ZONE (SELECT timezone FROM budget_settings WHERE id = 1)))::date,
         'the last week is this one, starting on a Monday';
  ASSERT (st->'weekly'->-1->>'found')::int = 4, 'the jobs were found this week';
  TRUNCATE jobs CASCADE;
  st := dashboard_stats();
  ASSERT (st->>'applied')::int = 0 AND st->'avg_days_to_reply' = 'null'::jsonb, 'no data: no average, no division by zero';
END $$;

-- 018, second pass: the cases a first reading gets wrong ---------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; st jsonb; bd jsonb; r record; wk date; tzname text;
BEGIN
  TRUNCATE jobs CASCADE;
  SELECT timezone INTO tzname FROM budget_settings WHERE id = 1;
  wk := (date_trunc('week', now() AT TIME ZONE tzname))::date;
  a := pg_temp.dash_job(501, 'shortlisted', 80);
  b := pg_temp.dash_job(502, 'shortlisted', 80);
  c := pg_temp.dash_job(503, 'shortlisted', 80);
  d := pg_temp.dash_job(504, 'shortlisted', 80);
  e := pg_temp.dash_job(505, 'scam_blocked', 80);
  f := pg_temp.dash_job(506, 'shortlisted', 80);

  -- the dashboard's most common move, on a job that has no application yet, is a first event with its note and source
  PERFORM dashboard_set_status(a, 'applied', 'shortlisted', 'applied on the site');
  SELECT * INTO r FROM application_events WHERE job_id = a;
  ASSERT r.from_status IS NULL AND r.to_status = 'applied' AND r.note = 'applied on the site' AND r.source = 'dashboard',
         'the first event of a dashboard move has its note and source';

  -- tailoring a job again clears review_note on the same status: not an event
  UPDATE applications SET review_note = 'kept in mind' WHERE job_id = a;
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = 2, 'a new note is an event';
  UPDATE applications SET review_note = NULL WHERE job_id = a;
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = 2, 'a cleared note is not an event';

  -- days in a stage: a note does not reset it, and not applied yet counts from the scoring
  PERFORM set_application_status(b, 'applied');
  PERFORM set_application_status(b, 'interview');
  UPDATE application_events SET at = now() - interval '10 days' WHERE job_id = b;
  PERFORM set_application_status(b, 'interview', 'asked about salary');            -- a note-only event, now
  bd := dashboard_board();
  ASSERT (SELECT (x->>'status_since')::timestamptz < now() - interval '9 days'
            FROM jsonb_array_elements(bd->'columns'->3->'items') x WHERE (x->>'id')::bigint = b),
         'a note does not make a card look newly moved';
  UPDATE jobs SET scored_at = now() - interval '30 days' WHERE id = c;
  PERFORM set_application_status(c, 'approved');
  bd := dashboard_board();
  ASSERT (SELECT (x->>'status_since')::timestamptz < now() - interval '29 days'
            FROM jsonb_array_elements(bd->'columns'->0->'items') x WHERE (x->>'id')::bigint = c),
         'a job not applied to yet shows when it was scored, not when it was approved';

  -- the board leaves out scam-blocked jobs and counts withdrawn and skipped separately
  PERFORM set_application_status(e, 'rejected');
  PERFORM set_application_status(d, 'withdrawn');
  PERFORM set_application_status(f, 'skipped');
  PERFORM set_application_status(pg_temp.dash_job(507, 'shortlisted', 70), 'withdrawn');
  bd := dashboard_board();
  ASSERT (bd->'columns'->5->>'total')::int = 0, 'a scam-blocked job is not in Rejected';
  ASSERT (bd->'closed'->>'withdrawn')::int = 2 AND (bd->'closed'->>'skipped')::int = 1, 'the two counts are not swapped';

  -- the numbers are consistent: everything is about jobs that were ever submitted, once each
  TRUNCATE jobs CASCADE;
  a := pg_temp.dash_job(601, 'shortlisted', 80);   -- applied, then interview, then skipped (applied_at is kept)
  b := pg_temp.dash_job(602, 'shortlisted', 80);   -- straight to interview, never 'applied'
  c := pg_temp.dash_job(603, 'shortlisted', 80);   -- applied, then withdrawn: not an answer
  d := pg_temp.dash_job(604, 'shortlisted', 80);   -- applied, moved back, applied again
  e := pg_temp.dash_job(605, 'shortlisted', 80);   -- never submitted
  PERFORM set_application_status(a, 'applied'); PERFORM set_application_status(a, 'interview'); PERFORM set_application_status(a, 'skipped');
  PERFORM set_application_status(b, 'interview');
  PERFORM set_application_status(c, 'applied'); PERFORM set_application_status(c, 'withdrawn');
  PERFORM set_application_status(d, 'applied'); PERFORM set_application_status(d, 'approved'); PERFORM set_application_status(d, 'applied');
  PERFORM set_application_status(e, 'approved');
  st := dashboard_stats();
  ASSERT (st->>'applied')::int = 4, 'applied counts every job ever submitted, as the Jobs page does, got ' || st::text;
  ASSERT (st->>'responded')::int = 2, 'a and b answered; a withdrawal is not an answer, got ' || st::text;
  ASSERT (st->>'reached_interview')::int = 2, 'a and b reached an interview even though a was skipped later';
  ASSERT (st->>'responded')::int <= (st->>'applied')::int AND (st->>'reached_interview')::int <= (st->>'applied')::int;
  ASSERT (st->'weekly'->-1->>'applied')::int = 4, 'a job moved straight to interview is in the week it was submitted, got ' || st::text;
  ASSERT (st->'weekly'->-1->>'found')::int = 5;

  -- a job applied twice counts in the week of the FIRST time
  UPDATE application_events SET at = (wk - 14)::timestamp AT TIME ZONE tzname + interval '1 day' WHERE job_id = d AND id = (SELECT min(id) FROM application_events WHERE job_id = d);
  st := dashboard_stats();
  ASSERT (st->'weekly'->-3->>'applied')::int = 1 AND (st->'weekly'->-1->>'applied')::int = 3, 'the first submission decides the week, got ' || st::text;

  -- shortlisted and found by week, in the configured time zone, with scam-blocked and filtered-out jobs left out
  UPDATE search_profile SET score_threshold = 60 WHERE name = 'default';
  UPDATE jobs SET scored_at = (wk - 7)::timestamp AT TIME ZONE tzname + interval '1 hour', score = 70, first_seen = (wk - 7)::timestamp AT TIME ZONE tzname + interval '1 hour' WHERE id = e;
  UPDATE jobs SET scored_at = (wk::timestamp AT TIME ZONE tzname) - interval '1 second', score = 70, first_seen = (wk::timestamp AT TIME ZONE tzname) - interval '1 second' WHERE id = c;   -- the last second of last week
  UPDATE jobs SET scored_at = now(), score = 50 WHERE id = a;                       -- below the threshold
  UPDATE jobs SET scored_at = now(), score = 90, status = 'scam_blocked' WHERE id = b;
  st := dashboard_stats();
  ASSERT (st->'weekly'->-2->>'shortlisted')::int = 2, 'e and c were scored last week (c in its last second), got ' || st::text;
  ASSERT (st->'weekly'->-1->>'shortlisted')::int = 0, 'below the threshold and scam-blocked do not count, got ' || st::text;
  ASSERT (st->'weekly'->-2->>'found')::int = 2 AND (st->'weekly'->-1->>'found')::int = 2, 'found by first-seen week, scam-blocked left out, got ' || st::text;
END $$;

-- 019: notes, contacts and follow-up reminders -------------------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; t jsonb; v1 text; v2 text; n int; r record; bd jsonb; tzname text; today date;
BEGIN
  TRUNCATE jobs CASCADE;
  SELECT timezone INTO tzname FROM budget_settings WHERE id = 1;
  today := (now() AT TIME ZONE tzname)::date;
  a := pg_temp.dash_job(701, 'shortlisted', 80);
  b := pg_temp.dash_job(702, 'shortlisted', 80);
  c := pg_temp.dash_job(703, 'shortlisted', 80);
  d := pg_temp.dash_job(704, 'shortlisted', 80);
  e := pg_temp.dash_job(705, 'shortlisted', 80);

  -- an empty job has nothing; saving creates the row; a change needs the version that was read
  t := dashboard_job_tracking(a);
  ASSERT t->'version' = 'null'::jsonb AND t->'notes' = 'null'::jsonb, 'no row yet: all empty, no version';
  t := dashboard_set_tracking(a, NULL, ('{"notes": "  call Priya on Friday ", "contact_name": "Priya N", "contact_email": "priya@example.com", "referral": "via Sam", "follow_up_on": "' || (today + 3) || '"}')::jsonb);
  ASSERT t->>'notes' = 'call Priya on Friday' AND t->>'contact_name' = 'Priya N' AND t->>'referral' = 'via Sam', 'values are trimmed and saved';
  ASSERT (t->>'follow_up_on')::date = today + 3 AND t->>'version' IS NOT NULL;
  v1 := t->>'version';
  ASSERT dashboard_job_tracking(a) = t, 'a read returns what the save returned';

  BEGIN PERFORM dashboard_set_tracking(a, NULL, '{"notes": "again"}'); RAISE EXCEPTION 'a save without the version was accepted';
  EXCEPTION WHEN SQLSTATE 'JD412' THEN NULL; END;
  BEGIN PERFORM dashboard_set_tracking(a, '2000-01-01T00:00:00.000000Z', '{"notes": "again"}'); RAISE EXCEPTION 'a save with an old version was accepted';
  EXCEPTION WHEN SQLSTATE 'JD412' THEN NULL; END;
  ASSERT dashboard_job_tracking(a)->>'notes' = 'call Priya on Friday', 'a refused save changes nothing';

  -- only the fields given change; each save has a new version
  t := dashboard_set_tracking(a, v1, '{"notes": "moved to Monday"}');
  v2 := t->>'version';
  ASSERT v2 <> v1 AND t->>'contact_name' = 'Priya N' AND t->>'notes' = 'moved to Monday', 'a patch keeps the other fields';
  BEGIN PERFORM dashboard_set_tracking(a, v1, '{"notes": "stale"}'); RAISE EXCEPTION 'the old version still worked';
  EXCEPTION WHEN SQLSTATE 'JD412' THEN NULL; END;

  -- empty or null clears a field; clearing everything removes the row
  t := dashboard_set_tracking(a, v2, '{"contact_email": "", "referral": null, "follow_up_on": null}');
  ASSERT t->'contact_email' = 'null'::jsonb AND t->'referral' = 'null'::jsonb AND t->'follow_up_on' = 'null'::jsonb AND t->>'notes' IS NOT NULL;
  t := dashboard_set_tracking(a, t->>'version', '{"notes": " ", "contact_name": null}');
  ASSERT t->'version' = 'null'::jsonb, 'nothing left: no version';
  ASSERT NOT EXISTS (SELECT 1 FROM job_tracking WHERE job_id = a), 'the row is gone when every field is empty';

  -- refusals
  FOREACH v1 IN ARRAY ARRAY['{}', '[]', '{"title": "x"}', '{"notes": 5}', '{"notes": true}', '{"contact_email": "not an email"}', '{"contact_email": "a@b"}',
        '{"follow_up_on": "31/10/2026"}', '{"follow_up_on": "2026-02-31"}', '{"follow_up_on": "1999-01-01"}', '{"follow_up_on": "2999-01-01"}',
        '{"contact_name": "' || repeat('x', 101) || '"}', '{"referral": "' || repeat('x', 201) || '"}', '{"notes": "' || repeat('x', 2001) || '"}'] LOOP
    BEGIN PERFORM dashboard_set_tracking(a, NULL, v1::jsonb); RAISE EXCEPTION 'accepted: %', left(v1, 60);
    EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  END LOOP;
  PERFORM dashboard_set_tracking(a, NULL, ('{"notes": "' || repeat('x', 2000) || '"}')::jsonb);
  ASSERT length(dashboard_job_tracking(a)->>'notes') = 2000, 'the longest allowed note is kept';
  BEGIN PERFORM dashboard_set_tracking(-1, NULL, '{"notes": "x"}'); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;
  BEGIN PERFORM dashboard_job_tracking(-1); RAISE EXCEPTION 'unknown job accepted';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;
  DELETE FROM job_tracking;

  -- what is due. a: a date today; b: a date tomorrow on an application waiting 30 days (the date wins: not due);
  -- c: applied 10 days ago, nothing set (waiting); d: applied 3 days ago (not yet); e: applied 10 days ago then rejected
  PERFORM dashboard_set_tracking(a, NULL, ('{"follow_up_on": "' || today || '"}')::jsonb);
  PERFORM set_application_status(b, 'applied');
  PERFORM dashboard_set_tracking(b, NULL, ('{"follow_up_on": "' || (today + 1) || '"}')::jsonb);
  PERFORM set_application_status(c, 'applied');
  PERFORM set_application_status(d, 'applied');
  PERFORM set_application_status(e, 'applied');
  UPDATE applications SET applied_at = now() - interval '30 days' WHERE job_id = b;
  UPDATE applications SET applied_at = now() - interval '10 days' WHERE job_id IN (c, e);
  UPDATE applications SET applied_at = now() - interval '3 days' WHERE job_id = d;
  PERFORM set_application_status(e, 'rejected');
  SELECT count(*) INTO n FROM follow_ups_due();
  ASSERT n = 2, 'a (date) and c (waiting) are due, got ' || n;
  ASSERT (SELECT reason FROM follow_ups_due() WHERE job_id = a) = 'date' AND (SELECT reason FROM follow_ups_due() WHERE job_id = c) = 'waiting';
  ASSERT (SELECT days_waiting FROM follow_ups_due() WHERE job_id = c) = 10, 'ten days waiting';
  ASSERT NOT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id IN (b, d, e)), 'a future date, too soon and rejected are not due';

  -- the boundary is "at least N days": exactly 7 is due, 6 is not; a date on a rejected or withdrawn job is not due
  PERFORM set_application_status(pg_temp.dash_job(711, 'shortlisted', 70), 'applied');
  PERFORM set_application_status(pg_temp.dash_job(712, 'shortlisted', 70), 'applied');
  UPDATE applications SET applied_at = (now() AT TIME ZONE tzname)::date::timestamp AT TIME ZONE tzname - interval '7 days' + interval '1 hour'
   WHERE job_id = (SELECT id FROM jobs WHERE company = 'DashCo 711');
  UPDATE applications SET applied_at = (now() AT TIME ZONE tzname)::date::timestamp AT TIME ZONE tzname - interval '6 days' + interval '1 hour'
   WHERE job_id = (SELECT id FROM jobs WHERE company = 'DashCo 712');
  ASSERT (SELECT count(*) FROM follow_ups_due() WHERE job_id = (SELECT id FROM jobs WHERE company = 'DashCo 711')) = 1, 'exactly seven days waiting is due';
  ASSERT (SELECT count(*) FROM follow_ups_due() WHERE job_id = (SELECT id FROM jobs WHERE company = 'DashCo 712')) = 0, 'six days is not';
  PERFORM dashboard_set_tracking(e, NULL, ('{"follow_up_on": "' || (today - 2) || '"}')::jsonb);      -- e was rejected
  ASSERT NOT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id = e), 'a past date on a rejected job is not due';
  DELETE FROM jobs WHERE company IN ('DashCo 711', 'DashCo 712');
  DELETE FROM job_tracking WHERE job_id = e;

  -- the number of days is a setting; a past date is due; a reply or a later status stops the waiting rule
  PERFORM dashboard_set_follow_up_days(2);
  ASSERT (SELECT count(*) FROM follow_ups_due() WHERE job_id = d) = 1, 'three days waiting is due after two';
  PERFORM dashboard_set_follow_up_days(7);
  PERFORM set_application_status(c, 'interview');
  ASSERT NOT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id = c), 'an answered application is not waiting';
  PERFORM dashboard_set_tracking(c, NULL, ('{"follow_up_on": "' || (today - 4) || '"}')::jsonb);
  ASSERT (SELECT reason FROM follow_ups_due() WHERE job_id = c) = 'date', 'a past follow-up date is due even after a reply';
  UPDATE jobs SET status = 'scam_blocked' WHERE id = c;
  ASSERT NOT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id = c), 'scam-blocked jobs are never due';
  UPDATE jobs SET status = 'shortlisted' WHERE id = c;
  BEGIN PERFORM dashboard_set_follow_up_days(0); RAISE EXCEPTION 'zero days accepted';  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_set_follow_up_days(91); RAISE EXCEPTION '91 days accepted';  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_set_follow_up_days(NULL); RAISE EXCEPTION 'NULL days accepted'; EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN UPDATE follow_up_settings SET days = 0; RAISE EXCEPTION 'the table accepted 0'; EXCEPTION WHEN check_violation THEN NULL; END;

  -- the board lists what is due, and flags the cards
  PERFORM set_application_status(d, 'applied');
  UPDATE applications SET applied_at = now() - interval '12 days' WHERE job_id = d;
  bd := dashboard_board();
  ASSERT (bd->>'follow_up_days')::int = 7 AND (bd->>'follow_ups_total')::int = 3, 'a, c and d are due, got ' || (bd->>'follow_ups_total');
  ASSERT (bd->'follow_ups'->0->>'reason') = 'date', 'explicit dates come first';
  ASSERT (SELECT array_agg((x->>'id')::bigint ORDER BY ord) FROM jsonb_array_elements(bd->'follow_ups') WITH ORDINALITY AS q(x, ord)) = ARRAY[c, a, d], 'oldest date first, then the waiting ones, got ' || (bd->'follow_ups')::text;
  ASSERT (SELECT x->>'follow_up' FROM jsonb_array_elements(bd->'columns'->3->'items') x WHERE (x->>'id')::bigint = c) = 'date', 'the card says why';
  ASSERT (SELECT x->>'follow_up' FROM jsonb_array_elements(bd->'columns'->1->'items') x WHERE (x->>'id')::bigint = d) = 'waiting';
  ASSERT (SELECT x->>'follow_up' FROM jsonb_array_elements(bd->'columns'->1->'items') x WHERE (x->>'id')::bigint = b) IS NULL, 'not due: no flag';
  PERFORM dashboard_set_tracking(c, (dashboard_job_tracking(c))->>'version', '{"contact_name": "Ravi", "contact_email": "ravi@example.com"}');
  ASSERT (dashboard_board()->'follow_ups'->0->>'contact_name') = 'Ravi', 'the list carries the contact';

  -- the history goes with the job
  DELETE FROM jobs WHERE id = a;
  ASSERT NOT EXISTS (SELECT 1 FROM job_tracking WHERE job_id = a);
END $$;

-- 019, second pass --------------------------------------------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; t jsonb; v text; n int; tzname text; today date; ids bigint[]; session_tz text := current_setting('timezone');
BEGIN
  TRUNCATE jobs CASCADE;
  SELECT timezone INTO tzname FROM budget_settings WHERE id = 1;
  today := (now() AT TIME ZONE tzname)::date;

  -- every status that must never be due, one by one (a past date is set on each)
  a := pg_temp.dash_job(901, 'shortlisted', 70); PERFORM set_application_status(a, 'withdrawn');
  b := pg_temp.dash_job(902, 'shortlisted', 70); PERFORM set_application_status(b, 'skipped');
  c := pg_temp.dash_job(903, 'expired', 70);
  d := pg_temp.dash_job(904, 'filtered_out', 70);
  e := pg_temp.dash_job(905, 'shortlisted', 70);
  FOR n IN 1..5 LOOP
    PERFORM dashboard_set_tracking((ARRAY[a, b, c, d, e])[n], NULL, ('{"follow_up_on": "' || (today - 1) || '"}')::jsonb);
  END LOOP;
  ASSERT (SELECT array_agg(job_id) FROM follow_ups_due()) = ARRAY[e], 'withdrawn, skipped, expired and filtered-out jobs are not due, got ' || coalesce((SELECT array_agg(job_id)::text FROM follow_ups_due()), 'none');

  -- "today" is the configured zone's, whatever the session's zone is
  PERFORM set_config('timezone', 'Pacific/Kiritimati', true);                    -- UTC+14: its date is often a day ahead of the configured zone's
  UPDATE budget_settings SET timezone = 'Pacific/Pago_Pago' WHERE id = 1;       -- UTC-11: a day behind
  today := (now() AT TIME ZONE 'Pacific/Pago_Pago')::date;
  PERFORM dashboard_set_tracking(e, (dashboard_job_tracking(e))->>'version', ('{"follow_up_on": "' || today || '"}')::jsonb);
  ASSERT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id = e), 'a date equal to today in the configured zone is due';
  PERFORM dashboard_set_tracking(e, (dashboard_job_tracking(e))->>'version', ('{"follow_up_on": "' || (today + 1) || '"}')::jsonb);
  ASSERT NOT EXISTS (SELECT 1 FROM follow_ups_due() WHERE job_id = e), 'tomorrow in the configured zone is not due, even if it is already today elsewhere';
  ASSERT (dashboard_job_tracking(e)->>'today')::date = today AND (dashboard_board()->>'today')::date = today, 'the server tells the page its today';
  UPDATE budget_settings SET timezone = tzname WHERE id = 1;
  PERFORM set_config('timezone', session_tz, true);
  today := (now() AT TIME ZONE tzname)::date;
  DELETE FROM job_tracking;

  -- the list: at most 20 on the board with the true total; dates first, the oldest date first, then the longest waiting
  ids := ARRAY[]::bigint[];
  FOR n IN 1..25 LOOP
    ids := ids || pg_temp.dash_job(1000 + n, 'shortlisted', 70);
    PERFORM set_application_status(ids[n], 'applied');
    UPDATE applications SET applied_at = now() - make_interval(days => 7 + n) WHERE job_id = ids[n];
  END LOOP;
  a := pg_temp.dash_job(990, 'shortlisted', 70);
  PERFORM dashboard_set_tracking(a, NULL, ('{"follow_up_on": "' || (today - 3) || '"}')::jsonb);
  b := pg_temp.dash_job(991, 'shortlisted', 70);
  PERFORM dashboard_set_tracking(b, NULL, ('{"follow_up_on": "' || (today - 9) || '"}')::jsonb);
  t := dashboard_board();
  ASSERT (t->>'follow_ups_total')::int = 27 AND jsonb_array_length(t->'follow_ups') = 20, 'twenty shown of 27';
  ASSERT (t->'follow_ups'->0->>'id')::bigint = b AND (t->'follow_ups'->1->>'id')::bigint = a, 'dates first, oldest date first';
  ASSERT (t->'follow_ups'->2->>'id')::bigint = ids[25] AND (t->'follow_ups'->3->>'id')::bigint = ids[24], 'then the longest waiting first';

  -- a save that changes nothing is not a change: the version stays, so another tab is not told of one
  TRUNCATE jobs CASCADE;
  a := pg_temp.dash_job(1101, 'shortlisted', 70);
  t := dashboard_set_tracking(a, NULL, '{"notes": "one", "contact_name": "Priya"}');
  v := t->>'version';
  t := dashboard_set_tracking(a, v, '{"notes": " one ", "contact_name": "Priya", "referral": "", "contact_email": null}');
  ASSERT t->>'version' = v, 'the same values saved again keep the version';
  t := dashboard_set_tracking(a, v, '{"notes": "two"}');
  ASSERT t->>'version' <> v, 'a real change makes a new one';
END $$;

DO $$
DECLARE a bigint; t jsonb;
BEGIN
  a := pg_temp.dash_job(1201, 'shortlisted', 70);
  -- a note that is only line breaks is empty (the form's trim() and the database agree), so the entry is removed
  t := dashboard_set_tracking(a, NULL, E'{"notes": "\\n\\r\\n \\t"}'::jsonb);
  ASSERT t->'version' = 'null'::jsonb AND NOT EXISTS (SELECT 1 FROM job_tracking WHERE job_id = a), 'line breaks and tabs count as empty';
  t := dashboard_set_tracking(a, NULL, E'{"notes": "\\n line one\\nline two \\n"}'::jsonb);
  ASSERT t->>'notes' = E'line one\nline two', 'outer line breaks are trimmed, inner ones kept';
  t := dashboard_set_tracking(a, t->>'version', '{"follow_up_on": " "}');
  ASSERT t->'follow_up_on' = 'null'::jsonb, 'a blank date clears it';
END $$;

DO $$
DECLARE j bigint; t jsonb;
BEGIN
  j := pg_temp.dash_job(801, 'shortlisted', 80);
  SET LOCAL ROLE jobsearch_dashboard;
  t := dashboard_set_tracking(j, NULL, '{"notes": "as the role"}');
  ASSERT dashboard_job_tracking(j)->>'notes' = 'as the role' AND dashboard_set_follow_up_days(5) = 5;
  BEGIN PERFORM 1 FROM job_tracking; RAISE EXCEPTION 'the role read job_tracking';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE job_tracking SET notes = 'x'; RAISE EXCEPTION 'the role wrote job_tracking';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM follow_up_settings; RAISE EXCEPTION 'the role read follow_up_settings';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM follow_ups_due(); RAISE EXCEPTION 'the role ran follow_ups_due';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM tracking_today(); RAISE EXCEPTION 'the role ran tracking_today';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM tracking_version(now()); RAISE EXCEPTION 'the role ran tracking_version';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  UPDATE follow_up_settings SET days = 7;
END $$;

-- 020, second pass (from the independent review) ----------------------------------------------------------------------------
DO $$
DECLARE j bigint; r jsonb; t0 timestamptz := now() - interval '3 days'; t1 timestamptz := now() - interval '2 days'; n int; ev jsonb; m jsonb;
BEGIN
  TRUNCATE jobs CASCADE;
  -- the review time and the update time come back as they were (the earlier values are in the past, so a wrong restore shows)
  j := pg_temp.dash_job(1701, 'shortlisted', 80);
  PERFORM set_application_status(j, 'approved');
  UPDATE applications SET reviewed_at = t0, updated_at = t1 WHERE job_id = j;
  PERFORM dashboard_set_status(j, 'applied', 'approved');
  PERFORM dashboard_undo_status(j, 'applied');
  ASSERT (SELECT reviewed_at FROM applications WHERE job_id = j) = t0, 'reviewed_at restored';
  ASSERT (SELECT updated_at FROM applications WHERE job_id = j) = t1, 'updated_at restored';

  -- a tailored job (drafted application with files, jobs.status tailored) moved on the dashboard and moved back
  j := pg_temp.dash_job(1702, 'shortlisted', 80);
  PERFORM save_application(j, 'tailored', '/o/a', '/o/a/r.pdf', '/o/a/c.pdf', '{}', '[]', '{}', 'm', 0.1, false, NULL, 'review');
  ASSERT (SELECT status FROM jobs WHERE id = j) = 'tailored';
  PERFORM dashboard_set_status(j, 'skipped', 'drafted');
  r := dashboard_undo_status(j, 'skipped');
  ASSERT r->>'status' = 'drafted', 'the answer is the application status, got ' || r::text;
  ASSERT (SELECT status FROM jobs WHERE id = j) = 'tailored' AND (SELECT status FROM applications WHERE job_id = j) = 'drafted', 'the job is tailored again';

  -- the first move on jobs that were never shortlisted: the answer and the job status are what they were
  j := pg_temp.dash_job(1703, 'new', 80);
  PERFORM dashboard_set_status(j, 'applied', 'new');
  r := dashboard_undo_status(j, 'applied');
  ASSERT r->>'status' = 'new', 'answer for a new job';
  ASSERT (SELECT status FROM jobs WHERE id = j) = 'new', 'jobs.status for a new job';
  j := pg_temp.dash_job(1704, 'expired', 80);
  PERFORM dashboard_set_status(j, 'applied', 'expired');
  r := dashboard_undo_status(j, 'applied');
  ASSERT r->>'status' = 'expired', 'answer for an expired job';
  ASSERT (SELECT status FROM jobs WHERE id = j) = 'expired', 'jobs.status for an expired job';

  -- an application that gained files or became a tailored one since the move is not offered and not touched
  j := pg_temp.dash_job(1705, 'shortlisted', 80);
  PERFORM dashboard_set_status(j, 'applied', 'shortlisted');
  UPDATE applications SET cover_letter_path = '/o/c.pdf' WHERE job_id = j;
  ASSERT NOT (dashboard_job_events(j)->-1->>'undoable')::boolean, 'a cover letter appeared';
  BEGIN PERFORM dashboard_undo_status(j, 'applied'); RAISE EXCEPTION 'undid over a cover letter'; EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  UPDATE applications SET cover_letter_path = NULL, variant = 'tailored' WHERE job_id = j;
  ASSERT NOT (dashboard_job_events(j)->-1->>'undoable')::boolean, 'the variant changed';
  BEGIN PERFORM dashboard_undo_status(j, 'applied'); RAISE EXCEPTION 'undid over a changed variant'; EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  ASSERT EXISTS (SELECT 1 FROM applications WHERE job_id = j), 'nothing was removed';

  -- tailoring a job again after a dashboard move (the status stays, the files are replaced, no entry is written)
  j := pg_temp.dash_job(1706, 'shortlisted', 80);
  PERFORM save_application(j, 'tailored', '/o/a', '/o/a/r.pdf', '/o/a/c.pdf', '{}', '[]', '{}', 'm', 0.1, false, NULL, 'review');
  PERFORM dashboard_set_status(j, 'approved', 'drafted');
  ASSERT (dashboard_job_events(j)->-1->>'undoable')::boolean, 'offered right after the move';
  PERFORM save_application(j, 'tailored', '/o/b', '/o/b/r.pdf', '/o/b/c.pdf', '{}', '[]', '{}', 'm', 0.1, false, NULL, 'auto');
  IF (SELECT status FROM applications WHERE job_id = j) = 'approved' THEN                  -- the row was rewritten in place
    ASSERT NOT (dashboard_job_events(j)->-1->>'undoable')::boolean, 'not offered after the files were replaced';
    BEGIN PERFORM dashboard_undo_status(j, 'approved'); RAISE EXCEPTION 'undid over replaced files'; EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
    ASSERT (SELECT resume_path FROM applications WHERE job_id = j) = '/o/b/r.pdf', 'the new files are untouched';
  END IF;

  -- scam-blocked is not offered
  j := pg_temp.dash_job(1707, 'shortlisted', 80);
  PERFORM dashboard_set_status(j, 'applied', 'shortlisted');
  UPDATE jobs SET status = 'scam_blocked' WHERE id = j;
  ASSERT NOT (dashboard_job_events(j)->-1->>'undoable')::boolean, 'a scam-blocked job is not offered';

  -- a note-only entry names its status too
  j := pg_temp.dash_job(1708, 'shortlisted', 80);
  PERFORM dashboard_set_status(j, 'applied', 'shortlisted');
  PERFORM dashboard_set_status(j, 'applied', 'applied', 'just a note');
  BEGIN PERFORM dashboard_undo_status(j, 'interview'); RAISE EXCEPTION 'a note was undone with the wrong expected status'; EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;

  -- dashboard_move_status names the entry it wrote, and none when nothing was recorded
  j := pg_temp.dash_job(1709, 'shortlisted', 80);
  m := dashboard_move_status(j, 'applied', 'shortlisted');
  ASSERT m->>'status' = 'applied' AND (m->>'event_id')::bigint = (SELECT max(id) FROM application_events WHERE job_id = j), 'the first move names its entry';
  m := dashboard_move_status(j, 'applied', 'applied', '');                                  -- the same status, no note: nothing is recorded
  ASSERT m->>'status' = 'applied' AND m->'event_id' = 'null'::jsonb, 'a no-op names no entry, got ' || m::text;
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = j) = 1;
  m := dashboard_move_status(j, 'interview', 'applied', 'phone screen');
  ASSERT (m->>'event_id')::bigint = (SELECT max(id) FROM application_events WHERE job_id = j) AND (SELECT count(*) FROM application_events WHERE job_id = j) = 2;
  -- so the "undo" of a no-op toast cannot reach an older move: there is nothing to name, and an old id is refused
  BEGIN PERFORM dashboard_undo_status(j, 'interview', (SELECT min(id) FROM application_events WHERE job_id = j)); RAISE EXCEPTION 'undid an older entry by id';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  BEGIN PERFORM dashboard_move_status(j, 'applied', 'offer'); RAISE EXCEPTION 'a stale expected status was moved';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  BEGIN PERFORM dashboard_move_status(-1, 'applied', 'new'); RAISE EXCEPTION 'unknown job moved';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;

  -- nobody can switch the history off with a plain value: only the undo's own transaction id counts
  j := pg_temp.dash_job(1710, 'shortlisted', 80);
  PERFORM set_config('jobsearch.undo', 'on', true);
  PERFORM set_application_status(j, 'applied');
  PERFORM set_config('jobsearch.undo', '', true);
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = j) = 1, 'a forged marker does not silence the history';
END $$;

-- 020: undoing a change made on the dashboard ---------------------------------------------------------------------------
DO $$
DECLARE a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; r jsonb; ev jsonb; n int; st jsonb; applied1 timestamptz; sub text;
BEGIN
  TRUNCATE jobs CASCADE;
  a := pg_temp.dash_job(1301, 'shortlisted', 80);
  b := pg_temp.dash_job(1302, 'shortlisted', 80);
  c := pg_temp.dash_job(1303, 'shortlisted', 80);
  d := pg_temp.dash_job(1304, 'shortlisted', 80);
  e := pg_temp.dash_job(1305, 'shortlisted', 80);
  f := pg_temp.dash_job(1306, 'shortlisted', 80);

  -- 1. the first move of a job creates the application; undoing it takes it away again, as if it never happened
  PERFORM dashboard_set_status(a, 'applied', 'shortlisted', 'sent my CV');
  ev := dashboard_job_events(a);
  ASSERT (ev->-1->>'undoable')::boolean, 'the newest dashboard entry can be undone';
  r := dashboard_undo_status(a, 'applied', (ev->-1->>'id')::bigint);
  ASSERT r->>'status' = 'shortlisted' AND (r->>'id')::bigint = a, 'back to shortlisted, got ' || r::text;
  ASSERT NOT EXISTS (SELECT 1 FROM applications WHERE job_id = a), 'the application it created is gone';
  ASSERT (SELECT status FROM jobs WHERE id = a) = 'shortlisted';
  ASSERT NOT EXISTS (SELECT 1 FROM application_events WHERE job_id = a), 'the entry is gone, and the undo left none';
  ASSERT (dashboard_stats()->>'applied')::int = 0, 'the stats never saw it';

  -- 2. the mistake of the day: applied, then dropped on Offer by accident. Undo puts it back to applied, applied date kept
  PERFORM dashboard_set_status(b, 'applied', 'shortlisted');
  applied1 := (SELECT applied_at FROM applications WHERE job_id = b);
  UPDATE applications SET applied_at = applied1 - interval '5 days' WHERE job_id = b;       -- an older date: it must be the one restored
  applied1 := applied1 - interval '5 days';
  PERFORM dashboard_set_status(b, 'offer', 'applied', 'oops');
  ASSERT (dashboard_stats()->>'reached_offer')::int = 1;
  ev := dashboard_job_events(b);
  ASSERT (ev->-1->>'undoable')::boolean AND NOT (ev->0->>'undoable')::boolean, 'only the newest entry can be undone';
  r := dashboard_undo_status(b, 'offer');
  ASSERT r->>'status' = 'applied';
  ASSERT (SELECT status FROM applications WHERE job_id = b) = 'applied' AND (SELECT status FROM jobs WHERE id = b) = 'applied';
  ASSERT (SELECT applied_at FROM applications WHERE job_id = b) = applied1, 'the applied date is what it was';
  ASSERT (SELECT review_note FROM applications WHERE job_id = b) IS NULL, 'the note of the mistaken move is gone with it';
  ASSERT (dashboard_stats()->>'reached_offer')::int = 0 AND (dashboard_stats()->>'responded')::int = 0, 'the stats do not count the mistake';
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = b) = 1, 'one entry left';
  ASSERT (dashboard_job_events(b)->-1->>'undoable')::boolean, 'and it can be undone in turn';
  r := dashboard_undo_status(b, 'applied');
  ASSERT r->>'status' = 'shortlisted' AND NOT EXISTS (SELECT 1 FROM applications WHERE job_id = b), 'a chain of undos goes back step by step';

  -- 3. a note kept from an earlier move comes back when a later move is undone
  PERFORM dashboard_set_status(c, 'applied', 'shortlisted', 'first note');
  PERFORM dashboard_set_status(c, 'interview', 'applied', 'second note');
  PERFORM dashboard_undo_status(c, 'interview');
  ASSERT (SELECT review_note FROM applications WHERE job_id = c) = 'first note', 'the earlier note is back';
  ASSERT (SELECT reviewed_at FROM applications WHERE job_id = c) IS NOT NULL;
  -- a note added without a change of status is undone too
  PERFORM dashboard_set_status(c, 'applied', 'applied', 'a later thought');
  ASSERT (SELECT review_note FROM applications WHERE job_id = c) = 'a later thought';
  PERFORM dashboard_undo_status(c, 'applied');
  ASSERT (SELECT review_note FROM applications WHERE job_id = c) = 'first note' AND (SELECT status FROM applications WHERE job_id = c) = 'applied';

  -- 4. a move back (applied, then skipped) keeps the applied date, and undoing the move restores the job's status too
  PERFORM dashboard_set_status(d, 'applied', 'shortlisted');
  PERFORM dashboard_set_status(d, 'skipped', 'applied');
  ASSERT (SELECT status FROM jobs WHERE id = d) = 'skipped';
  PERFORM dashboard_undo_status(d, 'skipped');
  ASSERT (SELECT status FROM jobs WHERE id = d) = 'applied' AND (SELECT applied_at FROM applications WHERE job_id = d) IS NOT NULL;

  -- 5. what is refused
  PERFORM set_application_status(e, 'applied');                                            -- the CLI, not the dashboard
  ASSERT NOT (dashboard_job_events(e)->-1->>'undoable')::boolean, 'a change made elsewhere is not offered';
  BEGIN PERFORM dashboard_undo_status(e, 'applied'); RAISE EXCEPTION 'a CLI change was undone';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  PERFORM dashboard_set_status(e, 'interview', 'applied');
  BEGIN PERFORM dashboard_undo_status(e, 'applied'); RAISE EXCEPTION 'undid with the wrong expected status';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  BEGIN PERFORM dashboard_undo_status(e, NULL); RAISE EXCEPTION 'undid without an expected status';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  BEGIN PERFORM dashboard_undo_status(e, 'interview', (SELECT min(id) FROM application_events WHERE job_id = e)); RAISE EXCEPTION 'undid an entry that is not the newest';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  PERFORM set_application_status(e, 'offer');                                              -- the CLI moves it on after the dashboard
  ASSERT NOT (dashboard_job_events(e)->0->>'undoable')::boolean AND NOT (dashboard_job_events(e)->-1->>'undoable')::boolean, 'nothing is offered once the CLI moved it on';
  BEGIN PERFORM dashboard_undo_status(e, 'interview'); RAISE EXCEPTION 'undid an old move after a newer change';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  ASSERT (SELECT status FROM applications WHERE job_id = e) = 'offer', 'nothing changed by the refusals';
  BEGIN PERFORM dashboard_undo_status(f, 'applied'); RAISE EXCEPTION 'undo with no application';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;
  BEGIN PERFORM dashboard_undo_status(-1, 'applied'); RAISE EXCEPTION 'unknown job';
  EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;

  -- an entry from before this migration has no starting point and cannot be undone
  PERFORM dashboard_set_status(f, 'applied', 'shortlisted');
  UPDATE application_events SET prev_job_status = NULL WHERE job_id = f;
  ASSERT NOT (dashboard_job_events(f)->-1->>'undoable')::boolean;
  BEGIN PERFORM dashboard_undo_status(f, 'applied'); RAISE EXCEPTION 'an old entry was undone';
  EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  UPDATE application_events SET prev_job_status = 'shortlisted' WHERE job_id = f;
  -- an application that has files is never removed by an undo
  UPDATE applications SET resume_path = '/output/x.pdf' WHERE job_id = f;
  ASSERT NOT (dashboard_job_events(f)->-1->>'undoable')::boolean, 'not offered for an application with files';
  BEGIN PERFORM dashboard_undo_status(f, 'applied'); RAISE EXCEPTION 'an application with files was removed';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;                          -- the application changed since the move
  ASSERT EXISTS (SELECT 1 FROM applications WHERE job_id = f);
  -- scam-blocked
  UPDATE jobs SET status = 'scam_blocked' WHERE id = f;
  BEGIN PERFORM dashboard_undo_status(f, 'applied'); RAISE EXCEPTION 'a scam-blocked job was changed';
  EXCEPTION WHEN SQLSTATE 'JD403' THEN NULL; END;

  -- the applied date: a move from "ready to apply" to applied sets it, and undoing that move clears it again
  a := pg_temp.dash_job(1601, 'shortlisted', 80);
  PERFORM set_application_status(a, 'approved');
  ASSERT (SELECT applied_at FROM applications WHERE job_id = a) IS NULL;
  PERFORM dashboard_set_status(a, 'applied', 'approved');
  ASSERT (SELECT applied_at FROM applications WHERE job_id = a) IS NOT NULL;
  PERFORM dashboard_undo_status(a, 'applied');
  ASSERT (SELECT applied_at FROM applications WHERE job_id = a) IS NULL AND (SELECT status FROM applications WHERE job_id = a) = 'approved'
         AND (SELECT status FROM jobs WHERE id = a) = 'approved', 'the move set the applied date and the undo cleared it';

  -- only the newest entry is ever offered, even when an older one set the status the job is in now
  b := pg_temp.dash_job(1602, 'shortlisted', 80);
  PERFORM dashboard_set_status(b, 'applied', 'shortlisted');
  PERFORM dashboard_set_status(b, 'interview', 'applied');
  PERFORM dashboard_set_status(b, 'applied', 'interview');
  ev := dashboard_job_events(b);
  ASSERT (ev->0->>'to_status') = 'applied' AND (ev->-1->>'to_status') = 'applied', 'the oldest and the newest both say applied';
  ASSERT NOT (ev->0->>'undoable')::boolean AND NOT (ev->1->>'undoable')::boolean AND (ev->-1->>'undoable')::boolean, 'only the newest of them is offered';

  -- belt and braces: if the status was changed without leaving an entry, the undo refuses
  c := pg_temp.dash_job(1603, 'shortlisted', 80);
  PERFORM dashboard_set_status(c, 'applied', 'shortlisted');
  PERFORM set_config('jobsearch.undo', 'on', true);
  UPDATE applications SET status = 'interview' WHERE job_id = c;                              -- no entry is made
  PERFORM set_config('jobsearch.undo', '', true);
  ASSERT NOT (dashboard_job_events(c)->-1->>'undoable')::boolean;
  BEGIN PERFORM dashboard_undo_status(c, 'applied'); RAISE EXCEPTION 'undid a move whose result had been changed';
  EXCEPTION WHEN SQLSTATE 'JD413' THEN NULL; END;

  -- the marker of an undo does not outlive it: the next change is recorded again
  a := pg_temp.dash_job(1401, 'shortlisted', 80);
  PERFORM dashboard_set_status(a, 'applied', 'shortlisted');
  PERFORM dashboard_undo_status(a, 'applied');
  ASSERT coalesce(current_setting('jobsearch.undo', true), '') = '';
  PERFORM dashboard_set_status(a, 'applied', 'shortlisted');
  ASSERT (SELECT count(*) FROM application_events WHERE job_id = a) = 1, 'a new move after an undo is recorded';
  -- the starting point is recorded for every kind of change
  SELECT count(*) INTO n FROM application_events WHERE source = 'dashboard' AND prev_job_status IS NULL;
  ASSERT n = 0, 'dashboard entries always carry their starting point';
END $$;

DO $$
DECLARE j bigint;
BEGIN
  j := pg_temp.dash_job(1501, 'shortlisted', 80);
  SET LOCAL ROLE jobsearch_dashboard;
  PERFORM dashboard_set_status(j, 'applied', 'shortlisted');
  ASSERT (dashboard_job_events(j)->-1->>'undoable')::boolean;
  ASSERT dashboard_undo_status(j, 'applied')->>'status' = 'shortlisted', 'the role can undo through the function';
  BEGIN PERFORM entry_is_undoable(1); RAISE EXCEPTION 'the role ran entry_is_undoable';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN DELETE FROM application_events; RAISE EXCEPTION 'the role deleted history directly';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
END $$;

-- the role may run the three functions and read nothing of the history
DO $$
DECLARE j bigint;
BEGIN
  j := pg_temp.dash_job(401, 'shortlisted', 80);
  SET LOCAL ROLE jobsearch_dashboard;
  ASSERT dashboard_job_events(j) = '[]'::jsonb AND dashboard_board() ? 'columns' AND dashboard_stats() ? 'weekly';
  ASSERT dashboard_set_status(j, 'applied', 'shortlisted') = 'applied';
  ASSERT jsonb_array_length(dashboard_job_events(j)) = 1;
  BEGIN PERFORM 1 FROM application_events; RAISE EXCEPTION 'the role read application_events';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN INSERT INTO application_events (job_id, to_status) VALUES (j, 'offer'); RAISE EXCEPTION 'the role wrote application_events';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  BEGIN INSERT INTO application_events (job_id, to_status, source) VALUES (j, 'offer', 'telegram'); RAISE EXCEPTION 'an unknown source was accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  DELETE FROM jobs WHERE id = j;
  ASSERT NOT EXISTS (SELECT 1 FROM application_events WHERE job_id = j), 'the history goes with the job';
END $$;

-- 021: Gmail status tracking, foundations ---------------------------------------------------------------------------------
-- the switch: off by default (a part that needs a Google login must be asked for), the other three untouched
DO $$
BEGIN
  ASSERT (SELECT column_default FROM information_schema.columns WHERE table_name = 'feature_settings' AND column_name = 'gmail') = 'false',
         'the Gmail switch defaults to off';
  ASSERT (SELECT count(*) FROM feature_settings) = 1, 'still one row of switches';
  UPDATE feature_settings SET gmail = false, tailoring = true, telegram = true, digest = true;
  UPDATE feature_settings SET gmail = true;
  ASSERT (SELECT gmail AND tailoring AND telegram AND digest FROM feature_settings), 'the switches are independent';
  UPDATE feature_settings SET gmail = false;
  BEGIN UPDATE feature_settings SET gmail = NULL; RAISE EXCEPTION 'a null switch accepted';
  EXCEPTION WHEN not_null_violation THEN NULL; END;
END $$;

-- the mail stage: one more schedule, every 30 minutes, 20 messages a run, never more often than every 15
DO $$
DECLARE r record; n int;
BEGIN
  SELECT * INTO r FROM schedule_settings WHERE name = 'mail';
  ASSERT r.every_minutes = 30 AND r.batch = 20 AND r.at_time IS NULL AND r.model IS NULL AND r.enabled, 'the default mail schedule';
  BEGIN UPDATE schedule_settings SET every_minutes = 14 WHERE name = 'mail'; RAISE EXCEPTION 'mail more often than every 15 minutes accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE schedule_settings SET every_minutes = 15 WHERE name = 'mail';
  UPDATE schedule_settings SET every_minutes = 30 WHERE name = 'mail';
  BEGIN UPDATE schedule_settings SET every_minutes = NULL, at_time = '08:00' WHERE name = 'mail'; RAISE EXCEPTION 'a daily mail schedule accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET batch = 0 WHERE name = 'mail'; RAISE EXCEPTION 'a mail batch of 0 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET batch = 51 WHERE name = 'mail'; RAISE EXCEPTION 'a mail batch of 51 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE schedule_settings SET model = 'claude-haiku-5-5' WHERE name = 'mail'; RAISE EXCEPTION 'a mail model accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO schedule_settings (name, every_minutes, batch) VALUES ('inbox', 60, 1); RAISE EXCEPTION 'an unknown stage accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;

  -- it is claimed like the others: once when due, not twice, and held back while its switch is off (p_skip)
  UPDATE schedule_settings SET last_run_at = NULL, run_now = false, enabled = true WHERE name = 'mail';
  ASSERT NOT EXISTS (SELECT 1 FROM claim_due_schedules(ARRAY['mail']) WHERE name = 'mail'), 'skipped while the Gmail part is off';
  SELECT count(*) INTO n FROM claim_due_schedules() WHERE name = 'mail';
  ASSERT n = 1, 'claimed as soon as it is no longer skipped';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'mail') = 0, 'and not twice in a row';
  UPDATE schedule_settings SET last_run_at = now() - interval '31 minutes' WHERE name = 'mail';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'mail') = 1, 'due again after 30 minutes';
  UPDATE schedule_settings SET last_run_at = now() - interval '10 minutes' WHERE name = 'mail';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'mail') = 0, 'not due after 10 minutes';
  UPDATE schedule_settings SET run_now = true WHERE name = 'mail';
  ASSERT (SELECT count(*) FROM claim_due_schedules(ARRAY['mail']) WHERE name = 'mail') = 0, 'a run request waits while the part is off';
  ASSERT (SELECT run_now FROM schedule_settings WHERE name = 'mail'), 'and is kept';
  ASSERT (SELECT count(*) FROM claim_due_schedules() WHERE name = 'mail') = 1, 'and runs once it is on';
  ASSERT (SELECT array_agg(name) FROM schedule_status()) = ARRAY['collect', 'score', 'tailor', 'digest', 'mail'], 'mail is listed last';
END $$;

-- the health panel knows the stage: "feature off" while the switch is off, and no alert for a stage that never ran
DO $$
DECLARE h jsonb; e jsonb;
BEGIN
  UPDATE schedule_control SET paused = false, paused_at = NULL, reason = NULL;
  UPDATE schedule_settings SET enabled = true, last_run_at = now();
  UPDATE feature_settings SET tailoring = true, telegram = true, digest = true, gmail = false;
  h := dashboard_health();
  SELECT x INTO e FROM jsonb_array_elements(h->'schedules') x WHERE x->>'name' = 'mail';
  ASSERT (e->>'feature_off')::boolean, 'mail is feature-off while Gmail is off';
  ASSERT (SELECT array_agg(x->>'name') FROM jsonb_array_elements(h->'schedules') x) = ARRAY['collect', 'score', 'tailor', 'digest', 'mail'];
  ASSERT h->'alerts' = '[]'::jsonb, 'no alert for a stage that is off: ' || (h->'alerts')::text;
  UPDATE feature_settings SET gmail = true;
  SELECT x INTO e FROM jsonb_array_elements(dashboard_health()->'schedules') x WHERE x->>'name' = 'mail';
  ASSERT NOT (e->>'feature_off')::boolean, 'mail is in effect when Gmail is on';
  -- the others are as before
  SELECT x INTO e FROM jsonb_array_elements(dashboard_health()->'schedules') x WHERE x->>'name' = 'digest';
  ASSERT NOT (e->>'feature_off')::boolean;
  -- a mail stage that has never run raises no alert, on or off: only collect and score can be "overdue"
  UPDATE schedule_settings SET last_run_at = NULL, updated_at = now() - interval '2 days' WHERE name = 'mail';
  UPDATE feature_settings SET gmail = true;
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'a never-run mail stage is not an alert: ' || (dashboard_health()->'alerts')::text;
  UPDATE feature_settings SET gmail = false;
  ASSERT dashboard_health()->'alerts' = '[]'::jsonb, 'nor while it is off';
  UPDATE schedule_settings SET last_run_at = now() WHERE name = 'mail';
  UPDATE feature_settings SET tailoring = false, gmail = false;
  ASSERT (SELECT bool_and((x->>'feature_off')::boolean) FROM jsonb_array_elements(dashboard_health()->'schedules') x WHERE x->>'name' IN ('tailor', 'mail')),
         'tailor and mail are both feature-off';
  UPDATE feature_settings SET tailoring = true;
END $$;

-- gmail_state: one row, sane limits
DO $$
DECLARE o jsonb;
BEGIN
  ASSERT (SELECT count(*) FROM gmail_state) = 1, 'one row of state';
  ASSERT (SELECT lookback_days = 60 AND NOT from_now AND search_cursor IS NULL AND token_status = 'none' AND last_error IS NULL FROM gmail_state),
         'a fresh install: 60 days back, never searched, no login';
  BEGIN INSERT INTO gmail_state (id) VALUES (2); RAISE EXCEPTION 'a second state row accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE gmail_state SET lookback_days = 0; RAISE EXCEPTION '0 days accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE gmail_state SET lookback_days = 366; RAISE EXCEPTION '366 days accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE gmail_state SET lookback_days = 1; UPDATE gmail_state SET lookback_days = 365; UPDATE gmail_state SET lookback_days = 60;
  BEGIN UPDATE gmail_state SET token_status = 'revoked'; RAISE EXCEPTION 'an unknown token status accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE gmail_state SET token_status = 'ok'; UPDATE gmail_state SET token_status = 'expired'; UPDATE gmail_state SET token_status = 'error'; UPDATE gmail_state SET token_status = 'none';
  BEGIN UPDATE gmail_state SET last_error = repeat('x', 301); RAISE EXCEPTION 'an overlong error accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE gmail_state SET from_now = NULL; RAISE EXCEPTION 'a null from_now accepted';
  EXCEPTION WHEN not_null_violation THEN NULL; END;
  o := gmail_overview();
  ASSERT o->>'lookback_days' = '60' AND o->>'token_status' = 'none' AND (o->>'messages_seen')::int = 0 AND (o->>'pending')::int = 0
         AND o->'search_cursor' = 'null'::jsonb, 'the overview of a fresh install: ' || o::text;
  ASSERT NOT (o::text ~* 'subject|excerpt'), 'the overview carries no email text';
END $$;

-- gmail_seen and email_suggestions: what is allowed in, and what is not
CREATE TEMP TABLE gm_ids (name text PRIMARY KEY, id bigint NOT NULL);
DO $$
DECLARE j1 bigint; j2 bigint; s1 bigint; s2 bigint; ev bigint; o jsonb;
BEGIN
  j1 := pg_temp.dash_job(6001, 'applied', 80);
  j2 := pg_temp.dash_job(6002, 'shortlisted', 70);
  INSERT INTO gm_ids VALUES ('j1', j1), ('j2', j2);

  -- gmail_seen: a message is recorded once; a bad outcome or an empty id is refused; deleting the job keeps the row
  INSERT INTO gmail_seen (message_id, thread_id, outcome, job_id) VALUES ('18f0a1', '18f0a0', 'suggested', j1);
  INSERT INTO gmail_seen (message_id, outcome) VALUES ('18f0a2', 'noise'), ('18f0a3', 'unmatched'), ('18f0a4', 'failed');
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES ('18f0a1', 'noise'); RAISE EXCEPTION 'a message was recorded twice';
  EXCEPTION WHEN unique_violation THEN NULL; END;
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES ('18f0a5', 'maybe'); RAISE EXCEPTION 'an unknown outcome accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES ('', 'noise'); RAISE EXCEPTION 'an empty message id accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES (repeat('a', 65), 'noise'); RAISE EXCEPTION 'an overlong message id accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO gmail_seen (message_id, outcome, job_id) VALUES ('18f0a6', 'suggested', -5); RAISE EXCEPTION 'a message about a job that does not exist accepted';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;

  -- a suggestion
  INSERT INTO email_suggestions (job_id, message_id, thread_id, received_at, from_domain, subject, excerpt, signal, proposed_status,
                                 status_at_proposal, confidence, source, flags)
  VALUES (j1, '18f0a1', '18f0a0', now() - interval '1 day', 'greenhouse.io', 'Interview invitation', 'We would like to schedule a call.',
          'interview', 'interview', 'applied', 'high', 'rules', ARRAY['date_from_email']) RETURNING id INTO s1;
  ASSERT (SELECT state = 'pending' AND decided_at IS NULL AND event_id IS NULL FROM email_suggestions WHERE id = s1), 'a new suggestion is pending';
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, '18f0a1', now(), 'x.com', 's', 'e', 'interview', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'two suggestions for one message accepted';
  EXCEPTION WHEN unique_violation THEN NULL; END;

  -- statuses an email can never propose; and every other field's allowed values
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h1', now(), 'x.com', 's', 'e', 'offer', 'hired', 'offer', 'high', 'rules');
    RAISE EXCEPTION 'hired accepted as a proposal';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h2', now(), 'x.com', 's', 'e', 'offer', 'offer_declined', 'offer', 'high', 'rules');
    RAISE EXCEPTION 'offer_declined accepted as a proposal';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h3', now(), 'x.com', 's', 'e', 'offer', 'in_progress', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'in_progress accepted as a proposal';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h4', now(), 'x.com', 's', 'e', 'newsletter', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'an unknown signal accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h5', now(), 'x.com', 's', 'e', 'interview', 'interview', 'applied', 'certain', 'rules');
    RAISE EXCEPTION 'an unknown confidence accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h6', now(), 'x.com', 's', 'e', 'interview', 'interview', 'applied', 'high', 'human');
    RAISE EXCEPTION 'an unknown source accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h7', now(), 'x.com', repeat('s', 301), 'e', 'interview', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'an overlong subject accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h8', now(), 'x.com', 's', repeat('e', 601), 'interview', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'an overlong excerpt accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'h9', now(), '', 's', 'e', 'interview', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'an empty sender domain accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
    VALUES (j1, 'h10', now(), 'x.com', 's', 'e', 'interview', 'interview', 'applied', 'high', 'rules', ARRAY['a','b','c','d','e','f','g','h','i','j','k']);
    RAISE EXCEPTION 'eleven flags accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (-5, 'h11', now(), 'x.com', 's', 'e', 'interview', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'a suggestion for a job that does not exist accepted';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;

  -- the status follows from the signal: a rejection cannot be filed as an offer, an acknowledgement cannot be an interview
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'm1', now(), 'x.com', 's', 'e', 'rejection', 'offer', 'applied', 'high', 'model');
    RAISE EXCEPTION 'a rejection was filed as an offer';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j1, 'm2', now(), 'x.com', 's', 'e', 'acknowledgement', 'interview', 'applied', 'high', 'rules');
    RAISE EXCEPTION 'an acknowledgement was filed as an interview';
  EXCEPTION WHEN check_violation THEN NULL; END;
  -- every signal has exactly its own status
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, state, decided_at)
  SELECT j1, 'sig-' || signal, now(), 'x.com', 's', 'e', signal, status, 'applied', 'low', 'rules', 'dismissed', now()
    FROM (VALUES ('acknowledgement', 'applied'), ('assessment', 'interview'), ('interview', 'interview'), ('offer', 'offer'), ('rejection', 'rejected')) v(signal, status);
  ASSERT (SELECT count(*) FROM email_suggestions WHERE message_id LIKE 'sig-%') = 5, 'all five signals are accepted with their own status';
  DELETE FROM email_suggestions WHERE message_id LIKE 'sig-%';

  -- pending means undecided, and only that
  BEGIN UPDATE email_suggestions SET decided_at = now() WHERE id = s1; RAISE EXCEPTION 'a pending suggestion with a decision time accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE email_suggestions SET state = 'accepted' WHERE id = s1; RAISE EXCEPTION 'an accepted suggestion with no decision time accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE email_suggestions SET state = 'done', decided_at = now() WHERE id = s1; RAISE EXCEPTION 'an unknown state accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;

  -- the counts in the overview follow the states
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, state, decided_at)
  VALUES (j2, '18f0b1', now(), 'lever.co', 'Application received', 'Thanks for applying.', 'acknowledgement', 'applied', 'shortlisted', 'high', 'rules', 'dismissed', now());
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, state, decided_at)
  VALUES (j2, '18f0b2', now(), 'lever.co', 'Unfortunately', 'We will not move forward.', 'rejection', 'rejected', 'shortlisted', 'medium', 'model', 'accepted', now())
  RETURNING id INTO s2;
  o := gmail_overview();
  ASSERT (o->>'pending')::int = 1 AND (o->>'accepted')::int = 1 AND (o->>'dismissed')::int = 1 AND (o->>'messages_seen')::int = 4, 'counts: ' || o::text;

  -- an accepted suggestion points at the history entry it created; if that entry goes, the suggestion stays
  INSERT INTO application_events (job_id, from_status, to_status, source) VALUES (j2, 'shortlisted', 'applied', 'dashboard') RETURNING id INTO ev;
  UPDATE email_suggestions SET event_id = ev WHERE id = s2;
  DELETE FROM application_events WHERE id = ev;
  ASSERT (SELECT event_id IS NULL AND state = 'accepted' FROM email_suggestions WHERE id = s2), 'deleting the history entry leaves the suggestion';

  -- deleting a job removes its suggestions, and keeps the "seen" row so the email is not looked at again
  DELETE FROM jobs WHERE id = j1;
  ASSERT NOT EXISTS (SELECT 1 FROM email_suggestions WHERE job_id = j1), 'a deleted job takes its suggestions with it';
  ASSERT (SELECT job_id IS NULL FROM gmail_seen WHERE message_id = '18f0a1'), 'the seen row stays, without the job';
END $$;

-- the dashboard's role reads and writes none of it directly, and cannot run the overview
DO $$
BEGIN
  SET LOCAL ROLE jobsearch_dashboard;
  ASSERT dashboard_health() ? 'schedules', 'the role still reads the health panel';
  BEGIN PERFORM 1 FROM gmail_state; RAISE EXCEPTION 'the role read gmail_state';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM gmail_seen; RAISE EXCEPTION 'the role read gmail_seen';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM email_suggestions; RAISE EXCEPTION 'the role read email_suggestions';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE gmail_state SET token_status = 'ok'; RAISE EXCEPTION 'the role wrote gmail_state';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES ('zz', 'noise'); RAISE EXCEPTION 'the role wrote gmail_seen';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_overview(); RAISE EXCEPTION 'the role ran gmail_overview';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- not merely unable to read the tables inside it: the function itself is not callable by the role (no PUBLIC grant)
  ASSERT NOT has_function_privilege('jobsearch_dashboard', 'gmail_overview()', 'EXECUTE'), 'the role was granted gmail_overview';
  BEGIN UPDATE feature_settings SET gmail = true; RAISE EXCEPTION 'the role flipped the Gmail switch';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
END $$;

-- 022: Gmail step 6c: the candidates a reply could be about, and recording one email's result ------------------------------------
DO $$
DECLARE ids jsonb := '{}'::jsonb; r record; n int;
BEGIN
  -- one job per case: name, job status, application status
  PERFORM pg_temp.dash_job(7001, 'applied', 80);       -- submitted
  PERFORM pg_temp.dash_job(7002, 'applied', 80);       -- interview
  PERFORM pg_temp.dash_job(7003, 'applied', 80);       -- rejected
  PERFORM pg_temp.dash_job(7004, 'applied', 80);       -- withdrawn
  PERFORM pg_temp.dash_job(7005, 'skipped', 80);       -- skipped
  PERFORM pg_temp.dash_job(7006, 'scam_blocked', 80);  -- scam
  PERFORM pg_temp.dash_job(7007, 'shortlisted', 80);   -- no application: may have been applied by hand
  PERFORM pg_temp.dash_job(7008, 'tailored', 80);      -- drafted
  PERFORM pg_temp.dash_job(7009, 'shortlisted', 80);   -- approved
  PERFORM pg_temp.dash_job(7010, 'new', NULL);         -- not even shortlisted
  PERFORM pg_temp.dash_job(7011, 'scored', 40);        -- below the threshold
  PERFORM pg_temp.dash_job(7012, 'shortlisted', 80);   -- an application the user skipped
  PERFORM pg_temp.dash_job(7013, 'applied', 80);       -- offer
  FOR r IN SELECT id, regexp_replace(company, '\D', '', 'g')::int AS n FROM jobs WHERE company LIKE 'DashCo 70%' LOOP
    ids := ids || jsonb_build_object(r.n::text, r.id);
  END LOOP;
  PERFORM set_application_status((ids->>'7001')::bigint, 'applied');
  PERFORM set_application_status((ids->>'7002')::bigint, 'interview');
  PERFORM set_application_status((ids->>'7003')::bigint, 'rejected');
  PERFORM set_application_status((ids->>'7004')::bigint, 'withdrawn');
  PERFORM set_application_status((ids->>'7005')::bigint, 'skipped');
  INSERT INTO applications (job_id, status, variant) VALUES ((ids->>'7008')::bigint, 'drafted', 'tailored');
  PERFORM set_application_status((ids->>'7009')::bigint, 'approved');
  PERFORM set_application_status((ids->>'7012')::bigint, 'skipped');
  PERFORM set_application_status((ids->>'7013')::bigint, 'offer');
  CREATE TEMP TABLE IF NOT EXISTS gm22 (k text PRIMARY KEY, v jsonb);
  INSERT INTO gm22 VALUES ('ids', ids);

  -- who may receive a reply
  SELECT count(*) INTO n FROM gmail_candidates() WHERE company LIKE 'DashCo 70%';
  ASSERT n = 8, 'eight candidates (7001-7004, 7007-7009, 7013; none of skipped, scam-blocked, new, scored): ' || n;
  ASSERT NOT EXISTS (SELECT 1 FROM gmail_candidates() WHERE job_id IN ((ids->>'7005')::bigint, (ids->>'7006')::bigint, (ids->>'7010')::bigint,
                                                                       (ids->>'7011')::bigint, (ids->>'7012')::bigint)),
         'skipped, scam-blocked, new, low-scored and user-skipped jobs are never candidates';
  ASSERT (SELECT app_status IS NULL FROM gmail_candidates() WHERE job_id = (ids->>'7007')::bigint), 'a shortlisted job with no application has no application status';
  ASSERT (SELECT app_status FROM gmail_candidates() WHERE job_id = (ids->>'7003')::bigint) = 'rejected', 'a final status is still a candidate (a late email is a conflict worth showing)';
  -- active submitted applications come first
  ASSERT (SELECT app_status FROM gmail_candidates() LIMIT 1) IN ('applied', 'interview', 'offer'), 'the first candidate is an active submitted application';
  ASSERT (SELECT count(*) FROM (SELECT * FROM gmail_candidates() LIMIT 3) t WHERE t.app_status IN ('applied', 'interview', 'offer')) = 3
         AND (SELECT count(*) FROM gmail_candidates(2)) = 2, 'a limit is honoured';
  ASSERT (SELECT count(*) FROM gmail_candidates(0)) = 1 AND (SELECT count(*) FROM gmail_candidates(-5)) = 1, 'a limit below 1 means 1';
END $$;

DO $$
DECLARE ids jsonb; j1 bigint; j2 bigint; res text; sug jsonb; off jsonb;
BEGIN
  SELECT v INTO ids FROM gm22 WHERE k = 'ids';
  j1 := (ids->>'7001')::bigint; j2 := (ids->>'7002')::bigint;
  sug := jsonb_build_object('received_at', '2026-10-12T09:30:00+00:00', 'from_domain', 'acme.com', 'subject', 'Interview invitation',
                            'excerpt', 'We would like to schedule a call.', 'signal', 'interview', 'proposed_status', 'interview',
                            'status_at_proposal', 'applied', 'confidence', 'high', 'source', 'rules', 'flags', jsonb_build_array('unverified_sender', 'date_from_email'));

  off := sug || '{"signal": "offer", "proposed_status": "offer"}'::jsonb;        -- j2 has no pending offer, so these reach the table's own rules

  -- the unclear outcome exists, an unknown one does not
  BEGIN INSERT INTO gmail_seen (message_id, outcome) VALUES ('u1', 'maybe'); RAISE EXCEPTION 'an unknown outcome accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  INSERT INTO gmail_seen (message_id, outcome, job_id) VALUES ('u1', 'unclear', j1);

  -- a suggestion and the sighting are recorded together
  res := gmail_record_result('m22a', 't22a', 'suggested', j1, sug);
  ASSERT res = 'suggested', res;
  ASSERT (SELECT state = 'pending' AND flags = ARRAY['unverified_sender', 'date_from_email'] AND signal = 'interview' AND job_id = j1
            FROM email_suggestions WHERE message_id = 'm22a'), 'the suggestion has its fields and flags';
  ASSERT (SELECT outcome = 'suggested' AND job_id = j1 FROM gmail_seen WHERE message_id = 'm22a');

  -- recording the same email again changes nothing
  res := gmail_record_result('m22a', 't22a', 'suggested', j1, sug);
  ASSERT res = 'noise', 'the same email twice: ' || res;
  ASSERT (SELECT count(*) FROM email_suggestions WHERE message_id = 'm22a') = 1 AND (SELECT count(*) FROM gmail_seen WHERE message_id = 'm22a') = 1;

  -- a second email that proposes the same status for the same job does not create a second pending suggestion
  res := gmail_record_result('m22b', 't22b', 'suggested', j1, sug);
  ASSERT res = 'noise' AND NOT EXISTS (SELECT 1 FROM email_suggestions WHERE message_id = 'm22b'), 'one pending suggestion per job and status';
  ASSERT (SELECT outcome FROM gmail_seen WHERE message_id = 'm22b') = 'noise', 'and the email is still recorded as looked at';
  -- a different status for the job, or the same status for another job, is its own suggestion
  res := gmail_record_result('m22c', 't22c', 'suggested', j1, sug || '{"signal": "rejection", "proposed_status": "rejected"}'::jsonb);
  ASSERT res = 'suggested', 'a different status is a different suggestion';
  res := gmail_record_result('m22d', 't22d', 'suggested', j2, sug);
  ASSERT res = 'suggested', 'the same status for another job is its own suggestion';
  -- once the first is decided, the next email may suggest it again
  UPDATE email_suggestions SET state = 'dismissed', decided_at = now() WHERE message_id = 'm22a';
  res := gmail_record_result('m22e', 't22e', 'suggested', j1, sug);
  ASSERT res = 'suggested', 'a decided suggestion no longer blocks a new one';

  -- outcomes without a suggestion
  ASSERT gmail_record_result('m22f', NULL, 'noise', NULL) = 'noise' AND gmail_record_result('m22g', 't', 'unmatched', NULL) = 'unmatched'
     AND gmail_record_result('m22h', 't', 'unclear', j1) = 'unclear' AND gmail_record_result('m22i', 't', 'failed', NULL) = 'failed';
  ASSERT (SELECT count(*) FROM gmail_seen WHERE message_id IN ('m22f', 'm22g', 'm22h', 'm22i')) = 4;

  -- what must be refused, and leaves nothing behind
  BEGIN PERFORM gmail_record_result('m22j', 't', 'noise', j1, sug); RAISE EXCEPTION 'a suggestion with the outcome noise accepted';
  EXCEPTION WHEN raise_exception THEN NULL; END;
  BEGIN PERFORM gmail_record_result('m22k', 't', 'suggested', NULL, sug); RAISE EXCEPTION 'a suggestion without a job accepted';
  EXCEPTION WHEN raise_exception THEN NULL; END;
  BEGIN PERFORM gmail_record_result('m22l', 't', 'maybe', NULL); RAISE EXCEPTION 'an unknown outcome accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN PERFORM gmail_record_result('m22m', 't', 'suggested', j2, off || '{"signal": "rejection"}'::jsonb); RAISE EXCEPTION 'a signal that does not fit its status accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN PERFORM gmail_record_result('m22n', 't', 'suggested', j2, off || jsonb_build_object('excerpt', repeat('x', 601))); RAISE EXCEPTION 'an overlong excerpt accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN PERFORM gmail_record_result('m22o', 't', 'suggested', j2, off || '{"proposed_status": "hired"}'::jsonb); RAISE EXCEPTION 'hired accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  ASSERT NOT EXISTS (SELECT 1 FROM gmail_seen WHERE message_id IN ('m22j', 'm22k', 'm22l', 'm22m', 'm22n', 'm22o'))
     AND NOT EXISTS (SELECT 1 FROM email_suggestions WHERE message_id IN ('m22j', 'm22k', 'm22l', 'm22m', 'm22n', 'm22o')),
         'a refused result leaves neither a suggestion nor a sighting';
  -- ten flags at most (the column's own rule)
  BEGIN PERFORM gmail_record_result('m22p', 't', 'suggested', j2, off || jsonb_build_object('flags', (SELECT jsonb_agg('f' || g) FROM generate_series(1, 11) g)));
        RAISE EXCEPTION 'eleven flags accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
END $$;

DO $$
DECLARE ids jsonb; j3 bigint; j4 bigint; res text; low jsonb; high jsonb; n int; pm text; pc text; pd text; ps text;
BEGIN
  SELECT v INTO ids FROM gm22 WHERE k = 'ids';
  j3 := (ids->>'7003')::bigint; j4 := (ids->>'7004')::bigint;
  low  := jsonb_build_object('received_at', '2026-10-12T09:30:00+00:00', 'from_domain', 'evil.example', 'subject', 'Job offer', 'excerpt', 'pay a fee',
                             'signal', 'offer', 'proposed_status', 'offer', 'status_at_proposal', 'applied', 'confidence', 'low', 'source', 'rules', 'flags', '["phishing_risk"]'::jsonb);
  high := low || '{"from_domain": "acme.com", "subject": "Offer letter", "excerpt": "pleased to offer you the role", "confidence": "high", "flags": []}'::jsonb;

  -- at most one pending suggestion per job and status, enforced by the table (two runs at once cannot make two)
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
  VALUES (j3, 'dupA', now(), 'x.com', 's', 'e', 'offer', 'offer', 'applied', 'low', 'rules');
  BEGIN
    INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
    VALUES (j3, 'dupB', now(), 'x.com', 's', 'e', 'offer', 'offer', 'applied', 'low', 'rules');
    RAISE EXCEPTION 'two pending suggestions for one job and status accepted';
  EXCEPTION WHEN unique_violation THEN NULL; END;
  UPDATE email_suggestions SET state = 'dismissed', decided_at = now() WHERE message_id = 'dupA';
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source)
  VALUES (j3, 'dupC', now(), 'x.com', 's', 'e', 'offer', 'offer', 'applied', 'low', 'rules');
  DELETE FROM email_suggestions WHERE message_id IN ('dupA', 'dupC');

  -- a spoof first, then the genuine email: the genuine one takes the place, it is not lost
  res := gmail_record_result('spoof1', 't', 'suggested', j4, low);
  ASSERT res = 'suggested' AND (SELECT confidence FROM email_suggestions WHERE message_id = 'spoof1') = 'low';
  res := gmail_record_result('real1', 't', 'suggested', j4, high);
  ASSERT res = 'suggested', 'a more trustworthy email replaces the pending one: ' || res;
  SELECT count(*), max(message_id), max(confidence), max(from_domain), max(subject) INTO n, pm, pc, pd, ps
    FROM email_suggestions WHERE job_id = j4 AND state = 'pending' AND proposed_status = 'offer';
  ASSERT n = 1 AND pm = 'real1' AND pc = 'high' AND pd = 'acme.com' AND ps = 'Offer letter', 'one pending suggestion, now the genuine email';
  ASSERT (SELECT flags = '{}' FROM email_suggestions WHERE message_id = 'real1'), 'its flags replaced the spoof''s';
  ASSERT (SELECT outcome FROM gmail_seen WHERE message_id = 'spoof1') = 'suggested' AND (SELECT outcome FROM gmail_seen WHERE message_id = 'real1') = 'suggested';
  -- an equal or less trustworthy one afterwards changes nothing
  res := gmail_record_result('spoof2', 't', 'suggested', j4, low);
  ASSERT res = 'noise' AND (SELECT message_id FROM email_suggestions WHERE job_id = j4 AND state = 'pending' AND proposed_status = 'offer') = 'real1', 'a worse email never displaces a better one';
  res := gmail_record_result('real2', 't', 'suggested', j4, high);
  ASSERT res = 'noise' AND (SELECT message_id FROM email_suggestions WHERE job_id = j4 AND state = 'pending' AND proposed_status = 'offer') = 'real1', 'an equal one does not displace it either';
  -- the same email again, as a replacement candidate, is still idempotent
  res := gmail_record_result('real1', 't', 'suggested', j4, high);
  ASSERT res = 'noise' AND (SELECT count(*) FROM email_suggestions WHERE job_id = j4) = 1;
  -- a decided suggestion is never overwritten
  UPDATE email_suggestions SET state = 'accepted', decided_at = now() WHERE message_id = 'real1';
  res := gmail_record_result('real3', 't', 'suggested', j4, high);
  ASSERT res = 'suggested' AND (SELECT state FROM email_suggestions WHERE message_id = 'real1') = 'accepted' AND (SELECT count(*) FROM email_suggestions WHERE job_id = j4) = 2,
         'an accepted suggestion stays as it was; a new email makes a new pending one';

  -- rescan: forget the cursor, re-open what was looked at but not acted on, keep suggestions and failures
  UPDATE gmail_state SET search_cursor = now();
  DELETE FROM gmail_seen WHERE message_id LIKE 'rs-%';
  INSERT INTO gmail_seen (message_id, outcome) VALUES ('rs-noise', 'noise'), ('rs-unm', 'unmatched'), ('rs-unc', 'unclear'), ('rs-sug', 'suggested'), ('rs-fail', 'failed');
  n := gmail_rescan();
  ASSERT n >= 3, 'at least the three re-opened rows were counted: ' || n;
  ASSERT (SELECT search_cursor IS NULL FROM gmail_state), 'the cursor is forgotten';
  ASSERT NOT EXISTS (SELECT 1 FROM gmail_seen WHERE message_id IN ('rs-noise', 'rs-unm', 'rs-unc')) AND (SELECT count(*) FROM gmail_seen WHERE message_id IN ('rs-sug', 'rs-fail')) = 2,
         'noise, unmatched and unclear are re-opened; suggested and failed stay';
  ASSERT (SELECT count(*) FROM email_suggestions WHERE job_id = j4) = 2, 'suggestions already made are untouched';
END $$;

DO $$
BEGIN
  SET LOCAL ROLE jobsearch_dashboard;
  BEGIN PERFORM gmail_candidates(); RAISE EXCEPTION 'the role ran gmail_candidates'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_record_result('zz', 't', 'noise', NULL); RAISE EXCEPTION 'the role ran gmail_record_result'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_rescan(); RAISE EXCEPTION 'the role ran gmail_rescan'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  ASSERT NOT has_function_privilege('jobsearch_dashboard', 'gmail_candidates(int)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_record_result(text, text, text, bigint, jsonb)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_rescan()', 'EXECUTE'), 'no PUBLIC grant';
END $$;


-- 023: Gmail step 6d: claiming unclear emails for the model, counted and parked; settling them -------------------------------------
DO $$
DECLARE ids jsonb; j1 bigint; j2 bigint; j7 bigint; j8 bigint; n int; r record; res text; sug jsonb; before_n int;
BEGIN
  SELECT v INTO ids FROM gm22 WHERE k = 'ids';
  j1 := (ids->>'7001')::bigint; j2 := (ids->>'7002')::bigint; j7 := (ids->>'7007')::bigint; j8 := (ids->>'7008')::bigint;
  DELETE FROM email_suggestions;
  DELETE FROM gmail_seen WHERE outcome = 'unclear';
  INSERT INTO gmail_seen (message_id, thread_id, outcome, job_id, seen_at) VALUES
    ('c-old',  't-old',  'unclear', j1, now() - interval '3 hours'),
    ('c-mid',  't-mid',  'unclear', j2, now() - interval '2 hours'),
    ('c-new',  't-new',  'unclear', j7, now() - interval '1 hour'),
    ('c-nojob', NULL,    'unclear', NULL, now()),
    ('c-noise', NULL,    'noise',   j1, now());
  ASSERT (SELECT classify_claims = 0 AND classify_claimed_at IS NULL FROM gmail_seen WHERE message_id = 'c-old'), 'new rows start unclaimed';
  BEGIN UPDATE gmail_seen SET classify_claims = 21 WHERE message_id = 'c-old'; RAISE EXCEPTION 'claims above 20 accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE gmail_seen SET classify_claims = -1 WHERE message_id = 'c-old'; RAISE EXCEPTION 'negative claims accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;

  -- the counts before anything is claimed: three waiting, none parked (the row without a job and the noise row are not counted)
  ASSERT gmail_unclear_counts() = '{"waiting": 3, "parked": 0}'::jsonb, gmail_unclear_counts()::text;

  -- a claim: newest first, counted, leased, with what the model step needs about the application
  SELECT count(*) INTO n FROM gmail_claim_unclear(2);
  ASSERT n = 2, 'two claimed: ' || n;
  ASSERT (SELECT classify_claims = 1 AND classify_claimed_at IS NOT NULL FROM gmail_seen WHERE message_id = 'c-new')
     AND (SELECT classify_claims = 1 FROM gmail_seen WHERE message_id = 'c-mid')
     AND (SELECT classify_claims = 0 FROM gmail_seen WHERE message_id = 'c-old'), 'the two newest were claimed and counted';
  SELECT * INTO r FROM gmail_claim_unclear(10);
  ASSERT r.message_id = 'c-old' AND r.job_id = j1 AND r.thread_id = 't-old' AND r.company = (SELECT company FROM jobs WHERE id = j1)
         AND r.job_status = 'applied' AND r.app_status = 'applied', 'what is returned: the email, its job, company, title and statuses';
  SELECT count(*) INTO n FROM gmail_claim_unclear(10);
  ASSERT n = 0, 'leased rows are not claimed again, and the row without a job and the noise row never are: ' || n;
  ASSERT (SELECT app_status IS NULL FROM gmail_claim_unclear(0) LIMIT 1) IS NULL, 'a limit of zero claims nothing';
  ASSERT (SELECT count(*) FROM gmail_claim_unclear(-3)) = 0 AND (SELECT count(*) FROM gmail_claim_unclear(1000)) = 0, 'limits are clamped';

  -- the lease runs out: claimable again, and counted again
  UPDATE gmail_seen SET classify_claimed_at = now() - interval '21 minutes' WHERE message_id = 'c-new';
  SELECT count(*) INTO n FROM gmail_claim_unclear(10);
  ASSERT n = 1 AND (SELECT classify_claims = 2 FROM gmail_seen WHERE message_id = 'c-new'), 'an expired lease is claimed again and counted: ' || n;

  -- a refund: the claim is given back (and a row that is no longer unclear is left alone)
  n := gmail_release_classify_claim('c-new');
  ASSERT n = 1 AND (SELECT classify_claims = 1 AND classify_claimed_at IS NULL FROM gmail_seen WHERE message_id = 'c-new'), 'refunded';
  ASSERT gmail_release_classify_claim('c-noise') = 0 AND gmail_release_classify_claim('no-such-id') = 0, 'nothing to refund for other rows';
  PERFORM gmail_release_classify_claim('c-new'); PERFORM gmail_release_classify_claim('c-new');
  ASSERT (SELECT classify_claims = 0 FROM gmail_seen WHERE message_id = 'c-new'), 'a refund never goes below zero';

  -- three claims park an email: never claimed again, counted as parked
  UPDATE gmail_seen SET classify_claims = 3, classify_claimed_at = NULL WHERE message_id = 'c-new';
  ASSERT (SELECT count(*) FROM gmail_claim_unclear(10) WHERE message_id = 'c-new') = 0, 'a parked email is not claimed';
  ASSERT gmail_unclear_counts() ->> 'parked' = '1', gmail_unclear_counts()::text;
  ASSERT gmail_unclear_counts(5) ->> 'parked' = '0', 'the limit is a parameter';
  ASSERT (SELECT count(*) FROM gmail_claim_unclear(10, 5) WHERE message_id = 'c-new') = 1, 'and so is it for a claim';
  UPDATE gmail_seen SET classify_claims = 3, classify_claimed_at = NULL WHERE message_id = 'c-new';

  -- settling
  sug := jsonb_build_object('received_at', '2026-10-12T09:30:00+00:00', 'from_domain', 'acme.com', 'subject', 'An update',
                            'excerpt', 'we will not be moving forward', 'signal', 'rejection', 'proposed_status', 'rejected',
                            'status_at_proposal', 'applied', 'confidence', 'medium', 'source', 'model', 'flags', jsonb_build_array('classified_by_model'));
  BEGIN PERFORM gmail_settle_unclear('c-old', 't-old', 'unmatched', j1); RAISE EXCEPTION 'settled as unmatched';
  EXCEPTION WHEN raise_exception THEN IF SQLERRM LIKE 'settled as%' THEN RAISE; END IF; END;
  BEGIN PERFORM gmail_settle_unclear('c-old', 't-old', 'suggested', j1); RAISE EXCEPTION 'suggested without a suggestion';
  EXCEPTION WHEN raise_exception THEN IF SQLERRM LIKE 'suggested without%' THEN RAISE; END IF; END;
  BEGIN PERFORM gmail_settle_unclear('c-old', 't-old', 'noise', j1, sug); RAISE EXCEPTION 'noise with a suggestion';
  EXCEPTION WHEN raise_exception THEN IF SQLERRM LIKE 'noise with%' THEN RAISE; END IF; END;
  res := gmail_settle_unclear('c-old', 't-old', 'noise', j2);
  ASSERT res = 'gone' AND (SELECT outcome = 'unclear' AND job_id = j1 FROM gmail_seen WHERE message_id = 'c-old'), 'an answer for another job than the email now has changes nothing';
  ASSERT (SELECT outcome = 'unclear' FROM gmail_seen WHERE message_id = 'c-old'), 'a refused settle changes nothing';

  res := gmail_settle_unclear('c-old', 't-old', 'suggested', j1, sug);
  ASSERT res = 'suggested', res;
  ASSERT (SELECT outcome = 'suggested' AND classify_claimed_at IS NULL FROM gmail_seen WHERE message_id = 'c-old'), 'the sighting is now suggested';
  ASSERT (SELECT source = 'model' AND signal = 'rejection' AND proposed_status = 'rejected' AND confidence = 'medium' AND state = 'pending'
            AND flags = ARRAY['classified_by_model'] AND job_id = j1 FROM email_suggestions WHERE message_id = 'c-old'), 'a model suggestion is stored as one';
  res := gmail_settle_unclear('c-old', 't-old', 'noise', j1);
  ASSERT res = 'gone', 'settling again changes nothing: gone';

  -- an equal suggestion for the same job and status is noise; a more trustworthy one takes the pending suggestion's place
  INSERT INTO gmail_seen (message_id, outcome, job_id) VALUES ('c-dup', 'unclear', j1), ('c-better', 'unclear', j1);
  res := gmail_settle_unclear('c-dup', NULL, 'suggested', j1, sug);
  ASSERT res = 'noise' AND (SELECT count(*) FROM email_suggestions WHERE job_id = j1 AND state = 'pending') = 1, 'an equal one is noise: ' || res;
  ASSERT (SELECT outcome = 'noise' FROM gmail_seen WHERE message_id = 'c-dup'), 'and is recorded as noise';
  res := gmail_settle_unclear('c-better', NULL, 'suggested', j1, sug || '{"confidence": "high"}'::jsonb);
  ASSERT res = 'suggested' AND (SELECT message_id = 'c-better' AND confidence = 'high' FROM email_suggestions WHERE job_id = j1 AND state = 'pending'), 'a better one replaces it';

  -- noise and failed
  res := gmail_settle_unclear('c-mid', 't-mid', 'noise', j2);
  ASSERT res = 'noise' AND (SELECT outcome = 'noise' FROM gmail_seen WHERE message_id = 'c-mid'), 'noise';
  INSERT INTO gmail_seen (message_id, outcome, job_id) VALUES ('c-fail', 'unclear', j8);
  res := gmail_settle_unclear('c-fail', NULL, 'failed', j8);
  ASSERT res = 'failed' AND (SELECT outcome = 'failed' FROM gmail_seen WHERE message_id = 'c-fail'), 'failed';

  -- a row that a rescan removed is not brought back
  res := gmail_settle_unclear('removed-by-rescan', NULL, 'noise', j1);
  ASSERT res = 'gone' AND NOT EXISTS (SELECT 1 FROM gmail_seen WHERE message_id = 'removed-by-rescan'), 'nothing is written for a row that is gone';

  -- the per-day count: only today's model calls for emails, in the budget's time zone
  before_n := gmail_classified_today();
  INSERT INTO llm_usage (provider, purpose, model, job_id, ok) VALUES ('anthropic', 'mail_classify', 'claude-haiku-5-5', j1, true), ('anthropic', 'mail_classify', 'claude-haiku-5-5', j1, false),
                                                                 ('anthropic', 'score', 'claude-haiku-5-5', j1, true), ('apify', 'mail_classify', NULL, NULL, true);
  INSERT INTO llm_usage (at, provider, purpose, model, job_id, ok) VALUES (now() - interval '2 days', 'anthropic', 'mail_classify', 'claude-haiku-5-5', j1, true);
  ASSERT gmail_classified_today() = before_n + 2, 'two calls today (a failed call counts too); not another purpose, provider or day: ' || (gmail_classified_today() - before_n);

  -- only jobs that can get a reply are claimed and counted; the predicate is gmail_candidates()'s rule
  ASSERT (SELECT bool_and((c.job_id IS NOT NULL) = gmail_can_get_reply(j.status, a.status))
            FROM jobs j LEFT JOIN applications a ON a.job_id = j.id LEFT JOIN gmail_candidates(1000) c ON c.job_id = j.id
           WHERE j.company LIKE 'DashCo 70%'), 'gmail_can_get_reply says the same as gmail_candidates for every test job';
  ASSERT NOT gmail_can_get_reply('scam_blocked', 'applied') AND NOT gmail_can_get_reply('shortlisted', 'skipped')
     AND NOT gmail_can_get_reply('new', NULL) AND NOT gmail_can_get_reply('scored', 'drafted') AND gmail_can_get_reply('shortlisted', NULL)
     AND gmail_can_get_reply('tailored', 'drafted') AND gmail_can_get_reply('applied', 'offer') AND gmail_can_get_reply('applied', 'withdrawn')
     AND NOT gmail_can_get_reply(NULL, NULL), 'the predicate, case by case';
  DELETE FROM gmail_seen WHERE outcome = 'unclear';
  INSERT INTO gmail_seen (message_id, outcome, job_id) VALUES
    ('e-skipped', 'unclear', (ids->>'7005')::bigint), ('e-scam', 'unclear', (ids->>'7006')::bigint),
    ('e-new', 'unclear', (ids->>'7010')::bigint), ('e-ok', 'unclear', j1);
  SELECT count(*) INTO n FROM gmail_claim_unclear(10);
  ASSERT n = 1 AND (SELECT classify_claims = 1 FROM gmail_seen WHERE message_id = 'e-ok')
     AND (SELECT count(*) = 0 FROM gmail_seen WHERE classify_claims > 0 AND message_id IN ('e-skipped', 'e-scam', 'e-new')),
         'a skipped, scam-blocked or not-yet-shortlisted job is never claimed: ' || n;
  ASSERT gmail_unclear_counts() = '{"waiting": 1, "parked": 0}'::jsonb, 'and not counted: ' || gmail_unclear_counts()::text;
  SELECT classify_claims INTO n FROM gmail_seen WHERE message_id = 'e-skipped';
  ASSERT n = 0;
  -- the job becomes a candidate again (it is un-scam-blocked): its email is claimable again
  UPDATE jobs SET status = 'shortlisted' WHERE id = (ids->>'7006')::bigint;
  SELECT count(*) INTO n FROM gmail_claim_unclear(10) WHERE message_id = 'e-scam';
  ASSERT n = 1, 'a job that can get a reply again has its email claimed';

  -- a rescan keeps the noise the model settled, re-opens the rules' noise, unmatched, unclear and parked rows
  DELETE FROM gmail_seen;
  INSERT INTO gmail_seen (message_id, outcome, job_id, classify_claims) VALUES
    ('r-model-noise', 'noise', j1, 1), ('r-rules-noise', 'noise', j1, 0), ('r-unm', 'unmatched', NULL, 0), ('r-parked', 'unclear', j1, 3),
    ('r-sug', 'suggested', j1, 1), ('r-fail', 'failed', NULL, 0);
  n := gmail_rescan();
  ASSERT n = 3 AND (SELECT array_agg(message_id ORDER BY message_id) FROM gmail_seen) = ARRAY['r-fail', 'r-model-noise', 'r-sug'],
         'a rescan keeps what the model settled (and suggested and failed): ' || n;

  -- a rescan clears the parked rows as well
  INSERT INTO gmail_seen (message_id, outcome, job_id, classify_claims) VALUES ('c-new', 'unclear', j7, 3);
  n := gmail_rescan();
  ASSERT NOT EXISTS (SELECT 1 FROM gmail_seen WHERE message_id = 'c-new') AND (SELECT count(*) FROM gmail_seen WHERE message_id IN ('r-fail', 'r-sug')) = 2, 'a rescan reopens parked rows; suggested and failed stay';
END $$;

DO $$
BEGIN
  SET LOCAL ROLE jobsearch_dashboard;
  BEGIN PERFORM gmail_claim_unclear(1); RAISE EXCEPTION 'the role ran gmail_claim_unclear'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_release_classify_claim('x'); RAISE EXCEPTION 'the role ran the release'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_settle_unclear('x', NULL, 'noise', 1); RAISE EXCEPTION 'the role ran the settle'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_unclear_counts(); RAISE EXCEPTION 'the role ran the counts'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_classified_today(); RAISE EXCEPTION 'the role ran the day count'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_can_get_reply('applied', 'applied'); RAISE EXCEPTION 'the role ran the predicate'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  ASSERT NOT has_function_privilege('jobsearch_dashboard', 'gmail_claim_unclear(int, int, interval)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_release_classify_claim(text)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_settle_unclear(text, text, text, bigint, jsonb)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_unclear_counts(int)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_classified_today()', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_can_get_reply(text, text)', 'EXECUTE'), 'no PUBLIC grant';
END $$;


-- 024: Gmail step 6e: the stage's record, and what the digest says about mail ------------------------------------------------------
DO $$
DECLARE ids jsonb; j1 bigint; j2 bigint; d jsonb; n int; sid1 bigint; sid2 bigint; sid3 bigint; o jsonb;
BEGIN
  SELECT v INTO ids FROM gm22 WHERE k = 'ids';
  j1 := (ids->>'7001')::bigint; j2 := (ids->>'7002')::bigint;
  DELETE FROM email_suggestions;
  UPDATE feature_settings SET gmail = true;
  UPDATE gmail_state SET token_status = 'ok', login_alerted_at = NULL, early_stops = 0, last_summary = NULL;

  -- the run record: counts stored, early stops counted in a row, a clean run resets them
  PERFORM gmail_record_run('{"search": {"opened": 3}}'::jsonb, true);
  PERFORM gmail_record_run('{"search": {"opened": 4}}'::jsonb, true);
  ASSERT (SELECT early_stops = 2 AND last_summary #>> '{search,opened}' = '4' AND last_run_at IS NOT NULL FROM gmail_state), 'two early runs in a row are counted';
  PERFORM gmail_record_run('{"search": {"opened": 5}}'::jsonb, false);
  ASSERT (SELECT early_stops = 0 FROM gmail_state), 'a clean run resets the count';
  BEGIN PERFORM gmail_record_run(('{"x": "' || repeat('a', 2100) || '"}')::jsonb, false); RAISE EXCEPTION 'a huge summary accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  o := gmail_overview();
  ASSERT o ? 'last_summary' AND o ? 'early_stops' AND (o->>'early_stops') = '0', 'the overview carries both';

  -- the digest: only pending suggestions that were not announced yet, newest first, no subject and no text
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  VALUES (j1, 'dg1', now(), 'acme.com', 'SECRET SUBJECT', 'SECRET EXCERPT', 'interview', 'interview', 'applied', 'medium', 'rules', '{}'),
         (j2, 'dg2', now(), 'beta.com', 'SECRET SUBJECT', 'SECRET EXCERPT', 'rejection', 'rejected', 'applied', 'high', 'model', ARRAY['classified_by_model']),
         (j1, 'dg3', now(), 'acme.com', 'SECRET SUBJECT', 'SECRET EXCERPT', 'offer', 'offer', 'applied', 'low', 'rules', ARRAY['phishing_risk']);
  SELECT id INTO sid1 FROM email_suggestions WHERE message_id = 'dg1'; SELECT id INTO sid2 FROM email_suggestions WHERE message_id = 'dg2'; SELECT id INTO sid3 FROM email_suggestions WHERE message_id = 'dg3';
  d := gmail_digest(5);
  ASSERT (d->>'total_new')::int = 3 AND jsonb_array_length(d->'suggestions') = 3 AND (d->>'login_alert') = 'false', d::text;
  ASSERT (d->'suggestions'->0->>'id')::bigint = sid3, 'newest first';
  ASSERT d::text NOT LIKE '%SECRET%', 'neither the subject nor the excerpt is in the digest';
  ASSERT (d->'suggestions'->0) ? 'company' AND (d->'suggestions'->0) ? 'title' AND (d->'suggestions'->0) ? 'flags' AND (d->'suggestions'->1->>'source') = 'model', 'what the message needs';
  ASSERT jsonb_array_length(gmail_digest(2)->'suggestions') = 2 AND (gmail_digest(2)->>'total_new')::int = 3, 'the limit cuts the list, not the count';
  ASSERT jsonb_array_length(gmail_digest(0)->'suggestions') = 1 AND jsonb_array_length(gmail_digest(1000)->'suggestions') = 3, 'limits are clamped';

  -- announced once
  n := gmail_digest_sent(ARRAY[sid1, sid2], false);
  ASSERT n = 2 AND (gmail_digest()->>'total_new')::int = 1, 'two were announced: ' || n;
  ASSERT gmail_digest_sent(ARRAY[sid1, sid2], false) = 0, 'announcing again changes nothing';
  ASSERT gmail_digest_sent(NULL, false) = 0 AND gmail_digest_sent('{}', false) = 0, 'nothing to announce';
  UPDATE email_suggestions SET state = 'dismissed', decided_at = now() WHERE id = sid3;
  ASSERT (gmail_digest()->>'total_new')::int = 0, 'a suggestion decided before it was announced is never announced';

  -- the login alert: due once while the login is not working, never while it works, re-armed by a working login
  ASSERT (gmail_digest()->>'login_alert') = 'false', 'no alert while the login works';
  UPDATE gmail_state SET token_status = 'expired';
  ASSERT (gmail_digest()->>'login_alert') = 'true', 'due while the login does not work';
  n := gmail_digest_sent(NULL, true);
  ASSERT (gmail_digest()->>'login_alert') = 'false' AND (SELECT login_alerted_at IS NOT NULL FROM gmail_state), 'said once';
  UPDATE gmail_state SET login_alerted_at = NULL;
  UPDATE gmail_state SET token_status = 'error';
  ASSERT (gmail_digest()->>'login_alert') = 'false', 'an error (a rate limit, an outage) is not an expired login';
  UPDATE gmail_state SET token_status = 'none';
  ASSERT (gmail_digest()->>'login_alert') = 'false', 'no login at all is not an expired one';
  UPDATE gmail_state SET token_status = 'ok', login_alerted_at = NULL;       -- what record_gmail_login does for a working login
  UPDATE gmail_state SET token_status = 'expired';
  ASSERT (gmail_digest()->>'login_alert') = 'true', 'a later expiry is said again';
  UPDATE gmail_state SET token_status = 'ok';
  PERFORM gmail_digest_sent(NULL, true);
  ASSERT (SELECT login_alerted_at IS NULL FROM gmail_state), 'a working login is never marked as announced';

  -- while the Gmail part is off the digest says nothing about mail
  UPDATE email_suggestions SET state = 'pending', decided_at = NULL, notified_at = NULL WHERE id = sid3;
  UPDATE gmail_state SET token_status = 'expired', login_alerted_at = NULL;
  UPDATE feature_settings SET gmail = false;
  d := gmail_digest(5);
  ASSERT (d->>'total_new') = '0' AND (d->>'login_alert') = 'false' AND jsonb_array_length(d->'suggestions') = 0, 'off: nothing: ' || d::text;
  UPDATE feature_settings SET gmail = true;
  UPDATE gmail_state SET token_status = 'ok';
END $$;

DO $$
BEGIN
  SET LOCAL ROLE jobsearch_dashboard;
  BEGIN PERFORM gmail_record_run('{}'::jsonb, false); RAISE EXCEPTION 'the role ran gmail_record_run'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_digest(1); RAISE EXCEPTION 'the role ran gmail_digest'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_digest_sent(NULL, false); RAISE EXCEPTION 'the role ran gmail_digest_sent'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_overview(); RAISE EXCEPTION 'the role ran gmail_overview'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  ASSERT NOT has_function_privilege('jobsearch_dashboard', 'gmail_record_run(jsonb, boolean)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_digest(int)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_digest_sent(bigint[], boolean)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'gmail_overview()', 'EXECUTE'), 'no PUBLIC grant, and the replaced overview kept its revoke';
END $$;


-- 025: Gmail step 6f: the dashboard's email inbox ------------------------------------------------------------------------------------
DO $$
DECLARE ids jsonb; j1 bigint; j2 bigint; j6 bigint; j7 bigint; inbox jsonb; it jsonb; r jsonb; sid bigint; sid2 bigint; sid3 bigint; ev bigint;
        st text; det text;
BEGIN
  SELECT v INTO ids FROM gm22 WHERE k = 'ids';
  j1 := (ids->>'7001')::bigint; j2 := (ids->>'7002')::bigint; j6 := (ids->>'7006')::bigint; j7 := (ids->>'7007')::bigint;
  DELETE FROM email_suggestions;
  UPDATE feature_settings SET gmail = true;
  UPDATE gmail_state SET token_status = 'ok', early_stops = 0;
  UPDATE jobs SET status = 'scam_blocked' WHERE id = j6;

  -- the transition rule, pair by pair (the same table as scraper/mail_rules.propose, which the rehearsal compares for every pair)
  ASSERT email_transition_ok('acknowledgement', 'none') AND email_transition_ok('acknowledgement', 'drafted') AND NOT email_transition_ok('acknowledgement', 'applied'), 'ack';
  ASSERT email_transition_ok('interview', 'applied') AND email_transition_ok('assessment', 'in_progress') AND NOT email_transition_ok('interview', 'interview')
     AND NOT email_transition_ok('interview', 'offer') AND NOT email_transition_ok('assessment', 'rejected'), 'interview and assessment';
  ASSERT email_transition_ok('offer', 'interview') AND NOT email_transition_ok('offer', 'offer') AND NOT email_transition_ok('offer', 'rejected') AND NOT email_transition_ok('offer', 'withdrawn'), 'offer';
  ASSERT email_transition_ok('rejection', 'interview') AND NOT email_transition_ok('rejection', 'rejected') AND NOT email_transition_ok('rejection', 'offer') AND NOT email_transition_ok('rejection', 'withdrawn'), 'rejection';
  ASSERT NOT email_transition_ok('hired', 'applied') AND NOT email_transition_ok(NULL, 'applied'), 'an unknown signal never changes anything';
  ASSERT email_needs_ack(ARRAY['conflict'], 'high') AND email_needs_ack(ARRAY['phishing_risk'], 'medium') AND email_needs_ack('{}', 'low')
     AND email_needs_ack(ARRAY['unverified_sender'], 'medium') AND email_needs_ack(ARRAY['display_name_only'], 'medium')
     AND email_needs_ack(ARRAY['unfamiliar_domain_ending'], 'medium') AND email_needs_ack(ARRAY['domain_matches_first_word_only'], 'medium')
     AND NOT email_needs_ack(ARRAY['classified_by_model', 'date_from_email', 'template_mixed_signals'], 'medium') AND NOT email_needs_ack(NULL, 'high'), 'which suggestions need a second look';

  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags) VALUES
    (j1, 'in1', now() - interval '2 hours', 'acme.com', 'Interview invitation', 'We would like to schedule a call.', 'interview', 'interview', 'applied', 'medium', 'rules', '{}'),
    (j1, 'in2', now() - interval '1 hour',  'acme.com', 'Unfortunately', 'we will not be moving forward', 'rejection', 'rejected', 'applied', 'medium', 'model', ARRAY['classified_by_model']),
    (j2, 'in3', now(), 'beta.com', 'Offer', 'pleased to offer you the position', 'offer', 'offer', 'interview', 'low', 'rules', ARRAY['phishing_risk']),
    (j6, 'in4', now(), 'gamma.com', 'Hello', 'thank you for applying', 'acknowledgement', 'applied', 'none', 'high', 'rules', '{}'),
    (j7, 'in5', now(), 'delta.com', 'Thanks', 'thank you for applying', 'acknowledgement', 'applied', 'none', 'high', 'rules', ARRAY['date_from_email']);
  SELECT id INTO sid FROM email_suggestions WHERE message_id = 'in1'; SELECT id INTO sid2 FROM email_suggestions WHERE message_id = 'in2'; SELECT id INTO sid3 FROM email_suggestions WHERE message_id = 'in3';

  -- the inbox: shape, counts, ordering, what the page may do
  inbox := dashboard_email_inbox('pending', 50);
  ASSERT (inbox->>'enabled') = 'true' AND (inbox->>'login') = 'ok' AND (inbox->'counts'->>'pending') = '5' AND jsonb_array_length(inbox->'items') = 5, inbox::text;
  it := (SELECT x FROM jsonb_array_elements(inbox->'items') x WHERE (x->>'id')::bigint = sid);
  ASSERT it->>'company' = (SELECT company FROM jobs WHERE id = j1) AND it->>'from_domain' = 'acme.com' AND it->>'subject' = 'Interview invitation'
     AND it->>'excerpt' LIKE 'We would like%' AND it->>'signal' = 'interview' AND it->>'proposed_status' = 'interview' AND it->>'current_status' = 'applied'
     AND (it->>'stale') = 'false' AND (it->>'can_accept') = 'true' AND (it->>'needs_ack') = 'false' AND (it->>'can_undo') = 'false', it::text;
  ASSERT (SELECT (x->>'needs_ack') FROM jsonb_array_elements(inbox->'items') x WHERE (x->>'id')::bigint = sid3) = 'true', 'a phishing flag needs a second look';
  ASSERT (SELECT (x->>'can_accept') FROM jsonb_array_elements(inbox->'items') x WHERE x->>'job_id' = j6::text) = 'false', 'a scam-blocked job cannot be accepted into';
  ASSERT (SELECT (x->>'needs_ack') FROM jsonb_array_elements(inbox->'items') x WHERE x->>'job_id' = j7::text) = 'false', 'a date note is not a trust flag';
  ASSERT (inbox->'items'->0->>'id')::bigint IN (SELECT id FROM email_suggestions WHERE message_id IN ('in3', 'in4', 'in5')), 'newest first';
  ASSERT jsonb_array_length(dashboard_email_inbox('pending', 2)->'items') = 2 AND jsonb_array_length(dashboard_email_inbox('pending', 0)->'items') = 1
     AND jsonb_array_length(dashboard_email_inbox('pending', 100000)->'items') = 5, 'limits are clamped';
  BEGIN PERFORM dashboard_email_inbox('superseded'); RAISE EXCEPTION 'a state that is not offered was accepted'; EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_email_inbox(NULL); RAISE EXCEPTION 'null accepted'; EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  ASSERT dashboard_email_count() = jsonb_build_object('enabled', true, 'pending', 5), dashboard_email_count()::text;
  UPDATE feature_settings SET gmail = false;
  ASSERT (dashboard_email_count()->>'enabled') = 'false' AND (dashboard_email_inbox()->>'enabled') = 'false' AND (dashboard_email_count()->>'pending') = '5', 'the inbox is still readable while the part is off';
  UPDATE feature_settings SET gmail = true;

  -- refusals
  BEGIN PERFORM dashboard_email_accept(-1, 'applied'); RAISE EXCEPTION 'no such suggestion accepted'; EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;
  BEGIN PERFORM dashboard_email_accept(sid3, 'interview'); RAISE EXCEPTION 'a flagged suggestion accepted without a second look'; EXCEPTION WHEN SQLSTATE 'JD422' THEN NULL; END;
  BEGIN PERFORM dashboard_email_accept((SELECT id FROM email_suggestions WHERE message_id = 'in4'), 'scam_blocked', true); RAISE EXCEPTION 'a scam-blocked job accepted'; EXCEPTION WHEN SQLSTATE 'JD403' THEN NULL; END;
  BEGIN PERFORM dashboard_email_accept(sid, 'shortlisted'); RAISE EXCEPTION 'a wrong expected status accepted';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN GET STACKED DIAGNOSTICS det = PG_EXCEPTION_DETAIL; ASSERT det = 'applied', 'the current status is in the detail: ' || coalesce(det, 'null'); END;
  ASSERT (SELECT state FROM email_suggestions WHERE id = sid) = 'pending', 'a refused accept changes nothing';
  ASSERT NOT EXISTS (SELECT 1 FROM application_events WHERE job_id = j1 AND to_status = 'interview'), 'and writes no history';

  -- accept: the board's own function, with its history entry and its Undo
  r := dashboard_email_accept(sid, 'applied');
  ASSERT r->>'status' = 'interview' AND (r->>'event_id') IS NOT NULL AND (r->>'job_id')::bigint = j1, r::text;
  ev := (r->>'event_id')::bigint;
  ASSERT (SELECT status FROM applications WHERE job_id = j1) = 'interview', 'the application moved';
  ASSERT (SELECT source = 'dashboard' AND to_status = 'interview' AND from_status = 'applied' FROM application_events WHERE id = ev), 'recorded in the history as a dashboard change';
  ASSERT (SELECT state = 'accepted' AND event_id = ev AND decided_at IS NOT NULL FROM email_suggestions WHERE id = sid), 'the suggestion is closed and tied to the entry';
  ASSERT (SELECT (x->>'can_undo') FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE (x->>'id')::bigint = sid) = 'true', 'it can be undone';
  -- the other pending suggestion for the job (a rejection at 'applied') is still meaningful from 'interview': it stays, and is not stale
  ASSERT (SELECT state FROM email_suggestions WHERE id = sid2) = 'pending'
     AND (SELECT (x->>'stale') FROM jsonb_array_elements(dashboard_email_inbox('pending')->'items') x WHERE (x->>'id')::bigint = sid2) = 'false', 'a rejection is still possible after an interview';
  BEGIN PERFORM dashboard_email_accept(sid, 'interview'); RAISE EXCEPTION 'an accepted suggestion accepted twice'; EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  r := dashboard_undo_status(j1, 'interview', ev);
  ASSERT (SELECT status FROM applications WHERE job_id = j1) = 'applied', 'Undo takes it back: ' || r::text;
  ASSERT (SELECT state FROM email_suggestions WHERE id = sid) = 'accepted', 'the suggestion stays accepted: it was acted on';
  BEGIN PERFORM dashboard_email_accept(sid, 'applied'); RAISE EXCEPTION 'an accepted suggestion accepted again after the undo';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  ASSERT (SELECT status FROM applications WHERE job_id = j1) = 'applied', 'and the second attempt changed nothing';
  ASSERT NOT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_email_inbox('pending')->'items') x WHERE (x->>'id')::bigint = sid)
     AND EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE (x->>'id')::bigint = sid)
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_email_inbox('dismissed')->'items') x WHERE (x->>'id')::bigint = sid), 'each tab holds only its own state';
  ASSERT (SELECT (x->>'can_undo') FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE (x->>'id')::bigint = sid) = 'false', 'and cannot be undone twice';

  -- stale: the job moved on so that the change no longer makes sense
  PERFORM dashboard_set_status(j1, 'rejected', 'applied');
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  VALUES (j1, 'in6', now(), 'acme.com', 'Invite', 'schedule a call', 'interview', 'interview', 'applied', 'medium', 'rules', '{}');
  it := (SELECT x FROM jsonb_array_elements(dashboard_email_inbox('pending')->'items') x WHERE x->>'subject' = 'Invite');
  ASSERT (it->>'stale') = 'true' AND (it->>'can_accept') = 'false', 'an interview invitation for a job that is now rejected is stale: ' || it::text;
  BEGIN PERFORM dashboard_email_accept((it->>'id')::bigint, 'rejected'); RAISE EXCEPTION 'a stale suggestion accepted';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN GET STACKED DIAGNOSTICS det = PG_EXCEPTION_DETAIL; ASSERT det = 'rejected', 'the current status is in the detail'; END;
  ASSERT (SELECT state FROM email_suggestions WHERE message_id = 'in6') = 'pending', 'refused, so still pending: it can only be dismissed';
  PERFORM dashboard_set_status(j1, 'applied', 'rejected');

  -- accepting leaves the other suggestions of the job alone: whether one still fits is worked out each time it is read
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  VALUES (j2, 'in7', now(), 'beta.com', 'Rejected', 'not moving forward', 'rejection', 'rejected', 'interview', 'medium', 'rules', '{}');
  r := dashboard_email_accept((SELECT id FROM email_suggestions WHERE message_id = 'in7'), 'interview');
  ASSERT r->>'status' = 'rejected', r::text;
  ASSERT (SELECT state FROM email_suggestions WHERE id = sid3) = 'pending', 'the offer suggestion is not closed behind your back';
  it := (SELECT x FROM jsonb_array_elements(dashboard_email_inbox('pending')->'items') x WHERE (x->>'id')::bigint = sid3);
  ASSERT (it->>'stale') = 'true' AND (it->>'can_accept') = 'false', 'it is shown as stale (an offer for a rejected job): ' || it::text;
  -- the undo of that accept brings everything back to how it was, siblings included
  ev := (r->>'event_id')::bigint;
  PERFORM dashboard_undo_status(j2, 'rejected', ev);
  it := (SELECT x FROM jsonb_array_elements(dashboard_email_inbox('pending')->'items') x WHERE (x->>'id')::bigint = sid3);
  ASSERT (it->>'stale') = 'false' AND (it->>'can_accept') = 'true', 'after the undo the offer suggestion is acceptable again: ' || it::text;
  it := (SELECT x FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE x->>'subject' = 'Rejected');
  ASSERT (it->>'can_restore') = 'true' AND (it->>'can_undo') = 'false', 'the accepted suggestion whose change was undone can be brought back: ' || it::text;
  r := dashboard_email_restore((it->>'id')::bigint);
  ASSERT r->>'state' = 'pending' AND (SELECT event_id IS NULL AND decided_at IS NULL FROM email_suggestions WHERE message_id = 'in7'), 'brought back as pending with no history entry';
  -- an accepted suggestion the job still reflects cannot be brought back
  r := dashboard_email_accept((SELECT id FROM email_suggestions WHERE message_id = 'in7'), 'interview');
  BEGIN PERFORM dashboard_email_restore((SELECT id FROM email_suggestions WHERE message_id = 'in7')); RAISE EXCEPTION 'an accepted suggestion the job reflects was brought back';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  ASSERT (SELECT (x->>'can_restore') FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE x->>'subject' = 'Rejected' LIMIT 1) = 'false', 'and is not offered';
  PERFORM dashboard_undo_status(j2, 'rejected', (r->>'event_id')::bigint);

  -- dismiss and restore
  PERFORM dashboard_email_dismiss((SELECT id FROM email_suggestions WHERE message_id = 'in6'));
  ASSERT (SELECT state = 'dismissed' AND decided_at IS NOT NULL FROM email_suggestions WHERE message_id = 'in6');
  BEGIN PERFORM dashboard_email_dismiss((SELECT id FROM email_suggestions WHERE message_id = 'in6')); RAISE EXCEPTION 'dismissed twice'; EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  BEGIN PERFORM dashboard_email_dismiss(-5); RAISE EXCEPTION 'unknown dismissed'; EXCEPTION WHEN SQLSTATE 'JD404' THEN NULL; END;
  r := dashboard_email_restore((SELECT id FROM email_suggestions WHERE message_id = 'in6'));
  ASSERT r->>'state' = 'pending' AND (SELECT decided_at IS NULL FROM email_suggestions WHERE message_id = 'in6'), 'restored';
  BEGIN PERFORM dashboard_email_restore((SELECT id FROM email_suggestions WHERE message_id = 'in6')); RAISE EXCEPTION 'a pending one restored'; EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  PERFORM dashboard_email_dismiss((SELECT id FROM email_suggestions WHERE message_id = 'in6'));
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  VALUES (j1, 'in9', now(), 'acme.com', 'Invite 2', 'schedule a call', 'interview', 'interview', 'applied', 'medium', 'rules', '{}');
  BEGIN PERFORM dashboard_email_restore((SELECT id FROM email_suggestions WHERE message_id = 'in6')); RAISE EXCEPTION 'restored over another pending one';
  EXCEPTION WHEN SQLSTATE 'JD409' THEN NULL; END;
  ASSERT (SELECT state FROM email_suggestions WHERE message_id = 'in6') = 'dismissed', 'and stays dismissed';

  -- an accepted suggestion the job moved past (not undone: its history entry is still there) can be reviewed again, with no entry attached
  UPDATE email_suggestions SET state = 'dismissed', decided_at = now() WHERE job_id = j7 AND state = 'pending';
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  VALUES (j7, 'in11', now(), 'delta.com', 'Thanks again', 'thank you for applying', 'acknowledgement', 'applied', 'none', 'high', 'rules', '{}');
  r := dashboard_email_accept((SELECT id FROM email_suggestions WHERE message_id = 'in11'), 'shortlisted');
  ASSERT (SELECT event_id IS NOT NULL FROM email_suggestions WHERE message_id = 'in11'), 'accepted with its history entry';
  PERFORM dashboard_set_status(j7, 'rejected', 'applied');
  it := (SELECT x FROM jsonb_array_elements(dashboard_email_inbox('accepted')->'items') x WHERE x->>'subject' = 'Thanks again');
  ASSERT (it->>'can_restore') = 'true' AND (it->>'can_undo') = 'false', 'the job moved on, so it can be reviewed again: ' || it::text;
  PERFORM dashboard_email_restore((it->>'id')::bigint);
  ASSERT (SELECT state = 'pending' AND event_id IS NULL AND decided_at IS NULL FROM email_suggestions WHERE message_id = 'in11'), 'pending again, with no history entry attached';

  -- the Health alerts, only while the part is on
  UPDATE gmail_state SET token_status = 'expired', early_stops = 0;
  ASSERT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_health()->'alerts') a WHERE a->>'code' = 'gmail_login' AND a->>'level' = 'bad'), 'an expired login is an alert';
  UPDATE gmail_state SET token_status = 'ok', early_stops = 3;
  ASSERT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_health()->'alerts') a WHERE a->>'code' = 'gmail_stalled' AND a->>'level' = 'warn')
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_health()->'alerts') a WHERE a->>'code' = 'gmail_login'), 'runs that keep ending early are another';
  UPDATE gmail_state SET early_stops = 2;
  ASSERT NOT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_health()->'alerts') a WHERE a->>'code' IN ('gmail_stalled', 'gmail_login')), 'two early runs are not an alert';
  UPDATE gmail_state SET token_status = 'expired', early_stops = 9;
  UPDATE feature_settings SET gmail = false;
  ASSERT NOT EXISTS (SELECT 1 FROM jsonb_array_elements(dashboard_health()->'alerts') a WHERE a->>'code' IN ('gmail_stalled', 'gmail_login')), 'nothing about mail while the part is off';
  UPDATE feature_settings SET gmail = true;
  UPDATE gmail_state SET token_status = 'ok', early_stops = 0;
END $$;

DO $$
DECLARE sid bigint; n int;
BEGIN
  INSERT INTO email_suggestions (job_id, message_id, received_at, from_domain, subject, excerpt, signal, proposed_status, status_at_proposal, confidence, source, flags)
  SELECT id, 'role1', now(), 'x.com', 's', 'e', 'acknowledgement', 'applied', 'none', 'high', 'rules', '{}' FROM jobs WHERE company LIKE 'DashCo 7008%' LIMIT 1;
  SET LOCAL ROLE jobsearch_dashboard;
  -- what the role may do
  PERFORM dashboard_email_inbox('pending', 5); PERFORM dashboard_email_count();
  -- what it may not
  BEGIN PERFORM 1 FROM email_suggestions; RAISE EXCEPTION 'the role read the suggestions table'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM gmail_seen; RAISE EXCEPTION 'the role read gmail_seen'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM gmail_state; RAISE EXCEPTION 'the role read gmail_state'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE email_suggestions SET state = 'accepted'; RAISE EXCEPTION 'the role wrote the suggestions table'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM email_transition_ok('interview', 'applied'); RAISE EXCEPTION 'the role ran a helper'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM gmail_digest(1); RAISE EXCEPTION 'the role ran the digest function'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  ASSERT has_function_privilege('jobsearch_dashboard', 'dashboard_email_accept(bigint, text, boolean)', 'EXECUTE')
     AND has_function_privilege('jobsearch_dashboard', 'dashboard_email_dismiss(bigint)', 'EXECUTE')
     AND has_function_privilege('jobsearch_dashboard', 'dashboard_email_restore(bigint)', 'EXECUTE')
     AND NOT has_function_privilege('public', 'dashboard_email_accept(bigint, text, boolean)', 'EXECUTE')
     AND NOT has_function_privilege('public', 'dashboard_email_inbox(text, int)', 'EXECUTE')
     AND NOT has_function_privilege('jobsearch_dashboard', 'email_needs_ack(text[], text)', 'EXECUTE'), 'grants: the five functions to the role, nothing to PUBLIC';
  -- the functions are pinned to a safe search_path
  ASSERT (SELECT bool_and(p.prosecdef AND p.proconfig @> ARRAY['search_path=public, pg_temp']) FROM pg_proc p
           WHERE p.proname IN ('dashboard_email_inbox', 'dashboard_email_count', 'dashboard_email_accept', 'dashboard_email_dismiss', 'dashboard_email_restore')), 'security definer with a pinned search_path';
END $$;

DO $$
BEGIN
  -- 026 (tailoring v2): the defaults change nothing, the values are checked, and the cache is not the dashboard's business
  ASSERT (SELECT engine = 'v2' AND gap_policy = 'omit' AND cover_style = 'evidence' AND tone = 'direct' AND reviewer AND reviewer_model = 'claude-haiku-5-5' AND company_research
            FROM tailor_settings WHERE id = 1), 'the v2 settings default to engine v2 (027) and the documented choices';
  BEGIN UPDATE tailor_settings SET engine = 'v1'; RAISE EXCEPTION 'engine accepted v1 after it was retired'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET engine = 'v3'; RAISE EXCEPTION 'engine accepted v3'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET gap_policy = 'hide'; RAISE EXCEPTION 'gap_policy accepted hide'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET cover_style = 'fancy'; RAISE EXCEPTION 'cover_style accepted fancy'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET tone = 'rude'; RAISE EXCEPTION 'tone accepted rude'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN UPDATE tailor_settings SET reviewer_model = 'two words'; RAISE EXCEPTION 'reviewer_model accepted a bad name'; EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE tailor_settings SET engine = 'v2', gap_policy = 'bridge', cover_style = 'classic', tone = 'formal', reviewer = false, company_research = false, reviewer_model = 'claude-haiku-5-5';
  ASSERT (SELECT engine = 'v2' AND NOT reviewer AND NOT company_research FROM tailor_settings WHERE id = 1), 'the v2 settings can be changed';
  UPDATE tailor_settings SET gap_policy = 'omit', cover_style = 'evidence', tone = 'direct', reviewer = true, company_research = true;

  INSERT INTO company_notes (company_key, facts, source_url) VALUES ('acme', '["Acme builds payment software."]', 'https://acme.com/');
  ASSERT (SELECT count(*) FROM company_notes WHERE company_key = 'acme') = 1 AND (SELECT fetched_at > now() - interval '1 minute' FROM company_notes WHERE company_key = 'acme'), 'a note is stored and stamped';
  BEGIN INSERT INTO company_notes (company_key, facts) VALUES ('four', '["a","b","c","d"]'); RAISE EXCEPTION 'four facts accepted'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO company_notes (company_key, facts) VALUES ('obj', '{"a": 1}'); RAISE EXCEPTION 'a non-list accepted'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO company_notes (company_key, facts) VALUES ('', '[]'); RAISE EXCEPTION 'an empty key accepted'; EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN INSERT INTO company_notes (company_key, facts, source_url) VALUES ('long', '[]', repeat('x', 301)); RAISE EXCEPTION 'a long url accepted'; EXCEPTION WHEN check_violation THEN NULL; END;
  SET LOCAL ROLE jobsearch_dashboard;
  BEGIN PERFORM 1 FROM company_notes; RAISE EXCEPTION 'the dashboard role read the company notes'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN UPDATE tailor_settings SET engine = 'v2'; RAISE EXCEPTION 'the dashboard role changed the engine'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RESET ROLE;
  ASSERT NOT has_table_privilege('public', 'company_notes', 'SELECT'), 'PUBLIC cannot read the company notes';
END $$;

DO $$
BEGIN
  -- 027 (v1 retired): a column default of v2, v1 refused by the database as well, and the setting is still changeable to v2
  ASSERT (SELECT column_default FROM information_schema.columns WHERE table_name = 'tailor_settings' AND column_name = 'engine') LIKE '%v2%', 'the default is v2';
  BEGIN UPDATE tailor_settings SET engine = 'V1'; RAISE EXCEPTION 'engine accepted V1'; EXCEPTION WHEN check_violation THEN NULL; END;
  UPDATE tailor_settings SET engine = 'v2';
  ASSERT (SELECT engine FROM tailor_settings WHERE id = 1) = 'v2', 'v2 is accepted';
END $$;

ROLLBACK;
\echo ALL SQL TESTS PASSED
