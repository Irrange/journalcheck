import pytest
from journalcheck.server import delivery
from tests.test_store import notification_config


class Session:
    def __init__(self, result=None, status=200):
        self.result = result
        self.status = status
        self.calls = []
        self.proxies = {}
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url,kwargs))
        return self

    @property
    def status_code(self):
        return self.status

    def json(self):
        return self.result


def test_pushplus_uses_only_mail_with_plain_text_and_keeps_token_out_of_url(monkeypatch):
    session=Session({'code':200,'data':'receipt-123'})
    monkeypatch.setattr(delivery.requests,'Session',lambda:session)
    cfg=notification_config()
    cfg['https_proxy']='http://proxy.test:8080'
    outcome=delivery.send('pushplus',{'subject':'稿件更新','body':'标题 <script> ·\n审稿信息'},cfg)
    assert outcome=={'status':'accepted','receipt_id':'receipt-123'}
    url,kwargs=session.calls[0]
    assert url=='https://www.pushplus.plus/send' and cfg['pushplus_token'] not in url
    assert kwargs['json']=={'token':cfg['pushplus_token'],'title':'稿件更新','content':'标题 <script> ·\n审稿信息','channel':'mail','template':'txt'}
    assert kwargs['allow_redirects'] is False and not session.trust_env
    assert session.proxies=={'https':'http://proxy.test:8080'}


def test_custom_mail_code_and_proxy_bypass(monkeypatch):
    session=Session({'code':200,'data':'receipt-custom'})
    monkeypatch.setattr(delivery.requests,'Session',lambda:session)
    cfg={**notification_config(),'pushplus_option':'my-mail','https_proxy':'http://proxy.test','no_proxy':'www.pushplus.plus'}
    delivery.send('pushplus',{'subject':'s','body':'b'},cfg)
    assert session.calls[0][1]['json']['option']=='my-mail'
    assert not session.proxies


@pytest.mark.parametrize('result,status,retryable',[
    ({'code':401,'msg':'secret-token-echo'},200,False),
    ({'code':200,'data':None},200,False),
    ({'code':200,'data':'<invalid>'},200,False),
    (['secret-token-echo'],200,True),
    ({},503,True),
    ({},302,False),
])
def test_rejections_and_invalid_receipts_do_not_claim_sent(monkeypatch,result,status,retryable):
    session=Session(result,status)
    monkeypatch.setattr(delivery.requests,'Session',lambda:session)
    with pytest.raises(delivery.DeliveryError) as error:
        delivery.send('pushplus',{'subject':'s','body':'b'},notification_config())
    assert error.value.retryable==retryable
    assert 'secret-token-echo' not in str(error.value)


def test_disabled_and_legacy_channels_never_make_requests(monkeypatch):
    def unexpected():
        raise AssertionError('must not connect')
    monkeypatch.setattr(delivery.requests,'Session',unexpected)
    for channel,cfg in [('email',notification_config()),('wecom',notification_config()),
                        ('pushplus',{**notification_config(),'pushplus_enabled':False})]:
        with pytest.raises(delivery.DeliveryError):
            delivery.send(channel,{'subject':'s','body':'b'},cfg)
