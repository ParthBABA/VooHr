"""Check ipinfo responses and VooVr's location lookup without changing app state."""

import os
import sys

from dotenv import load_dotenv
import requests


IPINFO_URL = "https://ipinfo.io"
TEST_IPS = (
    ("Google DNS", "8.8.8.8"),
    ("Indian public IP", "49.36.0.1"),
)
FIELDS = ("city", "region", "country")


def _fetch(token, ip=None):
    url = f"{IPINFO_URL}/{ip}/json" if ip else f"{IPINFO_URL}/json"
    try:
        response = requests.get(url, params={"token": token}, timeout=5)
    except requests.RequestException as exc:
        return None, None, f"network error ({type(exc).__name__})"

    try:
        data = response.json()
    except (ValueError, requests.RequestException):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return response.status_code, data, None


def _print_probe(label, status, data, lookup_location=None, error=None):
    print(label)
    print(f"  HTTP status: {status if status is not None else 'unavailable'}")
    for field in FIELDS:
        value = data.get(field) if data else None
        if isinstance(value, str):
            value = value.strip() or None
        if value:
            print(f"  {field}: present ({value})")
        else:
            print(f"  {field}: missing")
    if error:
        print(f"  Request: {error}")
    print(f"  _lookup_location: {lookup_location}")


def main():
    load_dotenv()
    token = os.environ.get("IP_API_KEY", "").strip()
    if not token:
        print("IP_API_KEY is missing. Set it in the environment or .env file.", file=sys.stderr)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from login_flow import _lookup_location

    results = []
    for label, ip in TEST_IPS:
        status, data, error = _fetch(token, ip)
        lookup_location = _lookup_location(ip)
        _print_probe(label, status, data, lookup_location, error)
        results.append((label, status, data, error))

    own_status, own_data, own_error = _fetch(token)
    own_ip = own_data.get("ip") if own_data else None
    own_location = _lookup_location(own_ip) if own_ip else None
    _print_probe("Your public IP", own_status, own_data, own_location, own_error)
    results.append(("Your public IP", own_status, own_data, own_error))

    if own_error:
        verdict = f"Failed ({own_error})"
    elif own_status != 200:
        verdict = f"Failed (HTTP {own_status})"
    elif own_data.get("city") and own_data.get("region"):
        verdict = "City+State available"
    elif own_data.get("country"):
        verdict = "Country only (free Lite plan)"
    else:
        verdict = "Failed (no location fields returned)"
    print(f"Verdict: {verdict}")
    return 0 if own_status == 200 and own_data.get("country") else 1


if __name__ == "__main__":
    raise SystemExit(main())