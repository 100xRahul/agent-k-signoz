"""One-command SigNoz bootstrap for a fresh clone.

Takes a virgin SigNoz instance to API-ready:
  1. First-boot admin registration (if setup is not completed yet)
  2. Service account creation + admin role attachment
  3. API key creation, written into .env as SIGNOZ_API_KEY

Idempotent: safe to re-run — skips whatever already exists. Admin credentials
(and the org id needed for API login) are saved to ~/signoz-login.txt at
registration time and reused on later runs.

Run from the repo root (the Makefile does this):
    python -m provisioning.bootstrap
"""

from __future__ import annotations

import json
import pathlib
import secrets
import sys
import urllib.error
import urllib.request

SIGNOZ_URL = "http://localhost:8080"
ENV_PATH = pathlib.Path(__file__).parent.parent / ".env"
LOGIN_FILE = pathlib.Path.home() / "signoz-login.txt"

ADMIN_EMAIL = "admin@agentk.local"
SERVICE_ACCOUNT_NAME = "agent-k"


def call(
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        SIGNOZ_URL + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
            if body.lstrip().startswith("<"):
                return r.status, "HTML"
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def save_login(email: str, password: str, org_id: str) -> None:
    LOGIN_FILE.write_text(
        "SigNoz local login\n"
        f"url: {SIGNOZ_URL}\n"
        f"email: {email}\n"
        f"password: {password}\n"
        f"org: {org_id}\n"
    )


def load_login() -> dict[str, str]:
    if not LOGIN_FILE.exists():
        return {}
    return dict(
        line.split(": ", 1)
        for line in LOGIN_FILE.read_text().strip().splitlines()[1:]
        if ": " in line
    )


def api_key_works(key: str) -> bool:
    req = urllib.request.Request(
        SIGNOZ_URL + "/api/v1/rules", headers={"SIGNOZ-API-KEY": key}
    )
    try:
        urllib.request.urlopen(req)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def main() -> None:
    print("🕶️  Agent K — SigNoz bootstrap\n")

    status, version = call("GET", "/api/v1/version")
    if status != 200 or not isinstance(version, dict):
        sys.exit(f"❌ SigNoz not reachable at {SIGNOZ_URL} — run `make up` first")
    print(
        f"SigNoz {version.get('version')} · "
        f"setupCompleted={version.get('setupCompleted')}"
    )

    if not ENV_PATH.exists():
        sys.exit(f"❌ {ENV_PATH} not found — run: cp .env.example .env")
    env = dict(
        line.split("=", 1)
        for line in ENV_PATH.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    if env.get("SIGNOZ_API_KEY") and api_key_works(env["SIGNOZ_API_KEY"]):
        print("✅ Existing SIGNOZ_API_KEY works — nothing to do")
        return

    # ── 1. Admin credentials ──────────────────────────────────────
    if not version.get("setupCompleted"):
        password = "AgentK-" + secrets.token_urlsafe(9)
        status, reg = call(
            "POST",
            "/api/v1/register",
            {
                "email": ADMIN_EMAIL,
                "password": password,
                "firstName": "Agent K Admin",
                "orgName": "agent-k",
                "orgDisplayName": "Agent K",
            },
        )
        if status != 200 or not isinstance(reg, dict):
            sys.exit(f"❌ Registration failed ({status}): {reg}")
        creds = {
            "email": ADMIN_EMAIL,
            "password": password,
            "org": reg["data"]["orgId"],
        }
        save_login(creds["email"], creds["password"], creds["org"])
        print(f"✅ Admin registered ({ADMIN_EMAIL}) — credentials saved to {LOGIN_FILE}")
    else:
        creds = load_login()
        if not creds.get("email") or not creds.get("org"):
            sys.exit(
                "❌ SigNoz is already set up but no saved credentials found.\n"
                "   Either create an API key in the UI (Settings → Service Accounts)\n"
                f"   and put it in {ENV_PATH} as SIGNOZ_API_KEY, or save your login to\n"
                f"   {LOGIN_FILE} in this format:\n"
                "     SigNoz local login\n"
                f"     url: {SIGNOZ_URL}\n"
                "     email: <admin email>\n"
                "     password: <password>\n"
                "     org: <org id>\n"
            )

    # ── 2. Login ──────────────────────────────────────────────────
    status, login = call(
        "POST",
        "/api/v2/sessions/email_password",
        {
            "email": creds["email"],
            "password": creds["password"],
            "orgID": creds["org"],
        },
    )
    if status != 200 or not isinstance(login, dict):
        sys.exit(f"❌ Login failed ({status}): {login}")
    jwt = login["data"]["accessToken"]
    print("✅ Logged in")

    # ── 3. Service account ────────────────────────────────────────
    sa_id = None
    status, sa_list = call("GET", "/api/v1/service_accounts", token=jwt)
    if isinstance(sa_list, dict):
        for sa in sa_list.get("data") or []:
            if sa.get("name") == SERVICE_ACCOUNT_NAME:
                sa_id = sa["id"]
                print("✔️  Service account already exists")
    if sa_id is None:
        status, sa = call(
            "POST", "/api/v1/service_accounts", {"name": SERVICE_ACCOUNT_NAME}, jwt
        )
        if status not in (200, 201) or not isinstance(sa, dict):
            sys.exit(f"❌ Service account creation failed ({status}): {sa}")
        sa_id = sa["data"]["id"]
        print("✅ Service account created")

    # ── 4. Admin role ─────────────────────────────────────────────
    status, roles = call("GET", "/api/v1/roles", token=jwt)
    if not isinstance(roles, dict):
        sys.exit(f"❌ Could not list roles ({status}): {roles}")
    admin_role = next(r for r in roles["data"] if r["name"] == "signoz-admin")
    status, _ = call(
        "POST",
        f"/api/v1/service_accounts/{sa_id}/roles",
        {"id": admin_role["id"]},
        jwt,
    )
    # 204 = attached; 400/409 = already attached — both fine, verified below.
    status, sa_detail = call("GET", f"/api/v1/service_accounts/{sa_id}", token=jwt)
    attached = isinstance(sa_detail, dict) and any(
        (r.get("role") or {}).get("name") == "signoz-admin"
        for r in (sa_detail["data"].get("serviceAccountRoles") or [])
    )
    if not attached:
        sys.exit("❌ Admin role not attached to service account")
    print("✅ Admin role attached")

    # ── 5. API key ────────────────────────────────────────────────
    status, key = call(
        "POST", f"/api/v1/service_accounts/{sa_id}/keys", {"name": "agent-k"}, jwt
    )
    if status not in (200, 201) or not isinstance(key, dict):
        sys.exit(f"❌ Key creation failed ({status}): {key}")
    api_key = key["data"]["key"]

    lines = [
        f"SIGNOZ_API_KEY={api_key}" if line.startswith("SIGNOZ_API_KEY=") else line
        for line in ENV_PATH.read_text().splitlines()
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    if not api_key_works(api_key):
        sys.exit("❌ New API key failed verification against /api/v1/rules")
    print(f"✅ API key created, verified, and written to {ENV_PATH}")

    print(
        "\nDone. Restart the services that read the key, then provision:\n"
        "    docker compose up -d mcp-server agent && make provision"
    )


if __name__ == "__main__":
    main()
