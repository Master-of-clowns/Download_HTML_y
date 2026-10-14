"""Сбор страниц desktop-выдачи."""

import json
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, parse_qsl, urlencode
from .parser import ParseError, parse_images


def native_state(soup):
    root = soup.select_one('[id^="ImagesApp"][data-state]')
    if root is None:
        raise ParseError('Native image state is missing')
    state = json.loads(root['data-state'])
    if not isinstance(state.get('initialState', {}).get('serpList', {}).get('items'), dict):
        raise ParseError('Native image list is missing')
    return root, state


def next_page_url(link, current_url, initial_url):
    candidate = urlsplit(urljoin(current_url, link))
    initial = urlsplit(initial_url)
    if (candidate.scheme, candidate.netloc, candidate.path) != (initial.scheme, initial.netloc, initial.path):
        raise ParseError('Pagination changed the search host or path')
    source = parse_qs(initial.query, keep_blank_values=True)
    next_params = parse_qs(candidate.query, keep_blank_values=True)
    for key, values in source.items():
        if key != 'p' and key in next_params and next_params[key] != values:
            raise ParseError(f'Pagination changed requested parameter: {key}')
    page = next_params.get('p', [''])
    previous = parse_qs(urlsplit(current_url).query).get('p', ['0'])
    if len(page) != 1 or not page[0].isdigit() or int(page[0]) <= int(previous[0]):
        raise ParseError('Pagination did not advance to the next page')
    # Next links sometimes omit experimental parameters; retain the full input.
    pairs = [(k, v) for k, v in parse_qsl(candidate.query, keep_blank_values=True)
             if k not in source or k == 'p']
    pairs.extend((k, v) for k, v in parse_qsl(initial.query, keep_blank_values=True) if k != 'p')
    return urlunsplit((candidate.scheme, candidate.netloc, candidate.path, urlencode(pairs), ''))


def collect_desktop(soup, items, initial_url, fetch_page, minimum, max_pages):
    root, state = native_state(soup)
    combined = state['initialState']['serpList']
    records = combined['items']['entities']
    keys = list(combined['items'].get('keys', records))
    seen_sources = {(item.image_url, item.page_url) for item in items}
    initial_count = len(items)
    source_pages = [initial_url]
    latest, current_url = combined, initial_url
    stop_reason = 'target_reached'

    while len(keys) < minimum:
        link = latest.get('navi', {}).get('next')
        if not link:
            stop_reason = 'end_of_results'
            break
        if len(source_pages) >= max_pages:
            raise ParseError(f'Collected {len(keys)} images; target {minimum} not reached within {max_pages} pages')
        url = next_page_url(link, current_url, initial_url)
        next_soup, next_items, current_url = fetch_page(url)
        _, next_state = native_state(next_soup)
        latest = next_state['initialState']['serpList']
        next_records = latest['items']['entities']
        added = 0
        for item in next_items:
            if item.id in records or (item.image_url, item.page_url) in seen_sources:
                continue
            records[item.id] = next_records[item.id]
            keys.append(item.id)
            seen_sources.add((item.image_url, item.page_url))
            added += 1
        source_pages.append(current_url)
        if not added:
            stop_reason = 'no_new_items'
            break

    combined['items']['keys'] = keys
    # Native desktop HTML starts with a skeleton; React lays out these records
    # at the actual display width and keeps its own original viewer handlers.
    combined['lastPage'] = latest.get('lastPage', combined.get('lastPage', 0))
    combined['loadNextState'] = 'finished'
    combined['prefetchNext'] = False
    combined.setdefault('autoload', {})['enabled'] = False
    combined.setdefault('navi', {})['next'] = ''
    root['data-state'] = json.dumps(state, ensure_ascii=True, separators=(',', ':'))
    merged_items = parse_images(soup, initial_url)
    if len(merged_items) != len(keys):
        raise ParseError('Merged native state lost result cards')
    return soup, merged_items, {
        'initial_items': initial_count, 'items_collected': len(merged_items),
        'pages_collected': len(source_pages), 'source_pages': source_pages,
        'minimum_items': minimum, 'target_reached': len(merged_items) >= minimum,
        'stop_reason': stop_reason,
    }
