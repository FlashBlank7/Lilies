"""Import a requirement ZIP as a new application's input workspace, without building it."""
from __future__ import annotations

import asyncio
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .workflow_models import ApplicationCreateRequest
from .workflow_storage import WorkflowStorage

MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 2000
PACKAGE_DIRECTORY = "requirement-package"


class PackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["lilies-requirement-package"]
    version: Literal[1]
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    requirement_file: str = Field(min_length=1, max_length=500)


def safe_member(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or len(name) > 500
            or path.is_absolute() or ".." in path.parts
            or any(ord(char) < 32 for char in name)):
        raise ValueError("需求包包含无效文件路径")
    normalized = path.as_posix()
    if normalized == ".":
        raise ValueError("需求包包含空文件路径")
    return normalized


def unpack_package(source: BinaryIO, destination: Path) -> tuple[PackageManifest, str, list[dict]]:
    source.seek(0, 2)
    if source.tell() > MAX_UPLOAD_BYTES:
        raise ValueError("需求包超过 256 MB，请拆分后导入")
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        members = []
        names = set()
        total = 0
        for info in archive.infolist():
            name = safe_member(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise ValueError("需求包不能包含符号链接或特殊文件")
            if info.flag_bits & 1:
                raise ValueError("请先解密 ZIP 再导入")
            if info.is_dir() or name.startswith("__MACOSX/") or PurePosixPath(name).name == ".DS_Store":
                continue
            if name.casefold() in names:
                raise ValueError("需求包包含重复文件名")
            names.add(name.casefold())
            total += info.file_size
            members.append((name, info))
            if len(members) > MAX_FILES or total > MAX_EXPANDED_BYTES:
                raise ValueError("需求包解压后不能超过 1 GB 或 2000 个文件")
        manifests = [name for name, _ in members
                     if PurePosixPath(name).name == "requirement.json" and len(PurePosixPath(name).parts) <= 2]
        if len(manifests) != 1:
            raise ValueError("ZIP 根目录（或单个外层文件夹）需有一份 requirement.json 需求包说明")
        manifest_path = manifests[0]
        prefix = manifest_path.removesuffix("requirement.json")
        by_name = dict(members)
        for name, _ in members:
            if any(parent.as_posix().casefold() in names for parent in PurePosixPath(name).parents
                   if parent.as_posix() != "."):
                raise ValueError("需求包文件与文件夹路径冲突")
        if by_name[manifest_path].file_size > 16_384:
            raise ValueError("requirement.json 超过 16 KB")
        manifest = PackageManifest.model_validate_json(archive.read(by_name[manifest_path]))
        requirement_path = prefix + safe_member(manifest.requirement_file)
        if requirement_path not in by_name or not requirement_path.endswith((".md", ".txt")):
            raise ValueError("需求正文必须是包内存在的 Markdown 或 TXT 文件")
        if by_name[requirement_path].file_size > 100_000:
            raise ValueError("需求正文过长，请将详细资料放到附件中")
        requirement = archive.read(by_name[requirement_path]).decode("utf-8-sig").strip()
        if not requirement or len(requirement) > 24_000:
            raise ValueError("需求正文不能为空或超过 24000 字符")
        files = []
        for name, info in members:
            if not name.startswith(prefix):
                raise ValueError("需求包所有文件必须位于同一个文件夹内")
            relative = name[len(prefix):]
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            files.append({"path": f"{PACKAGE_DIRECTORY}/{relative}", "size": info.file_size})
        return manifest, requirement, sorted(files, key=lambda item: item["path"])


async def import_package(source: BinaryIO, workspace_root: Path, store: WorkflowStorage) -> dict:
    root = workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    application_id = str(uuid4())
    workspace = root / application_id
    # All ZIP checks and extraction complete before an application becomes visible.
    with tempfile.TemporaryDirectory(prefix=".requirement-import-", dir=root) as temp:
        staged = Path(temp) / PACKAGE_DIRECTORY
        staged.mkdir()
        manifest, requirement, files = await asyncio.to_thread(unpack_package, source, staged)
        requirement += (
            f"\n\n企业提供的资料：{PACKAGE_DIRECTORY}/，共 {len(files)} 个文件。"
            f"原始诉求：{PACKAGE_DIRECTORY}/{manifest.requirement_file}。"
        )
        request = ApplicationCreateRequest(name=manifest.name, description=manifest.description,
                                           requirement=requirement)
        workspace.mkdir()
        try:
            staged.rename(workspace / PACKAGE_DIRECTORY)
            application = await store.create_application(request, application_id=application_id)
        except BaseException:
            shutil.rmtree(workspace)
            raise
    return {"application": application, "package": {
        "name": manifest.name, "requirement_file": f"{PACKAGE_DIRECTORY}/{manifest.requirement_file}",
        "file_count": len(files), "size_bytes": sum(f["size"] for f in files), "files": files,
    }}
