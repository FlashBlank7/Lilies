"""Append customer files without replacing earlier project inputs."""
import hashlib
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from starlette.datastructures import UploadFile

from .requirement_package import MAX_UPLOAD_BYTES


async def add_material(services, project_id, file):
    workspace = services.projects.workspace(project_id).resolve()
    name = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    if not name or name in {'.', '..'} or '\x00' in name or len(name.encode('utf-8')) > 240:
        raise ValueError('文件名无效或过长')
    parent = workspace / 'requirement-package'
    uploads = parent / '追加资料'
    for folder in (parent, uploads):
        if folder.is_symlink() or not folder.resolve().is_relative_to(workspace):
            raise ValueError('项目资料目录不可用')
        folder.mkdir(exist_ok=True)
    # Staging is outside every project. Only the completed directory becomes
    # visible to the agent; an interrupted upload leaves no partial input.
    staging = services.settings.workspace_root.resolve() / '.material-uploads'
    if staging.is_symlink():
        raise ValueError('上传暂存目录不可用')
    staging.mkdir(exist_ok=True, mode=0o700)
    digest, size = hashlib.sha256(), 0
    with tempfile.TemporaryDirectory(dir=staging) as temporary:
        source = Path(temporary) / 'complete'
        source.mkdir()
        with (source / name).open('xb') as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError('单个文件不能超过 256 MB')
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise ValueError('不能上传空文件')
        target = uploads / str(uuid4())
        source.rename(target)
    services.sandboxes.protect_inputs(workspace, ['requirement-package', 'requirements'])
    return {'name': name, 'path': (target / name).relative_to(workspace).as_posix(),
            'size': size, 'sha256': digest.hexdigest()}


async def copy_material(services, project_id, source_project_id, source_path):
    await services.projects.store.get(source_project_id)
    relative = PurePosixPath(source_path)
    if (relative.is_absolute() or not relative.parts or relative.parts[0] != 'requirement-package'
            or '..' in relative.parts or '\\' in source_path or '\x00' in source_path):
        raise ValueError('只能选择来源项目中的原始资料文件')
    workspace = services.projects.workspace(source_project_id).resolve()
    source = workspace
    for part in relative.parts:
        source = source / part
        if source.is_symlink():
            raise ValueError('不能复制符号链接资料')
    if not source.is_file() or not source.resolve().is_relative_to(workspace):
        raise ValueError('来源资料不存在或不是普通文件')
    with source.open('rb') as stream:
        return await add_material(services, project_id, UploadFile(file=stream, filename=source.name))
