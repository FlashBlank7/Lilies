import ctypes
import errno
import os

import pytest

from agent_platform import workspace_copy


@pytest.mark.parametrize('fallback', [False, True])
def test_workspace_copies_are_independent_and_preserve_metadata(tmp_path, monkeypatch, fallback):
    if fallback:
        def unsupported(*args):
            ctypes.set_errno(errno.ENOTSUP)
            return -1
        monkeypatch.setattr(workspace_copy, '_clonefile', unsupported)
    source, first, second = [tmp_path / name for name in ('source', 'first', 'second')]
    source.write_bytes(b'original' * 4096)
    source.chmod(0o640)
    os.utime(source, (1700000000, 1700000000))
    for dest in (first, second):
        workspace_copy.copy_workspace_file(source, dest)
        assert dest.read_bytes() == source.read_bytes()
        assert dest.stat().st_ino != source.stat().st_ino
        assert dest.stat().st_mode == source.stat().st_mode
        assert dest.stat().st_mtime_ns == source.stat().st_mtime_ns
    first.write_text('first test')
    source.write_text('later draft')
    assert second.read_bytes() == b'original' * 4096
    assert first.read_text() == 'first test'
    workspace_copy.copy_workspace_file(source, first)
    assert first.read_text() == 'later draft'


def test_clone_storage_errors_are_reported_without_retrying_full_copy(tmp_path, monkeypatch):
    def full(*args):
        ctypes.set_errno(errno.ENOSPC)
        return -1
    monkeypatch.setattr(workspace_copy, '_clonefile', full)
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    source.write_text('keep')
    with pytest.raises(OSError) as error:
        workspace_copy.copy_workspace_file(source, dest)
    assert error.value.errno == errno.ENOSPC
    assert source.read_text() == 'keep'
    assert not dest.exists()
