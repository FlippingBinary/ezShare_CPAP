from datetime import datetime
from unittest.mock import Mock

import pytest
import requests

from download_cpap import EzShareCard, download_datalog, download_root_files, download_settings


def response(start, end, total, body, **headers):
    r = Mock()
    r.status_code = 206
    r.headers = {'Content-Range': f'bytes {start}-{end}/{total}', **headers}
    r.iter_content.return_value = iter([body])
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


def card_with(*replies, **kwargs):
    card = EzShareCard(**kwargs)
    card.session = Mock()
    card.session.get.side_effect = replies
    return card


def test_chunks_and_final_short_range(tmp_path):
    data = bytes(range(256)) * 65
    card = card_with(response(0, 8191, len(data), data[:8192]),
                     response(8192, 16383, len(data), data[8192:16384]),
                     response(16384, len(data)-1, len(data), data[16384:]))
    dest = tmp_path / 'file'
    assert card.download_file('STR.edf', dest, len(data))
    assert dest.read_bytes() == data
    assert [c.kwargs['headers']['Range'] for c in card.session.get.call_args_list] == [
        'bytes=0-8191', 'bytes=8192-16383', 'bytes=16384-16639']
    assert all(c.kwargs['timeout'] == 8 for c in card.session.get.call_args_list)


@pytest.mark.parametrize('failure', ['timeout', 'short', 'range', 'length', 'html', 'status', 'oversized', 'midstream'])
def test_retry_validates_before_append(tmp_path, failure):
    bad = response(0, 3, 4, b'abcd')
    if failure == 'timeout':
        bad = requests.Timeout('no reply')
    elif failure == 'short':
        bad.iter_content.return_value = iter([b'ab'])
    elif failure == 'range':
        bad.headers['Content-Range'] = 'bytes 1-4/4'
    elif failure == 'length':
        bad.headers['Content-Length'] = '9'
    elif failure == 'html':
        bad.headers['Content-Type'] = 'text/html'
    elif failure == 'status':
        bad.status_code = 500
    elif failure == 'oversized':
        bad.iter_content.return_value = iter([b'abcde'])
    elif failure == 'midstream':
        def interrupted():
            yield b'ab'
            raise requests.ConnectionError('closed')
        bad.iter_content.return_value = interrupted()
    card = card_with(bad, response(0, 3, 4, b'abcd'), retries=1)
    dest = tmp_path / 'file'
    assert card.download_file('file', dest, 4)
    assert dest.read_bytes() == b'abcd'
    assert card.session.get.call_count == 2


def plain(body):
    r = response(0, 0, 0, body, **{'Content-Type': 'text/plain',
                                    'Content-Length': str(len(body))})
    r.status_code = 200
    del r.headers['Content-Range']
    return r


def test_whole_file_range_answered_with_200(tmp_path):
    # Firmware 4.4.0 answers bytes=0-<last> with 200 and the full body.
    card = card_with(plain(b'abcd'))
    dest = tmp_path / 'file'
    assert card.download_file('file', dest, 4)
    assert dest.read_bytes() == b'abcd'


def test_200_for_a_partial_range_is_rejected(tmp_path):
    card = card_with(plain(b'abcdefgh'), plain(b'abcdefgh'), chunk_size=4, retries=1)
    dest = tmp_path / 'file'
    assert not card.download_file('file', dest, 8)
    assert not dest.exists() or dest.read_bytes() == b''


def test_bounded_failure_and_resume(tmp_path):
    card = card_with(response(0, 3, 8, b'abcd'), requests.Timeout(), requests.Timeout(),
                     chunk_size=4, retries=1)
    dest = tmp_path / 'file'
    assert not card.download_file('file', dest, 8)
    assert dest.read_bytes() == b'abcd'
    assert card.session.get.call_count == 3
    card.session.get.side_effect = [response(4, 7, 8, b'efgh')]
    assert card.download_file('file', dest, 8)
    assert card.session.get.call_args.kwargs['headers']['Range'] == 'bytes=4-7'
    assert dest.read_bytes() == b'abcdefgh'
    assert not card.download_file('file', dest, 2)
    assert dest.read_bytes() == b'abcdefgh'


@pytest.mark.parametrize('error', [requests.Timeout(), requests.ConnectionError()])
def test_head_no_reply(error):
    card = card_with()
    card.session.head.side_effect = error
    assert card.is_real_file('missing') == (False, 0)


@pytest.mark.parametrize('ftype', ['SAD', 'SA2'])
def test_datalog_types_and_cross_midnight(tmp_path, ftype):
    card = Mock()
    card.is_real_directory.return_value = True
    files = {f'DATALOG/20261104/20261105_000005_{kind}.edf': 4
             for kind in ['BRP', 'PLD', ftype]}
    files.update({f'DATALOG/20261104/20261104_235950_{kind}.edf': 4
                  for kind in ['EVE', 'CSL']})
    card.is_real_file.side_effect = lambda path: (path in files, files.get(path, 0))
    card.download_file.return_value = True
    assert download_datalog(card, [{'record_date': '20261104',
        'start_time': datetime(2026, 11, 4, 23, 59), 'duration_min': 60}], tmp_path) == 5
    assert {c.args[0] for c in card.download_file.call_args_list} == set(files)


def test_as11_metadata(tmp_path):
    card = Mock()
    files = {'Identification.json', 'Identification.crc', 'journal.jnl',
             'SETTINGS/CurrentSettings.json', 'SETTINGS/CurrentSettings.crc', 'STR.edf'}
    card.is_real_file.side_effect = lambda path: (path in files, 4)
    card.download_file.return_value = True
    download_root_files(card, tmp_path)
    download_settings(card, tmp_path)
    assert {c.args[0] for c in card.download_file.call_args_list} == files


@pytest.mark.parametrize('headers,status', [({'Content-Type': 'text/html'}, 200),
    ({'Content-Length': 'broken'}, 200), ({'Content-Length': '4'}, 404)])
def test_head_miss(headers, status):
    card = card_with()
    card.session.head.return_value = Mock(headers=headers, status_code=status)
    assert card.is_real_file('missing') == (False, 0)


def test_probe_size_and_empty_file(tmp_path):
    card = card_with(response(0, 3, 4, b'abcd'))
    card.session.head.return_value = Mock(
        headers={'Content-Type': 'text/plain', 'Content-Length': '4'}, status_code=200)
    assert card.download_file('file', tmp_path / 'file')
    assert (tmp_path / 'file').read_bytes() == b'abcd'
    assert card.download_file('empty', tmp_path / 'empty', 0)
    assert (tmp_path / 'empty').read_bytes() == b''
