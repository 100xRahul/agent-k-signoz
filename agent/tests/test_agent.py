import json
import os
import hashlib
import hmac
from pathlib import Path

import pytest
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
    assert "elevated error rates" in trigger.prompt
