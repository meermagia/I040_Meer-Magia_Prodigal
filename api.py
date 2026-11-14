"""Thin client for the candidate DB API.

- quoted If-Match with the record version (optimistic concurrency)
- one Idempotency-Key per logical change, reused only when retrying the identical request
- bounded retries for timeouts / 429 / 5xx; 409 raised as Conflict for the caller to resolve
"""
import time
import uuid

import httpx


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class Conflict(APIError):
    pass


class CandidateAPI:
    def __init__(self, base_url, api_key, timeout=20.0, log=None, max_retries=3):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        self.log = log or (lambda event, **kw: None)
        self.max_retries = max_retries

    def _request(self, method, path, json_body=None, headers=None):
        last = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                r = self.client.request(method, path, json=json_body, headers=headers or {})
            except httpx.TransportError as e:  # timeouts, connection resets
                last = APIError(0, f"network error: {type(e).__name__}")
                self.log("api", method=method, path=path, request=json_body, headers=headers,
                         error=str(last), attempt=attempt, ms=int((time.time() - t0) * 1000))
                time.sleep(min(2 ** attempt, 8))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:500]}
            self.log("api", method=method, path=path, request=json_body, headers=headers,
                     status=r.status_code, response=body, attempt=attempt,
                     ms=int((time.time() - t0) * 1000))
            if r.status_code < 300:
                return body
            msg = body.get("detail", body) if isinstance(body, dict) else body
            if r.status_code == 409:
                raise Conflict(409, msg)
            if r.status_code == 429 or r.status_code >= 500:
                last = APIError(r.status_code, msg)
                wait = float(r.headers.get("Retry-After", 0) or 0) or min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            raise APIError(r.status_code, msg)  # 401/403/404/422: retrying will not help
        raise last

    def get_candidate(self, cid):
        return self._request("GET", f"/v1/candidates/{cid}")

    def patch_candidate(self, cid, fields, version, idempotency_key=None):
        """PATCH only `fields`. The same key is reused across this call's network retries."""
        headers = {
            "If-Match": f'"{version}"',
            "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
        }
        return self._request("PATCH", f"/v1/candidates/{cid}", json_body=fields, headers=headers)
