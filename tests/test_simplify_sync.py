from applypilot.database import init_db
from applypilot.discovery import simplify


def test_canonical_job_key_strips_utm_and_uses_job_id():
    assert simplify.canonical_job_key(
        "https://boards.greenhouse.io/robinhood/jobs/8142963?utm_source=Simplify"
    ) == simplify.canonical_job_key(
        "https://boards.greenhouse.io/robinhood/jobs/8142963"
    )


def test_sync_listings_inserts_unseen_undergrad_roles(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "simplify.db")
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, apply_attempts)
        VALUES ('https://boards.greenhouse.io/robinhood/jobs/8142963?utm_source=Simplify',
                'Existing Web Intern', 'Robinhood', 0)
        """
    )
    conn.commit()
    monkeypatch.setattr(simplify, "get_connection", lambda: conn)
    monkeypatch.setattr(simplify, "init_db", lambda: conn)

    records = [
        {
            "id": "new-ios",
            "company_name": "Robinhood",
            "title": "iOS Software Developer Intern",
            "url": "https://boards.greenhouse.io/robinhood/jobs/8199729",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["Menlo Park, CA"],
        },
        {
            "id": "existing-web",
            "company_name": "Robinhood",
            "title": "Software Engineer Intern - Web",
            "url": "https://boards.greenhouse.io/robinhood/jobs/8142963",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["Menlo Park, CA"],
        },
        {
            "id": "phd-only",
            "company_name": "Quant Co",
            "title": "Research Intern",
            "url": "https://example.com/jobs/99",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["PhD"],
            "category": "Quant",
            "locations": ["NYC"],
        },
        {
            "id": "google",
            "company_name": "Google",
            "title": "Software Engineer Intern",
            "url": "https://example.com/jobs/google",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["Remote"],
        },
        {
            "id": "oracle",
            "company_name": "Oracle",
            "title": "Software Engineer Intern",
            "url": "https://careers.oracle.com/jobs/intern",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["Austin, TX"],
        },
        {
            "id": "tiktok",
            "company_name": "TikTok",
            "title": "Software Engineer Intern",
            "url": "https://lifeattiktok.com/search/1",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["San Jose, CA"],
        },
        {
            "id": "icims-vanity",
            "company_name": "Vanity Co",
            "title": "Software Engineer Intern",
            "url": "https://careers.example.com/jobs/55?icims=1",
            "active": True,
            "is_visible": True,
            "terms": ["Summer 2027"],
            "degrees": ["Bachelor's"],
            "category": "Software",
            "locations": ["Remote"],
        },
    ]

    result = simplify.sync_listings(records)

    assert result["inserted"] == 5
    assert result["skipped"] == 1
    urls = {row["url"] for row in conn.execute("SELECT url FROM jobs")}
    assert "https://boards.greenhouse.io/robinhood/jobs/8199729" in urls
    assert "https://example.com/jobs/99" not in urls
    assert "https://example.com/jobs/google" in urls
    assert "https://careers.oracle.com/jobs/intern" in urls
    assert "https://lifeattiktok.com/search/1" in urls
    parked = conn.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url LIKE '%icims=%'"
    ).fetchone()
    assert tuple(parked) == ("failed", "icims_unsupported", 99)
