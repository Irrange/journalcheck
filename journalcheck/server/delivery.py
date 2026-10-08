from __future__ import annotations
import re
import requests
from journalcheck.notifications import _proxy_bypass_matches

ENDPOINT = 'https://www.pushplus.plus/send'


class DeliveryError(Exception):
    """Only fixed, safe messages may cross into notification records."""
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def send(channel, payload, cfg):
    if channel != 'pushplus':
        raise DeliveryError('旧通知渠道已停用。')
    if not cfg['pushplus_enabled']:
        raise DeliveryError('PushPlus 邮件通知已暂停；启用后可手动补发。')
    if not cfg['pushplus_token']:
        raise DeliveryError('请先配置 PushPlus Token。')
    body = {'token':cfg['pushplus_token'], 'title':payload['subject'],
            'content':payload['body'], 'channel':'mail', 'template':'txt'}
    if cfg['pushplus_option']:
        body['option'] = cfg['pushplus_option']
    # Secrets live only in a JSON body; never follow a redirect with that body.
    with requests.Session() as session:
        session.trust_env = False
        if not _proxy_bypass_matches('www.pushplus.plus',cfg['no_proxy']):
            session.proxies = {key:value for key,value in
                               (('http',cfg['http_proxy']),('https',cfg['https_proxy'])) if value}
        response = session.post(ENDPOINT,json=body,timeout=(10,30),allow_redirects=False)
        if response.status_code != 200:
            raise DeliveryError('PushPlus 请求失败，请检查网络和服务状态。',
                                retryable=response.status_code>=500 or response.status_code==429)
        try:
            result = response.json()
        except ValueError:
            raise DeliveryError('PushPlus 返回格式无法识别，请稍后重试。',retryable=True) from None
    if not isinstance(result,dict):
        raise DeliveryError('PushPlus 返回格式无法识别，请稍后重试。',retryable=True)
    if result.get('code') != 200:
        # Do not persist provider messages: they may echo the token or content.
        raise DeliveryError('PushPlus 未受理通知，请检查 Token、邮箱绑定、邮件编码及发送额度。')
    receipt = result.get('data')
    if not isinstance(receipt,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',receipt):
        # The provider may already have accepted it. Avoid an automatic resend.
        raise DeliveryError('PushPlus 未返回有效流水号；请先在 PushPlus 消息列表确认，再手动补发。')
    # /send is asynchronous. Accepted does not assert email delivery.
    return {'status':'accepted','receipt_id':receipt}
