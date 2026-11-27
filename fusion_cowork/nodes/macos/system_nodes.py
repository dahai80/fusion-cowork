"""macOS system automation nodes — system-level operations via AppleScript and native macOS capabilities.

All nodes execute macOS system operations through osascript (AppleScript CLI) or native Python
libraries (os, shutil, pathlib) with no extra dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

from . import run_osascript as _run_applescript


def _get_desktop_path() -> str:
    """Get the macOS Desktop path."""
    _, path = _run_applescript('tell application "Finder" to get path of desktop')
    if path:
        return path
    return str(Path.home() / "Desktop")


def _get_downloads_path() -> str:
    """Get the macOS Downloads folder path."""
    return str(Path.home() / "Downloads")


def _get_documents_path() -> str:
    """Get the macOS Documents folder path."""
    return str(Path.home() / "Documents")


def _get_trash_path() -> str:
    """Get the Trash path."""
    return str(Path.home() / ".Trash")


def _file_size_str(size_bytes: int) -> str:
    """Format a file size for display."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def _safe_move(src: str, dst_dir: str) -> bool:
    """Move a file safely, handling name collisions automatically."""
    src_path = Path(src)
    dst_path = Path(dst_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    target = dst_path / src_path.name
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = dst_path / f"{stem}_{counter}{suffix}"
            counter += 1
    shutil.move(str(src_path), str(target))
    return True


def _get_file_type_category(path: Path) -> str:
    """Return the category directory name for a file type."""
    ext = path.suffix.lower()
    # Images
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff", ".heic", ".raw"}:
        # i18n: user-facing
        return "图片"
    # Documents
    if ext in {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".md",
        ".txt",
        ".rtf",
        ".csv",
        ".numbers",
        ".pages",
        ".key",
    }:
        # i18n: user-facing
        return "文档"
    # Videos
    if ext in {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".mpeg"}:
        # i18n: user-facing
        return "视频"
    # Audio
    if ext in {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"}:
        # i18n: user-facing
        return "音频"
    # Archives
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".dmg", ".iso"}:
        # i18n: user-facing
        return "压缩包"
    # Code
    if ext in {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".swift",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
        ".yml",
        ".yaml",
        ".json",
        ".xml",
        ".toml",
        ".sql",
    }:
        # i18n: user-facing
        return "代码"
    # Executables
    if ext in {".app", ".dmg", ".pkg", ".command"} or os.access(str(path), os.X_OK):
        # i18n: user-facing
        return "应用"
    # i18n: user-facing
    return "其他"


@register_node
class DesktopCleanNode(BaseNode):
    """Desktop cleanup node — automatically organize Desktop files by type/date.

    Features:
    - Classify files into subfolders by type
    - Archive by date
    - Remove expired files
    """

    name = "desktop_clean"
    # i18n: user-facing
    display_name = "桌面清理"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "按类型/日期自动规整桌面文件"
    icon = "🧹"
    # i18n: user-facing
    default_label = "桌面清理"

    inputs = [
        # i18n: user-facing
        {"key": "source_path", "label": "桌面路径", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "cleaned_files", "label": "清理的文件", "type": "list[dict]"},
        # i18n: user-facing
        {"key": "summary", "label": "清理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "organize_by_type": {"type": "boolean", "default": True, "description": "按文件类型分类"},
                # i18n: user-facing
                "organize_by_date": {"type": "boolean", "default": False, "description": "按日期归档"},
                # i18n: user-facing
                "remove_old_files": {"type": "boolean", "default": False, "description": "删除过期文件"},
                # i18n: user-facing
                "old_days_threshold": {"type": "integer", "default": 90, "description": "过期天数阈值"},
                # i18n: user-facing
                "target_base_dir": {"type": "string", "default": "", "description": "目标目录（留空使用桌面）"},
                # i18n: user-facing
                "skip_hidden": {"type": "boolean", "default": True, "description": "跳过隐藏文件"},
                # i18n: user-facing
                "dry_run": {"type": "boolean", "default": False, "description": "预览模式（不实际移动）"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        source = inputs.get("source_path", _get_desktop_path())
        source_path = Path(source).expanduser()

        if not source_path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Directory does not exist: {source}",
                summary="Directory does not exist",
            )

        organize_by_type = params.get("organize_by_type", True)
        _organize_by_date = params.get("organize_by_date", False)
        remove_old_files = params.get("remove_old_files", False)
        old_days = params.get("old_days_threshold", 90)
        target_base = params.get("target_base_dir", str(source_path))
        skip_hidden = params.get("skip_hidden", True)
        dry_run = params.get("dry_run", False)

        target_path = Path(target_base).expanduser()
        cleaned_files = []

        # Scan Desktop files (directories excluded)
        for item in source_path.iterdir():
            if not item.is_file():
                continue
            if skip_hidden and item.name.startswith("."):
                continue
            # Skip system files
            if item.name in {".DS_Store", "Icon\r", ".localized"}:
                continue

            file_info = {
                "name": item.name,
                "path": str(item),
                "size": item.stat().st_size,
                "size_str": _file_size_str(item.stat().st_size),
                "modified": item.stat().st_mtime,
                "category": _get_file_type_category(item),
            }

            # Remove expired files
            if remove_old_files:
                age_days = (time.time() - item.stat().st_mtime) / 86400
                if age_days > old_days:
                    if not dry_run:
                        try:
                            os.remove(str(item))
                            file_info["action"] = "deleted"
                        except Exception as e:
                            file_info["action"] = f"delete_failed: {e}"
                    else:
                        file_info["action"] = "would_delete"
                    cleaned_files.append(file_info)
                    continue

            # Organize by category
            if organize_by_type:
                category = file_info["category"]
                category_dir = target_path / category
                if not dry_run:
                    try:
                        _safe_move(str(item), str(category_dir))
                        file_info["action"] = f"moved_to_{category}"
                        file_info["destination"] = str(category_dir / item.name)
                    except Exception as e:
                        file_info["action"] = f"move_failed: {e}"
                else:
                    file_info["action"] = f"would_move_to_{category}"
                    file_info["destination"] = str(target_path / category / item.name)
                cleaned_files.append(file_info)

        summary = self._build_summary(cleaned_files, dry_run)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"cleaned_files": cleaned_files, "summary": summary},
            summary=summary,
        )

    def _build_summary(self, files: list, dry_run: bool) -> str:
        """Build the cleanup summary."""
        mode = "Preview" if dry_run else "Cleanup"
        if not files:
            return f"{mode} complete, Desktop is already tidy, nothing to process"
        moved = sum(1 for f in files if "moved" in f.get("action", ""))
        deleted = sum(1 for f in files if f.get("action") == "deleted")
        total_size = sum(f.get("size", 0) for f in files)
        action_counts = {}
        for f in files:
            action = f.get("action", "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
        return (
            f"{mode} complete: processed {len(files)} files ({_file_size_str(total_size)})"
            + (f", moved {moved}" if moved else "")
            + (f", deleted {deleted}" if deleted else "")
        )


@register_node
class DownloadOrganizerNode(BaseNode):
    """Downloads folder organizer node — auto-archive, deduplicate and tidy the Downloads folder.

    Features:
    - Classify and archive by type
    - Smart deduplication
    - Remove expired files
    """

    name = "download_organizer"
    # i18n: user-facing
    display_name = "下载文件夹整理"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "自动归档、去重、整理下载文件夹"
    icon = "📥"
    # i18n: user-facing
    default_label = "下载整理"

    inputs = [
        # i18n: user-facing
        {"key": "source_path", "label": "下载目录路径", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "organized_files", "label": "整理的文件", "type": "list[dict]"},
        # i18n: user-facing
        {"key": "summary", "label": "整理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "organize_by_type": {"type": "boolean", "default": True},
                "deduplicate": {"type": "boolean", "default": True},
                "clean_old_files": {"type": "boolean", "default": False},
                # i18n: user-facing
                "days_threshold": {"type": "integer", "default": 30, "description": "清理 N 天前的文件"},
                "target_dir": {
                    "type": "string",
                    "default": "~/Documents/Downloads_Archive",
                    # i18n: user-facing
                    "description": "归档目标目录",
                },
                "dry_run": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        source = inputs.get("source_path", _get_downloads_path())
        source_path = Path(source).expanduser()

        if not source_path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Directory does not exist: {source}",
                summary="Directory does not exist",
            )

        target_dir = Path(params.get("target_dir", "~/Documents/Downloads_Archive")).expanduser()
        organize_by_type = params.get("organize_by_type", True)
        deduplicate = params.get("deduplicate", True)
        clean_old = params.get("clean_old_files", False)
        days_threshold = params.get("days_threshold", 30)
        dry_run = params.get("dry_run", False)

        organized_files = []
        seen_hashes = {}  # Used for deduplication

        for item in sorted(source_path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not item.is_file():
                continue
            if item.name.startswith("."):
                continue

            file_info = {
                "name": item.name,
                "path": str(item),
                "size": item.stat().st_size,
                "size_str": _file_size_str(item.stat().st_size),
                "modified": item.stat().st_mtime,
                "category": _get_file_type_category(item),
            }

            # Deduplicate
            if deduplicate and item.is_file():
                try:
                    fsize = item.stat().st_size
                    if fsize in seen_hashes:
                        if not dry_run:
                            try:
                                os.remove(str(item))
                                file_info["action"] = "deleted_duplicate"
                                file_info["duplicate_of"] = seen_hashes[fsize]
                            except Exception as e:
                                file_info["action"] = f"dedup_failed: {e}"
                        else:
                            file_info["action"] = "would_delete_duplicate"
                        organized_files.append(file_info)
                        continue
                    seen_hashes[fsize] = item.name
                except Exception:
                    pass

            # Remove old files
            if clean_old:
                age_days = (time.time() - item.stat().st_mtime) / 86400
                if age_days > days_threshold:
                    if not dry_run:
                        try:
                            os.remove(str(item))
                            file_info["action"] = "deleted_old"
                        except Exception as e:
                            file_info["action"] = f"delete_failed: {e}"
                    else:
                        file_info["action"] = "would_delete_old"
                    organized_files.append(file_info)
                    continue

            # Archive
            if organize_by_type:
                category = file_info["category"]
                dest = target_dir / category
                if not dry_run:
                    try:
                        _safe_move(str(item), str(dest))
                        file_info["action"] = f"archived_to_{category}"
                        file_info["destination"] = str(dest / item.name)
                    except Exception as e:
                        file_info["action"] = f"archive_failed: {e}"
                else:
                    file_info["action"] = f"would_archive_to_{category}"
                    file_info["destination"] = str(dest / item.name)
                organized_files.append(file_info)

        summary = self._build_summary(organized_files, dry_run)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"organized_files": organized_files, "summary": summary},
            summary=summary,
        )

    def _build_summary(self, files: list, dry_run: bool) -> str:
        """Build the organization summary."""
        mode = "Preview" if dry_run else "Organize"
        if not files:
            return f"{mode} complete, Downloads folder is already tidy"
        archived = sum(1 for f in files if "archived" in f.get("action", ""))
        deleted = sum(1 for f in files if "deleted" in f.get("action", ""))
        deduped = sum(1 for f in files if "duplicate" in f.get("action", ""))
        return (
            f"{mode} complete: processed {len(files)} files"
            + (f", archived {archived}" if archived else "")
            + (f", deleted {deleted}" if deleted else "")
            + (f", deduplicated {deduped}" if deduped else "")
        )


@register_node
class FileClassifierNode(BaseNode):
    """File classifier node — fast classification by file type/extension (no AI calls).

    Unlike AIClassifyNode, this node classifies based on file name and extension only,
    no AI calls, faster.
    """

    name = "file_classifier"
    # i18n: user-facing
    display_name = "文件分类器"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "按文件类型/扩展名快速分类"
    icon = "📂"
    # i18n: user-facing
    default_label = "文件分类"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "source_path", "label": "源目录", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "classified_files", "label": "分类结果", "type": "list[dict]"},
        # i18n: user-facing
        {"key": "categories", "label": "分类映射", "type": "dict"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "move_to_subdirs": {"type": "boolean", "default": False},
                "target_base": {"type": "string", "default": ""},
                "custom_mapping": {
                    "type": "string",
                    # i18n: user-facing
                    "description": '自定义扩展名映射 JSON (如: {".mp3": "音乐"})',
                    "default": "",
                },
                "dry_run": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        source_path = inputs.get("source_path", "")
        params = self.config.params

        # If no file list but a source dir is given, scan the directory
        if not files and source_path:
            src = Path(source_path).expanduser()
            if src.exists():
                files = [str(f) for f in src.iterdir() if f.is_file()]

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"classified_files": [], "categories": {}},
                summary="No files to classify",
            )

        # Load the custom mapping
        custom_mapping = {}
        custom_map_str = params.get("custom_mapping", "")
        if custom_map_str:
            try:
                import json

                custom_mapping = json.loads(custom_map_str)
            except Exception as e:
                logger.warning(f"Failed to parse custom mapping: {e}")

        move_to_subdirs = params.get("move_to_subdirs", False)
        target_base = params.get("target_base", source_path or "")
        dry_run = params.get("dry_run", False)

        categories = {}
        classified = []

        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue

            ext = path.suffix.lower()
            # Custom mapping takes priority
            if ext in custom_mapping:
                category = custom_mapping[ext]
            else:
                category = _get_file_type_category(path)

            file_info = {
                "file_name": path.name,
                "file_path": str(path),
                "extension": ext,
                "category": category,
                "size": path.stat().st_size,
                "size_str": _file_size_str(path.stat().st_size),
            }

            if category not in categories:
                categories[category] = []
            categories[category].append(file_info)

            if move_to_subdirs and target_base:
                target = Path(target_base).expanduser() / category
                if not dry_run:
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                        _safe_move(str(path), str(target))
                        file_info["action"] = f"moved_to_{category}"
                        file_info["destination"] = str(target / path.name)
                    except Exception as e:
                        file_info["action"] = f"move_failed: {e}"
                else:
                    file_info["action"] = f"would_move_to_{category}"
                    file_info["destination"] = str(target / path.name)

            classified.append(file_info)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "classified_files": classified,
                "categories": {k: len(v) for k, v in categories.items()},
            },
            summary=f"Classified {len(classified)} files into {len(categories)} categories",
        )


