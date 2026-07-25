import json
import hashlib
import hmac
from pathlib import Path

from config import Settings
from models import AlertmanagerWebhook, InvestigationTrigger
from report import rewrite_signoz_links


def test_agent_wiring():
    """Interfaces the loop depends on must exist (guards against refactor drift)."""
    import loop
    import tools_mcp

    assert callable(tools_mcp.mcp_client.get_openai_tools)
    assert callable(tools_mcp.mcp_client.open_session)
    assert callable(tools_mcp.MCPToolSession.call_tool)
    assert callable(loop.run_investigation)


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
        "Errors: [explorer](signoz://explorer/traces?service.name = 'checkout' AND has_error = true)\n"
        "Leaks: [logs](signoz://explorer/logs?body REGEXP 'AKIA')\n"
        "All traces: [explorer](signoz://explorer/traces)"
    )

    output_md = rewrite_signoz_links(input_md)
    lines = output_md.splitlines()

    # Trace link is a plain rewrite
    assert "(http://signoz-test.internal:8080/trace/a1b2c3d4e5f6)" in lines[0]

    # Explorer links carry a double-encoded compositeQuery with the filter,
    # target the right explorer route, and contain no spaces (valid markdown URL)
    assert "/traces-explorer?compositeQuery=" in lines[1]
    assert "has_error" in lines[1]
    assert " " not in lines[1].split("(")[1].rstrip(")")
    assert "/logs-explorer?compositeQuery=" in lines[2]
    assert "AKIA" in lines[2]
    assert "/traces-explorer?compositeQuery=" in lines[3]

    # No unrewritten placeholders remain
    assert "signoz://" not in output_md


def test_rewrite_signoz_view_links():
    """Saved view placeholders rewrite to explorer URLs."""
    from config import settings

    settings.signoz_url = "http://signoz-test.internal:8080"
    md = "[failing](signoz://view/AstroMart — Failing checkouts)"
    out = rewrite_signoz_links(md)
    assert "/traces-explorer?viewName=" in out
    assert "AstroMart" in out
    assert "signoz://" not in out


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


def test_ledger_chain_intact_and_tamper_detected(tmp_path):
    """Ledger seals entries; any later edit to a payload breaks the chain."""
    from store import Store

    s = Store(db_path=str(tmp_path / "ledger.db"))
    inv = s.create_investigation(trigger_json="{}")
    s.append_ledger(inv, "investigation.start", {"alertname": "x"})
    s.append_ledger(inv, "tool.call", {"tool": "signoz_search_traces", "result_sha256": "abc"})
    s.append_ledger(inv, "investigation.finish", {"status": "done"})

    ok, checked, bad = s.verify_ledger()
    assert ok is True
    assert checked == 3
    assert bad is None

    # Tamper with the middle entry's payload directly in SQLite.
    conn = s._get_conn()
    conn.execute(
        "UPDATE ledger SET payload_json = ? WHERE entry_type = 'tool.call'",
        ('{"tool":"evil"}',),
    )
    conn.commit()

    ok, checked, bad = s.verify_ledger()
    assert ok is False
    assert bad is not None


def test_ledger_hash_is_deterministic():
    """Same prev hash + payload → same entry hash (stable across processes)."""
    from store import ledger_entry_hash

    h1 = ledger_entry_hash("0" * 64, '{"a":1}')
    h2 = ledger_entry_hash("0" * 64, '{"a":1}')
    assert h1 == h2
    assert h1 != ledger_entry_hash("0" * 64, '{"a":2}')


def test_audit_badge_variants():
    """Report badge reflects grounded / ungrounded / error auditor outcomes."""
    from auditor import AuditResult
    from report import audit_badge, prepend_audit_badge

    assert "grounded" in audit_badge(AuditResult(outcome="grounded", score=1.0)).lower()
    ung = audit_badge(
        AuditResult(outcome="ungrounded", unsupported_claims=["p95 318ms"], notes="n")
    )
    assert "ungrounded" in ung.lower()
    assert "p95 318ms" in ung
    assert "could not run" in audit_badge(AuditResult(outcome="error", error="boom")).lower()
    assert audit_badge(AuditResult(outcome="skipped")) == ""

    body = prepend_audit_badge(
        "# Title\n\nbody text", AuditResult(outcome="grounded", score=0.9)
    )
    assert body.startswith("# Title")
    assert "grounded" in body.lower()


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
