#!/usr/bin/env python3
"""
Pull BigFix computer inventory once and ship one log per computer to Coralogix.

The inventory comes from one Session Relevance query against the BigFix REST API
(``POST /api/query``), documented at https://developer.bigfix.com/rest-api/api/query.html.
Each computer becomes one log on ``POST https://ingress.<domain>/logs/v1/singles``.

Run it once a day from cron. The log timestamp is the time of the pull, so the
whole snapshot shows up in "last 24 hours" even when an endpoint has not
reported recently. ``last_report_time`` stays inside the event.

  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cp .env.example .env   # then fill in secrets
  set -a && source .env && set +a
  .venv/bin/python bigfix_inventory_to_coralogix.py --dry-run
  .venv/bin/python bigfix_inventory_to_coralogix.py

Daily cron (02:15 server time)::

  15 2 * * * /opt/bigfix-inventory-coralogix/scripts/run_daily.sh >> /opt/bigfix-inventory-coralogix/logs/inventory.log 2>&1

Environment
-----------
Required:
  BIGFIX_URL              Root server. Hostname or URL. Port defaults to 52311.
  BIGFIX_USERNAME         Console operator
  BIGFIX_PASSWORD         Operator password
  CORALOGIX_PRIVATE_KEY   Send-Your-Data API key (not required with --dry-run)
  CORALOGIX_DOMAIN        e.g. coralogix.in (no https://, no ingress. prefix)

Optional:
  CX_APPLICATION_NAME     Default: bigfix
  CX_SUBSYSTEM_NAME       Default: inventory
  BIGFIX_PROFILE          full (default) or basic
  BIGFIX_COMPUTER_GROUP   Export only members of this computer group
  BIGFIX_RELEVANCE        Custom Session Relevance. Requires BIGFIX_COLUMNS.
  BIGFIX_COLUMNS          Comma-separated names, one per result column
  BIGFIX_VERIFY_SSL       true (default) or false
  BIGFIX_CA_BUNDLE        PEM path. When set, this is used instead of the flag.
  BIGFIX_TIMEOUT_SECONDS  Default 600
  BIGFIX_STALE_DAYS       Default 7. Older last-report times are severity Warning.
  CORALOGIX_BATCH_SIZE    Default 50
  DEBUG                   If true, print resolved settings with secrets redacted
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

DEFAULT_APP = "bigfix"
DEFAULT_SUB = "inventory"
DEFAULT_PORT = 52311

# Column order must match the tuple order inside each relevance query.
# The "| \"\"" form turns a missing property on one computer into an empty
# string. It does not protect against an inspector the server does not have;
# if a query fails with "operator is not defined", switch BIGFIX_PROFILE to
# basic or supply BIGFIX_RELEVANCE.
BASIC_COLUMNS = [
    "computer_id",
    "computer_name",
    "dns_name",
    "operating_system",
    "ip_addresses",
    "last_report_time",
    "agent_version",
    "agent_type",
]

FULL_COLUMNS = BASIC_COLUMNS + [
    "device_type",
    "license_type",
    "active_directory_path",
    "cpu",
    "relay_server",
    "relay_hostname",
    "locked",
    "root_server",
]

_BASIC_FIELDS = """
  (id of it as string)
, (name of it | "")
, (hostname of it | "")
, (operating system of it | "")
, ((concatenation ";" of (ip addresses of it as string)) | "")
, ((last report time of it as string) | "")
, (agent version of it | "")
, (agent type of it | "")
""".strip()

_FULL_FIELDS = (
    _BASIC_FIELDS
    + """