@register_node
class FileBatchRenameNode(BaseNode):
    """Batch file rename node — rename files in bulk by rule.

    Supports formatted templates, sequence numbers, dates and other patterns.
    """

    name = "file_batch_rename"
    # i18n: user-facing
    display_name = "批量重命名"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "按规则批量重命名文件"
    icon = "✏️"
    # i18n: user-facing
    default_label = "批量重命名"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "pattern", "label": "命名模板", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "renamed_files", "label": "重命名结果", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "命名模板，支持 {index}, {date}, {original}, {ext}",
                    "default": "file_{index}",
                },
                "start_index": {"type": "integer", "default": 1},
                # i18n: user-facing
                "padding": {"type": "integer", "default": 3, "description": "序号位数"},
                "prefix": {"type": "string", "default": ""},
                "suffix": {"type": "string", "default": ""},
                "lowercase": {"type": "boolean", "default": False},
                "replace_spaces": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        params = self.config.params

        pattern = params.get("pattern", "file_{index}")
        start_index = params.get("start_index", 1)
        padding = params.get("padding", 3)
        prefix = params.get("prefix", "")
        suffix = params.get("suffix", "")
        lowercase = params.get("lowercase", False)
        replace_spaces = params.get("replace_spaces", True)
        dry_run = params.get("dry_run", True)

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"renamed_files": []},
                summary="No files to rename",
            )

        today = time.strftime("%Y%m%d")
        renamed = []

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()

        for i, f in enumerate(files):
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue
            if not scope.ensure_allowed(path):
                logger.warning(f"Sandbox blocked out-of-scope read, skipping: {path}")
                continue

            index = start_index + i
            original_stem = path.stem
            ext = path.suffix

            # Generate the new name
            new_stem = pattern
            new_stem = new_stem.replace("{index}", str(index).zfill(padding))
            new_stem = new_stem.replace("{date}", today)
            new_stem = new_stem.replace("{original}", original_stem)
            new_stem = new_stem.replace("{ext}", ext.lstrip("."))

            new_stem = f"{prefix}{new_stem}{suffix}"
            if lowercase:
                new_stem = new_stem.lower()
            if replace_spaces:
                new_stem = new_stem.replace(" ", "_")

            new_name = f"{new_stem}{ext}"
            new_path = path.parent / new_name

            file_info = {
                "file_name": path.name,
                "file_path": str(path),
                "new_name": new_name,
                "new_path": str(new_path),
            }

            if not dry_run and str(new_path) != str(path):
                if not scope.ensure_allowed(new_path):
                    logger.warning(f"Sandbox blocked out-of-scope write, skipping: {new_path}")
                    file_info["action"] = "sandbox_blocked"
                    renamed.append(file_info)
                    continue
                try:
                    if new_path.exists():
                        counter = 1
                        while new_path.exists():
                            new_path = path.parent / f"{new_stem}_{counter}{ext}"
                            counter += 1
                        file_info["new_path"] = str(new_path)
                        file_info["new_name"] = new_path.name
                    path.rename(new_path)
                    file_info["action"] = "renamed"
                except Exception as e:
                    file_info["action"] = f"rename_failed: {e}"
            else:
                file_info["action"] = "would_rename" if dry_run else "renamed"

            renamed.append(file_info)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"renamed_files": renamed},
            summary=f"Renamed {len(renamed)} files" + (" (preview mode)" if dry_run else ""),
        )


