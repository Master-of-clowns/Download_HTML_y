import json
from html import escape
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from sbs_capture.models import Query
from sbs_capture.urls import search_url
from sbs_capture.parser import parse, ParseError
from sbs_capture.render import render


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4/8AAAAASUVORK5CYII="


def fixture_html(*, missing_page=False, censored=False, title='A <test> & title'):
    record = {"id": "one", "origUrl": "https://example.org/original.jpg",
              "image": "https://example.org/thumbnail.jpg", "censored": censored,
              "snippet": {"title": title, "url": "" if missing_page else "https://example.org/page?a=1&b=2"}}
    state = {"initialState": {"serpList": {"items": {"keys": ["one"], "entities": {"one": record}}}}}
    return f'''<html><head><script data-as="webpack-runtime" src="https://yastatic.net/runtime.js"></script></head><body>
    <div id="ImagesApp" data-state="{escape(json.dumps(state), quote=True)}"></div>
    <div class="SerpItem" id="one"><a href="https://yandex.ru/images/search?img_url=other"><img src="//example.org/thumbnail.jpg" onerror="alert(1)"></a></div>
    <iframe src="https://tracker.example.org/"></iframe><noframes>private-runtime</noframes></body></html>'''


def test_page_url_is_source_not_viewer_or_original():
    _, items = parse(fixture_html(), "images", "https://yandex.ru")
    assert items[0].page_url == "https://example.org/page?a=1&b=2"
    assert items[0].page_url != items[0].image_url


def test_missing_source_fails_instead_of_guessing():
    with pytest.raises(ParseError, match="Missing source"):
        parse(fixture_html(missing_page=True), "images", "https://yandex.ru")


def test_legacy_flat_grid_cannot_silently_invent_page_url():
    with pytest.raises(ParseError):
        parse('<html><a href="https://yandex.ru/images/search?img_url=x"><img src="x"></a></html>', "images", "https://yandex.ru")


def test_snapshot_preserves_native_viewer_and_source_metadata():
    soup, items = parse(fixture_html(), "images", "https://yandex.ru")
    assets = Mock(); assets.image.return_value = PNG
    html, audit = render(soup, items, Query("test", 213, "ANDROID"), "images", "https://yandex.ru", assets)
    out = BeautifulSoup(html, "html.parser")
    state = json.loads(out.select_one('[data-state]')['data-state'])
    record = state['initialState']['serpList']['items']['entities']['one']
    assert record['snippet']['url'] == items[0].page_url
    assert record['snippet']['title'] == 'A <test> & title'
    assert record['image'] == items[0].thumbnail_url
    assets.image.assert_not_called()
    assets.css.assert_not_called()
    assert out.select_one('script[data-as="webpack-runtime"]')['src'] == 'https://yastatic.net/runtime.js'
    assert not out.select('#sbs-panel, #sbs-banner, [data-sbs-image]')
    assert audit['viewer'] == 'yandex-native'
    assert audit['source_links'] == 1


def test_missing_native_runtime_is_rejected():
    soup, items = parse(fixture_html(), "images", "https://yandex.ru")
    soup.select_one('script').decompose()
    with pytest.raises(ParseError, match='runtime is missing'):
        render(soup, items, Query('x', 213, 'ANDROID'), 'images', 'https://yandex.ru', Mock())


@pytest.mark.parametrize('title', ['Пицца 🍕', 'Incomplete emoji \ud83c'])
def test_source_unicode_roundtrips_through_utf8_html(title):
    soup, items = parse(fixture_html(title=title), 'images', 'https://yandex.ru')
    assets = Mock(); assets.image.return_value = PNG
    html, _ = render(soup, items, Query('test', 213, 'ANDROID'), 'images', 'https://yandex.ru', assets)
    out = BeautifulSoup(html.encode('utf-8').decode('utf-8'), 'html.parser')
    state = json.loads(out.select_one('[data-state]')['data-state'])
    assert state['initialState']['serpList']['items']['entities']['one']['snippet']['title'] == title


@pytest.mark.parametrize('device', ['DESKTOP', 'ANDROID', 'IPHONE'])
def test_blur_disabled_without_downloading_images(device):
    soup, items = parse(fixture_html(censored=True), 'images', 'https://yandex.ru')
    assets = Mock()
    assets.image.side_effect = ValueError('remote image unavailable')
    assets.css.side_effect = ValueError('remote CSS unavailable')
    html, audit = render(soup, items, Query('test', 213, device), 'images', 'https://yandex.ru', assets)
    assert assets.mock_calls == []
    out = BeautifulSoup(html, 'html.parser')
    state = json.loads(out.select_one('[data-state]')['data-state'])
    record = state['initialState']['serpList']['items']['entities']['one']
    assert not record['censored']
    assert record['image'] == items[0].thumbnail_url
    assert record['origUrl'] == items[0].image_url
    assert out.html['data-sbs-unblur'] == 'true'
    assert audit['embedded_images'] == 0
    assert audit['image_loading'] == 'browser'
    assert audit['remove_blur'] is True


