"""Dış servis istekleri ve hata sınıflandırması. Hata metinleri sır içermez; URL'ler sorgu dizesi olmadan kaydedilir."""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "metan-turkiye/1.1 (+https://metan.sarpify.com)"


class ProviderError(Exception):
    """kind: auth | terms | quota | not_found | not_published | network | server | bad_response | not_configured"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    def public(self):
        return {"errorClass": self.kind, "message": str(self)[:200]}


def classify_http(code, body=""):
    text = (body or "").lower()
    if code in (401,):
        return "auth"
    if code == 403:
        return "terms" if "licen" in text or "terms" in text else "auth"
    if code == 429:
        return "quota"
    if code == 404:
        return "not_found"
    if 500 <= code < 600:
        return "server"
    return "bad_response"


def _strip_query(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def request(url, *, headers=None, data=None, method=None, timeout=30, raw=False):
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception as read_err:  # gövde okunamazsa yalnızca durum kodu raporlanır
            detail = f"(yanıt gövdesi okunamadı: {type(read_err).__name__})"
        raise ProviderError(classify_http(e.code, detail), f"HTTP {e.code} {_strip_query(url)}: {detail}", e.code) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ProviderError("network", f"{_strip_query(url)}: {getattr(e, 'reason', e)}") from e
    if raw:
        return payload
    try:
        return json.loads(payload)
    except ValueError as e:
        raise ProviderError("bad_response", f"{_strip_query(url)}: JSON çözülemedi") from e


def get_json(url, params=None, **kw):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    return request(url, **kw)
