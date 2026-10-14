import json
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from sbs_capture.capture import Capture
from sbs_capture.models import Query
from sbs_capture.pagination import next_page_url
from sbs_capture.parser import ParseError


PROFILES = SimpleNamespace(devices={
    'Desktop Chrome': {'user_agent': 'desktop-test'},
    'Pixel 7': {'user_agent': 'android-test', 'viewport': {'width': 393, 'height': 851}},
})


def page_html(ids, page=0, next_link=None):
    records = {str(i): {'id': str(i), 'origUrl': f'https://source.example/{i}.jpg',
                       'image': f'https://thumb.example/{i}.jpg', 'pos': i,
                       'snippet': {'url': f'https://source.example/page/{i}', 'title': f'Photo {i}'},
                       'viewerData': {'keep': {'original': i}}} for i in ids}
    state = {'initialState': {'serpList': {
        'items': {'keys': list(records), 'entities': records}, 'lastPage': page,
        'navi': {'next': next_link}, 'autoload': {'enabled': True}, 'prefetchNext': True,
    }}}
    return f'<html><head><script data-as="webpack-runtime" src="https://cdn.example/runtime.js"></script></head><body><div id="ImagesApp" data-state="{escape(json.dumps(state), quote=True)}"></div></body></html>'


def response(html, url):
    return Mock(text=html, url=url)


def fetch_sequence(pages):
    return lambda url, **kwargs: response(pages.pop(0), url)


def test_desktop_collects_pages_and_preserves_native_sources_and_parameters():
    pages = [page_html([0, 1], 0, '?text=mountains&lr=213&p=1'),
             page_html([2, 3], 1, '?text=mountains&lr=213&p=2'),
             page_html([4, 5], 2, '?text=mountains&lr=213&p=3')]
    config = {'desktop_min_items': 5, 'test_ids': ['12', '34'], 'cgi': {'flag': ['a', 'b']}}
    with patch('sbs_capture.capture.requests.get', side_effect=fetch_sequence(pages)) as get:
        soup, items, info = Capture(PROFILES, config).run(Query('mountains', 213, 'DESKTOP'))
    assert [item.id for item in items] == [str(i) for i in range(6)]
    assert [item.page_url for item in items] == [f'https://source.example/page/{i}' for i in range(6)]
    assert info['initial_items'] == 2 and info['pages_collected'] == 3
    assert info['target_reached'] and info['viewport'] == {'width': 1920, 'height': 1080}
    state = json.loads(soup.select_one('[data-state]')['data-state'])['initialState']['serpList']
    assert state['items']['entities']['5']['viewerData'] == {'keep': {'original': 5}}
    assert state['lastPage'] == 2 and state['loadNextState'] == 'finished'
    assert not state['autoload']['enabled'] and not state['navi']['next']
    for index, call in enumerate(get.call_args_list):
        params = parse_qs(urlsplit(call.args[0]).query)
        assert params['text'] == ['mountains'] and params['lr'] == ['213']
        assert params['test-id'] == ['12,34'] and params['flag'] == ['a', 'b']
        assert params.get('p', ['0']) == [str(index)]


def test_mobile_still_downloads_one_page():
    with patch('sbs_capture.capture.requests.get', side_effect=fetch_sequence([
        page_html([0, 1], next_link='?text=mountains&lr=213&p=1')
    ])) as get:
        _, items, info = Capture(PROFILES, {'desktop_min_items': 90}).run(Query('mountains', 213, 'ANDROID'))
    assert len(items) == 2 and get.call_count == 1
    assert info['viewport']['width'] == 393 and 'pages_collected' not in info


def test_repeated_page_is_not_counted_as_extra_images():
    pages = [page_html([0, 1], 0, '?text=mountains&lr=213&p=1'),
             page_html([0, 1], 1, '?text=mountains&lr=213&p=2')]
    with patch('sbs_capture.capture.requests.get', side_effect=fetch_sequence(pages)):
        _, items, info = Capture(PROFILES, {'desktop_min_items': 5}).run(Query('mountains', 213, 'DESKTOP'))
    assert len(items) == 2 and not info['target_reached']
    assert info['stop_reason'] == 'no_new_items'


def test_finite_results_report_actual_count():
    with patch('sbs_capture.capture.requests.get', side_effect=fetch_sequence([page_html([0, 1])])):
        _, items, info = Capture(PROFILES, {'desktop_min_items': 90}).run(Query('mountains', 213, 'DESKTOP'))
    assert len(items) == 2 and not info['target_reached']
    assert info['stop_reason'] == 'end_of_results'


def test_page_limit_fails_instead_of_silent_truncation():
    with patch('sbs_capture.capture.requests.get', side_effect=fetch_sequence([
        page_html([0, 1], next_link='?text=mountains&lr=213&p=1')
    ])):
        with pytest.raises(ParseError, match='not reached within 1 pages'):
            Capture(PROFILES, {'desktop_min_items': 5, 'desktop_max_pages': 1}).run(Query('mountains', 213, 'DESKTOP'))


@pytest.mark.parametrize('link', [
    'https://other.example/images/search?text=mountains&lr=213&p=1',
    '?text=changed&lr=213&p=1', '?text=mountains&lr=2&p=1',
    '?text=mountains&lr=213&p=0',
])
def test_pagination_cannot_change_source_query_region_or_go_backwards(link):
    initial = 'https://yandex.ru/images/search?text=mountains&lr=213'
    with pytest.raises(ParseError):
        next_page_url(link, initial, initial)


def test_redirect_cannot_silently_drop_requested_page():
    pages = [page_html([0, 1], next_link='?text=mountains&lr=213&p=1'), page_html([2, 3])]
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    def drop_page(url, **kwargs):
        parts = urlsplit(url)
        final = urlunsplit((parts.scheme, parts.netloc, parts.path,
                            urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != 'p']), ''))
        return response(pages.pop(0), final)
    with patch('sbs_capture.capture.requests.get', side_effect=drop_page):
        with pytest.raises(ValueError, match='parameter: p'):
            Capture(PROFILES, {'desktop_min_items': 5}).run(Query('mountains', 213, 'DESKTOP'))


def test_invalid_target_rejected_before_network_request():
    with patch('sbs_capture.capture.requests.get') as get:
        with pytest.raises(ValueError, match='positive integers'):
            Capture(PROFILES, {'desktop_min_items': True}).run(Query('mountains', 213, 'DESKTOP'))
    get.assert_not_called()
