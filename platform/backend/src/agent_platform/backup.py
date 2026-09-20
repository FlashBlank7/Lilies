"""Offline complete backups; restore only into a new directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from uuid import uuid4

from .db import connect
from .maintenance import data_access
from .workspace_copy import copy_workspace_file


def _remove_tree(path: Path):
    # Requirement packages are read-only, including their directories.
    for root, directories, _ in os.walk(path):
        os.chmod(root, 0o700)
        for name in directories:
            child = Path(root) / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(path)


def _copy_tree(source: Path, target: Path) -> dict:
    counts = {'files': 0, 'bytes': 0, 'symlinks': 0}

    def copy(src: Path, dst: Path):
        info = src.lstat()
        if stat.S_ISLNK(info.st_mode):
            # Do not import host login files or other external link targets.
            dst.symlink_to(os.readlink(src))
            counts['symlinks'] += 1
        elif stat.S_ISDIR(info.st_mode):
            dst.mkdir()
            for child in src.iterdir():
                if src != source or child.name != '.platform.lock':
                    copy(child, dst / child.name)
            shutil.copystat(src, dst)
        elif stat.S_ISREG(info.st_mode):
            copy_workspace_file(src, dst)
            counts['files'] += 1
            counts['bytes'] += info.st_size
        else:
            raise ValueError(f'无法备份特殊文件：{src}；请先停止相关进程')

    copy(source, target)
    return counts


def _check_database(root: Path):
    db = root / 'agent_platform.db'
    if db.is_symlink() or not db.is_file():
        raise ValueError('备份中缺少独立的平台数据库 agent_platform.db')
    for suffix in ('-wal', '-shm', '-journal'):
        if db.with_name(db.name + suffix).is_symlink():
            raise ValueError('数据库附属文件不能是符号链接')
    with connect(db, readonly=True, row_factory=None) as connection:
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('平台数据库完整性检查未通过')


def _tree_stats(root: Path) -> dict:
    counts = {'files': 0, 'bytes': 0, 'symlinks': 0}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            if Path(directory) == root and name == '.platform.lock':
                continue
            info = (Path(directory) / name).lstat()
            if stat.S_ISLNK(info.st_mode):
                counts['symlinks'] += 1
            elif stat.S_ISREG(info.st_mode):
                counts['files'] += 1
                counts['bytes'] += info.st_size
    return counts


def _separate(*paths: Path):
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError('数据、工作区、备份及恢复目录不能相同或相互嵌套')


def create_backup(data_dir: Path, workspace_root: Path, backup_root: Path,
                  config_path: Path | None = None) -> Path:
    data_dir, workspace_root, backup_root = [p.resolve() for p in
                                            (data_dir, workspace_root, backup_root)]
    _separate(data_dir, workspace_root, backup_root)
    for source in (data_dir, workspace_root):
        if not source.is_dir():
            raise ValueError(f'目录不存在：{source}')
    if config_path is not None and (config_path.is_symlink() or not config_path.is_file()):
        raise ValueError('配置必须是已有的普通文件')
    with data_access(data_dir, workspace_root, exclusive=True):
        backup_root.mkdir(parents=True, exist_ok=True)
        destination = backup_root / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8])
        temporary = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=backup_root))
        try:
            description = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                           'sources': {'data': str(data_dir), 'workspaces': str(workspace_root)},
                           'contents': {}}
            for name, source in [('data', data_dir), ('workspaces', workspace_root)]:
                print(f'正在备份 {name}…', flush=True)
                description['contents'][name] = _copy_tree(source, temporary / name)
            if config_path is not None:
                copy_workspace_file(config_path, temporary / '.env')
                (temporary / '.env').chmod(0o600)
            print('正在检查平台数据库…', flush=True)
            _check_database(temporary / 'data')
            # SQLite may create/remove WAL shared memory during its integrity check.
            description['contents']['data'] = _tree_stats(temporary / 'data')
            (temporary / 'backup.json').write_text(json.dumps(description, ensure_ascii=False,
                                                            indent=2), encoding='utf-8')
            temporary.rename(destination)
            return destination
        except BaseException:
            _remove_tree(temporary)
            raise


def _relocate_runs(data_dir: Path, old_workspace: Path, new_workspace: Path):
    """Move platform execution paths, never rewrite user code/inputs/outputs."""
    def moved(value):
        if isinstance(value, str) and Path(value).is_absolute():
            path = Path(value)
            if path.is_relative_to(old_workspace):
                return str(new_workspace / path.relative_to(old_workspace))
        return value

    with connect(data_dir / 'agent_platform.db', row_factory=None) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'workflow_runs' in tables:
            for ident, raw in connection.execute('SELECT id,state_json FROM workflow_runs').fetchall():
                state = json.loads(raw)
                for key in ('workspace_path', 'workspace_boundary'):
                    if key in state:
                        state[key] = moved(state[key])
                updated = json.dumps(state, ensure_ascii=False)
                if updated != raw:
                    connection.execute('UPDATE workflow_runs SET state_json=? WHERE id=?',
                                       (updated, ident))
        for table in ('generations', 'sessions'):
            if table in tables:
                for ident, path in connection.execute(f'SELECT id,workspace_path FROM {table}').fetchall():
                    connection.execute(f'UPDATE {table} SET workspace_path=? WHERE id=?',
                                       (moved(path), ident))
        connection.commit()


def _relocate_codex(data_dir: Path, old_data: Path, new_data: Path):
    """Relocate the copied CLI index while offline; never touch its conversation content.

    Paginated Codex threads reject resume(path=...) when their index still holds
    the pre-restore path. This is intentionally a restore-only migration, guarded
    by the actual schema, not part of the running platform's Codex integration.
    """
    for db in (data_dir / 'local-agents').glob('*/*/codex-home/state_*.sqlite'):
        if db.is_symlink():
            raise ValueError('Codex 会话索引不能是符号链接')
        with connect(db, row_factory=None) as connection:
            columns = {r[1] for r in connection.execute('PRAGMA table_info(threads)')}
            if not columns:
                continue
            if not {'id', 'rollout_path', 'cwd'}.issubset(columns):
                raise ValueError('此版本 Codex 会话索引不支持目录迁移；原备份未修改')
            for ident, rollout, cwd in connection.execute('SELECT id,rollout_path,cwd FROM threads').fetchall():
                values = []
                for value in (rollout, cwd):
                    path = Path(value) if isinstance(value, str) else None
                    values.append(str(new_data / path.relative_to(old_data))
                                  if path and path.is_absolute() and path.is_relative_to(old_data)
                                  else value)
                if values != [rollout, cwd]:
                    connection.execute('UPDATE threads SET rollout_path=?,cwd=? WHERE id=?',
                                       (*values, ident))
            connection.commit()


def restore_backup(backup: Path, destination: Path) -> Path:
    backup, destination = backup.resolve(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('恢复目标必须是尚不存在的新目录；不会覆盖现有项目')
    destination = destination.resolve()
    _separate(backup, destination.resolve())
    description = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
    if description.get('format') != 1:
        raise ValueError('不支持此备份版本')
    for name in ('data', 'workspaces'):
        if (backup / name).is_symlink() or not (backup / name).is_dir():
            raise ValueError(f'备份缺少独立的 {name} 目录')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.restore-', dir=destination.parent))
    try:
        for name in ('data', 'workspaces'):
            print(f'正在恢复 {name}…', flush=True)
            counts = _copy_tree(backup / name, temporary / name)
            if counts != description['contents'][name]:
                raise ValueError(f'{name} 文件数量或大小与备份不符，恢复已取消')
        print('正在检查恢复数据库及运行路径…', flush=True)
        _check_database(temporary / 'data')
        _relocate_runs(temporary / 'data', Path(description['sources']['workspaces']),
                       destination / 'workspaces')
        _relocate_codex(temporary / 'data', Path(description['sources']['data']),
                        destination / 'data')
        if (backup / '.env').exists():
            if (backup / '.env').is_symlink():
                raise ValueError('备份配置不能是符号链接')
            original = (backup / '.env').read_text(encoding='utf-8')
            # Last definitions win; keep credentials/images, point to restored files.
            (temporary / '.env').write_text(original + '\n' + '\n'.join([
                f'DATA_DIR={json.dumps(str(destination / "data"), ensure_ascii=False)}',
                f'WORKSPACE_ROOT={json.dumps(str(destination / "workspaces"), ensure_ascii=False)}',
                f'WORKSPACE_HOST_ROOT={json.dumps(str(destination / "workspaces"), ensure_ascii=False)}',
                'MODEL_EGRESS_ENABLED=false', '']), encoding='utf-8')
            (temporary / '.env').chmod(0o600)
        (temporary / 'restored-from.json').write_text(json.dumps(description, ensure_ascii=False,
                                                              indent=2), encoding='utf-8')
        temporary.rename(destination)
        return destination
    except BaseException:
        _remove_tree(temporary)
        raise


def main():
    parser = argparse.ArgumentParser(description='停止平台后完整备份；恢复至独立新目录')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('--data', type=Path, default=Path('data'))
    create.add_argument('--workspaces', type=Path, default=Path('workspaces'))
    create.add_argument('--output', type=Path, default=Path('backups'))
    create.add_argument('--config', type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        result = (create_backup(args.data, args.workspaces, args.output, args.config)
                  if args.action == 'create' else restore_backup(args.backup, args.destination))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f'{error}\n')
    print(f'{"备份" if args.action == "create" else "恢复"}完成 → {result}')


if __name__ == '__main__':
    main()