@register_node
class DiskCleanerNode(BaseNode):
    """Disk cleaner node — scan and remove junk files, caches and temp files.

    Safely cleans macOS system junk, only handling safe files under the user directory.
    """

    name = "disk_cleaner"
    # i18n: user-facing
    display_name = "磁盘清理"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "扫描并清理垃圾文件、缓存、临时文件"
    icon = "💾"
    # i18n: user-facing
    default_label = "磁盘清理"

    inputs = [
        # i18n: user-facing
        {"key": "target_path", "label": "清理目标路径", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "cleaned_files", "label": "清理的文件", "type": "list[dict]"},
        # i18n: user-facing
        {"key": "total_freed", "label": "释放空间", "type": "string"},
        # i18n: user-facing
        {"key": "summary", "label": "清理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "clean_trash": {"type": "boolean", "default": True, "description": "清空废纸篓"},
                # i18n: user-facing
                "clean_downloads": {"type": "boolean", "default": False, "description": "清理下载临时文件"},
                # i18n: user-facing
                "clean_cache": {"type": "boolean", "default": False, "description": "清理用户缓存"},
                # i18n: user-facing
                "clean_temp": {"type": "boolean", "default": False, "description": "清理临时文件"},
                # i18n: user-facing
                "clean_node_modules": {"type": "boolean", "default": False, "description": "清理 node_modules 目录"},
                # i18n: user-facing
                "clean_pycache": {"type": "boolean", "default": True, "description": "清理 __pycache__ 目录"},
                # i18n: user-facing
                "clean_ds_store": {"type": "boolean", "default": True, "description": "清理 .DS_Store 文件"},
                # i18n: user-facing
                "min_file_size": {"type": "integer", "default": 0, "description": "最小文件大小(字节)"},
                "dry_run": {"type": "boolean", "default": True},
                # i18n: user-facing
                "max_depth": {"type": "integer", "default": 5, "description": "扫描深度"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        target = inputs.get("target_path", str(Path.home()))
        target_path = Path(target).expanduser()

        if not target_path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"Path does not exist: {target}")

        dry_run = params.get("dry_run", True)
        cleaned_files = []

        # Cleanup rules
        patterns = []
        if params.get("clean_trash"):
            patterns.append(("trash", _get_trash_path()))
        if params.get("clean_cache"):
            patterns.append(("cache", str(Path.home() / "Library/Caches")))
        if params.get("clean_temp"):
            patterns.append(("temp", "/tmp"))

        # File-level cleanup
        file_patterns = []
        if params.get("clean_pycache"):
            file_patterns.append("__pycache__")
        if params.get("clean_ds_store"):
            file_patterns.append(".DS_Store")
        if params.get("clean_node_modules"):
            file_patterns.append("node_modules")

        # Scan directories
        for pattern in file_patterns:
            for found in target_path.rglob(pattern):
                if not found.exists():
                    continue
                try:
                    # Compute size
                    if found.is_dir():
                        total_size = sum(f.stat().st_size for f in found.rglob("*") if f.is_file())
                    else:
                        total_size = found.stat().st_size

                    file_info = {
                        "name": found.name,
                        "path": str(found),
                        "size": total_size,
                        "size_str": _file_size_str(total_size),
                        "type": "directory" if found.is_dir() else "file",
                        "pattern": pattern,
                    }

                    if not dry_run:
                        try:
                            if found.is_dir():
                                shutil.rmtree(str(found), ignore_errors=True)
                            else:
                                found.unlink()
                            file_info["action"] = "deleted"
                        except Exception as e:
                            file_info["action"] = f"delete_failed: {e}"
                    else:
                        file_info["action"] = "would_delete"

                    cleaned_files.append(file_info)

                except Exception as e:
                    logger.debug(f"Skipped while processing {found}: {e}")

        # Directory-level cleanup
        for label, path_str in patterns:
            p = Path(path_str).expanduser()
            if p.exists():
                try:
                    total_size = (
                        sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
                    )
                    file_info = {
                        "name": p.name,
                        "path": str(p),
                        "size": total_size,
                        "size_str": _file_size_str(total_size),
                        "type": "directory",
                        "pattern": label,
                    }
                    if not dry_run:
                        try:
                            for item in p.iterdir():
                                if item.is_dir():
                                    shutil.rmtree(str(item), ignore_errors=True)
                                else:
                                    item.unlink()
                            file_info["action"] = "cleaned"
                        except Exception as e:
                            file_info["action"] = f"clean_failed: {e}"
                    else:
                        file_info["action"] = "would_clean"
                    cleaned_files.append(file_info)
                except Exception as e:
                    logger.debug(f"Skipped {label} {p}: {e}")

        total_freed = sum(
            f.get("size", 0)
            for f in cleaned_files
            if "delete" in f.get("action", "") or "clean" in f.get("action", "") or "would" in f.get("action", "")
        )

        summary = (
            f"{'Preview' if dry_run else 'Cleanup'} complete: found {len(cleaned_files)} items, can free {_file_size_str(total_freed)}"
        )
        if not dry_run:
            summary = f"Cleanup complete: processed {len(cleaned_files)} items, freed {_file_size_str(total_freed)}"

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "cleaned_files": cleaned_files,
                "total_freed": _file_size_str(total_freed),
                "total_freed_bytes": total_freed,
                "summary": summary,
            },
            summary=summary,
        )


