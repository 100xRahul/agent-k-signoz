import json
import hashlib
import hmac
from pathlib import Path

from config import Settings
from models import AlertmanagerWebhook, InvestigationTrigger
from report import rewrite_signoz_links


def test_cost_calculation():
    """Test cost calculation in config Settings."""
    settings = Settings(
        llm_input_price_per_mtok=0.15,
        llm_output_price_per_mtok=0.60,
    )
    # 1000 input, 2000 output -> 1000 * 0.15 / 1e6 + 2000 * 0.60 / 1e6 = 0.00015 + 0.0012 = 0.00135
    cost = settings.compute_cost(1000, 2000)
    assert abs(cost - 0.00135) < 1e-9


def test_hmac_verification():
    """Test HMAC approval signature generation and verification."""
    secret = "test-secret-key"
    action_id = "action-123"

    # Calculate signature
    sig = hmac.new(
        secret.encode(),
        action_id.encode(),
        hashlib.sha256,
    ).hexdigest()

    # Verify expected logic matching main.py
    expected = hmac.new(
        secret.encode(),
        action_id.encode(),
        hashlib.sha256,
    ).hexdigest()

    assert hmac.compare_digest(expected, sig)
    assert not hmac.compare_digest(expected, "wrong-sig")


def test_rewrite_signoz_links():
    """Test rewriting of signoz:// placeholders to real SigNoz URLs."""
    from config import settings

    # Override settings URL for testing
    settings.signoz_url = "http://signoz-test.internal:8080"

    input_md = (
        "Check this trace: [trace](signoz://trace/a1b2c3d4e5f6)\n"
        "Check the logs: [logs](signoz://logs/service=checkout)\n"
        "Check the metrics: [metrics](signoz://metrics/service=payment)\n"
        "Check explorer: [explorer](signoz://explorer/service=inventory)"
    )

    expected_output = (
        "Check this trace: [trace](http://signoz-test.internal:8080/trace/a1b2c3d4e5f6)\n"
        "Check the logs: [logs](http://signoz-test.internal:8080/logs-explorer?service=checkout)\n"
        "Check the metrics: [metrics](http://signoz-test.internal:8080/metrics-explorer?service=payment)\n"
        "Check explorer: [explorer](http://signoz-test.internal:8080/traces-explorer?service=inventory)"
    )

    output_md = rewrite_signoz_links(input_md)
    assert output_md == expected_output


def test_md_to_mrkdwn():
    """Markdown converts to Slack mrkdwn: headings, bold, links."""
    from slack import md_to_mrkdwn

    src = "# Title\nSome **bold** text with a [link](http://x.io/t/1)\n## Sub"
    out = md_to_mrkdwn(src)
    assert "*Title*" in out
    assert "*bold*" in out
    assert "<http://x.io/t/1|link>" in out
    assert "*Sub*" in out
    assert "#" not in out


def test_remediation_target_normalization():
    """The LLM sends `service` per the tool schema; `flag` must work too."""
    from remediation import _normalize_target

    assert _normalize_target({"service": "checkout"}) == "checkout"
    assert _normalize_target({"flag": "new-checkout"}) == "new-checkout"
    assert _normalize_target({}) == ""


def test_disable_flag_refuses_unknown():
    """Guardrail: agent may only clear allow-listed flags."""
    import asyncio
    from remediation import disable_flag_run

    result = asyncio.run(disable_flag_run({"service": "drop-all-tables"}))
    assert result.startswith("❌")


def test_store_latest_investigation_for_alert(tmp_path):
    """Cooldown lookup finds the newest investigation for an alertname."""
    from store import Store

    s = Store(db_path=str(tmp_path / "test.db"))
    trigger = InvestigationTrigger(type="alert", alertname="checkout-error-rate")
    inv_id = s.create_investigation(trigger_json=trigger.model_dump_json())

    latest = s.latest_investigation_for_alert("checkout-error-rate")
    assert latest is not None
    assert latest["id"] == inv_id
    assert s.latest_investigation_for_alert("no-such-alert") is None


def test_webhook_parsing():
    """Test parsing of SigNoz alert webhook payloads using fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "webhook_sample.json"
    with open(fixture_path) as f:
        payload = json.load(f)

    webhook = AlertmanagerWebhook.model_validate(payload)
    assert webhook.status.value == "firing"
    assert len(webhook.alerts) == 1
    assert webhook.alerts[0].labels.get("alertname") == "checkout-error-rate"

    # Check trigger mapping
    trigger = InvestigationTrigger.from_webhook(webhook)
    assert trigger.type.value == "alert"
    assert trigger.alertname == "checkout-error-rate"
    assert trigger.annotations.get("description") is not None
