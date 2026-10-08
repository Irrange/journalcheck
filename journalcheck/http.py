from __future__ import annotations

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _explicit_proxy_configured() -> bool:
    return any(os.getenv(key, "").strip() for key in ("HTTP_PROXY", "HTTPS_PROXY"))


def make_session(with_retry: bool = True) -> requests.Session:
    session = requests.Session()
    # Keep the previous default behavior unless proxy values were explicitly configured.
    session.trust_env = _explicit_proxy_configured()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.7204.97 Safari/537.36"
            )
        }
    )

    if not with_retry:
        return session

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        # Login requests are POSTs and must never be replayed implicitly.
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
