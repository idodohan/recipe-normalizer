# Phase 0: Security & Robustness Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the production-blocking security and robustness holes found in the 2026-07-08 audit: SSRF, open catalog writes, unbounded LLM spend, missing rate limits, poison jobs, silent 500s, hardcoded CORS, unvalidated uploads, and small correctness bugs.

**Architecture:** All changes are surgical hardening of existing modules — one new `extraction/netguard.py` module (SSRF), one new `ratelimit.py` module, one new `ingestion/sniff.py` module, one alembic migration (`users.is_admin`), and edits to existing files. No new services or schema beyond that.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, httpx, pytest + testcontainers (existing suite conventions).

## Global Constraints

- All CI gates green after every task: `uv run ruff check`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest -q`.
- After any API-visible change (new field/endpoint/status code): run `make client` and commit the regenerated `web/openapi.json` + `web/src/api/schema.d.ts`.
- Error responses always use the envelope from `src/recipe_normalizer/errors.py` (`ApiError(status, code, message)`).
- Settings live in `src/recipe_normalizer/config.py` with the `RN_` env prefix; never hardcode deploy config.
- Tests use the existing fixtures in `tests/conftest.py` (real Postgres via testcontainers; per-test rollback).
- Module boundaries are enforced by import-linter — cross-module imports only via `service.py`/`schemas.py` (plus the dependency-free top-level modules `errors.py`, `config.py`, `db.py`).

---

### Task 1: SSRF guard module + safe fetching

**Files:**
- Create: `src/recipe_normalizer/extraction/netguard.py`
- Modify: `src/recipe_normalizer/extraction/url_plugin.py` (`default_fetch` ~line 91, `default_fetch_bytes` ~line 116)
- Modify: `src/recipe_normalizer/ingestion/service.py` (`submit_url` ~line 148: validate before creating the job)
- Test: `tests/extraction/test_netguard.py`, extend `tests/ingestion/test_service.py`

**Interfaces:**
- Produces: `netguard.assert_public_url(url: str) -> None` raising `netguard.UnsafeUrlError(ValueError)`; `netguard.MAX_REDIRECTS = 5`.
- `submit_url` raises `ApiError(422, "unsafe_url", "This URL points to a private or internal address and cannot be fetched.")` when `assert_public_url` rejects.

- [ ] **Step 1: Write failing tests for `assert_public_url`**

```python
# tests/extraction/test_netguard.py
"""SSRF guard: user-supplied URLs must never reach private/internal addresses."""

from unittest.mock import patch

import pytest

from recipe_normalizer.extraction.netguard import UnsafeUrlError, assert_public_url


def _fake_getaddrinfo(ip: str):
    def fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (ip, 0))]

    return fake


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # loopback
        "10.1.2.3",         # RFC1918
        "172.16.0.1",       # RFC1918
        "192.168.1.1",      # RFC1918
        "169.254.169.254",  # cloud metadata (link-local)
        "100.64.0.1",       # CGNAT
        "0.0.0.0",          # unspecified
        "::1",              # IPv6 loopback
        "fd00::1",          # IPv6 ULA
        "fe80::1",          # IPv6 link-local
    ],
)
def test_private_addresses_rejected(ip: str) -> None:
    with patch("socket.getaddrinfo", _fake_getaddrinfo(ip)):
        with pytest.raises(UnsafeUrlError):
            assert_public_url("http://example.com/recipe")


def test_public_address_allowed() -> None:
    with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
        assert_public_url("https://example.com/recipe")


def test_bad_scheme_rejected() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        assert_public_url("ftp://example.com/x")


def test_no_host_rejected() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("http:///nohost")


def test_unresolvable_host_rejected() -> None:
    import socket as socket_mod

    def boom(host, port, *args, **kwargs):
        raise socket_mod.gaierror("no such host")

    with patch("socket.getaddrinfo", boom):
        with pytest.raises(UnsafeUrlError):
            assert_public_url("http://definitely-not-a-real-host.example/")


def test_mixed_resolution_rejected() -> None:
    """If ANY resolved address is private, reject (DNS round-robin trickery)."""

    def fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.1", 0))]

    with patch("socket.getaddrinfo", fake):
        with pytest.raises(UnsafeUrlError):
            assert_public_url("http://example.com/")
