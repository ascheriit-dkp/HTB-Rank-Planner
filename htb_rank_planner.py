#!/usr/bin/env python3
"""
HTB Rank Planner (Labs API v4)

Rank-aware planner for active HTB Machines and Challenges.

Current version improvements:
- Uses challenge/list for active challenge metadata; avoids /challenges paging.
- TWO-CACHE approach:
  - list cache (TTL)
  - item cache (FOREVER) for machine/profile/* and challenge/info/*
- Incremental pulls: only fetch item endpoints for NEW active IDs.
- Prunes cached items that are no longer active.
- Rolling-window rate limiter + GLOBAL limiter + cooldown-on-429 to prevent cascaded 429s.
- No-dependency progress bars.
- Uses the API-reported retained rank/next rank so retirement rank protection is handled correctly.
- Accepts current challenge solve aliases including authUserSolve.
- Retries transient network/5xx failures in addition to 429s.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from email.utils import parsedate_to_datetime

import requests

APP_VERSION = "1.0.0"


# ---------------- Errors ----------------

class HTBApiError(RuntimeError):
    pass


# ---------------- Helpers ----------------

def _format_rate_headers(h: Dict[str, str]) -> str:
    limit = h.get("X-RateLimit-Limit")
    remaining = h.get("X-RateLimit-Remaining")
    reset = h.get("X-RateLimit-Reset")
    retry_after = h.get("Retry-After")
    parts = []
    if limit is not None:
        parts.append(f"limit={limit}")
    if remaining is not None:
        parts.append(f"remaining={remaining}")
    if reset is not None:
        parts.append(f"reset={reset}")
    if retry_after is not None:
        parts.append(f"retry_after={retry_after}")
    return " ".join(parts) if parts else "-"


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "owned", "solved", "completed"}
    return False


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        s = str(v).strip()
        if s.isdigit():
            return int(s)
        return None
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v).strip())
    except Exception:
        return None


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "message", "info", "result", "challenges", "machines"):
        v = payload.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict) and isinstance(v.get("data"), list):
            return [x for x in v["data"] if isinstance(x, dict)]
    if isinstance(payload.get("data"), list):
        return [x for x in payload["data"] if isinstance(x, dict)]
    return []


def _extract_meta(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("meta"), dict):
        return payload["meta"]
    for key in ("message", "data", "result", "info"):
        v = payload.get(key)
        if isinstance(v, dict) and isinstance(v.get("meta"), dict):
            return v["meta"]
    for key in ("pagination", "paginate", "pager"):
        v = payload.get(key)
        if isinstance(v, dict):
            return v
    return {}


def _uniq_by_id(items: Sequence[Dict[str, Any]], id_key: str = "id") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items:
        _id = it.get(id_key)
        if _id is None:
            out.append(it)
            continue
        if _id in seen:
            continue
        seen.add(_id)
        out.append(it)
    return out


def _bar(pct: float, width: int = 24) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + f"] {pct:.1f}%"


def _progress(done: int, total: int, prefix: str) -> None:
    width = 28
    total = max(1, int(total))
    done = max(0, min(total, int(done)))
    filled = int(round(width * (done / total)))
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r{prefix} [{bar}] {done}/{total}", end="", file=sys.stderr)
    if done >= total:
        print(file=sys.stderr)


# -------- Difficulty normalization (0..10) --------

def _normalize_to_0_10(x: float) -> float:
    if x is None:
        return 5.5
    v = float(x)
    if v > 10.0 and v <= 100.0:
        v = v / 10.0
    return max(0.0, min(10.0, v))


def _difficulty_from_value(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return _normalize_to_0_10(float(v))
    if isinstance(v, str):
        s = v.strip().lower()
        mapping_0_100 = {
            "very easy": 10.0, "too easy": 15.0, "easy": 25.0,
            "medium": 50.0,
            "hard": 75.0, "too hard": 82.0,
            "insane": 90.0, "brainfuck": 95.0,
        }
        if s in mapping_0_100:
            return _normalize_to_0_10(mapping_0_100[s])
        try:
            return _normalize_to_0_10(float(s))
        except Exception:
            return None
    return None


def _extract_user_rated_difficulty(obj: Dict[str, Any]) -> float:
    for k in (
        "user_difficulty", "userDifficulty",
        "difficulty_rating", "difficultyRating",
        "avg_difficulty", "avgDifficulty",
        "rating", "stars",
        "difficulty", "difficultyText",
    ):
        if k in obj:
            dv = _difficulty_from_value(obj.get(k))
            if dv is not None:
                return dv
    return 5.5


# -------- Time parsing + formatting --------

_TIME_RE_DHMS = re.compile(r"(?:(\d+)\s*D)?\s*(?:(\d+)\s*H)?\s*(?:(\d+)\s*M)?\s*(?:(\d+)\s*S)?", re.I)
_TIME_RE_HMS = re.compile(r"^\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\s*$")


def _parse_any_time_to_minutes(v: Any) -> Optional[float]:
    if v is None:
        return None

    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return None
        if x > 500:
            if x > 10_000_000_000:
                return None
            return x / 60.0
        if x <= 120:
            return x
        return x / 60.0

    if not isinstance(v, str):
        return None

    s = v.strip()
    if not s:
        return None

    m = _TIME_RE_HMS.match(s)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        c = m.group(3)
        if c is None:
            return a + b / 60.0
        return a * 60.0 + b + int(c) / 60.0

    m2 = _TIME_RE_DHMS.fullmatch(s.replace("  ", " "))
    if not m2:
        m2 = _TIME_RE_DHMS.search(s)
    if m2:
        d = int(m2.group(1) or 0)
        h = int(m2.group(2) or 0)
        mi = int(m2.group(3) or 0)
        sec = int(m2.group(4) or 0)
        total_minutes = (d * 24 * 60) + (h * 60) + mi + (sec / 60.0)
        if total_minutes > 0:
            return total_minutes

    try:
        return _parse_any_time_to_minutes(float(s))
    except Exception:
        return None


def _format_minutes(m: Optional[float]) -> str:
    if m is None or m <= 0:
        return "n/a"
    if m < 1.0:
        secs = int(round(m * 60.0))
        if secs <= 0:
            return "n/a"
        return f"{secs}s"
    if m < 90:
        return f"{m:.0f}m"
    h = int(m // 60)
    mm = int(round(m - 60 * h))
    return f"{h}h{mm:02d}m"


def _clamp_est_minutes(m: Optional[float]) -> float:
    if m is None:
        return 1.0
    if m < 1.0:
        return 1.0
    return float(m)


def _challenge_id(item: Dict[str, Any]) -> Optional[Any]:
    for k in ("id", "challenge_id", "challengeId"):
        if k in item and item.get(k) is not None:
            return item.get(k)
    return None


def _challenge_solved_flag(item: Dict[str, Any]) -> bool:
    keys = (
        "isCompleted", "is_completed",
        "isSolved", "is_solved",
        "authUserHasSolved", "authUserSolved", "authUserSolve",
        "solved", "completed",
        "userCompleted", "user_completed",
        "owned", "isOwned", "is_owned",
    )
    for k in keys:
        if k in item and _as_bool(item.get(k)):
            return True
    for nest in ("user", "authUser", "auth_user"):
        v = item.get(nest)
        if isinstance(v, dict):
            for k in keys:
                if k in v and _as_bool(v.get(k)):
                    return True
    return False


def _estimate_minutes_from_difficulty(diff_0_10: float, kind: str) -> float:
    diff = max(0.0, min(10.0, float(diff_0_10)))
    t = diff / 10.0
    if kind == "challenge":
        return 5.0 + t * 75.0
    return 40.0 + t * 440.0


# ---------------- Disk cache + index ----------------

@dataclasses.dataclass
class DiskCache:
    base_dir: str
    ttl_seconds: Optional[int] = 1800  # None = forever
    enabled: bool = True

    def _path(self, key: str) -> str:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.base_dir, f"{h}.json")

    def _ensure_dir(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except Exception:
            pass

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        try:
            p = self._path(key)
            if not os.path.exists(p):
                return None
            if self.ttl_seconds is not None:
                st = os.stat(p)
                if time.time() - st.st_mtime > self.ttl_seconds:
                    return None
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_dir()
            p = self._path(key)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f)
            os.replace(tmp, p)
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
        except Exception:
            pass

    def delete(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            p = self._path(key)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


@dataclasses.dataclass
class CacheIndex:
    path: str
    enabled: bool = True
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, init=False)

    def _ensure_parent(self) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except Exception:
            pass

    def load(self) -> Dict[str, Dict[str, str]]:
        if not self.enabled:
            return {"machine_profile": {}, "challenge_info": {}}
        with self._lock:
            try:
                if not os.path.exists(self.path):
                    return {"machine_profile": {}, "challenge_info": {}}
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return {"machine_profile": {}, "challenge_info": {}}
                mp = data.get("machine_profile", {})
                ci = data.get("challenge_info", {})
                return {
                    "machine_profile": mp if isinstance(mp, dict) else {},
                    "challenge_info": ci if isinstance(ci, dict) else {},
                }
            except Exception:
                return {"machine_profile": {}, "challenge_info": {}}

    def save(self, data: Dict[str, Dict[str, str]]) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                self._ensure_parent()
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except Exception:
                    pass
            except Exception:
                pass


# ---------------- Rolling-window rate limiter (with global + cooldown) ----------------

class RollingWindowLimiter:
    """
    Sliding window limiter:
      allow <= (limit - margin) requests within last window_seconds.
    Adds per-bucket cooldown, so one 429 stops the whole herd.

    We use:
      - a GLOBAL bucket acquired for every request
      - an endpoint bucket acquired for its specific group
    This matches servers that enforce global quotas (common) and/or per-endpoint quotas.
    """

    def __init__(self, window_seconds: float = 60.0, margin: int = 2):
        self.window = float(window_seconds)
        self.margin = max(0, int(margin))
        self._lock = threading.Lock()
        self._limits: Dict[str, int] = {}
        self._hits: Dict[str, deque] = {}
        self._cooldown_until: Dict[str, float] = {}

    def set_limit(self, bucket: str, limit: int) -> None:
        with self._lock:
            self._limits[bucket] = max(1, int(limit))
            self._hits.setdefault(bucket, deque())
            self._cooldown_until.setdefault(bucket, 0.0)

    def _purge(self, q: deque, now: float) -> None:
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()

    def acquire(self, bucket: str) -> None:
        while True:
            now = time.time()
            with self._lock:
                lim = self._limits.get(bucket, 60)
                allow = max(1, lim - self.margin)
                cd = self._cooldown_until.get(bucket, 0.0)
                if now < cd:
                    sleep_s = cd - now
                else:
                    q = self._hits.setdefault(bucket, deque())
                    self._purge(q, now)
                    if len(q) < allow:
                        q.append(now)
                        return
                    # need to wait until oldest falls out of window
                    sleep_s = (q[0] + self.window) - now + 0.02
            time.sleep(max(0.01, sleep_s))

    def note_429(self, bucket: str, retry_after_s: float) -> None:
        ra = float(retry_after_s) if retry_after_s else 1.0
        ra = max(0.5, ra)
        now = time.time()
        with self._lock:
            self._cooldown_until[bucket] = max(self._cooldown_until.get(bucket, 0.0), now + ra + 0.05)


# ---------------- API Client ----------------

@dataclasses.dataclass
class HTBClient:
    token: str
    timeout: int = 25
    debug: bool = False
    workers: int = 24

    cache_lists: Optional[DiskCache] = None
    cache_items: Optional[DiskCache] = None
    cache_index: Optional[CacheIndex] = None

    limiter: Optional[RollingWindowLimiter] = None

    _tls: threading.local = dataclasses.field(default_factory=threading.local, init=False)

    @property
    def base_url(self) -> str:
        return "https://labs.hackthebox.com/api/v4"

    def _session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            setattr(self._tls, "session", s)
        return s

    def _bucket_for(self, endpoint: str) -> str:
        ep = endpoint.lstrip("/")
        if ep.startswith("challenge/info/"):
            return "challenge_info"
        if ep.startswith("machine/profile/"):
            return "machine_profile"
        if ep.startswith("challenge/list"):
            return "lists_active_challenges"
        if ep.startswith("machine/paginated"):
            return "lists_machines"
        return "lists_other"

    def _cache_for(self, endpoint: str) -> Optional[DiskCache]:
        ep = endpoint.lstrip("/")
        if ep.startswith("challenge/info/") or ep.startswith("machine/profile/"):
            return self.cache_items
        return self.cache_lists

    def request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        params: Optional[dict] = None,
        json_body: Any = None,
        use_cache: bool = True,
        cache_key_override: Optional[str] = None,
    ) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"htb-rank-planner/{APP_VERSION}",
            "Accept": "application/json",
        }

        cache = self._cache_for(endpoint) if use_cache else None
        cache_key = None
        if cache and cache.enabled and method.upper() == "GET":
            if cache_key_override:
                cache_key = cache_key_override
            else:
                if params:
                    q = "&".join(sorted([f"{k}={v}" for k, v in params.items()]))
                    cache_key = url + "?" + q
                else:
                    cache_key = url
            hit = cache.get(cache_key)
            if hit is not None:
                if self.debug:
                    print(f"[DEBUG] CACHE HIT {method} {url}", file=sys.stderr)
                return hit

        bucket = self._bucket_for(endpoint)

        # acquire GLOBAL + endpoint bucket (prevents global quota 429 + endpoint quota 429)
        if self.limiter:
            self.limiter.acquire("global")
            self.limiter.acquire(bucket)

        max_tries = 6

        def retry_after_seconds(raw: Optional[str], fallback: float) -> float:
            if not raw:
                return fallback
            try:
                return max(0.5, float(raw))
            except Exception:
                try:
                    dt = parsedate_to_datetime(raw)
                    return max(0.5, dt.timestamp() - time.time())
                except Exception:
                    return fallback

        for attempt in range(max_tries):
            try:
                r = self._session().request(method, url, headers=headers, params=params, json=json_body, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt + 1 >= max_tries:
                    raise HTBApiError(f"{method} {url} failed after {max_tries} attempts: {e}") from e
                wait_s = min(20.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25)
                if self.debug:
                    print(f"[DEBUG] network error: retrying in {wait_s:.2f}s: {e}", file=sys.stderr)
                time.sleep(wait_s)
                continue

            if self.debug:
                rl = _format_rate_headers(dict(r.headers))
                print(f"[DEBUG] {method} {r.url} -> {r.status_code}  rl={rl}", file=sys.stderr)

            if r.status_code == 429:
                wait_s = retry_after_seconds(r.headers.get("Retry-After"), min(30.0, 1.0 * (2 ** attempt)))

                if self.debug:
                    print(f"[DEBUG] 429: cooldown {wait_s:.1f}s (bucket={bucket})", file=sys.stderr)

                if self.limiter:
                    # cooldown both bucket + global so all worker threads stop stampeding
                    self.limiter.note_429(bucket, wait_s)
                    self.limiter.note_429("global", wait_s)
                    time.sleep(wait_s)
                    self.limiter.acquire("global")
                    self.limiter.acquire(bucket)
                else:
                    time.sleep(wait_s)
                continue

            if 500 <= r.status_code <= 599:
                if attempt + 1 >= max_tries:
                    snippet = r.text[:450].replace("\n", " ")
                    raise HTBApiError(f"{method} {url} -> {r.status_code} after {max_tries} attempts: {snippet}")
                wait_s = min(20.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25)
                if self.debug:
                    print(f"[DEBUG] {r.status_code}: retrying in {wait_s:.2f}s", file=sys.stderr)
                time.sleep(wait_s)
                continue

            if r.status_code >= 400:
                snippet = r.text[:450].replace("\n", " ")
                raise HTBApiError(f"{method} {url} -> {r.status_code}: {snippet}")

            if not r.text.strip():
                return None

            try:
                data = r.json()
            except Exception:
                data = r.text

            if cache and cache_key:
                cache.set(cache_key, data)
            return data

        raise HTBApiError(f"{method} {url} -> 429: rate limit (exceeded retries)")

    def _fetch_all_pages(self, endpoint: str, *, per_page: int = 100, use_cache: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        per_page = min(int(per_page), 100)

        def fetch_page(page: int) -> Any:
            return self.request(endpoint, params={"per_page": per_page, "limit": per_page, "page": page}, use_cache=use_cache)

        first = fetch_page(1)
        items = _extract_list(first)
        meta = _extract_meta(first)

        if self.debug:
            m = {k: meta.get(k) for k in ("total", "per_page", "current_page", "last_page")}
            print(f"[DEBUG] {endpoint} page=1 items={len(items)} meta={m}", file=sys.stderr)

        out = list(items)
        last_page = _safe_int(meta.get("last_page")) or _safe_int(meta.get("lastPage")) or _safe_int(meta.get("pages")) or 1
        if last_page <= 1:
            return _uniq_by_id(out), meta

        with ThreadPoolExecutor(max_workers=max(4, self.workers)) as ex:
            futs = {ex.submit(fetch_page, p): p for p in range(2, last_page + 1)}
            for fut in as_completed(futs):
                payload = fut.result()
                chunk = _extract_list(payload)
                if self.debug:
                    p = futs[fut]
                    print(f"[DEBUG] {endpoint} page={p} items={len(chunk)}", file=sys.stderr)
                out.extend(chunk)

        return _uniq_by_id(out), meta

    # ---- endpoints ----

    def get_user_info(self) -> Dict[str, Any]:
        return self.request("user/info", use_cache=False)

    def get_user_profile_basic(self, user_id: int) -> Dict[str, Any]:
        return self.request(f"user/profile/basic/{user_id}", use_cache=False)

    def list_active_machines(self) -> List[Dict[str, Any]]:
        machines, _ = self._fetch_all_pages("machine/paginated", per_page=100, use_cache=False)
        out = []
        for m in machines:
            if m.get("retired") in (1, "1", True):
                continue
            if m.get("active") is not None and int(m.get("active")) == 0:
                continue
            out.append(m)
        return out

    def get_machine_profile_cached(self, machine_id: int, machine_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        cache_key = f"machine_profile:{machine_id}"
        try:
            return self.request(f"machine/profile/{machine_id}", use_cache=True, cache_key_override=cache_key)
        except HTBApiError:
            if not machine_name:
                return None
            try:
                # Some current clients/documentation describe this endpoint as slug/name based.
                return self.request(f"machine/profile/{machine_name}", use_cache=True, cache_key_override=cache_key)
            except HTBApiError:
                return None

    def list_active_challenges_items(self) -> List[Dict[str, Any]]:
        payload = self.request("challenge/list", use_cache=False)
        return _extract_list(payload)

    def get_challenge_info_cached(self, cid: Any) -> Optional[Dict[str, Any]]:
        try:
            return self.request(f"challenge/info/{cid}", use_cache=True, cache_key_override=f"challenge_info:{cid}")
        except HTBApiError:
            return None


# ---------------- Ownership + Ranks ----------------

@dataclasses.dataclass
class OwnershipSnapshot:
    active_machines_total: int
    active_challenges_total: int
    active_user_owns: int
    active_root_owns: int
    active_challenge_owns: int

    @property
    def denom_points(self) -> float:
        return (self.active_machines_total * 1.0) + (self.active_machines_total / 2.0) + (self.active_challenges_total / 10.0)

    @property
    def numer_points(self) -> float:
        return (self.active_root_owns * 1.0) + (self.active_user_owns / 2.0) + (self.active_challenge_owns / 10.0)

    @property
    def ownership_percent(self) -> float:
        if self.denom_points <= 0:
            return 0.0
        return (self.numer_points / self.denom_points) * 100.0


RANK_THRESHOLDS = [
    ("Noob", 0.0, True),
    ("Script Kiddie", 5.0, False),
    ("Hacker", 20.0, False),
    ("Pro Hacker", 45.0, False),
    ("Elite Hacker", 70.0, False),
    ("Guru", 90.0, False),
    ("Omniscient", 100.0, True),
]


def rank_for_ownership(pct: float) -> str:
    if pct >= 100.0 - 1e-9:
        return "Omniscient"
    current = "Noob"
    for name, thr, inclusive in RANK_THRESHOLDS:
        if inclusive:
            if pct >= thr:
                current = name
        else:
            if pct > thr:
                current = name
    return current


def rank_bounds(pct: float) -> Tuple[str, float, str, float]:
    cur = rank_for_ownership(pct)
    names = [x[0] for x in RANK_THRESHOLDS]
    thrs = {x[0]: x[1] for x in RANK_THRESHOLDS}
    idx = names.index(cur)
    if cur == "Omniscient":
        return ("Omniscient", 100.0, "Omniscient", 100.0)
    nxt = names[idx + 1]
    return (cur, thrs[cur], nxt, thrs[nxt])


def progress_to_next_rank(pct: float) -> float:
    cur, cur_thr, nxt, nxt_thr = rank_bounds(pct)
    if cur == "Omniscient":
        return 100.0
    span = max(1e-9, (nxt_thr - cur_thr))
    return max(0.0, min(100.0, ((pct - cur_thr) / span) * 100.0))


def _rank_threshold(name: Optional[str]) -> Optional[float]:
    if not name:
        return None
    normalized = str(name).strip().lower()
    for rank_name, thr, _inclusive in RANK_THRESHOLDS:
        if rank_name.lower() == normalized:
            return thr
    return None


def _rank_threshold_text(name: Optional[str], threshold: float) -> str:
    normalized = str(name or "").strip().lower()
    if normalized == "omniscient":
        return f"={threshold:.1f}%"
    if normalized == "noob":
        return f">={threshold:.1f}%"
    return f">{threshold:.1f}%"


def _next_rank_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    names = [x[0] for x in RANK_THRESHOLDS]
    for i, rank_name in enumerate(names):
        if rank_name.lower() == str(name).strip().lower():
            return names[i + 1] if i + 1 < len(names) else None
    return None


def _retained_rank_progress(ownership_pct: float, current_rank: str, next_rank: str) -> float:
    cur_thr = _rank_threshold(current_rank)
    nxt_thr = _rank_threshold(next_rank)
    if cur_thr is None or nxt_thr is None or nxt_thr <= cur_thr:
        return 100.0 if current_rank == "Omniscient" else 0.0
    if ownership_pct <= cur_thr:
        return 0.0
    return max(0.0, min(100.0, ((ownership_pct - cur_thr) / (nxt_thr - cur_thr)) * 100.0))


def _needed_points(snapshot: OwnershipSnapshot, target_pct: float, *, strict: bool = True) -> float:
    """Return the minimum numerator-point gain needed to cross a rank threshold.

    HTB rank thresholds are strict (>), except Omniscient (=100). Planner actions
    move the numerator in 0.1-point increments, so targeting the exact percentage
    can otherwise stop one challenge-equivalent short of the actual rank.
    """
    denom = snapshot.denom_points
    raw_target = (target_pct / 100.0) * denom
    if strict:
        unit = 0.1
        target_points = (math.floor((raw_target / unit) + 1e-9) + 1) * unit
    else:
        target_points = raw_target
    return max(0.0, target_points - snapshot.numer_points)


# ---------------- Challenge FB extraction ----------------

def _walk_json(obj: Any, path: str = "") -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            out.append((p, v))
            out.extend(_walk_json(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            out.append((p, v))
            out.extend(_walk_json(v, p))
    return out


def extract_challenge_first_blood_minutes(payload: Dict[str, Any]) -> Optional[float]:
    if not isinstance(payload, dict):
        return None

    root = payload
    for key in ("info", "challenge", "data", "result", "message"):
        v = payload.get(key)
        if isinstance(v, dict):
            root = v
            break

    candidates: List[float] = []
    for p, v in _walk_json(root):
        pl = p.lower()
        if ("blood" in pl) or ("first" in pl and "time" in pl):
            mm = _parse_any_time_to_minutes(v)
            if mm is not None and 0.0001 <= mm <= 60 * 24 * 365:
                candidates.append(mm)

    if not candidates:
        for p, v in _walk_json(root):
            pl = p.lower()
            if "time" in pl or "duration" in pl:
                mm = _parse_any_time_to_minutes(v)
                if mm is not None and 0.0001 <= mm <= 60 * 24 * 365:
                    candidates.append(mm)

    return min(candidates) if candidates else None


# ---------------- Actions / Planning ----------------

@dataclasses.dataclass(frozen=True)
class Action:
    kind: str
    group: str
    id: Any
    name: str
    difficulty: float
    gain_points: float
    flags: int
    est_minutes: float
    fb_user_min: Optional[float] = None
    fb_root_min: Optional[float] = None


@dataclasses.dataclass
class PlanResult:
    chosen: List[Action]
    total_points: float
    total_flags: int
    total_minutes: float
    total_difficulty: float


def _dp_choose_min_cost(groups: List[List[Optional[Action]]], needed_points: float, cost_fn, tie_fn) -> PlanResult:
    """Choose a minimum-cost plan while keeping user-only machine solves terminal.

    A ``machine_user`` action is allowed only when:
    - the plan contains at most one such action; and
    - removing it would leave the plan below the target.

    That means it can always be presented as the final granularity step instead of
    becoming a normal recommendation alongside full machine solves.
    """
    unit = 0.1
    need_units = int(math.ceil(needed_points / unit - 1e-9))
    max_units = need_units + 200
    INF = 1e30

    # For each gain bucket, keep separate best states for:
    #   terminal_gain_units == 0: no machine_user selected
    #   terminal_gain_units > 0: exactly one machine_user selected
    # Keeping these states separate prevents a cheap but invalid user-only plan from
    # hiding the best valid no-user plan at the same total gain.
    dp: List[Dict[int, Tuple[float, Tuple, List[Action]]]] = [dict() for _ in range(max_units + 1)]
    dp[0][0] = (0.0, tie_fn([]), [])

    for group in groups:
        new: List[Dict[int, Tuple[float, Tuple, List[Action]]]] = [dict() for _ in range(max_units + 1)]
        for u in range(max_units + 1):
            for terminal_gain_units, (base_cost, _base_tie, base_list) in dp[u].items():
                for opt in group:
                    terminal2 = terminal_gain_units
                    if opt is None:
                        cost2 = base_cost
                        lst2 = base_list
                        u2 = u
                    else:
                        gain_units = int(round(opt.gain_points / unit))
                        if opt.kind == "machine_user":
                            # Never recommend multiple user-only machine solves.
                            if terminal_gain_units > 0:
                                continue
                            terminal2 = gain_units
                        u2 = min(max_units, u + gain_units)
                        lst2 = base_list + [opt]
                        cost2 = base_cost + cost_fn(opt)

                    tie2 = tie_fn(lst2)
                    cur = new[u2].get(terminal2)
                    if cur is None or cost2 < cur[0] or (abs(cost2 - cur[0]) < 1e-9 and tie2 < cur[1]):
                        new[u2][terminal2] = (cost2, tie2, lst2)
        dp = new

    best = (INF, (), [])
    for u in range(need_units, max_units + 1):
        for terminal_gain_units, state in dp[u].items():
            # If user-only is present, it must be essential: without that action the
            # plan is still below target, so the user flag can genuinely be the last
            # step that crosses the threshold.
            if terminal_gain_units > 0 and (u - terminal_gain_units) >= need_units:
                continue
            if state[0] < best[0] or (abs(state[0] - best[0]) < 1e-9 and state[1] < best[1]):
                best = state

    chosen = best[2]
    if chosen:
        chosen = [a for a in chosen if a.kind != "machine_user"] + [a for a in chosen if a.kind == "machine_user"]
    return PlanResult(
        chosen=chosen,
        total_points=sum(a.gain_points for a in chosen),
        total_flags=sum(a.flags for a in chosen),
        total_minutes=sum(a.est_minutes for a in chosen),
        total_difficulty=sum(a.difficulty for a in chosen),
    )


def _fb_key_minutes(a: Action) -> float:
    times: List[float] = []
    if a.fb_user_min is not None and a.fb_user_min > 0:
        times.append(float(a.fb_user_min))
    if a.fb_root_min is not None and a.fb_root_min > 0:
        times.append(float(a.fb_root_min))
    return min(times) if times else float("inf")


def _choose_easiest_greedy(groups: List[List[Optional[Action]]], needed_points: float) -> PlanResult:
    normal_opts: List[Action] = []
    terminal_users: List[Action] = []
    for g in groups:
        for opt in g:
            if opt is not None:
                if opt.kind == "machine_user":
                    terminal_users.append(opt)
                else:
                    normal_opts.append(opt)

    def easiest_key(a: Action) -> Tuple:
        return (a.difficulty, _fb_key_minutes(a), -a.gain_points, a.flags)

    normal_opts.sort(key=easiest_key)
    terminal_users.sort(key=easiest_key)

    chosen: List[Action] = []
    used_groups: Set[str] = set()
    total = 0.0

    normal_i = 0
    while total + 1e-9 < needed_points:
        while normal_i < len(normal_opts) and normal_opts[normal_i].group in used_groups:
            normal_i += 1
        next_normal = normal_opts[normal_i] if normal_i < len(normal_opts) else None

        remaining = needed_points - total
        next_user = None
        if remaining <= 0.5 + 1e-9:
            for candidate in terminal_users:
                if candidate.group not in used_groups and candidate.gain_points + 1e-9 >= remaining:
                    next_user = candidate
                    break

        if next_user is not None:
            # Difficulty is the primary criterion and first-blood is the tie-breaker.
            # If both are identical, prefer the terminal user flag over unnecessary
            # extra root work because it is already sufficient to cross the target.
            user_key = (next_user.difficulty, _fb_key_minutes(next_user), 0, -next_user.gain_points, next_user.flags)
            normal_key = (
                (next_normal.difficulty, _fb_key_minutes(next_normal), 1, -next_normal.gain_points, next_normal.flags)
                if next_normal is not None else None
            )
            if normal_key is None or user_key < normal_key:
                chosen.append(next_user)
                used_groups.add(next_user.group)
                total += next_user.gain_points
                break

        if next_normal is None:
            break
        chosen.append(next_normal)
        used_groups.add(next_normal.group)
        total += next_normal.gain_points
        normal_i += 1

    return PlanResult(
        chosen=chosen,
        total_points=sum(x.gain_points for x in chosen),
        total_flags=sum(x.flags for x in chosen),
        total_minutes=sum(x.est_minutes for x in chosen),
        total_difficulty=sum(x.difficulty for x in chosen),
    )


def _summarize(snapshot: OwnershipSnapshot, chosen: List[Action]) -> Tuple[float, float, int, float]:
    gained = sum(a.gain_points for a in chosen)
    proj_num = snapshot.numer_points + gained
    proj_pct = (proj_num / snapshot.denom_points) * 100.0 if snapshot.denom_points > 0 else 0.0
    flags = sum(a.flags for a in chosen)
    minutes = sum(a.est_minutes for a in chosen)
    return gained, proj_pct, flags, minutes


def print_plan(title: str, snapshot: OwnershipSnapshot, chosen: List[Action], top: int) -> None:
    denom = snapshot.denom_points if snapshot.denom_points > 0 else 1.0
    gained, proj_pct, flags, minutes = _summarize(snapshot, chosen)

    print(title)
    print(f"  Steps: {len(chosen)}   Flags: {flags}   Est time: {_format_minutes(minutes)}")
    print(f"  Projected ownership%: {proj_pct:.4f}%   (gain {gained:.4f} numerator points)")
    print("  Recommended actions:")
    print(f"    {'type':12s} | {'name':28s} | {'diff':>5s} | {'fb(u/r)':>11s} | {'est':>6s} | {'gain':>8s} | {'gain/min':>9s} | {'flags':>5s}")
    print(f"    {'-'*12}-+-{'-'*28}-+-{'-'*5}-+-{'-'*11}-+-{'-'*6}-+-{'-'*8}-+-{'-'*9}-+-{'-'*5}")

    shown = 0
    for a in chosen:
        if shown >= top:
            print(f"    ... (+{len(chosen) - top} more)")
            break

        gain_pct = (a.gain_points / denom) * 100.0
        gm = gain_pct / max(1e-9, a.est_minutes)

        if a.fb_user_min is None and a.fb_root_min is None:
            fb = "n/a"
        else:
            u = _format_minutes(a.fb_user_min) if a.fb_user_min is not None else "-"
            r = _format_minutes(a.fb_root_min) if a.fb_root_min is not None else "-"
            fb = f"{u}/{r}"

        print(f"    {a.kind:12s} | {a.name:28.28s} | {a.difficulty:5.1f} | {fb:>11s} | {_format_minutes(a.est_minutes):>6s} | {gain_pct:7.4f}% | {gm:9.5f} | {a.flags:5d}")
        shown += 1
    print()


# ---------------- Main ----------------

def _read_token_from_file(path: str) -> str:
    if path == "-":
        data = sys.stdin.read()
    else:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            data = f.read()
    return (data or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="HTB Rank Planner (Labs API v4)")
    ap.add_argument("--token", default=None, help="HTB JWT token (otherwise uses env HTB_TOKEN). Prefer --token-file.")
    ap.add_argument("--token-file", default=None, help="Read token from file path (or '-' for stdin). Safer than --token.")
    ap.add_argument("--debug", action="store_true", help="Verbose request diagnostics")
    ap.add_argument("--top", type=int, default=12, help="Max actions to print per plan")
    ap.add_argument("--workers", type=int, default=24, help="Worker threads (default 24)")

    ap.add_argument("--no-cache", action="store_true", help="Disable ALL disk cache")
    ap.add_argument("--list-cache-ttl", type=int, default=6 * 3600, help="TTL for list cache (default 6h)")
    ap.add_argument("--show-progress", action="store_true", help="Show progress bars (stderr)")

    # Rolling-window limits (per 60s). We enforce GLOBAL + per-endpoint.
    ap.add_argument("--rl-global", type=int, default=65, help="Global requests per 60s (default 65)")
    ap.add_argument("--rl-challenge-info", type=int, default=60, help="challenge/info per 60s (default 60)")
    ap.add_argument("--rl-machine-profile", type=int, default=30, help="machine/profile per 60s (default 30)")
    ap.add_argument("--rl-lists", type=int, default=20, help="list endpoints per 60s (default 20)")
    ap.add_argument("--rl-margin", type=int, default=2, help="Safety margin (default 2)")
    ap.add_argument("--rl-window", type=float, default=60.0, help="Rolling window seconds (default 60.0)")

    ap.add_argument("--fb-challenge-cap", type=int, default=0, help="Cap challenge/info calls (0 = all unsolved active)")
    args = ap.parse_args()

    token = _read_token_from_file(args.token_file) if args.token_file else (args.token or os.environ.get("HTB_TOKEN") or "").strip()
    if not token:
        print("ERROR: Missing token. Set HTB_TOKEN or pass --token-file / --token.", file=sys.stderr)
        return 2

    token_ns = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    cache_root = os.path.expanduser(f"~/.cache/htb_rank_planner/{token_ns}")

    cache_enabled = not args.no_cache
    cache_lists = DiskCache(os.path.join(cache_root, "lists"), ttl_seconds=int(args.list_cache_ttl), enabled=cache_enabled)
    cache_items = DiskCache(os.path.join(cache_root, "items"), ttl_seconds=None, enabled=cache_enabled)
    cache_index = CacheIndex(os.path.join(cache_root, "items", "index.json"), enabled=cache_enabled)
    idx = cache_index.load()

    limiter = RollingWindowLimiter(window_seconds=float(args.rl_window), margin=int(args.rl_margin))
    limiter.set_limit("global", int(args.rl_global))
    limiter.set_limit("challenge_info", int(args.rl_challenge_info))
    limiter.set_limit("machine_profile", int(args.rl_machine_profile))
    limiter.set_limit("lists_active_challenges", int(args.rl_lists))
    limiter.set_limit("lists_machines", int(args.rl_lists))
    limiter.set_limit("lists_other", int(args.rl_lists))

    client = HTBClient(
        token=token,
        debug=args.debug,
        workers=max(4, int(args.workers)),
        cache_lists=cache_lists,
        cache_items=cache_items,
        cache_index=cache_index,
        limiter=limiter,
    )

    ui = client.get_user_info()
    info = ui.get("info", {})
    user_id = info.get("id")
    user_name = info.get("name", "?")
    tz = info.get("timezone", "?")
    rank_id = info.get("rank_id", "?")

    print(f"HTB Rank Planner v{APP_VERSION} — user: {user_name} (id={user_id})  tz={tz}")
    print(f"Rank (API user/info): rank_id={rank_id}")
    print()

    if not isinstance(user_id, int):
        print("ERROR: Could not determine user id from /user/info.", file=sys.stderr)
        return 2

    prof = client.get_user_profile_basic(user_id).get("profile", {}) or {}
    api_rank_name = prof.get("rank")
    api_next_rank = prof.get("next_rank")
    api_rank_ownership = _safe_float(prof.get("rank_ownership"))
    api_current_rank_progress = _safe_float(prof.get("current_rank_progress"))
    api_user_owns = _safe_int(prof.get("user_owns"))
    api_system_owns = _safe_int(prof.get("system_owns"))

    machines = client.list_active_machines()
    active_machine_ids: Set[int] = {m.get("id") for m in machines if isinstance(m.get("id"), int)}

    active_items = client.list_active_challenges_items()
    active_ids: List[Any] = []
    active_by_id: Dict[str, Dict[str, Any]] = {}
    for it in active_items:
        cid = _challenge_id(it)
        if cid is None:
            continue
        active_ids.append(cid)
        active_by_id[str(cid)] = it

    active_total = len(active_ids)
    active_solved = sum(1 for it in active_items if _challenge_solved_flag(it))
    unsolved_active_ids = [cid for cid in active_ids if not _challenge_solved_flag(active_by_id.get(str(cid), {}))]
    unowned_active = len(unsolved_active_ids)

    user_owned = 0
    root_owned = 0
    user_only_machines: List[Dict[str, Any]] = []
    unowned_machines: List[Dict[str, Any]] = []

    for m in machines:
        has_user = _as_bool(m.get("authUserInUserOwns"))
        has_root = _as_bool(m.get("authUserInRootOwns"))
        if has_root:
            root_owned += 1
            has_user = True
        if has_user:
            user_owned += 1
        if has_user and not has_root:
            user_only_machines.append(m)
        if not has_user and not has_root:
            unowned_machines.append(m)

    snap = OwnershipSnapshot(
        active_machines_total=len(machines),
        active_challenges_total=active_total,
        active_user_owns=user_owned,
        active_root_owns=root_owned,
        active_challenge_owns=active_solved,
    )

    print("Active content counts")
    print(f"  Machines:   {snap.active_machines_total}")
    print(f"    - user owns: {user_owned} (user-only: {len(user_only_machines)})")
    print(f"    - root owns: {root_owned}")
    print(f"    - unowned:   {len(unowned_machines)}")
    print(f"  Challenges: {snap.active_challenges_total}  (active via challenge/list)")
    print(f"    - solved (ACTIVE):   {active_solved}")
    print(f"    - unsolved (ACTIVE): {unowned_active}")
    print()

    filled = f"({snap.active_root_owns} + {snap.active_user_owns}/2 + {snap.active_challenge_owns}/10) / ({snap.active_machines_total} + {snap.active_machines_total}/2 + {snap.active_challenges_total}/10) * 100"
    print("Ownership% (HTB official formula)")
    print(f"  Current Ownership% = {filled} = {snap.ownership_percent:.4f}%")
    if api_rank_ownership is not None:
        diff = abs(api_rank_ownership - snap.ownership_percent)
        print(f"  API rank_ownership = {api_rank_ownership:.4f}%   (diff={diff:.4f}%)")
    if api_user_owns is not None or api_system_owns is not None:
        print(f"  API profile owns   = user_owns={api_user_owns} system_owns={api_system_owns}")
    print()

    computed_rank, computed_thr, computed_next, computed_next_thr = rank_bounds(snap.ownership_percent)

    # HTB protects an earned rank when content retires. Prefer the API-reported rank
    # and next rank for planning; fall back to ownership-derived values only if needed.
    cur_rank = api_rank_name if _rank_threshold(api_rank_name) is not None else computed_rank
    nxt_rank = api_next_rank if _rank_threshold(api_next_rank) is not None else _next_rank_name(cur_rank)

    if nxt_rank is None:
        nxt_rank = "Omniscient"
    cur_thr = _rank_threshold(cur_rank)
    nxt_thr = _rank_threshold(nxt_rank)
    if cur_thr is None:
        cur_thr = computed_thr
    if nxt_thr is None:
        nxt_thr = computed_next_thr

    prog = _retained_rank_progress(snap.ownership_percent, cur_rank, nxt_rank)
    cur_thr_text = _rank_threshold_text(cur_rank, cur_thr)
    nxt_thr_text = _rank_threshold_text(nxt_rank, nxt_thr)
    print("Rank progress (like HTB profile)")
    print(f"  Current rank: {cur_rank} (threshold {cur_thr_text})")
    print(f"  Next rank:    {nxt_rank} (threshold {nxt_thr_text})")
    print(f"  Progress:     {_bar(prog)}")
    if api_rank_name:
        print(f"  API rank:     {api_rank_name}")
    if api_next_rank:
        print(f"  API next:     {api_next_rank}")
    base_own = api_rank_ownership if api_rank_ownership is not None else snap.ownership_percent
    raw_ownership_gap = max(0.0, nxt_thr - base_own)
    print(f"  Raw ownership gap to {nxt_thr_text}: {raw_ownership_gap:.4f}%")
    print()

    if cur_rank == "Omniscient":
        print("You are already Omniscient by the API-reported rank.")
        return 0

    denom = snap.denom_points if snap.denom_points > 0 else 1.0
    cur_pct = snap.ownership_percent

    def rel(abs_pp: float) -> str:
        if cur_pct <= 1e-9:
            return "n/a"
        return f"+{(abs_pp / cur_pct) * 100.0:.2f}%"

    user_abs = (0.5 / denom) * 100.0
    root_abs = (1.0 / denom) * 100.0
    full_abs = (1.5 / denom) * 100.0
    chal_abs = (0.1 / denom) * 100.0

    user_min = _estimate_minutes_from_difficulty(5.5, "machine")
    root_min = _estimate_minutes_from_difficulty(5.5, "machine")
    full_min = _estimate_minutes_from_difficulty(5.5, "machine")
    chal_min = _estimate_minutes_from_difficulty(3.0, "challenge")

    print("Per-action ownership gains")
    print(f"{'type':16s} | {'absolute gain':>14s} | {'relative gain':>14s} | gain/min (est)")
    print("-" * 72)
    print(f"{'user on machine':16s} | {('+' + f'{user_abs:.4f}%'):>14s} | {rel(user_abs):>14s} | {user_abs/max(1e-9,user_min):14.5f}")
    print(f"{'root on machine':16s} | {('+' + f'{root_abs:.4f}%'):>14s} | {rel(root_abs):>14s} | {root_abs/max(1e-9,root_min):14.5f}")
    print(f"{'user + root':16s} | {('+' + f'{full_abs:.4f}%'):>14s} | {rel(full_abs):>14s} | {full_abs/max(1e-9,full_min):14.5f}")
    print(f"{'challenge':16s} | {('+' + f'{chal_abs:.4f}%'):>14s} | {rel(chal_abs):>14s} | {chal_abs/max(1e-9,chal_min):14.5f}")
    print()

    needed_points = _needed_points(snap, nxt_thr, strict=(nxt_rank != "Omniscient"))
    needed_pct_points = (needed_points / snap.denom_points) * 100.0 if snap.denom_points > 0 else 0.0
    if needed_points <= 1e-9:
        print("You already meet (or exceed) the next rank threshold by ownership%.")
        return 0

    print(f"Minimum achievable gain needed to reach {nxt_rank} ({nxt_thr_text}): +{needed_pct_points:.4f}%")
    print()

    # ---- prune cache entries for inactive IDs ----
    if cache_enabled:
        mp_index = idx.get("machine_profile", {})
        ci_index = idx.get("challenge_info", {})

        stale_m = [k for k in mp_index.keys() if str(k).isdigit() and int(k) not in active_machine_ids]
        for k in stale_m:
            cache_items.delete(mp_index[k])
            del mp_index[k]

        active_ids_str = set(str(x) for x in active_ids)
        stale_c = [k for k in ci_index.keys() if str(k) not in active_ids_str]
        for k in stale_c:
            cache_items.delete(ci_index[k])
            del ci_index[k]

        idx["machine_profile"] = mp_index
        idx["challenge_info"] = ci_index
        cache_index.save(idx)

    # ---- fetch machine/profile (incremental) ----
    def _parse_machine_fb(profm: Any) -> Tuple[Optional[float], Optional[float]]:
        fu = fr = None
        if isinstance(profm, dict):
            info_m = profm.get("info") if isinstance(profm.get("info"), dict) else profm
            fu = _parse_any_time_to_minutes(info_m.get("firstUserBloodTime"))
            fr = _parse_any_time_to_minutes(info_m.get("firstRootBloodTime"))
            if fu is None:
                ub = info_m.get("userBlood")
                if isinstance(ub, dict):
                    fu = _parse_any_time_to_minutes(ub.get("blood_difference"))
            if fr is None:
                rb = info_m.get("rootBlood")
                if isinstance(rb, dict):
                    fr = _parse_any_time_to_minutes(rb.get("blood_difference"))
        return fu, fr

    machine_fb: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    machine_ids: List[int] = []
    for m in (unowned_machines + user_only_machines):
        mid = m.get("id")
        if isinstance(mid, int):
            machine_ids.append(mid)

    mp_index = idx.get("machine_profile", {}) if cache_enabled else {}
    to_fetch_machines: List[int] = []
    for mid in machine_ids:
        k = str(mid)
        if cache_enabled and k in mp_index:
            cached = cache_items.get(mp_index[k])
            if cached is not None:
                machine_fb[mid] = _parse_machine_fb(cached)
                continue
        to_fetch_machines.append(mid)

    machine_name_by_id = {m.get("id"): m.get("name") for m in machines if isinstance(m.get("id"), int)}

    def fetch_machine(mid: int) -> Tuple[int, Tuple[Optional[float], Optional[float]], str]:
        profm = client.get_machine_profile_cached(mid, machine_name_by_id.get(mid))
        return mid, _parse_machine_fb(profm), f"machine_profile:{mid}"

    if to_fetch_machines:
        with ThreadPoolExecutor(max_workers=max(4, client.workers)) as ex:
            futs = [ex.submit(fetch_machine, mid) for mid in to_fetch_machines]
            done = 0
            total = len(futs)
            for fut in as_completed(futs):
                mid, tup, ck = fut.result()
                machine_fb[mid] = tup
                if cache_enabled:
                    mp_index[str(mid)] = ck
                done += 1
                if args.show_progress:
                    _progress(done, total, "machine/profile")
            if args.show_progress:
                _progress(total, total, "machine/profile")

    if cache_enabled:
        idx["machine_profile"] = mp_index
        cache_index.save(idx)

    # ---- fetch challenge/info (incremental) ----
    challenge_fb: Dict[Any, Optional[float]] = {}

    remaining = list(unsolved_active_ids)
    if args.fb_challenge_cap and args.fb_challenge_cap > 0:
        remaining = remaining[: int(args.fb_challenge_cap)]

    ci_index = idx.get("challenge_info", {}) if cache_enabled else {}
    to_fetch_chals: List[Any] = []
    for cid in remaining:
        k = str(cid)
        if cache_enabled and k in ci_index:
            cached = cache_items.get(ci_index[k])
            if isinstance(cached, dict):
                mm = extract_challenge_first_blood_minutes(cached)
                if mm is not None:
                    challenge_fb[cid] = mm
                    continue
        to_fetch_chals.append(cid)

    def fetch_chal(cid: Any) -> Tuple[Any, Optional[float], str]:
        det = client.get_challenge_info_cached(cid)
        mm = extract_challenge_first_blood_minutes(det) if isinstance(det, dict) else None
        return cid, mm, f"challenge_info:{cid}"

    if to_fetch_chals:
        with ThreadPoolExecutor(max_workers=max(4, client.workers)) as ex:
            futs = [ex.submit(fetch_chal, cid) for cid in to_fetch_chals]
            done = 0
            total = len(futs)
            for fut in as_completed(futs):
                cid, mm, ck = fut.result()
                if mm is not None:
                    challenge_fb[cid] = mm
                if cache_enabled:
                    ci_index[str(cid)] = ck
                done += 1
                if args.show_progress:
                    _progress(done, total, "challenge/info")
            if args.show_progress:
                _progress(total, total, "challenge/info")

    if cache_enabled:
        idx["challenge_info"] = ci_index
        cache_index.save(idx)

    # ---- build groups / plans ----
    groups: List[List[Optional[Action]]] = []

    for m in unowned_machines:
        mid = m.get("id")
        if not isinstance(mid, int):
            continue
        name = m.get("name") or f"machine#{mid}"
        diff = _extract_user_rated_difficulty(m)
        fu, fr = machine_fb.get(mid, (None, None))

        user_est = _clamp_est_minutes(fu if fu is not None else _estimate_minutes_from_difficulty(diff, "machine"))
        root_est = _clamp_est_minutes(fr if fr is not None else _estimate_minutes_from_difficulty(diff, "machine"))

        a_user = Action("machine_user", f"machine:{mid}", mid, name, diff, 0.5, 1, user_est, fu, None)
        a_full = Action("machine_full", f"machine:{mid}", mid, name, diff, 1.5, 2, root_est, fu, fr)
        groups.append([None, a_user, a_full])

    for m in user_only_machines:
        mid = m.get("id")
        if not isinstance(mid, int):
            continue
        name = m.get("name") or f"machine#{mid}"
        diff = _extract_user_rated_difficulty(m)
        fu, fr = machine_fb.get(mid, (None, None))
        root_est = _clamp_est_minutes(fr if fr is not None else _estimate_minutes_from_difficulty(diff, "machine"))
        a_up = Action("machine_upgrade_root", f"upgrade:{mid}", mid, name, diff, 1.0, 1, root_est, fu, fr)
        groups.append([None, a_up])

    for cid in unsolved_active_ids:
        base = active_by_id.get(str(cid), {})
        name = base.get("name") or base.get("title") or f"challenge#{cid}"
        diff = _extract_user_rated_difficulty(base) if isinstance(base, dict) else 5.5

        fbm = challenge_fb.get(cid)
        est = _clamp_est_minutes(fbm if fbm is not None else _estimate_minutes_from_difficulty(diff, "challenge"))
        a_ch = Action("challenge", f"chall:{cid}", cid, name, diff, 0.1, 1, est, fbm, None)
        groups.append([None, a_ch])

    def cost_time(a: Action) -> float:
        return a.est_minutes

    def tie_time(chosen: List[Action]) -> Tuple:
        return (sum(x.flags for x in chosen), sum(x.difficulty for x in chosen), len(chosen))

    fastest = _dp_choose_min_cost(groups, needed_points, cost_time, tie_time)
    easiest = _choose_easiest_greedy(groups, needed_points)

    def cost_hybrid(a: Action) -> float:
        return 0.65 * a.est_minutes + 0.35 * (a.difficulty * 5.0)

    def tie_hybrid(chosen: List[Action]) -> Tuple:
        return (sum(x.flags for x in chosen), sum(x.est_minutes for x in chosen), len(chosen))

    hybrid = _dp_choose_min_cost(groups, needed_points, cost_hybrid, tie_hybrid)

    print_plan("Fastest path (maximize % gain per minute; DP minimizes total estimated time)", snap, fastest.chosen, args.top)
    print_plan("Easiest path (greedy: lowest user-rated difficulty, then first-blood time)", snap, easiest.chosen, args.top)
    print_plan("Hybrid path (DP minimizes weighted time + difficulty)", snap, hybrid.chosen, args.top)

    machines_total_fb = len(set(machine_ids))
    machines_with_fb = sum(1 for _mid, (u, r) in machine_fb.items() if (u is not None or r is not None))

    challenges_total = len(unsolved_active_ids)
    challenges_with_fb = len(challenge_fb)

    print("First-blood data coverage")
    print(f"  Machines with FB parsed: {machines_with_fb}/{machines_total_fb} (missing {machines_total_fb - machines_with_fb})")
    print(f"  Challenges with FB parsed: {challenges_with_fb}/{challenges_total} (missing {challenges_total - challenges_with_fb})")
    print("  Note: remaining items fall back to a difficulty→minutes estimate.")
    print()

    print("Notes")
    print("  - Cold start time is limited by HTB API rate limits for item-detail endpoints.")
    print("  - With caching enabled, later runs fetch detail data only for newly active IDs.")
    print("  - First-blood time is a heuristic, not a personal completion-time prediction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())