#!/usr/bin/env python3
"""OpenRouter balance detection bot.

Shows every key on the OpenRouter account with the dashboard name
(b, sn34-1, sn34-2, ...), plus account and per-key remaining credits.

A regular API key can only see itself. Listing all keys and their
names requires OPENROUTER_MANAGEMENT_API_KEY.

Examples:
  python3 openrouter_balance_bot.py
  python3 openrouter_balance_bot.py --watch 60 --alert-below 10
  python3 openrouter_balance_bot.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


API_BASE = "https://openrouter.ai/api/v1"
KEY_RE = re.compile(r"sk-or-v1-[0-9a-f]{64}")
ENV_KEY_NAMES = (
    "OPEN_ROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "OPENROUTER_MANAGEMENT_API_KEY",
    "OPENROUTER_PROVISIONING_API_KEY",
)
BOT_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = BOT_DIR / "openrouter_balance.log"
DEFAULT_JSONL_PATH = BOT_DIR / "openrouter_balance.jsonl"
MANAGEMENT_KEYS_URL = "https://openrouter.ai/settings/management-keys"

# Dashboard names from the OpenRouter Keys page (label -> name).
KNOWN_LABEL_NAMES = {
    "sk-or-v1-11c...2f1": "b",
    "sk-or-v1-fff...684": "sn34-1",
    "sk-or-v1-bdb...7fc": "sn34-2",
}


def mask_key(key: str) -> str:
    if len(key) <= 16:
        return key[:6] + "..."
    return f"{key[:11]}...{key[-4:]}"


def source_name(source: str) -> str:
    if source in {"cli", "process-env", "management-api"}:
        return source
    path = Path(source)
    if path.name == ".env" and path.parent == BOT_DIR:
        return "btmind/.env"
    return path.name


def load_dotenv_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("'").strip('"')
        if name:
            values[name] = value
    return values


def collect_keys_from_text(text: str) -> List[str]:
    return KEY_RE.findall(text or "")


def is_real_key(value: str) -> bool:
    return bool(KEY_RE.fullmatch((value or "").strip()))


def parse_named_keys(env_map: Dict[str, str]) -> Dict[str, str]:
    """Parse OPENROUTER_KEYS=name=sk-or-...,name=sk-or-... and KEY_NAME overrides."""
    named: Dict[str, str] = {}
    raw = env_map.get("OPENROUTER_KEYS", "")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if "=" not in part:
                continue
            name, key = part.split("=", 1)
            name, key = name.strip(), key.strip()
            if name and is_real_key(key):
                named[key] = name
    primary = env_map.get("OPEN_ROUTER_API_KEY") or env_map.get("OPENROUTER_API_KEY")
    primary_name = env_map.get("OPEN_ROUTER_API_KEY_NAME", "").strip()
    if primary and is_real_key(primary) and primary_name:
        named.setdefault(primary, primary_name)
    return named


def parse_label_names(env_map: Dict[str, str]) -> Dict[str, str]:
    names = dict(KNOWN_LABEL_NAMES)
    raw = env_map.get("OPENROUTER_KEY_NAMES", "")
    if not raw:
        return names
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        left, right = part.split("=", 1)
        left, right = left.strip(), right.strip()
        if left.startswith("sk-or-"):
            names[left] = right
        elif right.startswith("sk-or-"):
            names[right] = left
    return names


def collect_keys_from_env_map(env_map: Dict[str, str]) -> List[str]:
    found: List[str] = []
    for name in ENV_KEY_NAMES:
        raw = env_map.get(name, "")
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(",") if part.strip()] if "," in raw else [raw.strip()]
        for part in parts:
            if is_real_key(part) and part not in found:
                found.append(part)
        for part in collect_keys_from_text(raw):
            if part not in found:
                found.append(part)
    for key in parse_named_keys(env_map):
        if key not in found:
            found.append(key)
    for part in collect_keys_from_text("\n".join(env_map.values())):
        if part not in found:
            found.append(part)
    return found


def iter_env_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            lowered = name.lower()
            if any(token in lowered for token in (".example", ".template", "sample", ".md")):
                continue
            if lowered.startswith(".env") or lowered.endswith(".env") or lowered.startswith("env"):
                path = Path(dirpath) / name
                if path.is_file() and path.stat().st_size < 2_000_000:
                    yield path


def discover_keys(extra_keys: Iterable[str], scan_roots: Iterable[Path]) -> Tuple[List[str], Dict[str, List[str]]]:
    sources: Dict[str, List[str]] = defaultdict(list)
    ordered: List[str] = []

    def add(key: str, source: str) -> None:
        key = (key or "").strip()
        if not is_real_key(key):
            return
        if key not in sources:
            ordered.append(key)
        if source not in sources[key]:
            sources[key].append(source)

    for key in extra_keys:
        add(key, "cli")

    for key in collect_keys_from_env_map(dict(os.environ)):
        add(key, "process-env")

    local_env = BOT_DIR / ".env"
    if local_env.exists():
        for key in collect_keys_from_env_map(load_dotenv_file(local_env)):
            add(key, str(local_env))

    keys_file = BOT_DIR / "keys.txt"
    if keys_file.exists():
        for key in collect_keys_from_text(keys_file.read_text(encoding="utf-8", errors="ignore")):
            add(key, str(keys_file))

    for root in scan_roots:
        for path in iter_env_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parsed = load_dotenv_file(path)
            for key in collect_keys_from_env_map(parsed) + collect_keys_from_text(text):
                add(key, str(path))

    return ordered, dict(sources)


def api_get(path: str, key: str, timeout: float = 30.0) -> Tuple[int, Any]:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.btmind.balance-bot",
            "X-Title": "btmind-openrouter-balance-bot",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw or str(exc), "code": exc.code}}
        return exc.code, payload
    except Exception as exc:
        return 0, {"error": {"message": str(exc), "code": 0}}


def list_account_keys(management_key: str) -> Tuple[int, List[Dict[str, Any]]]:
    keys: List[Dict[str, Any]] = []
    offset = 0
    last_status = 200
    while True:
        qs = urllib.parse.urlencode({"include_disabled": "true", "offset": offset})
        status, payload = api_get(f"/keys?{qs}", management_key)
        last_status = status
        if status != 200:
            return status, keys
        page = payload.get("data") or []
        if not isinstance(page, list):
            break
        keys.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    return last_status, keys


def money(value: Optional[float]) -> str:
    if value is None:
        return "unlimited"
    return f"${value:,.4f}"


def remaining_credits(total_credits: Optional[float], total_usage: Optional[float]) -> Optional[float]:
    if total_credits is None or total_usage is None:
        return None
    return total_credits - total_usage


def api_post(path: str, key: str, body: Dict[str, Any], timeout: float = 30.0) -> Tuple[int, Any]:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.btmind.balance-bot",
            "X-Title": "btmind-openrouter-balance-bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw or str(exc), "code": exc.code}}
        return exc.code, payload
    except Exception as exc:
        return 0, {"error": {"message": str(exc), "code": 0}}


def fetch_period_usage(management_key: str) -> Dict[str, Dict[str, float]]:
    """Return {key_name: {hourly, daily}} from the Analytics API."""
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    by_name: Dict[str, Dict[str, float]] = defaultdict(lambda: {"hourly": 0.0, "daily": 0.0})

    queries = (
        ("hourly", hour_start, "hour"),
        ("daily", day_start, "day"),
    )
    for field, start, granularity in queries:
        status, payload = api_post(
            "/analytics/query",
            management_key,
            {
                "metrics": ["total_usage"],
                "dimensions": ["api_key_id"],
                "granularity": granularity,
                "time_range": {
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "limit": 100,
            },
        )
        if status != 200:
            continue
        rows = ((payload.get("data") or {}).get("data")) or []
        for item in rows:
            name = str(item.get("api_key_id") or "").strip()
            if not name:
                continue
            by_name[name][field] = float(item.get("total_usage") or 0)

    return dict(by_name)


def fetch_account_credits(key: str) -> Optional[Dict[str, Any]]:
    status, payload = api_get("/credits", key)
    if status != 200:
        return None
    data = payload.get("data") or {}
    total_credits = data.get("total_credits")
    total_usage = data.get("total_usage")
    if total_credits is None or total_usage is None:
        return None
    return {
        "total_credits": total_credits,
        "total_usage": total_usage,
        "account_remaining": remaining_credits(total_credits, total_usage),
    }


def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def log_account_balance(
    log_path: Path,
    jsonl_path: Path,
    generated_at: str,
    account: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> None:
    remaining = account.get("account_remaining")
    purchased = account.get("total_credits")
    used = account.get("total_usage")
    key_bits = []
    for row in rows:
        name = row.get("name") or row.get("label") or "?"
        used_key = row.get("usage")
        hourly = row.get("usage_hourly")
        daily = row.get("usage_daily")
        key_bits.append(
            f"{name} used={money(used_key) if used_key is not None else '-'} "
            f"hourly={money(hourly) if hourly is not None else '-'} "
            f"daily={money(daily) if daily is not None else '-'}"
        )
    hourly_total = sum(r.get("usage_hourly") or 0 for r in rows)
    daily_total = sum(r.get("usage_daily") or 0 for r in rows)
    line = (
        f"{generated_at}  ACCOUNT balance={money(remaining)}  "
        f"purchased={money(purchased)}  total_usage={money(used)}  "
        f"hourly={money(hourly_total)}  daily={money(daily_total)}"
    )
    if key_bits:
        line += "  |  " + " ; ".join(key_bits)
    append_log(log_path, line)
    append_log(
        jsonl_path,
        json.dumps(
            {
                "ts": generated_at,
                "account_balance": remaining,
                "account_purchased": purchased,
                "account_used": used,
                "account_id": account.get("id"),
                "keys": [
                    {
                        "name": r.get("name"),
                        "label": r.get("label") or r.get("masked_key"),
                        "usage": r.get("usage"),
                        "usage_hourly": r.get("usage_hourly"),
                        "usage_daily": r.get("usage_daily"),
                        "limit": r.get("limit"),
                    }
                    for r in rows
                ],
            }
        ),
    )


def resolve_name(
    row: Dict[str, Any],
    named_keys: Dict[str, str],
    label_names: Dict[str, str],
) -> str:
    if row.get("name"):
        return str(row["name"])
    key = row.get("key")
    if key and named_keys.get(key):
        return named_keys[key]
    label = str(row.get("label") or "")
    if label and label_names.get(label):
        return label_names[label]
    if label:
        return label
    sources = row.get("sources") or []
    return source_name(sources[0]) if sources else row.get("masked_key") or "-"


def inspect_key(key: str, sources: List[str]) -> Dict[str, Any]:
    key_status, key_payload = api_get("/key", key)
    credits_status, credits_payload = api_get("/credits", key)

    row: Dict[str, Any] = {
        "key": key,
        "masked_key": mask_key(key),
        "sources": sources,
        "ok": key_status == 200,
        "key_status": key_status,
        "credits_status": credits_status,
        "error": None,
        "name": None,
        "label": None,
        "account_id": None,
        "is_management_key": False,
        "is_free_tier": None,
        "usage": None,
        "usage_hourly": None,
        "usage_daily": None,
        "usage_weekly": None,
        "usage_monthly": None,
        "limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "disabled": False,
        "total_credits": None,
        "total_usage": None,
        "account_remaining": None,
    }

    if key_status == 200:
        data = key_payload.get("data") or {}
        row.update(
            {
                "name": data.get("name"),
                "label": data.get("label"),
                "account_id": data.get("creator_user_id"),
                "is_management_key": bool(data.get("is_management_key")),
                "is_free_tier": data.get("is_free_tier"),
                "usage": data.get("usage"),
                "usage_daily": data.get("usage_daily"),
                "usage_weekly": data.get("usage_weekly"),
                "usage_monthly": data.get("usage_monthly"),
                "limit": data.get("limit"),
                "limit_remaining": data.get("limit_remaining"),
                "limit_reset": data.get("limit_reset"),
            }
        )
    else:
        err = key_payload.get("error") or {}
        row["error"] = err.get("message") or f"HTTP {key_status}"

    if credits_status == 200:
        data = credits_payload.get("data") or {}
        total_credits = data.get("total_credits")
        total_usage = data.get("total_usage")
        row["total_credits"] = total_credits
        row["total_usage"] = total_usage
        row["account_remaining"] = remaining_credits(total_credits, total_usage)
    elif row["error"] is None:
        err = credits_payload.get("error") or {}
        row["error"] = err.get("message") or f"credits HTTP {credits_status}"

    return row


def listed_to_row(item: Dict[str, Any]) -> Dict[str, Any]:
    label = item.get("label") or ""
    return {
        "key": None,
        "masked_key": label or "-",
        "sources": ["management-api"],
        "ok": True,
        "error": None,
        "name": item.get("name"),
        "label": label,
        "account_id": item.get("creator_user_id"),
        "is_management_key": bool(item.get("is_management_key")),
        "usage": item.get("usage"),
        "usage_hourly": None,
        "usage_daily": item.get("usage_daily"),
        "usage_weekly": item.get("usage_weekly"),
        "usage_monthly": item.get("usage_monthly"),
        "limit": item.get("limit"),
        "limit_remaining": item.get("limit_remaining"),
        "limit_reset": item.get("limit_reset"),
        "disabled": bool(item.get("disabled")),
        "total_credits": None,
        "total_usage": None,
        "account_remaining": None,
    }


def print_report(
    account_row: Optional[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    named_keys: Dict[str, str],
    label_names: Dict[str, str],
    listed_from_api: bool,
    alert_below: Optional[float],
) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\nOpenRouter balance report  ·  {now}")
    print("=" * 108)

    def _sum(field: str) -> Optional[float]:
        values = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        return sum(values) if values else None

    account_hourly = (account_row or {}).get("account_usage_hourly")
    account_daily = (account_row or {}).get("account_usage_daily")
    if account_hourly is None:
        account_hourly = _sum("usage_hourly")
    if account_daily is None:
        account_daily = _sum("usage_daily")

    if account_row and account_row.get("account_remaining") is not None:
        print(f"\nAccount: {account_row.get('account_id') or '-'}")
        print("  ACCOUNT CREDIT BALANCE")
        print(f"    Purchased    : {money(account_row['total_credits'])}")
        print(f"    Total usage  : {money(account_row['total_usage'])}")
        print(f"    Hourly usage : {money(account_hourly) if account_hourly is not None else '-'}")
        print(f"    Daily usage  : {money(account_daily) if account_daily is not None else '-'}")
        print(f"    Remaining    : {money(account_row['account_remaining'])}")
    elif rows:
        print("\nAccount credit balance unavailable")

    print(
        f"\n  {'Key name':<16} {'Key':<22} "
        f"{'Total':>12} {'Used':>12} {'Limit':>12} {'Hourly':>12} {'Daily':>12}"
    )
    print("  " + "-" * 104)

    def period_used(row: Dict[str, Any]) -> Optional[float]:
        limit = row.get("limit")
        remaining = row.get("limit_remaining")
        if isinstance(limit, (int, float)) and isinstance(remaining, (int, float)):
            return max(0.0, float(limit) - float(remaining))
        daily = row.get("usage_daily")
        if isinstance(daily, (int, float)):
            return float(daily)
        usage = row.get("usage")
        if isinstance(usage, (int, float)):
            return float(usage)
        return None

    alerts = 0
    used_values: List[float] = []
    for row in rows:
        name = resolve_name(row, named_keys, label_names)
        label = row.get("label") or row.get("masked_key") or "-"
        hourly = row.get("usage_hourly")
        daily = row.get("usage_daily")
        used = period_used(row)
        if used is not None:
            used_values.append(used)
        remaining = row.get("limit_remaining")
        if (
            alert_below is not None
            and isinstance(remaining, (int, float))
            and remaining < alert_below
        ):
            alerts += 1
        print(
            f"  {name:<16} {label:<22} "
            f"{money(row.get('usage')) if row.get('usage') is not None else '-':>12} "
            f"{money(used) if used is not None else '-':>12} "
            f"{money(row.get('limit')) if row.get('limit') is not None else 'none':>12} "
            f"{money(hourly) if hourly is not None else '-':>12} "
            f"{money(daily) if daily is not None else '-':>12}"
        )

    keys_total = _sum("usage")
    keys_hourly = _sum("usage_hourly")
    keys_daily = _sum("usage_daily")
    keys_used = sum(used_values) if used_values else None
    print("  " + "-" * 104)
    print(
        f"  {'TOTAL':<16} {'':<22} "
        f"{money(keys_total) if keys_total is not None else '-':>12} "
        f"{money(keys_used) if keys_used is not None else '-':>12} "
        f"{'':>12} "
        f"{money(keys_hourly) if keys_hourly is not None else '-':>12} "
        f"{money(keys_daily) if keys_daily is not None else '-':>12}"
    )

    ok_count = sum(1 for r in rows if r.get("ok"))
    print(f"\nChecked {len(rows)} key(s): {ok_count} ok, {len(rows) - ok_count} failed.")

    if not listed_from_api:
        print(
            "\nThis regular API key can only see itself. "
            "OpenRouter names (b, sn34-1, sn34-2) and the other keys "
            "are only available with a Management API key."
        )
        print(f"Create one at {MANAGEMENT_KEYS_URL}")
        print("Then add it to .env:")
        print("  OPENROUTER_MANAGEMENT_API_KEY=sk-or-v1-...")

    if alert_below is not None and account_row:
        remaining = account_row.get("account_remaining")
        if isinstance(remaining, (int, float)) and remaining < alert_below:
            print(f"ALERT: account remaining is below ${alert_below:.2f}")
            alerts += 1

    if alerts:
        print(f"Alerts: {alerts}")
    return 1 if alerts or ok_count == 0 else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect OpenRouter balances across all account keys.")
    parser.add_argument("--key", action="append", default=[], help="API key to check. Repeatable.")
    parser.add_argument(
        "--scan",
        action="append",
        default=[],
        help="Directory or file to scan for extra OpenRouter keys. Repeatable.",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="Keep keys that belong to other OpenRouter accounts (from --scan).",
    )
    parser.add_argument("--watch", type=float, default=0, help="Repeat every N seconds.")
    parser.add_argument("--alert-below", type=float, default=None, help="Exit 1 / print ALERT if remaining is below this USD amount.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--log",
        default=str(DEFAULT_LOG_PATH),
        help=f"Append account credit balance to this file (default: {DEFAULT_LOG_PATH.name}).",
    )
    parser.add_argument("--no-log", action="store_true", help="Do not write the balance log file.")
    return parser.parse_args()


def run_once(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    env_map = dict(os.environ)
    local_env = BOT_DIR / ".env"
    if local_env.exists():
        env_map = {**load_dotenv_file(local_env), **env_map}

    named_keys = parse_named_keys(env_map)
    label_names = parse_label_names(env_map)
    management_key = (
        env_map.get("OPENROUTER_MANAGEMENT_API_KEY")
        or env_map.get("OPENROUTER_PROVISIONING_API_KEY")
        or ""
    ).strip()

    extra_keys = list(args.key)
    extra_keys.extend(collect_keys_from_env_map(env_map))
    keys, sources = discover_keys(extra_keys, [Path(p) for p in args.scan])
    if management_key and is_real_key(management_key) and management_key not in keys:
        keys.append(management_key)
        sources[management_key] = ["process-env"]

    if not keys:
        print("No OpenRouter keys found. Set OPEN_ROUTER_API_KEY or pass --key.", file=sys.stderr)
        return 2, {"keys": [], "listed_keys": []}

    rows = [inspect_key(key, sources.get(key, [])) for key in keys]
    primary = next((r for r in rows if r.get("account_id") and not r.get("is_management_key")), None)
    if primary is None:
        primary = next((r for r in rows if r.get("account_id")), None)

    if primary and not args.all_accounts:
        primary_account = primary["account_id"]
        rows = [
            r
            for r in rows
            if r.get("account_id") == primary_account or r.get("is_management_key")
        ]

    listed_keys: List[Dict[str, Any]] = []
    list_status = None
    candidates = []
    if management_key and is_real_key(management_key):
        candidates.append(management_key)
    candidates.extend(r["key"] for r in rows if r.get("is_management_key") and r.get("ok") and r.get("key"))
    if primary and primary.get("key"):
        candidates.append(primary["key"])

    seen_mgmt = set()
    for key in candidates:
        if not key or key in seen_mgmt:
            continue
        seen_mgmt.add(key)
        list_status, listed_keys = list_account_keys(key)
        if list_status == 200 and listed_keys:
            break
        listed_keys = []

    listed_from_api = bool(listed_keys)
    display_rows = [listed_to_row(item) for item in listed_keys] if listed_from_api else rows
    period_usage: Dict[str, Dict[str, float]] = {}
    if management_key and is_real_key(management_key):
        period_usage = fetch_period_usage(management_key)
    for row in display_rows:
        row["name"] = resolve_name(row, named_keys, label_names)
        period = period_usage.get(str(row.get("name") or ""))
        if period:
            row["usage_hourly"] = period.get("hourly", 0.0)
            if period.get("daily") is not None:
                row["usage_daily"] = period.get("daily")

    credit_sources = []
    if management_key and is_real_key(management_key):
        credit_sources.append(management_key)
    if primary and primary.get("key"):
        credit_sources.append(primary["key"])
    credit_sources.extend(r["key"] for r in rows if r.get("ok") and r.get("key"))
    account_credits = None
    for key in credit_sources:
        if not key:
            continue
        account_credits = fetch_account_credits(key)
        if account_credits:
            break

    account = {
        "id": (primary or {}).get("account_id"),
        "total_credits": None,
        "total_usage": None,
        "account_remaining": None,
    }
    if account_credits:
        account.update(account_credits)
    if primary:
        primary = dict(primary)
        primary.update({k: account[k] for k in ("total_credits", "total_usage", "account_remaining")})

    generated_at = datetime.now(timezone.utc).isoformat()
    if not args.no_log:
        log_account_balance(
            Path(args.log),
            DEFAULT_JSONL_PATH if Path(args.log) == DEFAULT_LOG_PATH else Path(args.log).with_suffix(".jsonl"),
            generated_at,
            account,
            display_rows,
        )

    payload = {
        "generated_at": generated_at,
        "listed_from_api": listed_from_api,
        "list_status": list_status,
        "account": {
            "id": account.get("id"),
            "total_credits": account.get("total_credits"),
            "total_usage": account.get("total_usage"),
            "remaining": account.get("account_remaining"),
        },
        "keys": [
            {
                "name": r.get("name"),
                "label": r.get("label") or r.get("masked_key"),
                "usage": r.get("usage"),
                "usage_hourly": r.get("usage_hourly"),
                "usage_daily": r.get("usage_daily"),
                "limit": r.get("limit"),
                "disabled": r.get("disabled"),
            }
            for r in display_rows
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        alerts = 0
        if args.alert_below is not None:
            remaining = (primary or {}).get("account_remaining")
            if isinstance(remaining, (int, float)) and remaining < args.alert_below:
                alerts += 1
            for row in display_rows:
                left = row.get("limit_remaining")
                if isinstance(left, (int, float)) and left < args.alert_below:
                    alerts += 1
        return (1 if alerts else 0), payload

    code = print_report(
        primary,
        display_rows,
        named_keys,
        label_names,
        listed_from_api,
        args.alert_below,
    )
    if not args.no_log:
        print(f"Logged account credit balance to {args.log}")
    return code, payload


def main() -> int:
    args = parse_args()
    local_env = BOT_DIR / ".env"
    if local_env.exists():
        for name, value in load_dotenv_file(local_env).items():
            os.environ.setdefault(name, value)

    last_code = 0
    while True:
        last_code, _ = run_once(args)
        if not args.watch or args.watch <= 0:
            return last_code
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return last_code


if __name__ == "__main__":
    sys.exit(main())
