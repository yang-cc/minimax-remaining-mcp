"""Debug probe for the coding_plan/remains endpoint.

Standalone script for diagnosing cookie / header issues without bringing
up the full MCP server. Reads ``./data/cookies.json`` (relative to this
file) and hits both `www.minimaxi.com` and `api.minimaxi.com` with a
Chrome 151 user-agent.

Usage:

    .venv/Scripts/python.exe probe_coding_plan.py

Output: status code + first 800 chars of the response body for each
endpoint, so you can see whether you're getting 200, 401, 403, 429, or
a Cloudflare challenge page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
COOKIES_PATH = SCRIPT_DIR / "data" / "cookies.json"


def main() -> int:
    if not COOKIES_PATH.exists():
        print(f"[probe] {COOKIES_PATH} not found.")
        print("[probe] Run `minimax_login()` first, or copy a cookies.json into data/.")
        return 1

    with COOKIES_PATH.open("r", encoding="utf-8") as f:
        cookies = json.load(f)

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    group_id = next((c["value"] for c in cookies if c["name"] == "minimax_group_id_v2"), None)

    headers = {
        "Cookie": cookie_str,
        "x-group-id": group_id or "",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://platform.minimaxi.com/user-center/payment/coding-plan",
    }

    print(f"[probe] group_id from cookies: {group_id}")
    print(f"[probe] cookies count: {len(cookies)}")
    print()

    endpoints = [
        f"https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains?GroupId={group_id}",
        f"https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains?GroupId={group_id}",
    ]
    rc = 0
    for url in endpoints:
        print(f"[probe] GET {url}")
        try:
            r = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"[probe] request error: {type(e).__name__}: {e}")
            rc = 2
            print()
            continue
        print(f"[probe] status: {r.status_code}")
        print(f"[probe] content-type: {r.headers.get('content-type')}")
        print("[probe] body[:800]:")
        print(r.text[:800])
        print()
        if r.status_code != 200:
            rc = 3

    return rc


if __name__ == "__main__":
    sys.exit(main())