from applypilot.apply.chrome import (
    CAMPAIGN_SLIVER_PX,
    CAMPAIGN_WINDOW_H,
    CAMPAIGN_WINDOW_W,
    _campaign_demote_script,
    campaign_hidden_headed,
    campaign_sliver_origin,
)
from applypilot.discovery.jobspy import job_has_listed_pay


def test_campaign_headless_stays_headed_for_indeed():
    assert campaign_hidden_headed(50, True) is True
    assert campaign_hidden_headed(89, True) is True
    assert campaign_hidden_headed(50, False) is False
    assert campaign_hidden_headed(0, True) is False
    assert campaign_hidden_headed(40, True) is False


def test_campaign_demote_script_pins_sliver_not_minimize():
    left, top = campaign_sliver_origin()
    script = _campaign_demote_script([93721, 93722])
    assert "93721" in script
    assert "93722" in script
    assert f"{{{left}, {top}}}" in script
    assert "miniaturized of every window of procRef to false" in script
    assert "zoomed of every window of procRef to false" in script
    assert "visible of procRef to false" not in script
    assert CAMPAIGN_SLIVER_PX == 4
    assert f"{{{CAMPAIGN_WINDOW_W}, {CAMPAIGN_WINDOW_H}}}" in script
    assert _campaign_demote_script([]) == ""
    assert _campaign_demote_script([0]) == ""


def test_campaign_does_not_recycle_chrome_on_click_blocked():
    from applypilot.apply.launcher import _should_recycle_chrome

    assert _should_recycle_chrome("failed:browser_interaction_blocked", worker_id=50) is False
    assert _should_recycle_chrome("failed:browser_unavailable", worker_id=50) is True
    assert _should_recycle_chrome("failed:browser_interaction_blocked", worker_id=0) is True


def test_job_has_listed_pay_accepts_salary_or_listing_text():
    assert job_has_listed_pay("USD30,000-USD40,000/year")
    assert job_has_listed_pay(None, "Pay: $25 an hour, remote US")
    assert job_has_listed_pay("", "Compensation 20-25 per hour")
    assert not job_has_listed_pay(None, "Join our great team. Remote.")
    assert not job_has_listed_pay("nan", "")
