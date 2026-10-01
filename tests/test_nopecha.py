import json

from applypilot.apply import capsolver, captcha, icims, launcher, nopecha
from applypilot.database import init_db


def test_nopecha_handles_hcaptcha_instead_of_capsolver(monkeypatch):
    called = []
    cap_called = []
    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr(
        "applypilot.apply.nopecha.try_solve_on_cdp",
        lambda port: called.append(port) or True,
    )
    monkeypatch.setattr("applypilot.apply.capsolver.is_enabled", lambda: True)
    monkeypatch.setattr(
        "applypilot.apply.capsolver.try_solve_on_cdp",
        lambda port: cap_called.append(port) or True,
    )
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: {
            "type": "hcaptcha",
            "websiteKey": "9ff49460-test",
            "websiteURL": "https://careers-example.icims.com/login",
        },
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=40,
        port=9362,
        allow_manual_wait=False,
    )
    assert result == (True, None)
    assert result.provider == "nopecha"
    assert result.checkpoint == captcha.CHECKPOINT_ATTEMPT_COMPLETED
    assert called == [9362]
    assert cap_called == []


def test_capsolver_still_used_for_recaptcha(monkeypatch):
    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr("applypilot.apply.nopecha.try_solve_on_cdp", lambda port: False)
    monkeypatch.setattr("applypilot.apply.capsolver.is_enabled", lambda: True)
    monkeypatch.setattr("applypilot.apply.capsolver.try_solve_on_cdp", lambda port: True)
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: {
            "type": "recaptcha_v2",
            "websiteKey": "6LeExample",
            "websiteURL": "https://company.myworkdayjobs.com/apply",
        },
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=1,
        port=9323,
        allow_manual_wait=False,
    )
    assert result == (True, None)
    assert result.provider == "capsolver"


def test_nopecha_timeout_is_distinct(monkeypatch):
    def boom(port):
        raise nopecha.NopechaError("timeout", "Timed out waiting")

    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr("applypilot.apply.nopecha.try_solve_on_cdp", boom)
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: {"type": "hcaptcha", "websiteKey": "abc", "websiteURL": "https://x"},
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=40,
        port=9362,
        allow_manual_wait=False,
    )
    assert result.solved is False
    assert result.checkpoint == captcha.CHECKPOINT_TIMEOUT


def test_load_jobs_csv(tmp_path):
    path = tmp_path / "icims_jobs.csv"
    path.write_text(
        "company,title,location,url,application_url,apply_status,apply_error,icims_kind\n"
        "Shure,Software Engineer Intern,Niles IL,https://careersus-shure.icims.com/jobs/4956/job,"
        "https://careersus-shure.icims.com/jobs/4956/job,failed,icims_unsupported,classic_icims.com\n",
        encoding="utf-8",
    )
    rows = icims.load_jobs_csv(path)
    assert len(rows) == 1
    assert rows[0]["site"] == "Shure"
    assert "4956" in rows[0]["url"]