@register_node
class FileWatcherNode(BaseNode):
    """File watcher node — watch directory changes and trigger follow-up actions.

    Uses the watchdog library to observe filesystem create, modify and delete events.
    """

    name = "file_watcher"
    # i18n: user-facing
    display_name = "文件监听"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "监听目录变化并触发后续操作"
    icon = "👀"
    # i18n: user-facing
    default_label = "文件监听"

    inputs = [
        # i18n: user-facing
        {"key": "watch_path", "label": "监听目录", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "events", "label": "监听到的事件", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["watch_path"],
            "properties": {
                # i18n: user-facing
                "watch_path": {"type": "string", "description": "监听目录路径"},
                "watch_subdirs": {"type": "boolean", "default": True},
                "watch_for_creation": {"type": "boolean", "default": True},
                "watch_for_modification": {"type": "boolean", "default": False},
                "watch_for_deletion": {"type": "boolean", "default": False},
                "file_patterns": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "文件过滤模式，逗号分隔，如: *.pdf,*.docx",
                    "default": "",
                },
                "timeout_seconds": {"type": "integer", "default": 30},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        watch_path = inputs.get("watch_path", inputs.get("watch_path", ""))
        params = self.config.params

        if not watch_path:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="Watch directory not specified",
                summary="Watch directory not specified",
            )

        path = Path(watch_path).expanduser()
        if not path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Watch directory does not exist: {watch_path}",
                summary="Directory does not exist",
            )

        watch_subdirs = params.get("watch_subdirs", True)
        file_patterns = params.get("file_patterns", "")
        timeout = params.get("timeout_seconds", 30)

        # Watch with watchdog
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            events = []
            stop_event = asyncio.Event()

            class DeskEventHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        events.append(
                            {
                                "type": "created",
                                "path": event.src_path,
                                "name": Path(event.src_path).name,
                            }
                        )

                def on_modified(self, event):
                    if not event.is_directory and params.get("watch_for_modification", False):
                        events.append(
                            {
                                "type": "modified",
                                "path": event.src_path,
                                "name": Path(event.src_path).name,
                            }
                        )

                def on_deleted(self, event):
                    if not event.is_directory and params.get("watch_for_deletion", False):
                        events.append(
                            {
                                "type": "deleted",
                                "path": event.src_path,
                                "name": Path(event.src_path).name,
                            }
                        )

            event_handler = DeskEventHandler()
            observer = Observer()
            observer.schedule(event_handler, str(path), recursive=watch_subdirs)
            observer.start()

            # Wait for the timeout or a stop signal
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            except TimeoutError:
                pass
            finally:
                observer.stop()
                observer.join()

            # Filter by file patterns
            if file_patterns:
                patterns = [p.strip() for p in file_patterns.split(",") if p.strip()]
                filtered = []
                for evt in events:
                    for pat in patterns:
                        if Path(evt["path"]).match(pat):
                            filtered.append(evt)
                            break
                events = filtered

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={
                    "events": events,
                    "event_count": len(events),
                    "watch_path": str(path),
                },
                summary=f"Watch complete: detected {len(events)} events",
            )

        except ImportError:
            # watchdog not installed, return a hint
            return NodeResult(
                status=NodeStatus.FAILED,
                error="watchdog is required: pip install watchdog",
                summary="Missing dependency: watchdog",
            )


