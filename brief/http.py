"""Small HTTP layer: one session, retries with backoff, clear errors."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


class FetchError(Exception):
    """A source could not provide usable data."""


_SESSION: requests.Session | None = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*"})
        _SESSION = s
    return _SESSION


def _short(url: str) -> str:
    return url.split("?")[0][:90]


def _request(method: str, url: str, timeout: float, **kwargs) -> requests.Response:
    try:
        r = session().request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__} for {_short(url)}: {exc}") from exc
    if r.status_code >= 400:
        body = (r.text or "")[:160].replace("\n", " ")
        raise FetchError(f"HTTP {r.status_code} from {_short(url)}: {body}")
    return r


def get_json(url: str, params: dict | None = None, timeout: float = 20.0):
    r = _request("GET", url, timeout, params=params)
    try:
        return r.json()
    except ValueError as exc:
        raise FetchError(f"not JSON from {_short(url)}: {(r.text or '')[:120]}") from exc


def post_json(url: str, payload: dict, timeout: float = 20.0):
    r = _request("POST", url, timeout, json=payload)
    try:
        return r.json()
    except ValueError as exc:
        raise FetchError(f"not JSON from {_short(url)}: {(r.text or '')[:120]}") from exc


def get_text(url: str, params: dict | None = None, timeout: float = 20.0) -> str:
    return _request("GET", url, timeout, params=params).text
