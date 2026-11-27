import json
import random
import time
import urllib.error
import urllib.request

BASE = "https://api.explorium.ai/v1"
RETRYABLE = {429, 500, 502, 503, 504}


class ExploriumError(Exception):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Explorium:
    def __init__(self, api_key, timeout=60, min_interval=0.4, max_retries=5, log=None, allow_billing=False):
        if not api_key:
            raise ExploriumError("EXPLORIUM_API_KEY is not set", status=401)
        self.api_key = api_key
        self.allow_billing = allow_billing
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.log = log or (lambda *_: None)
        self.credits_used = 0
        self.calls = 0
        self._last_call = 0.0

    def _throttle(self):
        gap = self.min_interval - (time.monotonic() - self._last_call)
        if gap > 0:
            time.sleep(gap)

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"api_key": self.api_key, "Accept": "application/json", "credit-usage": "true"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._last_call = time.monotonic()
                    self.calls += 1
                    data = json.loads(resp.read().decode() or "{}")
                usage = data.get("credit_usage") or {}
                billed = int(usage.get("total_credits") or 0)
                self.credits_used += billed
                if billed and not self.allow_billing:
                    raise ExploriumError(f"{path} billed {billed} credit(s); billing is not allowed for this call "
                                         f"(set OUTREACH_ALLOW_SEARCH_CREDITS=true to permit)", status=402, retryable=False)
                return data
            except urllib.error.HTTPError as e:
                self._last_call = time.monotonic()
                self.calls += 1
                text = e.read().decode(errors="replace")[:600]
                if e.code in RETRYABLE and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** attempt) + random.uniform(0, 1)
                    self.log(f"HTTP {e.code} on {path}, retry {attempt}/{self.max_retries} in {delay:.0f}s")
                    time.sleep(delay)
                    last_error = ExploriumError(f"HTTP {e.code} on {path}: {text}", status=e.code, retryable=True)
                    continue
                hint = {401: " (invalid API key)", 403: " (forbidden - usually low credit balance)"}.get(e.code, "")
                raise ExploriumError(f"HTTP {e.code} on {path}{hint}: {text}", status=e.code, retryable=e.code in RETRYABLE)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = ExploriumError(f"network error on {path}: {e}", retryable=True)
                if attempt < self.max_retries:
                    delay = min(60, 2 ** attempt) + random.uniform(0, 1)
                    self.log(f"network error on {path}, retry {attempt}/{self.max_retries} in {delay:.0f}s: {e}")
                    time.sleep(delay)
                    continue
        raise last_error or ExploriumError(f"request to {path} failed")

    def credits(self):
        data = self._request("GET", "/credits")
        return {
            "remaining": int(data.get("remaining_credits") or 0),
            "allocated": int(data.get("allocated_credits") or 0),
            "account_type": data.get("account_type"),
        }

    def fetch_prospects(self, filters, page_size=10, mode="preview"):
        payload = {"mode": mode, "size": page_size, "page_size": page_size, "page": 1, "filters": filters}
        data = self._request("POST", "/prospects", payload)
        rows = data.get("data")
        if rows is None:
            rows = data.get("results") or []
        return rows

    def bulk_enrich_contacts(self, prospect_ids):
        if not prospect_ids:
            return []
        previous, self.allow_billing = self.allow_billing, True
        try:
            data = self._request("POST", "/prospects/contacts_information/bulk_enrich", {"prospect_ids": list(prospect_ids)})
        finally:
            self.allow_billing = previous
        rows = data.get("data")
        if rows is None:
            rows = data.get("results") or []
        return [normalize_contact_row(r) for r in rows]


def normalize_contact_row(row):
    inner = row.get("data") if isinstance(row.get("data"), dict) else row
    emails = []
    for e in inner.get("emails") or []:
        if isinstance(e, dict) and e.get("address"):
            emails.append({"address": str(e["address"]).strip().lower(), "type": (e.get("type") or "").lower()})
        elif isinstance(e, str) and e.strip():
            emails.append({"address": e.strip().lower(), "type": ""})
    prof = inner.get("professions_email") or inner.get("professional_email")
    if prof and not any(e["address"] == str(prof).strip().lower() for e in emails):
        emails.append({"address": str(prof).strip().lower(), "type": "professional"})
    return {
        "prospect_id": row.get("prospect_id") or inner.get("prospect_id"),
        "emails": emails,
        "professional_email_status": (inner.get("professional_email_status") or "").lower() or None,
        "mobile_phone": inner.get("mobile_phone"),
    }