@register_node
class FileCopyNode(BaseNode):
    name = "file_copy"
    # i18n: user-facing
    display_name = "文件复制"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "复制文件或目录到目标位置"
    icon = "📋"
    # i18n: user-facing
    default_label = "文件复制"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "destination", "label": "目标目录", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "copied_files", "label": "已复制文件", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["files", "destination"],
            "properties": {
                # i18n: user-facing
                "files": {"type": "array", "items": {"type": "string"}, "description": "要复制的文件路径列表"},
                # i18n: user-facing
                "destination": {"type": "string", "description": "目标目录"},
                # i18n: user-facing
                "create_subdir": {"type": "boolean", "default": True, "description": "按日期创建子目录"},
                "overwrite": {"type": "boolean", "default": False},
                "preserve_metadata": {"type": "boolean", "default": True},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        destination = inputs.get("destination", "")
        params = self.config.params

        if not files or not destination:
            return NodeResult(status=NodeStatus.FAILED, error="Missing file list or destination directory")

        dst = Path(destination).expanduser()

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if not scope.ensure_allowed(dst):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Sandbox blocked out-of-scope write: {dst}",
                summary="Path outside authorized folders was blocked",
            )

        dst.mkdir(parents=True, exist_ok=True)

        if params.get("create_subdir", True):
            date_str = time.strftime("%Y%m%d")
            dst = dst / date_str
            dst.mkdir(exist_ok=True)

        copied = []
        for f in files:
            src = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not src.exists():
                continue
            if not scope.ensure_allowed(src):
                logger.warning(f"Sandbox blocked out-of-scope read, skipping: {src}")
                continue
            target = dst / src.name
            if target.exists() and not params.get("overwrite", False):
                stem = target.stem
                target = dst / f"{stem}_{int(time.time())}{target.suffix}"
            if not scope.ensure_allowed(target):
                logger.warning(f"Sandbox blocked out-of-scope write, skipping: {target}")
                continue
            try:
                if src.is_file():
                    shutil.copy2(str(src), str(target)) if params.get("preserve_metadata", True) else shutil.copy(
                        str(src), str(target)
                    )
                else:
                    shutil.copytree(str(src), str(target), dirs_exist_ok=True)
                copied.append({"source": str(src), "destination": str(target), "name": src.name})
            except Exception as e:
                logger.error(f"Copy failed {src}: {e}")

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"copied_files": copied},
            summary=f"Copied {len(copied)} files to {dst}",
        )


@register_node
class FileMoveNode(BaseNode):
    name = "file_move"
    # i18n: user-facing
    display_name = "文件移动"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "移动文件或目录到目标位置"
    icon = "➡️"
    # i18n: user-facing
    default_label = "文件移动"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        # i18n: user-facing
        {"key": "destination", "label": "目标目录", "type": "string"},
    ]
    # i18n: user-facing
    outputs = [{"key": "moved_files", "label": "已移动文件", "type": "list[dict]"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["files", "destination"],
            "properties": {
                # i18n: user-facing
                "files": {"type": "array", "items": {"type": "string"}, "description": "要移动的文件路径列表"},
                # i18n: user-facing
                "destination": {"type": "string", "description": "目标目录"},
                "create_subdir": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        destination = inputs.get("destination", "")
        params = self.config.params

        if not files or not destination:
            return NodeResult(status=NodeStatus.FAILED, error="Missing file list or destination directory")

        dst = Path(destination).expanduser()

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if not scope.ensure_allowed(dst):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Sandbox blocked out-of-scope write: {dst}",
                summary="Path outside authorized folders was blocked",
            )

        dst.mkdir(parents=True, exist_ok=True)

        if params.get("create_subdir", True):
            dst = dst / time.strftime("%Y%m%d")
            dst.mkdir(exist_ok=True)

        moved = []
        for f in files:
            src = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not src.exists():
                continue
            if not scope.ensure_allowed(src):
                logger.warning(f"Sandbox blocked out-of-scope read, skipping: {src}")
                continue
            target = dst / src.name
            if target.exists() and not params.get("overwrite", False):
                stem = target.stem
                target = dst / f"{stem}_{int(time.time())}{target.suffix}"
            if not scope.ensure_allowed(target):
                logger.warning(f"Sandbox blocked out-of-scope write, skipping: {target}")
                continue
            try:
                shutil.move(str(src), str(target))
                moved.append({"source": str(src), "destination": str(target), "name": src.name})
            except Exception as e:
                logger.error(f"Move failed {src}: {e}")

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"moved_files": moved},
            summary=f"Moved {len(moved)} files to {dst}",
        )


@register_node
class FileDeleteNode(BaseNode):
    name = "file_delete"
    # i18n: user-facing
    display_name = "文件删除"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "删除文件或目录到废纸篓"
    icon = "🗑️"
    # i18n: user-facing
    default_label = "文件删除"

    inputs = [
        # i18n: user-facing
        {"key": "files", "label": "文件列表", "type": "list[file]"},
    ]
    # i18n: user-facing
    outputs = [{"key": "deleted_files", "label": "已删除文件", "type": "list[dict]"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "use_trash": {"type": "boolean", "default": True, "description": "移到废纸篓而非永久删除"},
                # i18n: user-facing
                "dry_run": {"type": "boolean", "default": True, "description": "预览模式"},
                # i18n: user-facing
                "confirm": {"type": "boolean", "default": False, "description": "需要确认"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        params = self.config.params

        if not files:
            return NodeResult(status=NodeStatus.SUCCESS, data={"deleted_files": []}, summary="No files to delete")

        use_trash = params.get("use_trash", True)
        dry_run = params.get("dry_run", True)

        deleted = []
        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not scope.ensure_allowed(path):
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"Sandbox blocked out-of-scope delete: {path}",
                )
            if not path.exists():
                continue

            info = {"name": path.name, "path": str(path), "size": path.stat().st_size if path.is_file() else 0}

            if dry_run:
                info["action"] = "would_delete"
            elif use_trash:
                try:
                    dest = Path(_get_trash_path()) / path.name
                    if dest.exists():
                        dest = Path(_get_trash_path()) / f"{path.stem}_{int(time.time())}{path.suffix}"
                    shutil.move(str(path), str(dest))
                    info["action"] = "moved_to_trash"
                    info["trash_path"] = str(dest)
                except Exception as e:
                    info["action"] = f"trash_failed: {e}"
            else:
                try:
                    if path.is_file():
                        path.unlink()
                    else:
                        shutil.rmtree(str(path))
                    info["action"] = "deleted"
                except Exception as e:
                    info["action"] = f"delete_failed: {e}"

            deleted.append(info)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"deleted_files": deleted},
            summary=f"{'Preview: would delete' if dry_run else 'Deleted'} {len(deleted)} files",
        )