```

- [ ] **Step 2: Run tests, verify they fail** — `uv run pytest tests/extraction/test_netguard.py -q`. Expected: ImportError (module doesn't exist).

- [ ] **Step 3: Implement `netguard.py`**

```python
# src/recipe_normalizer/extraction/netguard.py
"""Guards outbound fetches of user-supplied URLs against SSRF.

Every URL the pipeline fetches — the submitted page, hero images discovered in
page content, and each redirect hop — must pass ``assert_public_url`` first.
The check resolves the hostname and rejects any address that is not globally
routable (loopback, RFC1918, link-local incl. 169.254.169.254 cloud metadata,
CGNAT, ULA, unspecified). Resolution happens at check time, so a TOCTOU/DNS-
rebinding window remains; acceptable at friends-and-family scale, documented.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = ["MAX_REDIRECTS", "UnsafeUrlError", "assert_public_url"]

MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """The URL is not safe to fetch server-side."""


def assert_public_url(url: str) -> None:
    """Raise UnsafeUrlError unless *url* is http(s) to a globally-routable host."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme '{parsed.scheme or '(none)'}' is not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host '{host}'") from exc
    if not infos:
        raise UnsafeUrlError(f"could not resolve host '{host}'")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise UnsafeUrlError(f"host '{host}' resolved to an invalid address") from exc
        if not ip.is_global:
            raise UnsafeUrlError(f"host '{host}' resolves to a non-public address")
```

- [ ] **Step 4: Run tests, verify pass** — `uv run pytest tests/extraction/test_netguard.py -q`. Expected: all pass.

- [ ] **Step 5: Write failing tests for redirect-hop validation in `default_fetch`**

Add to `tests/extraction/test_netguard.py`:

```python
import httpx

from recipe_normalizer.extraction.base import TierFailed
from recipe_normalizer.extraction.url_plugin import default_fetch


def test_default_fetch_rejects_unsafe_initial_url() -> None:
    with patch("socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")):
        with pytest.raises(TierFailed, match="non-public|not allowed|blocked"):
            default_fetch("http://internal.example/")