@pytest.mark.parametrize('device,path', [('ANDROID','/images/touch/search'),('IPHONE','/images/touch/search'),('DESKTOP','/images/search')])
def test_request_preserves_region_query_and_experiment(device, path):
    task = Query('a & b + #?', 10777, device)
    url = search_url(task, {'host':'https://test.example.org:8080', 'test_ids':['12','34'], 'cgi':{'exp_confs':'testing','flag':['a','b']}})
    p = urlsplit(url)
    assert p.netloc == 'test.example.org:8080' and p.path == path
    q = parse_qs(p.query)
    assert q['text'] == [task.query] and q['lr'] == ['10777']
    assert q['test-id'] == ['12,34'] and q['flag'] == ['a','b']


def test_config_cannot_silently_override_query():
    with pytest.raises(ValueError):
        search_url(Query('a',213,'DESKTOP'), {'cgi':{'lr':1}})


@pytest.mark.parametrize('region,device', [(True,'DESKTOP'),(-1,'DESKTOP'),(213,'WATCH')])
def test_bad_input_rejected(region, device):
    with pytest.raises(ValueError):
        Query.load({'query':'x','region_id':region,'device':device})


def test_video_thumbnail_and_title_go_to_actual_source():
    source = '''<html><head></head><body><div class="VideoSnippet">
    <a class="VideoSnippet-Thumb" href="/video/preview/1"><video poster="https://example.org/thumbnail.jpg"></video></a>
    <a class="VideoSnippet-Title" href="/video/preview/1">Title</a>
    <a class="VideoHostExtended-Host" href="https://example.org/watch/1">Source</a>
    </div></body></html>'''
    soup, items = parse(source,'video','https://yandex.ru')
    assets = Mock(); assets.image.return_value = PNG
    html, audit = render(soup,items,Query('test',213,'DESKTOP'),'video','https://yandex.ru',assets)
    out=BeautifulSoup(html,'html.parser')
    assert len(out.select('[data-sbs-video]')) == 3
    assert all(a['href']=='https://example.org/watch/1' for a in out.select('[data-sbs-video]'))
    assert not out.select('video')
    assert audit['items']==1


def test_s3_readback_required_and_upload_stays_in_prefix():
    from sbs_capture.storage import Storage
    with patch('sbs_capture.storage.boto3.client') as client, patch('sbs_capture.storage.requests.get') as get:
        get.return_value.content = b'<html>ok</html>'
        get.return_value.headers = {'Content-Type':'text/html; charset=utf-8'}
        storage = Storage({'endpoint':'https://storage.example.org','bucket':'bucket','prefix':'sbs-capture'})
        url = storage.publish('run/images/1.html','<html>ok</html>')
        assert url == 'https://storage.example.org/bucket/sbs-capture/run/images/1.html'
        assert client.return_value.put_object.call_args.kwargs['Key'].startswith('sbs-capture/')
        get.return_value.content=b'wrong'
        with pytest.raises(ValueError, match='readback'):
            storage.publish('run/images/2.html','<html>ok</html>')


def test_mobile_video_source_comes_from_runtime_state():
    state = {'clips':{'items':{'parent':{'url':'https://other.example.org/different-source'}},
                      'dups':{'123':{'parentClipId':'parent','host':{'href':'https://example.org/watch/123'}}}}}
    source = '''<html><head></head><body><div class="VideoSnippet">
    <a class="VideoSnippet-Thumb" href="/video/touch/preview/123"><video poster="https://example.org/thumb.jpg"></video></a>
    <a class="VideoSnippet-Title" href="/video/touch/preview/123">Title</a>
    <div class="VideoHostExtended-Host">Example</div></div>
    <noframes id="UniAppVideo-PreloadedState">''' + json.dumps(state) + '</noframes></body></html>'
    soup, items = parse(source,'video','https://yandex.ru')
    assert items[0].page_url == 'https://example.org/watch/123'
    assets=Mock(); assets.image.return_value=PNG
    html,_=render(soup,items,Query('test',213,'ANDROID'),'video','https://yandex.ru',assets)
    out=BeautifulSoup(html,'html.parser')
    host=out.select_one('.VideoHostExtended-Host')
    assert host.name=='a' and host['href']==items[0].page_url