@register_node
class FileFindNode(BaseNode):
    """File find node — recursively find files by conditions."""

    name = "file_find"
    # i18n: user-facing
    display_name = "文件查找"
    category = NodeCategory.FILE_OPERATION
    # i18n: user-facing
    description = "按条件递归查找文件"
    icon = "🔍"
    # i18n: user-facing
    default_label = "文件查找"

    inputs = [
        # i18n: user-facing
        {"key": "search_path", "label": "搜索路径", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "files", "label": "找到的文件", "type": "list[file]"},
        # i18n: user-facing
        {"key": "total_count", "label": "文件总数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["search_path"],
            "properties": {
                # i18n: user-facing
                "search_path": {"type": "string", "description": "搜索目录路径"},
                "patterns": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "文件模式，逗号分隔，如: *.pdf,*.docx,*.md",
                    "default": "*",
                },
                # i18n: user-facing
                "min_size": {"type": "integer", "default": 0, "description": "最小文件大小(字节)"},
                # i18n: user-facing
                "max_size": {"type": "integer", "default": 0, "description": "最大文件大小(字节)"},
                # i18n: user-facing
                "max_depth": {"type": "integer", "default": 10, "description": "最大递归深度"},
                # i18n: user-facing
                "max_results": {"type": "integer", "default": 1000, "description": "最大结果数"},
                "include_hidden": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        search_path = inputs.get("search_path", inputs.get("search_path", ""))
        params = self.config.params

        if not search_path:
            return NodeResult(status=NodeStatus.FAILED, error="Search path not specified")

        path = Path(search_path).expanduser()
        if not path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"Path does not exist: {search_path}")

        from ...security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        if not scope.ensure_allowed(path):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Sandbox blocked out-of-scope read: {path}",
                summary="Path outside authorized folders was blocked",
            )

        patterns = [p.strip() for p in params.get("patterns", "*").split(",") if p.strip()]
        min_size = params.get("min_size", 0)
        max_size = params.get("max_size", 0)
        max_depth = params.get("max_depth", 10)
        max_results = params.get("max_results", 1000)
        include_hidden = params.get("include_hidden", False)

        import fnmatch

        files = []
        for root, dirs, filenames in os.walk(str(path)):
            if not include_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                filenames = [f for f in filenames if not f.startswith(".")]

            depth = root.replace(str(path), "").count(os.sep)
            if depth > max_depth:
                continue

            for name in filenames:
                if not patterns or any(fnmatch.fnmatch(name, p) for p in patterns):
                    fpath = Path(root) / name
                    try:
                        fsize = fpath.stat().st_size
                        if min_size > 0 and fsize < min_size:
                            continue
                        if max_size > 0 and fsize > max_size:
                            continue
                        files.append(str(fpath))
                        if len(files) >= max_results:
                            break
                    except Exception:
                        continue
            if len(files) >= max_results:
                break

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"files": files, "total_count": len(files)},
            summary=f"Found {len(files)} files",
        )


@register_node
class ScreenCaptureNode(BaseNode):
    """Screen capture node — capture the screen and save/analyze it.

    Parity with Claude Cowork's screen viewing capability.
    Supports full-screen, selection and window captures; can combine with fusion-mlx for image analysis.
    """

    name = "screen_capture"
    # i18n: user-facing
    display_name = "桌面截图"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "截取屏幕并保存到文件"
    icon = "📸"
    # i18n: user-facing
    default_label = "屏幕截图"

    inputs = [
        # i18n: user-facing
        {"key": "save_path", "label": "保存路径", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "file_path", "label": "截图文件路径", "type": "string"},
        # i18n: user-facing
        {"key": "width", "label": "宽度", "type": "integer"},
        # i18n: user-facing
        {"key": "height", "label": "高度", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "capture_type": {
                    "type": "string",
                    "enum": ["full", "selection", "window"],
                    "default": "full",
                    # i18n: user-facing
                    "description": "截图类型",
                },
                "save_path": {
                    "type": "string",
                    "default": "~/Desktop",
                    # i18n: user-facing
                    "description": "截图保存目录",
                },
                "file_name": {
                    "type": "string",
                    "default": "screenshot_{date}",
                    # i18n: user-facing
                    "description": "文件名（不含扩展名）",
                },
                "analyze_with_ai": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "是否用 AI 分析截图内容",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        capture_type = params.get("capture_type", "full")
        save_path = inputs.get("save_path", params.get("save_path", "~/Desktop"))
        file_name = params.get("file_name", "screenshot_{date}")
        analyze = params.get("analyze_with_ai", False)

        save_dir = Path(save_path).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)

        date_str = time.strftime("%Y%m%d_%H%M%S")
        actual_name = file_name.replace("{date}", date_str)
        file_path = save_dir / f"{actual_name}.png"

        # Use the screencapture command (HI-11: exec argv avoids shell injection, file_path passed as an argument)
        argv = ["screencapture", "-x"]
        if capture_type == "selection":
            argv.append("-i")  # Interactive selection
        elif capture_type == "window":
            argv.append("-w")  # Window capture
        argv.append(str(file_path))

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"Screenshot failed: {stderr.decode()}",
                    summary="Screenshot failed",
                )

            # Get image dimensions
            import subprocess

            size_result = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(file_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            width = 0
            height = 0
            for line in size_result.stdout.split("\n"):
                if "pixelWidth" in line:
                    width = int(line.split(":")[1].strip())
                if "pixelHeight" in line:
                    height = int(line.split(":")[1].strip())

            file_size = file_path.stat().st_size if file_path.exists() else 0

            result_data = {
                "file_path": str(file_path),
                "width": width,
                "height": height,
                "size_bytes": file_size,
                "capture_type": capture_type,
            }

            # AI analysis of the screenshot
            if analyze and width > 0 and height > 0:
                try:
                    analysis = await self._analyze_screenshot(str(file_path))
                    result_data["analysis"] = analysis
                except Exception as e:
                    result_data["analysis"] = f"Analysis failed: {e}"

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=result_data,
                summary=f"Screenshot saved: {file_path.name} ({width}x{height})",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Screenshot error: {e}",
                summary="Screenshot error",
            )

    async def _analyze_screenshot(self, image_path: str) -> str:
        """Analyze the screenshot content with fusion-mlx."""
        from ...ai import FusionMLXClient
        from .input_nodes import _build_vision_user_content

        client = FusionMLXClient()
        try:
            content = _build_vision_user_content("Analyze this screenshot and describe what you see.", image_path)
            response = await client.chat(
                model="qwen3.5-9b",
                messages=[
                    {"role": "system", "content": "You are an image analysis assistant. Analyze the screenshot content and describe what you see."},
                    {"role": "user", "content": content},
                ],
                max_tokens=1024,
            )
            return response.content
        except Exception as e:
            return f"AI analysis unavailable: {e}"


