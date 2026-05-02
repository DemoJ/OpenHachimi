"""工作区文件写入和编辑工具。"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import normalize_relative_path, read_text_file, resolve_workspace_path


logger = logging.getLogger(__name__)


def write_file(
    ctx: RunContext[AppConfig],
    path: str,
    content: str,
    overwrite: bool = True,
) -> dict[str, object]:
    """在工作区内写入文件内容，可用于新建或覆盖文件。"""
    logger.info("tool write_file path=%s content_bytes=%d overwrite=%s", path, len(content.encode("utf-8")), overwrite)
    target_file = resolve_workspace_path(ctx.deps.base_dir, path)
    existed_before = target_file.exists()
    if target_file.exists() and target_file.is_dir():
        raise IsADirectoryError(f"目标是目录，不能直接写入：{path}")
    if target_file.exists() and not overwrite:
        raise FileExistsError(f"文件已存在，且 overwrite=False：{path}")

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(content, encoding="utf-8")

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "bytes_written": len(content.encode("utf-8")),
        "overwritten": existed_before,
    }


def make_directory(
    ctx: RunContext[AppConfig],
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
) -> dict[str, object]:
    """在工作区内创建目录。"""
    logger.info("tool make_directory path=%s parents=%s exist_ok=%s", path, parents, exist_ok)
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path)
    existed_before = target_dir.exists()
    if existed_before and not target_dir.is_dir():
        raise NotADirectoryError(f"目标已存在且不是目录：{path}")

    target_dir.mkdir(parents=parents, exist_ok=exist_ok)

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_dir),
        "created": not existed_before,
    }


def replace_in_file(
    ctx: RunContext[AppConfig],
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, object]:
    """在工作区文件中替换指定文本片段。"""
    logger.info("tool replace_in_file path=%s replace_all=%s", path, replace_all)
    if not old_text:
        raise ValueError("old_text 不能为空")

    target_file, original_text = read_text_file(ctx.deps.base_dir, path)
    match_count = original_text.count(old_text)
    if match_count == 0:
        raise ValueError("未找到需要替换的文本片段")
    if match_count > 1 and not replace_all:
        raise ValueError("匹配到多个位置，请将 replace_all 设为 true 后重试")

    updated_text = (
        original_text.replace(old_text, new_text)
        if replace_all
        else original_text.replace(old_text, new_text, 1)
    )
    target_file.write_text(updated_text, encoding="utf-8")

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "replacements": match_count if replace_all else 1,
    }


def delete_path(
    ctx: RunContext[AppConfig],
    path: str,
) -> dict[str, object]:
    """在工作区内安全地删除文件或文件夹（自动递归删除）。"""
    import shutil
    import os
    import stat
    
    def remove_readonly(func, file_path, excinfo):
        """移除只读属性并重试删除（解决 Windows 下删除 .git 目录报错的问题）。"""
        try:
            os.chmod(file_path, stat.S_IWRITE)
            func(file_path)
        except Exception:
            pass

    logger.info("tool delete_path path=%s", path)
    target_path = resolve_workspace_path(ctx.deps.base_dir, path)
    
    if not target_path.exists():
        return {"message": f"路径不存在，跳过删除：{path}", "deleted": False}
        
    try:
        if target_path.is_file() or target_path.is_symlink():
            try:
                target_path.unlink()
            except PermissionError:
                os.chmod(target_path, stat.S_IWRITE)
                target_path.unlink()
            deleted_type = "file"
        elif target_path.is_dir():
            shutil.rmtree(target_path, onerror=remove_readonly)
            deleted_type = "directory"
            
        return {
            "path": normalize_relative_path(ctx.deps.base_dir, target_path),
            "deleted": True,
            "type": deleted_type,
        }
    except Exception as e:
        raise RuntimeError(f"删除失败：{e}")
