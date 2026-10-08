"""Strict, network-bounded adapters for polling journal submission accounts."""
from __future__ import annotations

import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class AuthenticationError(RuntimeError):
    """The site did not confirm a successful login."""


class ChallengeError(AuthenticationError):
    """A site security challenge requires manual attention, not new credentials."""


class ParseError(RuntimeError):
    """The response did not match a recognized submission-list structure."""


class _AdapterSession(requests.Session):
    def __init__(self, network: dict[str, Any]):
        super().__init__()
        self.trust_env = False
        self._no_proxy = str(network.get("no_proxy", "") or "")
        self._deadline = time.monotonic() + 600
        self._connect_timeout = float(network.get("connect_timeout", 40) or 40)
        self._read_timeout = float(network.get("read_timeout", 40) or 40)
        proxy_map = {}
        for scheme in ("http", "https"):
            value = network.get(f"{scheme}_proxy")
            if value:
                proxy_map[scheme] = str(value)
        self.proxies.update(proxy_map)
        self.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.7204.97 Safari/537.36"
                )
            }
        )
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.4,
            status_forcelist=(408, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("Journal account fetch exceeded its 600 second limit.")
        kwargs["timeout"] = (min(self._connect_timeout, remaining), min(self._read_timeout, remaining))
        if self._should_bypass_proxy(url):
            kwargs["proxies"] = {"http": "", "https": ""}
        return super().request(method, url, **kwargs)

    def _should_bypass_proxy(self, url: str) -> bool:
        return bool(self._no_proxy) and requests.utils.should_bypass_proxies(url, no_proxy=self._no_proxy)


def fetch_account(account: dict, network: dict) -> list[dict]:
    """Log in to one configured journal account and return normalized submissions."""
    # Import lazily: site checkers import the exception types above.
    from journalcheck.sites.aha import AHAChecker
    from journalcheck.sites.bmc import BMCChecker
    from journalcheck.sites.em import EditorialManagerChecker

    platform = str(account.get("platform", "")).strip().lower()
    name = str(account.get("name") or platform)
    username = str(account.get("username", ""))
    password = str(account.get("password", ""))
    if platform == 'scholarone':
        from .scholarone import fetch_scholarone
        return fetch_scholarone(account, network or {})
    session = _AdapterSession(network or {})
    try:
        if platform == "aha":
            checker = AHAChecker(
                str(account.get("base_url", "")), username, password, site_name=name,
                session=session, include_inactive=True, strict=True,
            )
        elif platform == "em":
            base_url=str(account.get('base_url',''))
            journal_code=str(account.get('journal_code') or '')
            if not journal_code:
                from .account_urls import editorial_manager_url
                base_url,journal_code=editorial_manager_url(base_url)
            checker = EditorialManagerChecker(
                base_url, journal_code,
                username, password, site_name=name, session=session,
                include_inactive=True, strict=True,
            )
        elif platform == "bmc":
            if account.get('login_method','password')=='orcid':
                from .orcid import fetch_orcid
                return fetch_orcid(account,network)
            checker = BMCChecker(
                str(account.get("submission_url") or account.get("base_url", "")),
                username, password, site_name=name, session=session,
                include_inactive=True, strict=True,
            )
        else:
            raise ValueError(f"Unsupported journal platform: {platform!r}")
        return [row.to_dict() for row in checker.check()]
    finally:
        session.close()


def fetch_account_result(account: dict, network: dict) -> dict:
    """One login context per BMC account, independent results per tracked link."""
    if account.get('platform')!='bmc':
        return {'ok':True,'rows':fetch_account(account,network)}
    if account.get('login_method','password')=='orcid':
        from .orcid import fetch_orcid_result
        return fetch_orcid_result(account,network)
    from journalcheck.sites.bmc import BMCChecker
    urls = account.get('submission_urls') or [account.get('submission_url','')]
    rows,results=[],[]
    with _AdapterSession(network or {}) as session:
        for url in urls:
            try:
                found = BMCChecker(url,account['username'],account['password'],site_name=account['name'],session=session,
                                   include_inactive=True,strict=True).check()
                for row in found:
                    data=row.to_dict();data.setdefault('metadata',{})['tracking_url']=url;rows.append(data)
                results.append({'url':url,'ok':True,'count':len(found)})
            except AuthenticationError:
                results.append({'url':url,'ok':False,'category':'authentication','message':'登录或稿件访问权限无法确认。'})
            except ParseError:
                results.append({'url':url,'ok':False,'category':'parse','message':'稿件页面结构无法识别，保留旧状态。'})
            except requests.RequestException:
                results.append({'url':url,'ok':False,'category':'network','message':'稿件连接失败，保留旧状态。'})
    return {'ok':True,'rows':rows,'targets':results,'partial':not all(r['ok'] for r in results)}