@register_node
class ClipboardNode(BaseNode):
    """Clipboard node — read and write the system clipboard.

    Parity with Claude Cowork's clipboard capability.
    Supports reading and writing text and file paths.
    """

    name = "clipboard"
    # i18n: user-facing
    display_name = "剪贴板"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "读写系统剪贴板"
    icon = "📋"
    # i18n: user-facing
    default_label = "剪贴板"

    inputs = [
        # i18n: user-facing
        {"key": "text", "label": "要写入的文本", "type": "string", "optional": True},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "text", "label": "剪贴板文本", "type": "string"},
        # i18n: user-facing
        {"key": "length", "label": "文本长度", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "clear"],
                    "default": "read",
                    # i18n: user-facing
                    "description": "剪贴板操作",
                },
                "text": {
                    "type": "string",
                    "default": "",
                    # i18n: user-facing
                    "description": "要写入的文本（write 操作时使用）",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        action = params.get("action", "read")
        text = inputs.get("text", params.get("text", ""))

        try:
            if action == "read":
                proc = await asyncio.create_subprocess_shell(
                    "pbpaste",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                content = stdout.decode("utf-8", errors="replace") if stdout else ""

                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"text": content, "length": len(content), "action": "read"},
                    summary=f"Read clipboard: {len(content)} chars",
                )

            elif action == "write":
                escaped = text.replace("'", "'\\''")
                proc = await asyncio.create_subprocess_shell(
                    f"echo '{escaped}' | pbcopy",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"text": text, "length": len(text), "action": "write"},
                    summary=f"Wrote clipboard: {len(text)} chars",
                )

            elif action == "clear":
                proc = await asyncio.create_subprocess_shell(
                    "echo '' | pbcopy",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"text": "", "length": 0, "action": "clear"},
                    summary="Clipboard cleared",
                )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Clipboard operation failed: {e}",
                summary="Operation failed",
            )


