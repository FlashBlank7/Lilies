"""Independent workspace copies, using APFS shared blocks when available."""
import ctypes
import errno
import os
import shutil
import sys


_clonefile = None
if sys.platform == 'darwin':
    _clonefile = getattr(ctypes.CDLL(None, use_errno=True), 'clonefile', None)
    if _clonefile is not None:
        _clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        _clonefile.restype = ctypes.c_int


def copy_workspace_file(src, dst):
    """Match copy2 semantics without hard-linking writable test inputs.

    Clones have independent inodes and metadata; writing either copy cannot
    change another test or the source. Unsupported volumes use normal copies.
    """
    if _clonefile is not None:
        if _clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
            shutil.copystat(src, dst)
            return dst
        error = ctypes.get_errno()
        if error not in {errno.ENOTSUP, errno.EXDEV, errno.ENOSYS,
                         errno.EINVAL, errno.EEXIST}:
            raise OSError(error, os.strerror(error), os.fspath(dst))
    return shutil.copy2(src, dst)
