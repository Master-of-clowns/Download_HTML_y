import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import sbs_capture.cli as capture
from sbs_capture.config import DEFAULT_CONFIG
from test_capture import fixture_html


@pytest.mark.parametrize('local_only', [False, True])
def test_cli_prints_public_link_and_saves_plain_link_file(tmp_path, capsys, local_only):
    source = tmp_path / 'source.html'
    source.write_text(fixture_html(censored=True))
    queries = tmp_path / 'input.json'
    queries.write_text(json.dumps([{'query': 'test', 'region_id': 213, 'device': 'ANDROID'}]))
    output = tmp_path / 'runs'
    argv = ['sbs_capture_single.py', '--input', str(queries), '--html', str(source),
            '--source-url', 'https://yandex.ru/images/touch/search?text=test&lr=213',
            '--output', str(output)]
    if local_only:
        argv.append('--local-only')
    public = 'https://storage.example.org/bucket/run/images/one.html'
    fake = Mock()
    fake.publish.return_value = public
    with patch.object(sys, 'argv', argv), patch.object(capture, 'Storage', return_value=fake) as storage:
        assert capture.main() == 0
    stdout, stderr = capsys.readouterr()
    result = json.loads(stdout.strip())
    assert result['status'] == 'ok'
    run = next(output.iterdir())
    if local_only:
        storage.assert_not_called()
        assert 's3_url' not in result
        assert '--local-only' in stderr
        assert not (run / 'urls.txt').exists()
    else:
        fake.publish.assert_called_once()
        assert result['s3_url'] == public
        assert 'HTML: ' + public in stderr
        assert (run / 'urls.txt').read_text() == public + '\n'


@pytest.mark.parametrize('launcher', ['module', 'script'])
def test_real_launchers_load_resources_and_ignore_false_blur_config(tmp_path, launcher):
    project = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source.html'
    source.write_text(fixture_html(censored=True), encoding='utf-8')
    queries = tmp_path / 'input.json'
    queries.write_text(json.dumps({'query': 'test', 'region_id': 213, 'device': 'ANDROID'}), encoding='utf-8')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({**DEFAULT_CONFIG, 'remove_blur': False}), encoding='utf-8')
    command = ([sys.executable, '-m', 'sbs_capture'] if launcher == 'module'
               else [sys.executable, str(project / 'sbs_capture_single.py')])
    result = subprocess.run(command + ['--input', str(queries), '--config', str(config),
        '--html', str(source), '--source-url', 'https://yandex.ru/images/touch/search?text=test&lr=213',
        '--output', str(tmp_path / 'runs'), '--local-only'],
        cwd=project if launcher == 'module' else tmp_path, capture_output=True, text=True,
        encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout.strip())
    assert record['status'] == 'ok'
    assert record['audit']['remove_blur'] is True
    assert 's3_url' not in record
    html = Path(record['html_path']).read_text(encoding='utf-8')
    assert 'sbs-native-style' in html and 'sbs-native-adapter' in html
    assert '-webkit-filter:none!important;filter:none!important' in html