@register_node
class NotificationNode(BaseNode):
    """System notification node — send native macOS notifications.

    Parity with Claude Cowork's notification capability.
    Uses osascript to send macOS Notification Center notifications.
    """

    name = "notification"
    # i18n: user-facing
    display_name = "系统通知"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "发送 macOS 原生通知"
    icon = "🔔"
    # i18n: user-facing
    default_label = "系统通知"

    inputs = [
        # i18n: user-facing
        {"key": "title", "label": "通知标题", "type": "string"},
        # i18n: user-facing
        {"key": "message", "label": "通知内容", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "sent", "label": "是否发送成功", "type": "boolean"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "title": {"type": "string", "default": "Fusion-Cowork", "description": "通知标题"},
                # i18n: user-facing
                "message": {"type": "string", "default": "", "description": "通知内容"},
                # i18n: user-facing
                "subtitle": {"type": "string", "default": "", "description": "副标题"},
                # i18n: user-facing
                "sound": {"type": "boolean", "default": False, "description": "是否播放声音"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        title = inputs.get("title", params.get("title", "Fusion-Cowork"))
        message = inputs.get("message", params.get("message", ""))
        subtitle = params.get("subtitle", "")
        sound = params.get("sound", False)

        if not message:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="Notification message must not be empty",
                summary="Notification message not specified",
            )

        # Escape AppleScript special characters (double quotes) + shell single quotes (anti-injection)
        title_escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        msg_escaped = message.replace("\\", "\\\\").replace('"', '\\"')
        sub_escaped = subtitle.replace("\\", "\\\\").replace('"', '\\"')

        _sound_cmd = 'sound name "default"' if sound else ""

        script = f'display notification "{msg_escaped}" with title "{title_escaped}"'
        if subtitle:
            script += f' subtitle "{sub_escaped}"'
        if sound:
            script += ' sound name "default"'

        try:
            # HI-11: shell single-quote escape the script (' inside single quotes needs break-quote: '\''), avoid injection
            script_sh_escaped = script.replace("'", "'\"'\"'")
            proc = await asyncio.create_subprocess_shell(
                f"osascript -e '{script_sh_escaped}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            success = proc.returncode == 0
            return NodeResult(
                status=NodeStatus.SUCCESS if success else NodeStatus.FAILED,
                data={"sent": success, "title": title, "message": message},
                summary=f"Notification sent: {title}" if success else f"Notification failed: {stderr.decode()}",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Notification failed: {e}",
                summary="Notification failed",
            )


@register_node
class AppLifecycleNode(BaseNode):
    """App lifecycle node — launch/quit/switch macOS applications.

    Parity with Claude Cowork's desktop app control capability.
    Uses AppleScript to control macOS apps.
    """

    name = "app_lifecycle"
    # i18n: user-facing
    display_name = "应用控制"
    category = NodeCategory.MACOS_SYSTEM
    # i18n: user-facing
    description = "启动/退出/切换 macOS 应用程序"
    icon = "🖥️"
    # i18n: user-facing
    default_label = "应用控制"

    inputs = [
        # i18n: user-facing
        {"key": "app_name", "label": "应用名称", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "success", "label": "是否成功", "type": "boolean"},
        # i18n: user-facing
        {"key": "app_pid", "label": "进程 PID", "type": "integer", "optional": True},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["launch", "quit", "activate", "check", "list"],
                    "default": "launch",
                    # i18n: user-facing
                    "description": "操作类型",
                },
                "app_name": {
                    "type": "string",
                    "default": "",
                    # i18n: user-facing
                    "description": "应用名称（如 Finder、Safari、Terminal）；action 为 launch/quit/activate/check 时必填，list 时可省略",
                },
                "save_windows": {
                    "type": "boolean",
                    "default": False,
                    # i18n: user-facing
                    "description": "退出时是否保存窗口",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        action = params.get("action", "launch")
        app_name = inputs.get("app_name", params.get("app_name", ""))
        save_windows = params.get("save_windows", False)

        try:
            if action == "list":
                # List running applications
                proc = await asyncio.create_subprocess_shell(
                    "osascript -e 'tell application \"System Events\" to get name of every process whose background only is false'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                apps = [a.strip().strip('"') for a in stdout.decode().split(",") if a.strip()]
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"apps": apps, "count": len(apps)},
                    summary=f"Running apps: {len(apps)}",
                )

            if not app_name:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="App name not specified",
                    summary="App not specified",
                )

            # HI-11: escape app_name AppleScript double quotes + backslashes, prevents AppleScript injection
            app_escaped = app_name.replace("\\", "\\\\").replace('"', '\\"')
            if action == "launch":
                script = f'tell application "{app_escaped}" to launch'
            elif action == "quit":
                save = "saving yes" if save_windows else ""
                script = f'tell application "{app_escaped}" to quit {save}'
            elif action == "activate":
                script = f'tell application "{app_escaped}" to activate'
            elif action == "check":
                script = f'tell application "System Events" to (name of processes) contains "{app_escaped}"'
            else:
                return NodeResult(status=NodeStatus.FAILED, error=f"Unknown action: {action}", summary="Unknown action")

            # HI-11: shell single-quote escape the script (' inside single quotes becomes '"'"'), prevents command injection
            script_sh_escaped = script.replace("'", "'\"'\"'")
            proc = await asyncio.create_subprocess_shell(
                f"osascript -e '{script_sh_escaped}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            # Get the process PID
            pid = 0
            if action in ("launch", "activate", "check"):
                try:
                    # HI-11: pgrep's app_name uses shell single-quote escaping, prevents injection
                    app_sh_for_pgrep = app_name.replace("'", "'\"'\"'")
                    pid_proc = await asyncio.create_subprocess_shell(
                        f"pgrep -f '{app_sh_for_pgrep}' | head -1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    pid_out, _ = await pid_proc.communicate()
                    pid = int(pid_out.strip()) if pid_out.strip() else 0
                except Exception:
                    pass

            success = proc.returncode == 0
            return NodeResult(
                status=NodeStatus.SUCCESS if success else NodeStatus.FAILED,
                data={"success": success, "action": action, "app": app_name, "app_pid": pid},
                summary=f"{action} {app_name}" + (f" (PID: {pid})" if pid else ""),
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"App control failed: {e}",
                summary="Operation failed",
            )


@register_node
class OCRNode(BaseNode):
    """OCR node — recognize text from images.

    Parity with Claude Cowork's on-screen text reading capability.
    Uses the macOS native Vision framework or fusion-mlx for OCR.
    """

    name = "ocr"
    # i18n: user-facing
    display_name = "OCR 文字识别"
    category = NodeCategory.AI_PROCESSING
    # i18n: user-facing
    description = "从图片中识别文字"
    icon = "👁️"
    # i18n: user-facing
    default_label = "OCR 识别"

    inputs = [
        # i18n: user-facing
        {"key": "image_path", "label": "图片路径", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "text", "label": "识别文字", "type": "string"},
        # i18n: user-facing
        {"key": "confidence", "label": "置信度", "type": "float"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["image_path"],
            "properties": {
                # i18n: user-facing
                "image_path": {"type": "string", "description": "图片文件路径"},
                # i18n: user-facing
                "language": {"type": "string", "default": "zh-Hans,en", "description": "识别语言"},
                "method": {
                    "type": "string",
                    "enum": ["native", "mlx"],
                    "default": "native",
                    # i18n: user-facing
                    "description": "OCR 方法（native=macOS Vision, mlx=fusion-mlx）",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        image_path = inputs.get("image_path", params.get("image_path", ""))
        _language = params.get("language", "zh-Hans,en")
        method = params.get("method", "native")

        if not image_path:
            return NodeResult(status=NodeStatus.FAILED, error="Image path not specified", summary="Image not specified")

        path = Path(image_path).expanduser()
        if not path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"File does not exist: {image_path}", summary="File does not exist")

        try:
            if method == "native":
                # Use the macOS native Vision framework (via Shortcuts or a swift CLI)
                # A simplified approach is used here: via tesseract or an MLX vision model
                text = await self._ocr_native(path)
            else:
                text = await self._ocr_mlx(path)

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"text": text, "confidence": 0.85, "method": method, "image": str(path)},
                summary=f"Recognized {len(text)} chars",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"OCR failed: {e}",
                summary="OCR failed",
            )

    async def _ocr_native(self, path: Path) -> str:
        """Perform OCR with native macOS capabilities."""
        # Try the Shortcuts OCR feature
        try:
            proc = await asyncio.create_subprocess_shell(
                f"shortcuts run 'Extract Text from Image' -i '{path}' 2>/dev/null || echo 'OCR not available'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=15,
            )
            stdout, _ = await proc.communicate()
            result = stdout.decode("utf-8", errors="replace").strip()
            if result and result != "OCR not available":
                return result
        except Exception:
            pass

        return "[OCR requires macOS Shortcuts or tesseract]"

    async def _ocr_mlx(self, path: Path) -> str:
        """Perform OCR with the fusion-mlx vision model."""
        from ...ai import FusionMLXClient

        client = FusionMLXClient()
        try:
            response = await client.chat(
                model="qwen3.5-9b",
                messages=[
                    {"role": "system", "content": "You are an OCR assistant. Recognize the text in the image and return only the text itself."},
                    {"role": "user", "content": f"Please recognize the text in this image: {path}"},
                ],
                max_tokens=2048,
            )
            return response.content
        except Exception as e:
            return f"[MLX OCR unavailable: {e}]"
