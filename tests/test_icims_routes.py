from applypilot.apply import icims
from applypilot.database import init_db


def _job(**overrides):
    job = {
        "url": "https://careers.amd.com/careers-home/jobs/90305?icims=1",
        "application_url": "https://careers.amd.com/careers-home/jobs/90305?icims=1",
        "title": "Graphics Software Engineer Intern/Co-op",
        "site": "AMD",
        "location": "Austin, TX",
    }
    job.update(overrides)
    return job


def test_classify_classic_vanity_and_other():
    assert icims.classify_listing("https://careers-amd.icims.com/jobs/90305/login") == icims.CLASSIC
    assert icims.classify_listing("https://careers.amd.com/jobs/90305?icims=1") == icims.VANITY
    assert icims.classify_listing("https://company.myworkdayjobs.com/job") == icims.NOT_ICIMS


def test_extract_req_id():
    assert icims.extract_req_id("https://careers.amd.com/jobs/90305?icims=1") == "90305"
    assert icims.extract_req_id("https://indeed.com/viewjob?jk=abc") == ""


def test_companies_match_aliases_and_initials():
    assert icims.companies_match("AMD", "Advanced Micro Devices")
    assert icims.companies_match("Johns Hopkins Applied Physics Laboratory", "JHU APL")
    assert not icims.companies_match("AMD", "Intel")


def test_score_mirror_requires_exact_req_and_rejects_icims_bounce():
    job = _job()
    good = icims.score_mirror(job, {
        "url": "https://www.indeed.com/viewjob?jk=abc123",
        "title": "Graphics Software Engineer Intern",
        "company": "Advanced Micro Devices",
        "location": "Austin, TX",
        "description": "Requisition 90305 intern opening",
        "source": "indeed",
    })
    assert good is not None
    assert good.confidence >= 0.98
    assert good.url.startswith("https://www.indeed.com/")

    bounce = icims.score_mirror(job, {
        "url": "https://careers-amd.icims.com/jobs/90305/apply",
        "title": "Graphics Software Engineer Intern",
        "company": "AMD",
        "location": "Austin, TX",
        "description": "90305",
        "source": "indeed",
    })
    assert bounce is None

    wrong_req = icims.score_mirror(job, {
        "url": "https://www.indeed.com/viewjob?jk=zzz",
        "title": "Graphics Software Engineer Intern",
        "company": "AMD",
        "location": "Austin, TX",
        "description": "job 11111",
        "job_url": "https://www.indeed.com/jobs/11111",
        "source": "indeed",
    })
    assert wrong_req is None


def test_score_mirror_rejects_title_only_without_req():
    job = _job()
    weak = icims.score_mirror(job, {
        "url": "https://www.indeed.com/viewjob?jk=nope",
        "title": "Software Engineer Intern",
        "company": "AMD",
        "location": "Austin, TX",
        "description": "general intern",
        "source": "indeed",
    })
    assert weak is None


def test_plan_vanity_without_probe():
    plan = icims.plan_job(_job())
    assert plan.classification == icims.VANITY
    assert plan.route == icims.ROUTE_VANITY


def test_plan_classic_skips_without_mirror():
    plan = icims.plan_job(_job(
        url="https://careers-foo.icims.com/jobs/90305/login",
        application_url="https://careers-foo.icims.com/jobs/90305/login",
    ))
    assert plan.route == icims.ROUTE_SKIP


def test_plan_classic_uses_high_confidence_mirror():
    job = _job(
        url="https://careers-foo.icims.com/jobs/90305/login",
        application_url="https://careers-foo.icims.com/jobs/90305/login",
    )
    plan = icims.plan_job(job, listings=[{
        "url": "https://www.indeed.com/viewjob?jk=abc123",
        "title": "Graphics Software Engineer Intern",
        "company": "AMD",
        "location": "Austin, TX",
        "description": "90305",
        "source": "indeed",
    }])
    assert plan.route == icims.ROUTE_APPLY_NETWORK
    assert "indeed.com" in plan.apply_url


def test_plan_hcaptcha_prefers_mirror_then_skips():
    job = _job()
    signals = {"hcaptcha": True, "classicHost": True, "authenticated": False}
    mirrored = icims.plan_from_signals(job, signals=signals, mirrors=[icims.Mirror(
        url="https://www.indeed.com/viewjob?jk=abc",
        title=job["title"],
        company="AMD",
        location="Austin, TX",
        source="indeed",
        confidence=0.99,
        reasons=["company", "title", "req:90305"],
    )])
    assert mirrored.route == icims.ROUTE_APPLY_NETWORK
    skipped = icims.plan_from_signals(job, signals=signals, mirrors=[])
    assert skipped.route == icims.ROUTE_SKIP


def test_plan_hcaptcha_uses_nopecha_when_enabled():
    job = _job()
    signals = {"hcaptcha": True, "classicHost": True, "authenticated": False}
    planned = icims.plan_from_signals(
        job, signals=signals, mirrors=[], captcha_provider="nopecha",
    )
    assert planned.route == icims.ROUTE_CAPTCHA_PROVIDER


