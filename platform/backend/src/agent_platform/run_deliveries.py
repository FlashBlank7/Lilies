"""Run-owned copies of explicitly returned files, outside the code sandbox."""
from __future__ import annotations

import hashlib
import asyncio
import fcntl
import json
import os
import re
import tempfile
import stat
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

FILE_KEYS = {'report', 'report_html', 'results_excel', 'results_csv', 'model_package',
             'manifest', 'file_path', 'file', 'artifact'}
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


def file_paths(value):
    found, budget = set(), 1000

    def visit(item, depth=0):
        nonlocal budget
        if depth > 12 or budget <= 0 or not isinstance(item, dict):
            return
        budget -= 1
        for key, child in item.items():
            if key in FILE_KEYS and isinstance(child, str) and valid_path(child):
                found.add(child)
            elif isinstance(child, dict):
                visit(child, depth + 1)
            elif key == 'artifacts' and isinstance(child, list):
                for entry in child[:200]:
                    visit(entry, depth + 1)
    visit(value)
    return sorted(found)


def valid_path(path):
    return (isinstance(path, str) and unquote(path) == path and path.startswith(('results/', 'solution/'))
            and not re.search(r'[\\\x00-\x1f]', path)
            and all(p not in {'', '.', '..'} for p in path.split('/')))