def test_default_fetch_rejects_unsafe_redirect_hop() -> None:
    """A public URL that redirects to a private address must be blocked."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="<html>secret</html>")

    def fake(host, port, *args, **kwargs):
        ip = "93.184.216.34" if host == "public.example" else "10.0.0.1"
        return [(2, 1, 6, "", (ip, 0))]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("socket.getaddrinfo", fake):
        with pytest.raises(TierFailed):
            default_fetch("http://public.example/recipe", client=client)
    assert calls == ["http://public.example/recipe"]  # never followed the redirect


def test_default_fetch_follows_safe_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://public.example/new"})
        return httpx.Response(200, text="<html>recipe here</html>", headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
        result = default_fetch("https://public.example/old", client=client)
    assert result.status == 200
    assert result.url == "https://public.example/new"
```

- [ ] **Step 6: Run, verify fail** — the redirect test fails because `default_fetch` currently follows redirects blindly.

- [ ] **Step 7: Rewrite `default_fetch`/`default_fetch_bytes` to validate every hop**

In `url_plugin.py`, import `from recipe_normalizer.extraction.netguard import MAX_REDIRECTS, UnsafeUrlError, assert_public_url` and replace the two fetchers:

```python
def _safe_get(
    url: str, *, client: httpx.Client | None = None
) -> httpx.Response:
    """GET with per-hop SSRF validation. Redirects are followed manually so
    every hop (not just the first URL) is checked against netguard."""
    own_client = client is None
    http = client or httpx.Client(timeout=_FETCH_TIMEOUT_S)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            assert_public_url(url)
            response = http.get(url, headers=_HEADERS, follow_redirects=False)
            if response.is_redirect and response.next_request is not None:
                url = str(response.next_request.url)
                continue
            response.raise_for_status()
            return response
        raise TierFailed(f"too many redirects (>{MAX_REDIRECTS})")
    except UnsafeUrlError as exc:
        raise TierFailed(f"blocked unsafe URL: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TierFailed(f"could not fetch page: {exc}") from exc
    finally:
        if own_client:
            http.close()


def default_fetch(url: str, *, client: httpx.Client | None = None) -> FetchResult:
    """Real page fetcher: browser-ish UA, netguard-validated redirects, 15s timeout."""
    response = _safe_get(url, client=client)
    return FetchResult(
        url=str(response.url),
        status=response.status_code,
        html=response.text,
        content_type=response.headers.get("content-type", ""),
    )


def default_fetch_bytes(url: str) -> tuple[bytes, str]:
    """Real binary fetcher for hero images; returns (data, media_type)."""
    response = _safe_get(url)
    media_type = response.headers.get("content-type", "application/octet-stream")
    return response.content, media_type.split(";")[0].strip()
```

NOTE: the old `default_fetch` used `with client:` (closing injected clients). Check existing tests in `tests/extraction/test_url_plugin.py` that inject clients — the new code does NOT close injected clients; if an existing test relies on the client being closed, update that test, not the semantics. Also patch `socket.getaddrinfo` (to a public IP) in any existing url_plugin tests that now hit netguard.

- [ ] **Step 8: Validate at submit time.** In `ingestion/service.py` `submit_url`, after the existing scheme/length checks:

```python
from recipe_normalizer.extraction.netguard import UnsafeUrlError, assert_public_url

    try:
        assert_public_url(url)
    except UnsafeUrlError as exc:
        raise ApiError(
            422,
            "unsafe_url",
            "This URL points to a private or internal address and cannot be fetched.",
        ) from exc
```

Check `.importlinter` first: if `ingestion` may not import from `extraction`, move `netguard.py` to top-level `src/recipe_normalizer/netguard.py` instead (it is dependency-free) and update all imports in this task accordingly.

Add to `tests/ingestion/test_service.py` (following its existing fixture style):

```python
def test_submit_url_rejects_private_address(db, user) -> None:
    from unittest.mock import patch

    def fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    with patch("socket.getaddrinfo", fake):
        with pytest.raises(ApiError) as exc_info:
            service.submit_url(db, user_id=user.id, url="http://localhost:8000/admin")
    assert exc_info.value.code == "unsafe_url"
```

Existing `submit_url` tests use non-resolving fake URLs — patch `socket.getaddrinfo` to a public IP in the shared fixture or per test as needed.

- [ ] **Step 9: Tier-3 browser guard.** In `extraction/browser.py`, before `page.goto(url)` (~line 198), call `assert_public_url(url)` and let `UnsafeUrlError` propagate as `TierFailed(f"blocked unsafe URL: {exc}")`. (Per-request interception inside the browser is out of scope — documented residual risk.)

- [ ] **Step 10: Full gates + commit**

```bash
uv run pytest -q && uv run ruff check && uv run mypy src && uv run lint-imports
git add -A && git commit -m "feat(security): SSRF guard on all user-driven fetches"
```

---

### Task 2: Admin role + catalog write authorization

**Files:**
- Modify: `src/recipe_normalizer/users/models.py` (add `is_admin`), `src/recipe_normalizer/users/schemas.py` (`UserOut`), `src/recipe_normalizer/users/service.py` (`register`: honor `RN_ADMIN_EMAILS`), `src/recipe_normalizer/config.py`, `src/recipe_normalizer/api_deps.py` (`require_admin`), `src/recipe_normalizer/catalog/router.py` (gate patch+merge)
- Create: `alembic/versions/<autogen>_users_is_admin.py`, `src/recipe_normalizer/users/make_admin.py`
- Test: `tests/users/test_service.py`, `tests/catalog/test_router.py`

**Interfaces:**
- Produces: `User.is_admin: bool` (default False); `api_deps.require_admin(user=Depends(get_current_user)) -> User` raising `ApiError(403, "forbidden", "Admin access required.")`; `Settings.admin_emails: str = ""` (comma-separated, case-insensitive); `python -m recipe_normalizer.users.make_admin <email>` CLI; `UserOut` gains `is_admin: bool`.

- [ ] **Step 1: Failing tests.** In `tests/catalog/test_router.py` add (mirroring its existing authed-client fixtures): a non-admin PATCH to `/api/catalog/ingredients/{id}` expects **403** with `code == "forbidden"`; same for `/merge`; an admin user (set `user.is_admin = True; db.flush()` in the fixture) gets 200. In `tests/users/test_service.py`: `register` with email listed in `settings.admin_emails` (monkeypatch `settings.admin_emails, "boss@example.com"`) yields `user.is_admin is True`; unlisted email yields `False`.
- [ ] **Step 2: Run, verify fail** (`is_admin` attribute missing).
- [ ] **Step 3: Implement.**

`users/models.py` — add to `User`:
```python
from sqlalchemy import Boolean
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
```

`config.py` — add `admin_emails: str = ""` to `Settings`.

`users/service.py` — in `register`, after building the user, before flush:
```python
admin_emails = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
if user.email.lower() in admin_emails:
    user.is_admin = True
```

`users/schemas.py` — add `is_admin: bool = False` to `UserOut`.

`api_deps.py`:
```python
def require_admin(user: User = Depends(get_current_user)) -> User:  # noqa: B008
    """Gate for endpoints that mutate global shared data (the catalog)."""
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admin access required.")
    return user
```

`catalog/router.py` — on `patch_ingredient` and `merge_ingredient`, replace `_user: object = Depends(get_current_user)` with `_user: object = Depends(require_admin)` (import from `api_deps`).

`users/make_admin.py`:
```python
"""CLI: grant admin to an existing user. Usage: python -m recipe_normalizer.users.make_admin <email>"""

import sys

from sqlalchemy import select

from recipe_normalizer.db import SessionLocal
from recipe_normalizer.users.models import User


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m recipe_normalizer.users.make_admin <email>")
    email = sys.argv[1].strip().lower()
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == email)).first()
        if user is None:
            raise SystemExit(f"no user with email {email}")
        user.is_admin = True
        db.commit()
        print(f"{email} is now an admin")


if __name__ == "__main__":
    main()
```
(Check `db.py` for the actual sessionmaker name — use what exists.)

- [ ] **Step 4: Migration.** `uv run alembic revision --autogenerate -m "users is_admin"`, review it (single `add_column` with `server_default="false"`, `nullable=False`), `uv run alembic upgrade head`.
- [ ] **Step 5: Run tests, verify pass.** Full suite: `uv run pytest -q`.
- [ ] **Step 6: Regenerate client** — `make client` (UserOut changed). Verify `git diff web/` shows `is_admin`.
- [ ] **Step 7: Gates + commit** — `git commit -m "feat(security): admin role gates catalog writes"`.

---

### Task 3: Per-job (not per-attempt) LLM cost cap + crash-cost accounting

**Files:**
- Modify: `src/recipe_normalizer/llm/client.py` (`LLMClient.__init__`), `src/recipe_normalizer/worker.py` (`make_llm_for_job` ~line 187, `process_job` ~line 241, `_fail_in_fresh_session` ~line 398), `src/recipe_normalizer/ingestion/service.py` (`retry_job` ~line 301: keep `attempts` reset, cost already preserved — no change needed there, but add a test pinning that cap counts preserved cost)
- Test: `tests/llm/test_client.py`, `tests/test_worker.py`

**Interfaces:**
- Produces: `LLMClient(recorder=..., cost_cap_usd=..., already_spent_usd: float = 0.0)`; `worker.JobProcessingError(exc, cost_usd: Decimal)` carrying attempt cost to the failure path.

- [ ] **Step 1: Failing tests.**

`tests/llm/test_client.py`:
```python
def test_already_spent_counts_toward_cap() -> None:
    client = LLMClient(cost_cap_usd=1.0, already_spent_usd=1.0)
    with pytest.raises(CostCapExceeded):
        client._check_cost_cap()
```

`tests/test_worker.py` (follow existing fake-LLM/job fixtures there):
- `test_make_llm_seeds_already_spent`: create a job with `cost_usd=Decimal("2.00")`, `settings.job_cost_cap_usd` monkeypatched to `1.50`; `make_llm_for_job(db, job)` returns a client whose `spent_usd == 2.0` and whose first call raises `CostCapExceeded`.
- `test_crash_attempt_cost_reaches_job_row`: a fake LLM that records spend then raises `RuntimeError`; after `_process_one`, the requeued job's `cost_usd` includes that attempt's spend.

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**

`llm/client.py` `__init__` — add parameter and seed:
```python
        already_spent_usd: float = 0.0,
    ...
        self._spent_usd = already_spent_usd
```

`worker.py` `make_llm_for_job`:
```python
    return LLMClient(
        recorder=DbUsageRecorder(db, user_id=job.user_id, job_id=job.id),
        cost_cap_usd=settings.job_cost_cap_usd,
        already_spent_usd=float(job.cost_usd or 0),
    )
```

`worker.py` — carry attempt cost through crashes. Define near the top:
```python
class JobProcessingError(Exception):
    """Wraps a processing exception with the LLM cost spent this attempt."""

    def __init__(self, original: BaseException, cost_usd: Decimal) -> None:
        super().__init__(str(original))
        self.original = original
        self.cost_usd = cost_usd
```
In `process_job`, wrap the body AFTER the llm client is created: catch `Exception as exc`, compute `attempt_cost = Decimal(str(llm.spent_usd)) - (job.cost_usd or Decimal("0"))` (floor at 0), `raise JobProcessingError(exc, attempt_cost) from exc`. Do NOT wrap control-flow outcomes that `process_job` already handles internally (`NotARecipe`, `CostCapExceeded`, tier failures) — read `process_job` first and wrap only the propagate-to-`_process_one` path.
In `_fail_in_fresh_session`, accept the cost and pass `cost_usd=` to `queue.fail`; in `_process_one`, unwrap `JobProcessingError` (use `.original` for the log, `.cost_usd` for the fail call).

- [ ] **Step 4: Run tests, verify pass; full suite.**
- [ ] **Step 5: Commit** — `git commit -m "fix(llm): cost cap is per-job across attempts and retries; crash attempts account their spend"`.

---

### Task 4: Rate limiting (auth + ingestion)

**Files:**
- Create: `src/recipe_normalizer/ratelimit.py`
- Modify: `src/recipe_normalizer/users/router.py` (login, register), `src/recipe_normalizer/ingestion/router.py` (3 submit endpoints), `src/recipe_normalizer/config.py`, `src/recipe_normalizer/errors.py` (add 429 to `code_map`: `429: "rate_limited"`)
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Produces: `ratelimit.SlidingWindowLimiter(max_events: int, window_s: float)` with `.check(key: str) -> bool` (False = limited) and `.reset()` (tests); FastAPI deps `limit_by_ip(name, max_events, window_s)` and `limit_by_user(name, max_events, window_s)` factories raising `ApiError(429, "rate_limited", "Too many requests — try again soon.")`.
- `Settings` gains: `rate_limit_auth_per_minute: int = 10`, `rate_limit_ingest_per_hour: int = 60`.

In-memory, per-process, thread-safe (`threading.Lock` + `dict[str, deque[float]]`); fine for the single-uvicorn deployment; documented in the module docstring.

- [ ] **Step 1: Failing tests.**
```python
# tests/test_ratelimit.py
from recipe_normalizer.ratelimit import SlidingWindowLimiter


def test_allows_up_to_max_then_blocks() -> None:
    lim = SlidingWindowLimiter(max_events=3, window_s=60.0)
    assert all(lim.check("k") for _ in range(3))
    assert lim.check("k") is False


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(max_events=1, window_s=60.0)
    assert lim.check("a") is True
    assert lim.check("b") is True


def test_window_expiry(monkeypatch) -> None:
    import recipe_normalizer.ratelimit as rl

    t = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: t[0])
    lim = SlidingWindowLimiter(max_events=1, window_s=10.0)
    assert lim.check("k") is True
    assert lim.check("k") is False
    t[0] += 11.0
    assert lim.check("k") is True
```
Plus router tests in `tests/users/test_router.py`: 11 rapid logins from the same client → 11th (or earlier, past the limit) returns 429 with envelope code `rate_limited`. Reset limiter state between tests via an autouse fixture calling the module-level limiter's `.reset()`.

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `ratelimit.py`**
```python
"""In-memory sliding-window rate limiting.

