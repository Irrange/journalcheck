"""Normalize the public journal entry URL without a second user-supplied code."""
import re
from urllib.parse import urlsplit,urlunsplit,unquote


def bmc_submission_url(raw):
    if not isinstance(raw, str):
        raise ValueError('稿件链接格式无效。')
    try:
        url = urlsplit(raw.strip())
        valid = (url.scheme == 'https' and url.hostname == 'submission.springernature.com'
                 and not url.username and not url.password and url.port in (None, 443)
                 and not url.query and not url.fragment
                 and re.fullmatch(r'/submission-details/[0-9a-fA-F-]{36}/?', url.path))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('请填写官方 Springer Nature submission-details 稿件链接。')
    return 'https://submission.springernature.com' + url.path.rstrip('/').lower()


def editorial_manager_url(raw):
    if not isinstance(raw,str):
        raise ValueError('请填写 EM 期刊链接。')
    try:
        url=urlsplit(raw.strip())
        valid=url.scheme=='https' and url.hostname in ('editorialmanager.com','www.editorialmanager.com') and not url.username and not url.password and url.port in (None,443)
    except ValueError:
        valid=False
    if not valid:
        raise ValueError('请填写 Editorial Manager 官方 HTTPS 期刊链接。')
    parts=url.path.split('/')
    code=unquote(parts[1]) if len(parts)>1 else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',code):
        raise ValueError('链接中缺少期刊目录，例如 https://www.editorialmanager.com/ghrpj/default2.aspx。')
    if any(unquote(part) in ('.','..') for part in parts):
        raise ValueError('EM 期刊链接路径无效。')
    tail='/'.join(parts[2:]) or 'default2.aspx'
    return urlunsplit(('https','www.editorialmanager.com',f'/{code}/{tail}','','')),code


def scholarone_url(raw):
    if not isinstance(raw, str):
        raise ValueError('请填写 ScholarOne 期刊链接。')
    try:
        url = urlsplit(raw.strip())
        valid = (url.scheme == 'https' and url.hostname == 'mc.manuscriptcentral.com'
                 and not url.username and not url.password and url.port in (None, 443))
        path = url.path.rstrip('/')
        code = path[1:] if path.startswith('/') else ''
        valid = valid and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('请填写官方 ScholarOne 期刊链接，例如 https://mc.manuscriptcentral.com/ageing。')
    # Session-bearing query strings must never become the persistent journal entry.
    return f'https://mc.manuscriptcentral.com/{code}', code
