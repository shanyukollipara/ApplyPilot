from applypilot.apply import capsolver, launcher
from applypilot.apply import codex_prompt as prompt
from applypilot.wizard import init as wizard


def test_is_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    assert capsolver.is_enabled() is False
    monkeypatch.setenv("CAPSOLVER_API_KEY", "  CAP-TEST  ")
    assert capsolver.get_api_key() == "CAP-TEST"
    assert capsolver.is_enabled() is True


def test_task_from_detection_maps_supported_types():
    task = capsolver.task_from_detection({
        "type": "recaptcha_v2",
        "websiteURL": "https://jobs.example/apply",
        "websiteKey": "6LeExample",
    })
    assert task == {
        "type": "ReCaptchaV2TaskProxyLess",
        "websiteURL": "https://jobs.example/apply",
        "websiteKey": "6LeExample",
    }
    assert capsolver.task_from_detection({"type": "turnstile", "websiteURL": "https://x"}) is None


def test_solve_creates_task_then_polls(monkeypatch):
    calls: list[str] = []

    def fake_post(path, payload):
        calls.append(path)
        if path == "/createTask":
            assert payload["task"]["type"] == "ReCaptchaV2TaskProxyLess"
            return {"errorId": 0, "taskId": "task-1"}
        return {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "tok"}}

    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-TEST")
    monkeypatch.setattr(capsolver, "_api_post", fake_post)

    solution = capsolver.solve({
        "type": "ReCaptchaV2TaskProxyLess",
        "websiteURL": "https://jobs.example/apply",
        "websiteKey": "6LeExample",
    })

    assert solution["gRecaptchaResponse"] == "tok"
    assert calls == ["/createTask", "/getTaskResult"]


def test_get_balance(monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-TEST")
    monkeypatch.setattr(capsolver, "_api_post", lambda path, payload: {"errorId": 0, "balance": 12.5})
    assert capsolver.get_balance() == 12.5


def test_write_extension_api_key(tmp_path):
    ext = tmp_path / "ext"
    (ext / "assets").mkdir(parents=True)
    (ext / "assets" / "config.js").write_text(
        "export const defaultConfig = {\n  apiKey: '',\n  useCapsolver: false,\n};\n",
        encoding="utf-8",
    )

    capsolver.write_extension_api_key(ext, "CAP-TEST")
    text = (ext / "assets" / "config.js").read_text(encoding="utf-8")

    assert "apiKey: 'CAP-TEST'" in text
    assert "useCapsolver: true" in text
    assert capsolver.extension_chrome_args(ext)[0].endswith(str(ext))


def test_prompt_mentions_capsolver_when_enabled(tmp_path, monkeypatch):
    profile = {
        "personal": {
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": "555-0100",
            "password": "secret-password",
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
            "url": "https://allowed.example/apply",
            "application_url": "https://allowed.example/apply",
            "title": "Software Engineer Intern",
            "site": "Allowed Co",
            "fit_score": 10,
            "tailored_resume_path": str(base.with_suffix(".txt")),
        },
        tailored_resume="Candidate resume facts",
        upload_dir=tmp_path / "worker",
        capsolver_enabled=True,
    )

    lower = text.lower()
    assert "capsolver" in lower
    assert "do not solve" in lower
    assert "wait up to 45 seconds" in lower
    assert profile["personal"]["password"] not in text


def test_resolve_captcha_uses_capsolver_before_manual_wait(monkeypatch):
    waited = []
    monkeypatch.setattr(launcher, "_wait_for_captcha_resolution", lambda job, worker_id: waited.append(True) or False)
    monkeypatch.setattr("applypilot.apply.capsolver.is_enabled", lambda: True)
    monkeypatch.setattr("applypilot.apply.capsolver.try_solve_on_cdp", lambda port: True)
    monkeypatch.setattr(launcher, "add_event", lambda message: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)

    assert launcher._resolve_captcha({"title": "Intern", "site": "Example"}, worker_id=1, port=9323) is True
    assert waited == []


def test_upsert_env_preserves_existing_keys(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=qwen3:8b\nCAPSOLVER_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(wizard, "ENV_PATH", env_path)

    wizard._upsert_env({"CAPSOLVER_API_KEY": "CAP-TEST"})

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_MODEL=qwen3:8b" in text
    assert "CAPSOLVER_API_KEY=CAP-TEST" in text