Per-process only — correct for the current single-uvicorn deployment; a
multi-instance deploy would need a shared store. Keys are "name:identity"
so different endpoints never share buckets.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Depends, Request

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.errors import ApiError
from recipe_normalizer.users.service import User

__all__ = ["SlidingWindowLimiter", "limit_by_ip", "limit_by_user"]


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_s: float) -> None:
        self._max = max_events
        self._window = window_s
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and q[0] <= now - self._window:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_limiter = SlidingWindowLimiter(max_events=1, window_s=1.0)  # replaced per-dep below


def _too_many() -> ApiError:
    return ApiError(429, "rate_limited", "Too many requests — try again soon.")


def limit_by_ip(name: str, max_events: int, window_s: float) -> Callable[..., None]:
    limiter = SlidingWindowLimiter(max_events, window_s)

    def dep(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.check(f"{name}:{ip}"):
            raise _too_many()

    dep.limiter = limiter  # type: ignore[attr-defined]  # test hook
    return dep


def limit_by_user(name: str, max_events: int, window_s: float) -> Callable[..., None]:
    limiter = SlidingWindowLimiter(max_events, window_s)

    def dep(user: User = Depends(get_current_user)) -> None:  # noqa: B008
        if not limiter.check(f"{name}:{user.id}"):
            raise _too_many()

    dep.limiter = limiter  # type: ignore[attr-defined]  # test hook
    return dep
```
(Drop the unused `_limiter` global if lint flags it. If importing `api_deps`/`users.service` from a top-level module violates import-linter, place the dep factories in `api_deps.py` instead and keep only `SlidingWindowLimiter` in `ratelimit.py`.)

Wire up: in `users/router.py` create module-level `_login_limit = limit_by_ip("login", settings.rate_limit_auth_per_minute, 60.0)` and add `_: None = Depends(_login_limit)` to `login` and (own instance) `register`; in `ingestion/router.py` one shared `_ingest_limit = limit_by_user("ingest", settings.rate_limit_ingest_per_hour, 3600.0)` on the three submit endpoints. Add `429: "rate_limited"` to the `code_map` in `errors.py`.

- [ ] **Step 4: Run tests + full gates.**
- [ ] **Step 5: Commit** — `git commit -m "feat(security): rate limiting on auth and ingestion endpoints"`.

---

### Task 5: Poison-job release cap

**Files:**
- Modify: `src/recipe_normalizer/ingestion/queue.py` (`release_stale` ~line 194)
- Test: `tests/ingestion/test_queue.py`

**Interfaces:** `release_stale` now counts each release as an attempt; a job released at `attempts >= MAX_ATTEMPTS - 1` goes to `failed` with `error="worker died repeatedly while processing this job"` instead of requeueing.

- [ ] **Step 1: Failing test** in `tests/ingestion/test_queue.py` (follow existing stale-release tests there): create a running job with `locked_at` 11 minutes old and `attempts = MAX_ATTEMPTS - 1`; `release_stale(db)`; job is `failed`, `error` mentions "worker died", lock cleared. Second test: `attempts = 0` → released to `queued` with `attempts == 1`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — replace the bulk `update()` in `release_stale` with a row-wise loop (job counts here are tiny):
```python
def release_stale(db: Session, *, older_than_minutes: int = 10) -> int:
    """Requeue running jobs whose lock is older than the threshold (dead workers).

    Each release counts as an attempt so a job that repeatedly kills its worker
    (OOM, segfault) cannot loop forever: past MAX_ATTEMPTS it fails terminally.
    Returns the number of jobs released or failed.
    """
    cutoff = _now() - timedelta(minutes=older_than_minutes)
    jobs = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.running, Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        job.attempts += 1
        _clear_lock(job)
        if job.attempts < MAX_ATTEMPTS:
            job.status = JobStatus.queued
        else:
            job.status = JobStatus.failed
            job.error = "worker died repeatedly while processing this job"
            if not job.reason:
                job.reason = _DEFAULT_FALLBACK_REASON
    db.flush()
    return len(jobs)
```
Update the existing test that asserts attempts are left unchanged (that behavior is the bug being fixed).
- [ ] **Step 4: Run + full suite.**
- [ ] **Step 5: Commit** — `git commit -m "fix(queue): stale releases count as attempts — poison jobs fail terminally"`.

---

### Task 6: Observability baseline — log 500s, request logging, session purge

**Files:**
- Modify: `src/recipe_normalizer/errors.py` (fallback handler), `src/recipe_normalizer/main.py` (request-logging middleware), `src/recipe_normalizer/worker.py` (housekeeping: purge expired sessions), `src/recipe_normalizer/users/service.py` (add `purge_expired_sessions`)
- Test: `tests/test_app.py`, `tests/users/test_service.py`

**Interfaces:** `users.service.purge_expired_sessions(db) -> int` (deletes sessions with `expires_at <= now`, returns count) — worker calls it in the same housekeeping cadence as `release_stale`.

- [ ] **Step 1: Failing tests.** `tests/test_app.py`: a route that raises `RuntimeError` (register a throwaway route on the test app) produces a 500 envelope AND `caplog` contains the traceback at ERROR level. `tests/users/test_service.py`: `purge_expired_sessions` deletes only expired rows.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**

`errors.py` — add `import logging` + `logger = logging.getLogger(__name__)`; in `fallback_handler`:
```python
        logger.exception(
            "unhandled error on %s %s", request.method, request.url.path, exc_info=exc
        )
```

`main.py` — after CORS middleware:
```python
import logging
import time as _time

logger = logging.getLogger("recipe_normalizer.access")

    @app.middleware("http")
    async def access_log(request, call_next):  # type: ignore[no-untyped-def]
        start = _time.perf_counter()
        response = await call_next(request)
        logger.info(
            "%s %s -> %d (%.0f ms)",
            request.method,
            request.url.path,
            response.status_code,
            (_time.perf_counter() - start) * 1000,
        )
        return response
```

`users/service.py`:
```python
def purge_expired_sessions(db: Session) -> int:
    """Delete expired session rows; returns how many were removed."""
    result = db.execute(delete(Session_).where(Session_.expires_at <= datetime.now(UTC)))
    db.flush()
    return int(result.rowcount or 0)
```
(Match the actual model import name used in that file.) In `worker.py`, call it wherever `release_stale` is invoked in the housekeeping pass (same session/commit).

- [ ] **Step 4: Run + gates.**
- [ ] **Step 5: Commit** — `git commit -m "feat(observability): log unhandled errors, access log, purge expired sessions"`.

---

### Task 7: Config-driven CORS origins

**Files:**
- Modify: `src/recipe_normalizer/config.py`, `src/recipe_normalizer/main.py:35`
- Test: `tests/test_app.py`

- [ ] **Step 1: Failing test** — monkeypatch `settings.cors_origins` to `"https://a.example, https://b.example"`, build `create_app()`, assert an OPTIONS preflight from `https://b.example` gets `access-control-allow-origin: https://b.example`.
- [ ] **Step 2: Implement** — `config.py`: `cors_origins: str = "http://localhost:5173"`; `main.py`: `allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()]`.
- [ ] **Step 3: Run + gates. Commit** — `git commit -m "feat(config): CORS origins configurable via RN_CORS_ORIGINS"`.

---

### Task 8: Upload content sniffing + decompression-bomb guards

**Files:**
- Create: `src/recipe_normalizer/ingestion/sniff.py`
- Modify: `src/recipe_normalizer/ingestion/service.py` (`submit_file` validation ~lines 229-237), `src/recipe_normalizer/extraction/image_plugin.py` (Pillow pixel cap), `src/recipe_normalizer/extraction/pdf_plugin.py` (per-page render pixel cap)
- Test: `tests/ingestion/test_sniff.py`, extend `tests/ingestion/test_service.py`

**Interfaces:** `sniff.detect_media_type(data: bytes) -> str | None` returning one of `application/pdf`, `image/png`, `image/jpeg`, `image/webp`, `image/gif`, or None.

- [ ] **Step 1: Failing tests.**
```python
# tests/ingestion/test_sniff.py
import pytest

from recipe_normalizer.ingestion.sniff import detect_media_type


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7 rest", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n rest", "image/png"),
        (b"\xff\xd8\xff\xe0 rest", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBP rest", "image/webp"),
        (b"GIF89a rest", "image/gif"),
        (b"GIF87a rest", "image/gif"),
        (b"<html>not a file</html>", None),
        (b"", None),
    ],
)
def test_detect_media_type(data: bytes, expected: str | None) -> None:
    assert detect_media_type(data) == expected
```
And in `tests/ingestion/test_service.py`: `submit_file` with `media_type="image/png"` but HTML bytes raises `ApiError` with code `unsupported_media_type` (match the existing code used for disallowed types — read the current validation first and reuse its error code); `submit_file` with a real `%PDF-` payload but client header `image/png` stores it as a **pdf** (sniffed type wins — assert the stored suffix/`input_type`).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `sniff.py`**
```python
"""Magic-byte media-type detection for uploads. Client Content-Type is a hint,
never trusted — the sniffed type decides validation and storage suffix."""

from __future__ import annotations

__all__ = ["detect_media_type"]

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def detect_media_type(data: bytes) -> str | None:
    for sig, media_type in _SIGNATURES:
        if data.startswith(sig):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
```
In `submit_file`: sniff first; if `None` or not in `_ALLOWED_MEDIA_TYPES` → the existing unsupported-type ApiError; use the sniffed type (not the client header) for `_MEDIA_TYPE_SUFFIX` and the stored media type.

`image_plugin.py` — at module import: `Image.MAX_IMAGE_PIXELS = 50_000_000` and wrap the open/downscale in `except Image.DecompressionBombError as exc: raise TierFailed("image too large to process safely") from exc` (match the plugin's existing error type — read it first).

`pdf_plugin.py` — before rasterizing each page, compute the page's pixel area at 144 dpi from its mediabox; skip pages over 50M pixels with a logged warning (read the existing render loop and integrate minimally).

- [ ] **Step 4: Run + full gates.**
- [ ] **Step 5: Commit** — `git commit -m "feat(security): magic-byte sniffing for uploads; image/pdf bomb guards"`.

---

### Task 9: Correctness nits

**Files:**
- Modify: `src/recipe_normalizer/cookbook/schemas.py` (`format_amount` ~line 27), `src/recipe_normalizer/catalog/conversion.py` (~lines 57, 63: `not density` → `density is None`), `src/recipe_normalizer/ingestion/service.py` (delete the pointless `remaining_in_db` copy ~line 124 and the `if TYPE_CHECKING: pass` no-op ~line 29), `.gitignore` (add `.DS_Store`)
- Test: `tests/cookbook/test_schemas.py`, `tests/catalog/test_conversion.py`

- [ ] **Step 1: Failing tests.** `format_amount(0.004) == "0.004"` (not `"0"`); `format_amount(0.0) == "0"` unchanged; conversion: an ingredient with `density_g_per_ml=0.0` (bypass the service guard by setting the attribute on a model instance directly) behaves as "no density" is WRONG per the new rule — with the fix, `0.0` divides… **no**: division by zero must not happen. Correct behavior: treat `density is None` as missing; an explicit `0.0` is invalid data — add a guard `if density is not None and density > 0` where it is used for division. Test: `density_g_per_ml=0.0` yields `None` conversion (no crash), `density_g_per_ml=None` yields `None`, positive density converts.
- [ ] **Step 2: Implement.**

`format_amount`:
```python
    rounded = round(x, 2)
    if rounded == 0 and x != 0:
        # Don't render tiny nonzero amounts as "0" — show 2 significant figures.
        return f"{x:.2g}"
    formatted = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return formatted
```
`conversion.py`: replace both `None if not density else Converted(...)` with `None if not (density and density > 0) else Converted(...)` — and the mass-path `if density is not None:` with `if density is not None and density > 0:`.

- [ ] **Step 3: Run + full gates + commit** — `git commit -m "fix: tiny-amount formatting, zero-density guards, dead code"`.

---

### Task 10: Phase gate — full verification

- [ ] Run everything CI runs: `uv run ruff check && uv run mypy src && uv run lint-imports && uv run pytest -q`, then `cd web && npm run typecheck`, then `make client && git diff --exit-code web/openapi.json web/src/api/schema.d.ts`.
- [ ] Run E2E: `cd e2e && npx playwright test` (requires Docker Postgres up; the config boots backend+frontend itself).
- [ ] Update `docs/superpowers/plans/2026-07-08-program-roadmap.md` Phase 0 status → done.
- [ ] Commit any stragglers; merge the phase branch to main.
