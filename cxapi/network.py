"""Bounded network access. Never automatically replay a submission."""
import requests
from urllib.parse import urlsplit
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import APIError


class AuthenticationError(APIError):
    pass


class ResourceAccessError(APIError):
    """A resource denial is not evidence that the whole account has expired."""


ACCOUNT_STATUS_URL = 'https://sso.chaoxing.com/apis/login/userLogin4Uname.do'
PLATFORM_HOSTS = {'mooc1-api.chaoxing.com', 'mooc1.chaoxing.com',
                  'sso.chaoxing.com', 'passport2.chaoxing.com'}


def login_page(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (parsed.hostname == 'passport2.chaoxing.com'
            and parsed.path.rstrip('/') in ('/login', '/mlogin', '/fanyalogin'))


class TimeoutSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Retry only connection failures before sending the request. Some GETs mutate state.
        retry = Retry(total=2, connect=2, read=0, status=0, redirect=0, backoff_factor=0.4)
        for protocol in ('http://', 'https://'):
            self.mount(protocol, HTTPAdapter(max_retries=retry))

    def _request_once(self, method, url, **kwargs):
        kwargs.setdefault('timeout', (8, 20))
        from runtime import current_context
        context = current_context()
        if context is None:
            response = super().request(method, url, **kwargs)
        else:
            # A cancelled call must not update a newly selected account's cookies
            # or share an in-flight connection with its next operation.
            isolated = TimeoutSession()
            for name in ('headers', 'cookies', 'proxies', 'params', 'hooks'):
                setattr(isolated, name, getattr(self, name).copy())
            for name in ('auth', 'verify', 'cert', 'trust_env', 'max_redirects'):
                setattr(isolated, name, getattr(self, name))
            def send():
                try:
                    return super(TimeoutSession, isolated).request(method, url, **kwargs)
                finally:
                    isolated.close()
            response = context.call(send)
            # Include cookie deletions too; abandoned requests never reach here.
            self.cookies = isolated.cookies.copy()
        return response

    def account_status(self):
        """Read-only confirmation: True valid, False expired, None inconclusive."""
        response = None
        try:
            response = self._request_once('GET', ACCOUNT_STATUS_URL)
            if response.status_code != 200:
                return None
            data = response.json()
            if not isinstance(data, dict):
                return None
            result = data.get('result')
            if result in (True, 1, '1', 'true') and isinstance(data.get('msg'), dict) and data['msg'].get('puid'):
                return True
            if result in (False, 0, '0', 'false'):
                return False
        except (requests.RequestException, ValueError):
            return None
        finally:
            if response is not None:
                response.close()
        return None

    def request(self, method, url, **kwargs):
        response = self._request_once(method, url, **kwargs)
        host = urlsplit(url).hostname
        redirected = host in PLATFORM_HOSTS and host != 'passport2.chaoxing.com' and login_page(response.url)
        if host not in PLATFORM_HOSTS or not (response.status_code in (401, 403) or redirected):
            return response
        code = response.status_code
        response.close()
        # Do not replay a study report or submission to diagnose a denial.
        valid = self.account_status() if urlsplit(url).path != urlsplit(ACCOUNT_STATUS_URL).path else None
        if valid is False:
            raise AuthenticationError('账号验证接口确认登录已失效，请重新登录')
        path = urlsplit(url).path
        resource = ('视频进度上报' if '/multimedia/log/' in path else
                    '视频资源' if '/ananas/status/' in path else
                    '测验' if '/work/' in path else '课程资源')
        reason = '要求单独登录或授权' if redirected else f'访问被拒绝（HTTP {code}）'
        verification = '账号登录仍有效' if valid is True else '暂无法确认账号状态'
        raise ResourceAccessError(f'{resource}{reason}；{verification}，请检查该资源权限或稍后重试')


def error_text(error):
    """Do not expose cookies, tokens or query strings from requests exceptions."""
    if isinstance(error, requests.Timeout):
        return '网络请求超时，请检查网络后重试'
    if isinstance(error, requests.ConnectionError):
        return '连接服务器失败，请检查网络或代理设置'
    if isinstance(error, requests.HTTPError):
        code = error.response.status_code if error.response is not None else '未知'
        return f'服务器返回 HTTP {code}'
    if isinstance(error, requests.exceptions.JSONDecodeError):
        return '服务器没有返回有效数据，可能需要重新登录或更新接口适配'
    if isinstance(error, (KeyError, IndexError)):
        return '服务器数据不完整或页面结构已变化，当前任务无法解析'
    return str(error) or type(error).__name__
