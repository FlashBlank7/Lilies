"""Keep offline file maintenance separate from a running API."""
from contextlib import contextmanager, ExitStack
import fcntl
from pathlib import Path


@contextmanager
def data_access(*roots: Path, exclusive: bool = False):
    with ExitStack() as stack:
        for root in sorted({Path(p).resolve() for p in roots}):
            root.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context((root / '.platform.lock').open('a+b'))
            try:
                fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('数据目录正在使用，请停止平台及计算任务后再备份或恢复') from error
        yield
