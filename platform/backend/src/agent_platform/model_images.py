"""Load image references from the workflow's actual isolated workspace."""
import base64
from pathlib import Path

from .models import ContentBlock


def image_blocks(workspace: Path, images) -> list[ContentBlock]:
    if not isinstance(images, list) or len(images) > 8:
        raise ValueError('图片输入须为数组，每次最多 8 张；请逐页或分批读取')
    root = workspace.resolve()
    blocks = []
    total = 0
    for entry in images:
        name = entry.get('file_path') if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('图片需要当前工作目录内的相对 file_path')
        path = root / name
        if not path.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and parent.is_relative_to(root)):
            raise ValueError('图片不能指向项目外部或符号链接')
        if not path.is_file():
            raise ValueError(f'图片文件不存在：{name}')
        total += path.stat().st_size
        if total > 20 * 1024 * 1024:
            raise ValueError('本次图片合计超过 20 MB，请缩小图片或分批读取')
        data = path.read_bytes()
        media = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else None
        if not media:
            raise ValueError(f'图片仅支持 PNG/JPEG，请先渲染文档页面：{name}')
        blocks.append(ContentBlock(type='image', source={'type': 'base64', 'media_type': media, 'data': base64.b64encode(data).decode('ascii')}))
    return blocks