class RunDeliveries:
    def __init__(self, root):
        self.root = Path(root) / '.run-deliveries'

    @asynccontextmanager
    async def workspace_guard(self, workspace):
        # Outside the sandbox; serializes file-producing steps across tasks and workers.
        directory = self.root / '.workspace-locks'
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
        with (directory / key).open('a+b') as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.02)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def folder(self, project_id, task_id, run_id):
        if not all(re.fullmatch(r'[\w-]+', part) for part in (project_id, task_id, run_id)):
            raise ValueError('运行标识无效')
        return self.root / project_id / task_id / run_id

    @staticmethod
    def unverified(path, run_id):
        return {'path': path, 'run_id': run_id, 'status': 'unverified',
                'error': '没有可确认的步骤文件副本，当前同名文件可能已被覆盖'}

    def select(self, paths, entries, run_id):
        selected = []
        for path in paths:
            matches = [e for e in entries if e['path'] == path]
            identities = {(e['status'], e.get('sha256'), e.get('reason')) for e in matches}
            selected.append({**matches[-1], 'run_id': run_id} if len(identities) == 1
                            else self.unverified(path, run_id))
        return selected

    @staticmethod
    def current_versions(state):
        """Observe known paths before/after a step; forwarding alone is not a write."""
        versions = {}
        for path in {e['path'] for entries in state.delivery_steps.values() for e in entries}:
            versions[path] = None
            try:
                if not valid_path(path):
                    continue
                with ExitStack() as stack:
                    descriptor = os.open(state.workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    stack.callback(os.close, descriptor)
                    parts = PurePosixPath(path).parts
                    for part in parts[:-1]:
                        descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        stack.callback(os.close, descriptor)
                    value = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISREG(value.st_mode):
                    versions[path] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            except OSError:
                pass
        return versions

    def capture_step(self, project_id, task_id, state, node_id, output, *, inherited=None, references=None, read_current=False, fresh=False, before=None):
        """Save returned files before marking the producing step completed."""
        paths = file_paths(output)
        if not paths:
            return
        folder = self.folder(project_id, task_id, state.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        known = [entry for entries in state.delivery_steps.values() for entry in entries]
        if inherited is not None:
            # A workflow call forwards its child's copies, never the shared workspace.
            entries = self.select(paths, inherited, state.run_id)
            for entry in entries:
                if entry['status'] == 'saved':
                    original = next(e for e in inherited if e['path'] == entry['path'] and e.get('sha256') == entry['sha256'])
                    try:
                        source = self.download(project_id, task_id, original['run_id'], entry['sha256'])
                        target = folder / entry['sha256']
                        if not target.exists():
                            os.link(source, target)
                    except OSError:
                        entry.update(status='unavailable', error='成员步骤的已保存副本不可用')
        else:
            entries = [self.select([path], (references or {}).get(path, known), state.run_id)[0] for path in paths]
            current = self.current_versions(state) if before is not None and read_current else {}
            pending = [path for path in paths
                       if (read_current and (before is None or path not in before
                                             or current.get(path) is not None and current[path] != before[path]))
                       or (fresh and not any(e['path'] == path for e in known))]
            # Count already preserved content once, including earlier step versions.
            total = sum({e['sha256']: e.get('size', 0) for e in known if e['status'] == 'saved'}.values())
            captured = {e['path']: e for e in self._capture_paths(folder, state.workspace_path, state.run_id, pending, total)}
            entries = [captured.get(entry['path'], entry) for entry in entries]
        state.delivery_steps[node_id] = entries

    def _capture_paths(self, folder, workspace_path, run_id, paths, total=0):
        root = Path(workspace_path).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        entries = []
        for path in paths:
            entry = {'path': path, 'run_id': run_id, 'status': 'unavailable'}
            try:
                # Resolve every directory by descriptor: a concurrent sandbox write
                # cannot swap a checked path for a link into another project.
                with ExitStack() as stack:
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    stack.callback(os.close, descriptor)
                    parts = PurePosixPath(path).parts
                    for part in parts[:-1]:
                        descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        stack.callback(os.close, descriptor)
                    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    stream = stack.enter_context(os.fdopen(fd, 'rb'))
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('输出不是普通文件')
                    if before.st_size > MAX_FILE or total + before.st_size > MAX_TOTAL:
                        entry.update(status='unverified', reason='size_limit', size=before.st_size,
                                     error='文件超出副本保存范围；可下载当前文件，但无法确认历史版本')
                        entries.append(entry)
                        continue
                    content = stream.read(MAX_FILE + 1)
                    after = os.fstat(stream.fileno())
                if len(content) > MAX_FILE or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('文件在读取时发生变化，未保存不确定版本')
                digest = hashlib.sha256(content).hexdigest()
                target = folder / digest
                if not target.exists():
                    with target.open('xb') as handle:
                        handle.write(content)
                total += len(content)
                entry.update(status='saved', sha256=digest, size=len(content))
            except (OSError, ValueError) as error:
                entry['error'] = str(error)
            entries.append(entry)
        return entries

    def capture(self, project_id, task_id, record):
        """Finalize already captured step files; never refresh them from a mutable path."""
        folder = self.folder(project_id, task_id, record['id'])
        folder.mkdir(parents=True, exist_ok=True)
        state = record['state']
        paths = file_paths(record.get('outputs') or {})
        if hasattr(state, 'delivery_steps'):
            known = [entry for entries in state.delivery_steps.values() for entry in entries]
            if paths:
                # The selected end output may refer to an earlier step's file version.
                known = [entry for node in state.snapshot.workflow.nodes if node.type == 'end' and node.id in state.completed
                         for entry in state.delivery_steps.get(node.id, [])]
            # Failed runs still expose their completed step files as partial products.
            entries = self.select(paths or sorted({e['path'] for e in known}), known, record['id'])
        else:
            entries = self._capture_paths(folder, state.workspace_path, record['id'], paths)
        manifest = {'files': entries, 'run_updated_at': record.get('updated_at')}
        # Resume can produce a new report; old content-addressed downloads remain stable.
        fd, name = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, 'w') as handle:
            json.dump(manifest, handle, ensure_ascii=False)
        os.replace(name, folder / 'manifest.json')

    def files(self, project_id, task_id, run):
        folder = self.folder(project_id, task_id, run['id'])
        try:
            manifest = json.loads((folder / 'manifest.json').read_text())
            if manifest.get('run_updated_at') != run.get('updated_at'):
                raise ValueError('运行已经更新，不能使用之前的副本清单')
            entries = manifest['files']
        except (OSError, ValueError, KeyError):
            entries = [{'path': path, 'run_id': run['id'], 'status': 'unverified',
                        'error': '此历史运行未保存文件副本，无法确认当前文件是否为当时版本'}
                       for path in file_paths(run.get('outputs') or {})]
        return entries

    def download(self, project_id, task_id, run_id, digest):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise FileNotFoundError('结果文件不存在')
        path = self.folder(project_id, task_id, run_id) / digest
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError('保存的结果文件不存在')
        return path