def test_duplicate_block_reason(tmp_path):
    conn = init_db(tmp_path / "dup.db")
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, application_url, apply_status, apply_error)
        VALUES ('https://careersus-shure.icims.com/jobs/4956/job', 'SWE Intern', 'Shure',
                'https://careersus-shure.icims.com/jobs/4956/job', 'applied', NULL)
        """
    )
    conn.commit()
    job = {
        "url": "https://careersus-shure.icims.com/jobs/4956/job",
        "application_url": "https://careersus-shure.icims.com/jobs/4956/job",
        "site": "Shure",
        "title": "SWE Intern",
    }
    assert icims.duplicate_block_reason(conn, job) == "already_applied"


def test_experiment_sanitizer_drops_tokens():
    from applypilot.apply.experiment import sanitize_record

    cleaned = sanitize_record({
        "company": "Shure",
        "password": "secret",
        "provider_result": "captcha_attempt_completed",
        "h-captcha-response": "P0_eyJtoken",
    })
    assert "password" not in cleaned
    assert "h-captcha-response" not in cleaned
    assert cleaned["company"] == "Shure"


def test_hcaptcha_sitekey_comes_from_iframe_hash():
    url = (
        "https://newassets.hcaptcha.com/captcha/v1/"
        "481ef825909d36ec3d5777ef46cc1c8557f355/static/hcaptcha.html"
        "#frame=checkbox&sitekey=9ff49460-aaaa-bbbb-cccc-ddddeeee0000&rqdata=eyJ0eXAi"
    )
    params = capsolver.hcaptcha_params_from_url(url)
    assert params["sitekey"] == "9ff49460-aaaa-bbbb-cccc-ddddeeee0000"
    assert params["rqdata"] == "eyJ0eXAi"


def test_merge_hcaptcha_uses_parent_page_url(monkeypatch):
    merged = capsolver.merge_hcaptcha_detection(
        [{"type": "hcaptcha", "websiteKey": "", "websiteURL": "https://newassets.hcaptcha.com/captcha"}],
        page_url="https://careers-blackhawknetwork.icims.com/jobs/26868/login",
        frame_urls=[
            "https://careers-blackhawknetwork.icims.com/jobs/26868/login",
            "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"
            "#frame=checkbox&sitekey=bbbbbbbb-1111-2222-3333-444444444444",
        ],
    )
    assert merged is not None
    assert merged["websiteKey"] == "bbbbbbbb-1111-2222-3333-444444444444"
    assert merged["websiteURL"].startswith("https://careers-blackhawknetwork.icims.com/")


def test_fleet_workers_never_call_nopecha(monkeypatch):
    called = []
    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr(
        "applypilot.apply.nopecha.try_solve_on_cdp",
        lambda port: called.append(port) or True,
    )
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: {
            "type": "hcaptcha",
            "websiteKey": "9ff49460-test",
            "websiteURL": "https://careers-example.icims.com/login",
        },
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=7,
        port=9329,
        allow_manual_wait=False,
    )
    assert result == (False, "hcaptcha")
    assert result.provider == "none"
    assert called == []


def test_lab_worker_tries_nopecha_when_inspect_misses_widget(monkeypatch):
    from applypilot.apply import captcha, launcher, nopecha

    called = []
    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr(
        "applypilot.apply.nopecha.try_solve_on_cdp",
        lambda port: called.append(port) or True,
    )
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: None,
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=40,
        port=9362,
        allow_manual_wait=False,
    )
    assert result.solved is True
    assert result.provider == "nopecha"
    assert called == [9362]
    from applypilot.apply import chrome

    killed = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: killed.append(("tree", pid)))
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: killed.append(("port", port)))
    with chrome._chrome_lock:
        chrome._chrome_procs[40] = type("P", (), {"poll": lambda self: None, "pid": 1})()
    chrome.kill_all_chrome()
    assert ("port", 9362) in killed
    assert ("port", 9322) not in killed


def test_seed_lab_session_cookies_skips_preferences(tmp_path):
    from applypilot.apply import chrome

    src = tmp_path / "src" / "Default"
    dst = tmp_path / "dst"
    src.mkdir(parents=True)
    (src / "Cookies").write_text("cookie-db")
    (src / "Preferences").write_text('{"extensions":{"ui":{"developer_mode":false}}}')
    chrome._seed_lab_session_cookies(dst, tmp_path / "src")
    assert (dst / "Default" / "Cookies").read_text() == "cookie-db"
    assert not (dst / "Default" / "Preferences").exists()
    chrome._ensure_lab_extension_prefs(dst)
    prefs = json.loads((dst / "Default" / "Preferences").read_text())
    assert prefs["extensions"]["ui"]["developer_mode"] is True


def test_write_nopecha_extension_api_key(tmp_path):
    ext = tmp_path / "nopecha"
    ext.mkdir()
    (ext / "manifest.json").write_text(
        json.dumps({
            "name": "NopeCHA: CAPTCHA Solver",
            "version": "0.6.1",
            "manifest_version": 3,
            "nopecha": {"key": "", "enabled": True, "hcaptcha_auto_solve": True},
        }),
        encoding="utf-8",
    )
    nopecha.write_extension_api_key(ext, "I-TESTKEY")
    data = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert data["nopecha"]["key"] == "I-TESTKEY"
    assert data["nopecha"]["hcaptcha_auto_open"] is True
    assert data["nopecha"]["mouse_visualization"] is False


def test_graphical_manifest_skips_key_write(tmp_path):
    ext = tmp_path / "nopecha"
    ext.mkdir()
    (ext / "manifest.json").write_text(
        json.dumps({"name": "NopeCHA: CAPTCHA Solver", "version": "0.6.1", "manifest_version": 3}),
        encoding="utf-8",
    )
    nopecha.write_extension_api_key(ext, "I-TESTKEY")
    data = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert "nopecha" not in data
    assert "I-TESTKEY" not in (ext / "manifest.json").read_text(encoding="utf-8")


def test_lab_extension_waits_instead_of_token_api(monkeypatch):
    waited = []
    posted = []
    monkeypatch.setattr("applypilot.apply.nopecha.is_enabled", lambda: True)
    monkeypatch.setattr("applypilot.apply.nopecha.use_chrome_extension", lambda: True)
    monkeypatch.setattr(
        "applypilot.apply.nopecha.wait_for_extension_solve",
        lambda port: waited.append(port) or True,
    )
    monkeypatch.setattr(
        "applypilot.apply.nopecha.try_solve_on_cdp",
        lambda port: posted.append(port) or True,
    )
    monkeypatch.setattr(
        "applypilot.apply.capsolver.inspect_live_captcha",
        lambda port: {"type": "hcaptcha", "websiteKey": "abc", "websiteURL": "https://x"},
    )
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    result = launcher._resolve_captcha(
        {"title": "Intern", "site": "Example"},
        worker_id=40,
        port=9362,
        allow_manual_wait=False,
    )
    assert result.solved is True
    assert result.provider == "nopecha"
    assert waited == [9362]
    assert posted == []
    assert "extension" in captcha.provider_config_id()


def test_extension_service_worker_present(monkeypatch):
    payload = [
        {"type": "page", "url": "about:blank", "title": ""},
        {
            "type": "service_worker",
            "url": "chrome-extension://abc/assets/4ncg2v.js",
            "title": "Service Worker chrome-extension://abc/assets/4ncg2v.js",
        },
    ]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Resp())
    monkeypatch.setenv("NOPECHA_API_KEY", "I-TESTKEY")
    assert nopecha.extension_service_worker_present(9362) is True
    nopecha.mark_extension_running(True)
    assert nopecha.use_chrome_extension() is True
    nopecha.mark_extension_running(False)
    assert nopecha.use_chrome_extension() is False


def test_chrome_for_testing_prefers_newest(tmp_path, monkeypatch):
    from applypilot import config as apconfig

    cache = tmp_path / "ms-playwright"
    for rev, name in ((10, "old"), (99, "new")):
        exe = (
            cache / f"chromium-{rev}" / "chrome-mac-arm64"
            / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
        )
        exe.parent.mkdir(parents=True)
        exe.write_text(name)
        exe.chmod(0o755)
    monkeypatch.setattr(apconfig, "_playwright_cache_roots", lambda: [cache])
    monkeypatch.delenv("CHROME_FOR_TESTING_PATH", raising=False)
    monkeypatch.setattr(apconfig.platform, "system", lambda: "Darwin")
    path = apconfig.get_chrome_for_testing_path()
    assert path is not None
    assert "chromium-99" in path
