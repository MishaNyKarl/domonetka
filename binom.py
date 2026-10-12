"""Read-only Binom v2 client; no tracker credentials are stored here."""
import json
import re
from datetime import date
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

LABELS = ('dm_source', 'dm_flow', 'dm_offer', 'dm_variant', 'dm_click')
TOKEN_FIELDS = (
    ('dm_source', 'Источник', 'Домонетка · Источник'),
    ('dm_flow', 'Поток', 'Домонетка · Поток'),
    ('dm_offer', 'Баннер', 'Домонетка · Баннер'),
    ('dm_variant', 'Вариация', 'Домонетка · Вариация'),
    ('dm_click', 'Переход', 'Домонетка · Переход'),
)


def token_mapping(value, complete=False):
    if not isinstance(value, dict) or set(value) - set(LABELS):
        raise ValueError('Некорректное соответствие меток токенам Binom')
    if any(type(slot) is not int or not 1 <= slot <= 30 for slot in value.values()):
        raise ValueError('Номер токена Binom должен быть от 1 до 30')
    if len(set(value.values())) != len(value):
        raise ValueError('Для каждой метки выберите отдельный токен Binom')
    if complete and set(value) != set(LABELS):
        raise ValueError('Выберите токен Binom для всех пяти меток')
    return {label: value[label] for label in LABELS if label in value}


def campaign_id(value):
    value = str(value or '').strip()
    if value and (not re.fullmatch(r'[1-9][0-9]{0,17}', value)):
        raise ValueError('ID кампании Binom должен быть положительным целым числом')
    return value


def validate_tracking_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise ValueError('Для кампании Binom укажите абсолютную ссылку http(s)')
    if any(k in LABELS for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError('Уберите dm_* метки из ссылки: витрина добавляет их автоматически')


def attributed_url(url, stream, offer, variant, click):
    validate_tracking_url(url)
    parts = urlsplit(url)
    extra = urlencode(dict(zip(LABELS, ('domonetka', stream, offer, variant, click))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       parts.query + ('&' if parts.query else '') + extra, parts.fragment))


def date_range(start='', end=''):
    for value in (start, end):
        if value:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Дата должна быть в формате ГГГГ-ММ-ДД')
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError('Начальная дата должна быть не позже конечной')
    return start or None, end or None


class BinomError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args): return None


def safe_open(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


class Client:
    def __init__(self, origin, api_key, transport=safe_open):
        parts = urlsplit(origin)
        if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
            raise ValueError('Укажите HTTPS-адрес трекера без пути и учётных данных')
        if not api_key or '\n' in api_key or '\r' in api_key:
            raise ValueError('Укажите API-ключ Binom')
        self.origin = origin.rstrip('/')
        self.api_key = api_key
        self.transport = transport

    def get(self, path, params=()):
        request = Request(self.origin + path + ('?' + urlencode(params, doseq=True) if params else ''),
                          headers={'Api-Key': self.api_key, 'Accept': 'application/json'})
        try:
            with self.transport(request, timeout=30) as response:
                return json.loads(response.read(8 * 1024 * 1024 + 1))
        except HTTPError as exc:
            # Do not expose tracker response bodies or API keys to the browser/log.
            raise BinomError(f'Binom: HTTP {exc.code}') from None
        except (URLError, TimeoutError, ValueError):
            raise BinomError('Binom: не удалось получить JSON-ответ') from None

    def campaign_groupings(self, campaign):
        cid = campaign_id(campaign)
        if not cid: raise ValueError('Укажите кампанию')
        return self.get('/public/api/v1/report/campaign/groupings', {'campaignId': cid})

    def campaign_report_page(self, campaign, start, end, token_slots, offset=0, limit=100):
        """Raw page for pilot validation, not a presumed conversion-status adapter."""
        cid = campaign_id(campaign)
        start, end = date_range(start, end)
        if not cid or not start or not end: raise ValueError('Нужны кампания и обе даты')
        slots = list(token_slots)
        if not slots or len(set(slots)) != len(slots) or any(type(n) is not int or not 1 <= n <= 30 for n in slots):
            raise ValueError('Укажите разные номера токенов от 1 до 30')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Некорректная страница отчёта')
        return self.get('/public/api/v1/report/campaign', {
            'ids[]': [cid], 'groupings[]': ['days', *(f'token_{n}' for n in slots), 'conversionStatusOne', 'conversionStatusTwo'],
            'datePreset': 'custom_time', 'dateFrom': start + ' 00:00:00', 'dateTo': end + ' 23:59:59',
            'timezone': 'UTC', 'sortColumn': 'clicks', 'sortType': 'desc', 'limit': limit, 'offset': offset})

    def mapped_campaign_report_page(self, campaign, start, end, mapping, offset=0, limit=100):
        mapping = token_mapping(mapping, complete=True)
        return self.campaign_report_page(campaign, start, end, mapping.values(), offset, limit)