def test_plan_existing_session_and_social_and_accountless():
    job = _job()
    session = icims.plan_from_signals(job, signals={
        "authenticated": True, "hcaptcha": False, "url": job["url"],
    })
    assert session.route == icims.ROUTE_EXISTING_SESSION
    social = icims.plan_from_signals(job, signals={
        "socialMicrosoft": True, "hcaptcha": False, "authenticated": False,
        "url": job["url"],
    })
    assert social.route == icims.ROUTE_SOCIAL
    guest = icims.plan_from_signals(job, signals={
        "skipAccount": True, "hcaptcha": False, "authenticated": False,
        "url": job["url"],
    })
    assert guest.route == icims.ROUTE_ACCOUNTLESS


def test_search_apply_network_uses_injected_scraper():
    job = _job()

    def fake_scrape(**kwargs):
        assert "indeed" in kwargs["site_name"]
        assert "90305" in kwargs["search_term"]
        return [{
            "url": "https://www.indeed.com/viewjob?jk=abc123",
            "title": "Graphics Software Engineer Intern",
            "company": "Advanced Micro Devices",
            "location": "Austin, TX",
            "description": "Requisition 90305",
            "source": "indeed",
        }]

    mirrors = icims.search_apply_network(job, scrape=fake_scrape)
    assert len(mirrors) == 1
    assert mirrors[0].confidence >= 0.98


def test_persist_plan_and_lab_jobs(tmp_path):
    conn = init_db(tmp_path / "icims-lab.db")
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, application_url, apply_status, apply_error, apply_attempts)
        VALUES (?, ?, ?, ?, 'failed', 'icims_lab_hold', 99)
        """,
        (
            "https://careers.amd.com/jobs/90305?icims=1",
            "Graphics Software Engineer Intern",
            "AMD",
            "https://careers.amd.com/jobs/90305?icims=1",
        ),
    )
    conn.commit()
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    plan = icims.plan_job(job)
    icims.persist_plan(conn, job["url"], plan)
    row = conn.execute("SELECT apply_route, icims_mirror_url FROM jobs").fetchone()
    assert row["apply_route"] == icims.ROUTE_VANITY
    held = icims.lab_jobs(conn, limit=5)
    assert held[0]["url"].endswith("90305?icims=1")


def test_icims_prompt_mentions_hcaptcha_and_social(tmp_path, monkeypatch):
    from applypilot.apply import codex_prompt as prompt

    profile = {
        "personal": {
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": "555-0100",
            "password": "secret-password",
            "address": "", "city": "", "province_state": "", "postal_code": "", "country": "",
        },
        "work_authorization": {},
        "compensation": {},
        "eeo_voluntary": {},
        "education": {},
        "availability": {},
        "application_authorizations": {},
        "legal_and_logistics": {},
        "application_writing": {},
    }
    base = tmp_path / "resume"
    base.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    text = prompt.build_prompt(
        job={
            "url": "https://careers.amd.com/jobs/90305?icims=1",
            "application_url": "https://careers.amd.com/jobs/90305?icims=1",
            "title": "Graphics Software Engineer Intern",
            "site": "AMD",
            "fit_score": 10,
            "tailored_resume_path": str(base.with_suffix(".txt")),
            "apply_route": "icims_social_auth",
        },
        tailored_resume="facts",
        upload_dir=tmp_path / "worker",
    )
    lower = text.lower()
    assert "icims / apply channel rules" in lower
    assert "hcaptcha" in lower
    assert "continue with microsoft" in lower
    assert profile["personal"]["password"] not in text


def test_classify_probe_signals_allow_block_try():
    assert icims.classify_probe_signals(None) == icims.POLICY_TRY
    assert icims.classify_probe_signals({
        "hcaptcha": True, "classicHost": False, "followedApply": True,
        "hostname": "careers.amd.com",
    }) == icims.POLICY_BLOCK
    assert icims.classify_probe_signals({
        "hcaptcha": False, "classicHost": True, "followedApply": True,
        "hostname": "careers-foo.icims.com",
    }) == icims.POLICY_BLOCK
    assert icims.classify_probe_signals({
        "hcaptcha": False, "classicHost": False, "followedApply": True,
        "hostname": "careers.cvent.com",
    }) == icims.POLICY_ALLOW
    assert icims.classify_probe_signals({
        "hcaptcha": False, "classicHost": False, "followedApply": False,
        "hostname": "careers.amd.com",
    }) == icims.POLICY_TRY


def test_host_policy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(icims, "HOST_POLICY_PATH", tmp_path / "icims_hosts.json")
    icims.record_host_policy("careers.amd.com", icims.POLICY_BLOCK, "survey")
    icims.record_host_policy("careers.cvent.com", icims.POLICY_ALLOW, "prior_applied")
    assert icims.host_decision("https://careers.amd.com/jobs/1?icims=1") == icims.POLICY_BLOCK
    assert icims.host_decision("https://careers.cvent.com/jobs/1?icims=1") == icims.POLICY_ALLOW
    assert "careers.amd.com" in icims.blocked_hostnames()