, (device type of it | "")
, (license type of it | "")
, ((active directory path of it as string) | "")
, (cpu of it | "")
, (relay server of it | "")
, (relay hostname of it | "")
, ((locked flag of it as string) | "")
, (root server of it | "")
"""
)

PROFILES: dict[str, tuple[list[str], str]] = {
    "basic": (BASIC_COLUMNS, _BASIC_FIELDS),
    "full": (FULL_COLUMNS, _FULL_FIELDS),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require_env(name: str) -> str:
    value = _env(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "y", "on")


def normalize_bigfix_url(host_or_url: str) -> str:
    """Return https://host:port with no path."""
    raw = host_or_url.strip().rstrip("/")
    if not raw:
        raise ValueError("BIGFIX_URL is empty")
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise ValueError(f"Invalid BIGFIX_URL: {host_or_url!r}")
    port = parsed.port or DEFAULT_PORT
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def relevance_string(value: str) -> str:
    """Escape a value for use inside a Session Relevance double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def computer_subject(computer_group: str | None) -> str:
    if not computer_group:
        return "bes computers"
    escaped = relevance_string(computer_group)
    return f'members of bes computer group whose (name of it = "{escaped}")'


def build_relevance(profile: str, computer_group: str | None) -> tuple[list[str], str]:
    try:
        columns, fields = PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown BIGFIX_PROFILE={profile!r}. Use one of: {known}.") from None
    relevance = f"(\n{fields}\n) of {computer_subject(computer_group)}"
    return list(columns), relevance


def resolve_query(profile: str, computer_group: str | None) -> tuple[list[str], str]:
    custom = _env("BIGFIX_RELEVANCE")
    custom_columns = _env("BIGFIX_COLUMNS")
    if custom or custom_columns:
        if computer_group:
            print(
                "BIGFIX_COMPUTER_GROUP is ignored because BIGFIX_RELEVANCE is set. "
                "Include the group filter in the custom relevance.",
                file=sys.stderr,
            )
        if not custom or not custom_columns:
            raise ValueError(
                "BIGFIX_RELEVANCE and BIGFIX_COLUMNS must be set together. "
                "BIGFIX_COLUMNS is a comma-separated list, one name per result column."
            )
        columns = [part.strip() for part in custom_columns.split(",") if part.strip()]
        if not columns:
            raise ValueError("BIGFIX_COLUMNS did not contain any column names.")
        return columns, custom
    return build_relevance(profile, computer_group)


def parse_bigfix_time(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, separators=(",", ":"), default=str)


def rows_from_query(body: dict[str, Any], columns: list[str]) -> list[dict[str, str]]:
    if not isinstance(body, dict):
        raise RuntimeError(f"BigFix query returned {type(body).__name__}, expected an object.")
    if body.get("error"):
        raise RuntimeError(f"BigFix relevance error: {body.get('error')}")
    result = body.get("result")
    if result is None:
        detail = body.get("Error") or body.get("message") or json.dumps(body)[:2000]
        raise RuntimeError(f"BigFix query returned no result: {detail}")
    if not isinstance(result, list):
        raise RuntimeError(f"BigFix result has unexpected type {type(result).__name__}.")

    rows: list[dict[str, str]] = []
    for item in result:
        if isinstance(item, dict):
            rows.append({key: _cell(item.get(key)) for key in columns})
            continue
        if isinstance(item, list):
            values = [_cell(part) for part in item]
        else:
            values = [_cell(item)]
        if len(values) < len(columns):
            values.extend([""] * (len(columns) - len(values)))
        rows.append(dict(zip(columns, values)))
    return rows


def is_stale(last_report_time: str, *, now: datetime, stale_days: int) -> bool:
    reported = parse_bigfix_time(last_report_time)
    if reported is None:
        return True
    return now - reported > timedelta(days=stale_days)


def inventory_record(
    row: dict[str, str],
    *,
    snapshot_id: str,
    now: datetime,
    stale_days: int,
) -> dict[str, Any]:
    last_report = row.get("last_report_time", "")
    stale = is_stale(last_report, now=now, stale_days=stale_days)
    record: dict[str, Any] = {
        "source": "bigfix",
        "event_type": "computer_inventory",
        "snapshot_id": snapshot_id,
        "stale": stale,
    }
    record.update(row)
    return record


def to_coralogix_log(
    record: dict[str, Any],
    *,
    application_name: str,
    subsystem_name: str,
    timestamp_ms: float,
) -> dict[str, Any]:
    computer = (
        record.get("computer_name")
        or record.get("dns_name")
        or record.get("computer_id")
        or "unknown-computer"
    )
    severity = 4 if record.get("stale") else 3
    return {
        "applicationName": application_name,
        "subsystemName": subsystem_name,
        "computerName": str(computer)[:1024],
        "timestamp": timestamp_ms,
        "severity": severity,
        "text": json.dumps(record, separators=(",", ":"), default=str),
    }


def ssl_verify() -> bool | str:
    bundle = _env("BIGFIX_CA_BUNDLE")
    if bundle:
        return bundle
    return _env_bool("BIGFIX_VERIFY_SSL", default=True)


def fetch_inventory(base_url: str, username: str, password: str, relevance: str, timeout: int) -> dict[str, Any]:
    import requests
    from requests.auth import HTTPBasicAuth

    url = f"{base_url}/api/query"
    response = requests.post(
        url,
        data={"relevance": relevance, "output": "json"},
        auth=HTTPBasicAuth(username, password),
        headers={"Accept": "application/json"},
        timeout=timeout,
        verify=ssl_verify(),
    )
    if response.status_code == 401:
        raise RuntimeError(
            "BigFix authentication failed (401). Check BIGFIX_USERNAME and BIGFIX_PASSWORD. "
            "The account must be a console operator allowed to use the REST API."
        )
    if response.status_code == 403:
        raise RuntimeError(
            "BigFix refused the query (403). The operator is not allowed to read these computers."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"BigFix query failed {response.status_code}: {response.text[:2000]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "BigFix did not return JSON. The first bytes were: "
            f"{response.text[:500]}"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"BigFix query returned {type(body).__name__}, expected an object.")
    return body


def send_coralogix_batch(
    ingress_url: str,
    private_key: str,
    batch: list[dict[str, Any]],
    timeout: int,
) -> None:
    import requests

    if not batch:
        return
    url = f"{ingress_url.rstrip('/')}/logs/v1/singles"
    response = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {private_key}",
        },
        data=json.dumps(batch),
        timeout=timeout,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Coralogix ingest failed {response.status_code}: {response.text[:2000]}"
        )


def _self_test() -> None:
    sample = {
        "result": [
            [
                "11112538",
                "WIN-PC-01",
                "win-pc-01.example.com",
                "Win11 23H2",
                "10.1.2.3;10.1.2.4",
                "Thu, 15 Oct 2026 06:00:00 +0000",
                "11.0.2.92",
                "Native",
            ],
            [
                "11112539",
                "OLD-PC",
                "",
                "Win10",
                "",
                "Mon, 01 Jan 2024 00:00:00 +0000",
                "10.0.8",
                "Native",
            ],
        ],
        "count": 2,
        "plural": True,
        "type": "tuple",
        "evaltime_ms": 1.2,
    }
    now = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    rows = rows_from_query(sample, BASIC_COLUMNS)
    assert len(rows) == 2
    assert rows[0]["computer_name"] == "WIN-PC-01"
    assert rows[0]["ip_addresses"] == "10.1.2.3;10.1.2.4"
    fresh = inventory_record(rows[0], snapshot_id="snap", now=now, stale_days=7)
    stale = inventory_record(rows[1], snapshot_id="snap", now=now, stale_days=7)
    assert fresh["stale"] is False
    assert stale["stale"] is True
    log = to_coralogix_log(
        fresh,
        application_name="bigfix",
        subsystem_name="inventory",
        timestamp_ms=now.timestamp() * 1000,
    )
    assert log["applicationName"] == "bigfix"
    assert log["severity"] == 3
    assert json.loads(log["text"])["computer_id"] == "11112538"
    stale_log = to_coralogix_log(
        stale,
        application_name="bigfix",
        subsystem_name="inventory",
        timestamp_ms=now.timestamp() * 1000,
    )
    assert stale_log["severity"] == 4

    columns, relevance = build_relevance("basic", None)
    assert columns == BASIC_COLUMNS
    assert relevance.rstrip().endswith("of bes computers")
    _, grouped = build_relevance("full", 'HQ "East"')
    assert 'name of it = "HQ \\"East\\""' in grouped
    assert _BASIC_FIELDS.count(", (") + 1 == len(BASIC_COLUMNS)
    assert grouped.count(", (") + 1 == len(FULL_COLUMNS)

    try:
        rows_from_query({"error": "The operator \"device type\" is not defined."}, BASIC_COLUMNS)
    except RuntimeError as exc:
        assert "device type" in str(exc)
    else:
        raise AssertionError("expected relevance error to raise")

    assert normalize_bigfix_url("bigfix.example.com") == "https://bigfix.example.com:52311"
    assert normalize_bigfix_url("https://bigfix.example.com/api") == "https://bigfix.example.com:52311"
    reported = parse_bigfix_time("Thu, 15 Oct 2026 06:00:00 +0000")
    assert reported == datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="BigFix computer inventory ? Coralogix.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query BigFix and print one sample log. Do not POST to Coralogix.",
    )
    parser.add_argument(
        "--max-computers",
        type=int,
        default=0,
        help="Ship at most this many computers (0 = all).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the local parser checks and exit. No network and no credentials.",
    )
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return

    bigfix_url = _require_env("BIGFIX_URL")
    username = _require_env("BIGFIX_USERNAME")
    password = _require_env("BIGFIX_PASSWORD")
    cx_domain = ""
    cx_key = ""
    if not args.dry_run:
        cx_key = _require_env("CORALOGIX_PRIVATE_KEY")
        cx_domain = _require_env("CORALOGIX_DOMAIN").removeprefix("https://").split("/")[0]
        if cx_domain.startswith("ingress."):
            cx_domain = cx_domain[len("ingress.") :]
    else:
        cx_domain = _env("CORALOGIX_DOMAIN").removeprefix("https://").split("/")[0]
        if cx_domain.startswith("ingress."):
            cx_domain = cx_domain[len("ingress.") :]

    profile = (_env("BIGFIX_PROFILE") or "full").lower()
    computer_group = _env("BIGFIX_COMPUTER_GROUP") or None
    try:
        columns, relevance = resolve_query(profile, computer_group)
        base_url = normalize_bigfix_url(bigfix_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    timeout = int(_env("BIGFIX_TIMEOUT_SECONDS") or "600")
    stale_days = int(_env("BIGFIX_STALE_DAYS") or "7")
    batch_size = int(_env("CORALOGIX_BATCH_SIZE") or "50")
    app_name = _env("CX_APPLICATION_NAME") or DEFAULT_APP
    sub_name = _env("CX_SUBSYSTEM_NAME") or DEFAULT_SUB
    if timeout < 1 or stale_days < 0 or batch_size < 1:
        print("BIGFIX_TIMEOUT_SECONDS and CORALOGIX_BATCH_SIZE must be >= 1.", file=sys.stderr)
        sys.exit(1)

    ingress = f"https://ingress.{cx_domain}" if cx_domain else ""
    if _env_bool("DEBUG"):
        print(
            json.dumps(
                {
                    "bigfix_url": base_url,
                    "bigfix_username": username,
                    "bigfix_password_set": bool(password),
                    "profile": profile,
                    "computer_group": computer_group,
                    "columns": columns,
                    "verify_ssl": ssl_verify(),
                    "coralogix_ingress": ingress or None,
                    "application": app_name,
                    "subsystem": sub_name,
                    "stale_days": stale_days,
                    "dry_run": args.dry_run,
                },
                indent=2,
            )
        )

    started = time.perf_counter()
    body = fetch_inventory(base_url, username, password, relevance, timeout)
    rows = rows_from_query(body, columns)
    if args.max_computers > 0:
        rows = rows[: args.max_computers]

    now = datetime.now(timezone.utc)
    snapshot_id = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    timestamp_ms = now.timestamp() * 1000.0
    records = [
        inventory_record(row, snapshot_id=snapshot_id, now=now, stale_days=stale_days)
        for row in rows
    ]
    logs = [
        to_coralogix_log(
            record,
            application_name=app_name,
            subsystem_name=sub_name,
            timestamp_ms=timestamp_ms,
        )
        for record in records
    ]
    elapsed = time.perf_counter() - started
    eval_ms = body.get("evaltime_ms")
    print(
        f"BigFix returned {len(rows)} computer(s)"
        + (f" (evaltime_ms={eval_ms})" if eval_ms is not None else "")
        + f" in {elapsed:.1f}s."
    )

    if args.dry_run:
        if logs:
            preview = dict(logs[0])
            preview["text"] = json.loads(logs[0]["text"])
            print(json.dumps(preview, indent=2, default=str)[:8000])
        destination = ingress or "Coralogix"
        print(
            f"Dry run: would ship {len(logs)} log(s) to {destination}/logs/v1/singles "
            f"as {app_name}/{sub_name}."
        )
        return

    for index in range(0, len(logs), batch_size):
        chunk = logs[index : index + batch_size]
        send_coralogix_batch(ingress, cx_key, chunk, timeout)
        print(f"Sent Coralogix batch {index // batch_size + 1} ({len(chunk)} logs).")

    stale_count = sum(1 for record in records if record.get("stale"))
    print(
        f"Done. Shipped {len(logs)} computer inventory log(s) "
        f"({app_name}/{sub_name}) to {ingress}. "
        f"{stale_count} had no report in the last {stale_days} day(s)."
    )


if __name__ == "__main__":
    main()
