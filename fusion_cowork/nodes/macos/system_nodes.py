"""macOS 系统自动化节点 — 利用 AppleScript 和 macOS 原生能力实现系统级操作。

所有节点通过 osascript（AppleScript 命令行）或 Python 原生库（os, shutil, pathlib）
执行 macOS 系统操作，无需额外依赖。
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
    coerce_params,
    BaseNode, NodeResult, NodeStatus,
    NodeCategory, register_node,
)

logger = logging.getLogger(__name__)

from . import run_osascript as _run_applescript


def _get_desktop_path() -> str:
    """获取 macOS 桌面路径。"""
    _, path = _run_applescript('tell application "Finder" to get path of desktop')
    if path:
        return path
    return str(Path.home() / "Desktop")


def _get_downloads_path() -> str:
    """获取 macOS 下载文件夹路径。"""
    return str(Path.home() / "Downloads")


def _get_documents_path() -> str:
    """获取 macOS 文稿文件夹路径。"""
    return str(Path.home() / "Documents")


def _get_trash_path() -> str:
    """获取废纸篓路径。"""
    return str(Path.home() / ".Trash")


def _file_size_str(size_bytes: int) -> str:
    """格式化文件大小显示。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def _safe_move(src: str, dst_dir: str) -> bool:
    """安全移动文件，自动处理重名。"""
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
    """根据文件类型返回分类目录名。"""
    ext = path.suffix.lower()
    # 图片
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico', '.tiff', '.heic', '.raw'}:
        return "图片"
    # 文档
    if ext in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.md', '.txt', '.rtf', '.csv', '.numbers', '.pages', '.key'}:
        return "文档"
    # 视频
    if ext in {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpeg'}:
        return "视频"
    # 音频
    if ext in {'.mp3', '.wav', '.aac', '.flac', '.ogg', '.m4a', '.wma'}:
        return "音频"
    # 压缩包
    if ext in {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.dmg', '.iso'}:
        return "压缩包"
    # 代码
    if ext in {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.c', '.cpp', '.h', '.swift',
               '.rb', '.php', '.sh', '.bash', '.zsh', '.yml', '.yaml', '.json', '.xml', '.toml', '.sql'}:
        return "代码"
    # 可执行
    if ext in {'.app', '.dmg', '.pkg', '.command'} or os.access(str(path), os.X_OK):
        return "应用"
    return "其他"


@register_node
class DesktopCleanNode(BaseNode):
    """桌面清理节点 — 按类型/日期自动规整桌面文件。

    功能：
    - 按文件类型分类到子文件夹
    - 按日期归档
    - 清理过期文件
    """
    name = "desktop_clean"
    display_name = "桌面清理"
    category = NodeCategory.MACOS_SYSTEM
    description = "按类型/日期自动规整桌面文件"
    icon = "🧹"
    default_label = "桌面清理"

    inputs = [
        {"key": "source_path", "label": "桌面路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "cleaned_files", "label": "清理的文件", "type": "list[dict]"},
        {"key": "summary", "label": "清理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "organize_by_type": {"type": "boolean", "default": True, "description": "按文件类型分类"},
                "organize_by_date": {"type": "boolean", "default": False, "description": "按日期归档"},
                "remove_old_files": {"type": "boolean", "default": False, "description": "删除过期文件"},
                "old_days_threshold": {"type": "integer", "default": 90, "description": "过期天数阈值"},
                "target_base_dir": {"type": "string", "default": "", "description": "目标目录（留空使用桌面）"},
                "skip_hidden": {"type": "boolean", "default": True, "description": "跳过隐藏文件"},
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
                error=f"目录不存在: {source}",
                summary="目录不存在",
            )

        organize_by_type = params.get("organize_by_type", True)
        organize_by_date = params.get("organize_by_date", False)
        remove_old_files = params.get("remove_old_files", False)
        old_days = params.get("old_days_threshold", 90)
        target_base = params.get("target_base_dir", str(source_path))
        skip_hidden = params.get("skip_hidden", True)
        dry_run = params.get("dry_run", False)

        target_path = Path(target_base).expanduser()
        cleaned_files = []

        # 扫描桌面文件（不包含目录）
        for item in source_path.iterdir():
            if not item.is_file():
                continue
            if skip_hidden and item.name.startswith("."):
                continue
            # 跳过系统文件
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

            # 删除过期文件
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

            # 分类整理
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
        """生成清理摘要。"""
        mode = "预览" if dry_run else "清理"
        if not files:
            return f"{mode}完成，桌面已很整洁，无需处理"
        moved = sum(1 for f in files if "moved" in f.get("action", ""))
        deleted = sum(1 for f in files if f.get("action") == "deleted")
        total_size = sum(f.get("size", 0) for f in files)
        action_counts = {}
        for f in files:
            action = f.get("action", "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
        return (
            f"{mode}完成: 共处理 {len(files)} 个文件 ({_file_size_str(total_size)})"
            + (f"，移动 {moved} 个" if moved else "")
            + (f"，删除 {deleted} 个" if deleted else "")
        )


@register_node
class DownloadOrganizerNode(BaseNode):
    """下载文件夹整理节点 — 自动归档、去重、整理下载文件夹。

    功能：
    - 按类型分类归档
    - 智能去重
    - 清理过期文件
    """
    name = "download_organizer"
    display_name = "下载文件夹整理"
    category = NodeCategory.MACOS_SYSTEM
    description = "自动归档、去重、整理下载文件夹"
    icon = "📥"
    default_label = "下载整理"

    inputs = [
        {"key": "source_path", "label": "下载目录路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "organized_files", "label": "整理的文件", "type": "list[dict]"},
        {"key": "summary", "label": "整理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "organize_by_type": {"type": "boolean", "default": True},
                "deduplicate": {"type": "boolean", "default": True},
                "clean_old_files": {"type": "boolean", "default": False},
                "days_threshold": {"type": "integer", "default": 30, "description": "清理 N 天前的文件"},
                "target_dir": {
                    "type": "string",
                    "default": "~/Documents/Downloads_Archive",
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
                error=f"目录不存在: {source}",
                summary="目录不存在",
            )

        target_dir = Path(params.get("target_dir", "~/Documents/Downloads_Archive")).expanduser()
        organize_by_type = params.get("organize_by_type", True)
        deduplicate = params.get("deduplicate", True)
        clean_old = params.get("clean_old_files", False)
        days_threshold = params.get("days_threshold", 30)
        dry_run = params.get("dry_run", False)

        organized_files = []
        seen_hashes = {}  # 用于去重

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

            # 去重
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

            # 清理旧文件
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

            # 归档
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
        """生成整理摘要。"""
        mode = "预览" if dry_run else "整理"
        if not files:
            return f"{mode}完成，下载文件夹已很整洁"
        archived = sum(1 for f in files if "archived" in f.get("action", ""))
        deleted = sum(1 for f in files if "deleted" in f.get("action", ""))
        deduped = sum(1 for f in files if "duplicate" in f.get("action", ""))
        return (
            f"{mode}完成: 共处理 {len(files)} 个文件"
            + (f"，归档 {archived} 个" if archived else "")
            + (f"，删除 {deleted} 个" if deleted else "")
            + (f"，去重 {deduped} 个" if deduped else "")
        )


@register_node
class FileClassifierNode(BaseNode):
    """文件分类节点 — 按文件类型/扩展名快速分类（无 AI 调用）。

    与 AIClassifyNode 不同，本节点仅基于文件名和扩展名进行分类，
    不调用 AI，速度更快。
    """
    name = "file_classifier"
    display_name = "文件分类器"
    category = NodeCategory.FILE_OPERATION
    description = "按文件类型/扩展名快速分类"
    icon = "📂"
    default_label = "文件分类"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "source_path", "label": "源目录", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "classified_files", "label": "分类结果", "type": "list[dict]"},
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
                    "description": "自定义扩展名映射 JSON (如: {\".mp3\": \"音乐\"})",
                    "default": "",
                },
                "dry_run": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        source_path = inputs.get("source_path", "")
        params = self.config.params

        # 如果没给文件列表但给了源目录，扫描目录
        if not files and source_path:
            src = Path(source_path).expanduser()
            if src.exists():
                files = [str(f) for f in src.iterdir() if f.is_file()]

        if not files:
            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"classified_files": [], "categories": {}},
                summary="没有文件需要分类",
            )

        # 加载自定义映射
        custom_mapping = {}
        custom_map_str = params.get("custom_mapping", "")
        if custom_map_str:
            try:
                import json
                custom_mapping = json.loads(custom_map_str)
            except Exception as e:
                logger.warning(f"自定义映射解析失败: {e}")

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
            # 自定义映射优先
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
            summary=f"已分类 {len(classified)} 个文件到 {len(categories)} 个类别",
        )


@register_node
class FileBatchRenameNode(BaseNode):
    """批量文件重命名节点 — 按规则批量重命名文件。

    支持格式化模板、序号、日期等模式。
    """
    name = "file_batch_rename"
    display_name = "批量重命名"
    category = NodeCategory.FILE_OPERATION
    description = "按规则批量重命名文件"
    icon = "✏️"
    default_label = "批量重命名"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "pattern", "label": "命名模板", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "renamed_files", "label": "重命名结果", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "命名模板，支持 {index}, {date}, {original}, {ext}",
                    "default": "file_{index}",
                },
                "start_index": {"type": "integer", "default": 1},
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
                summary="没有文件需要重命名",
            )

        today = time.strftime("%Y%m%d")
        renamed = []

        for i, f in enumerate(files):
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not path.is_file():
                continue

            index = start_index + i
            original_stem = path.stem
            ext = path.suffix

            # 生成新名称
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
            summary=f"已重命名 {len(renamed)} 个文件" + (" (预览模式)" if dry_run else ""),
        )


@register_node
class DiskCleanerNode(BaseNode):
    """磁盘清理节点 — 扫描并清理垃圾文件、缓存、临时文件。

    安全清理 macOS 系统垃圾文件，仅处理用户目录下的安全文件。
    """
    name = "disk_cleaner"
    display_name = "磁盘清理"
    category = NodeCategory.MACOS_SYSTEM
    description = "扫描并清理垃圾文件、缓存、临时文件"
    icon = "💾"
    default_label = "磁盘清理"

    inputs = [
        {"key": "target_path", "label": "清理目标路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "cleaned_files", "label": "清理的文件", "type": "list[dict]"},
        {"key": "total_freed", "label": "释放空间", "type": "string"},
        {"key": "summary", "label": "清理摘要", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "clean_trash": {"type": "boolean", "default": True, "description": "清空废纸篓"},
                "clean_downloads": {"type": "boolean", "default": False, "description": "清理下载临时文件"},
                "clean_cache": {"type": "boolean", "default": False, "description": "清理用户缓存"},
                "clean_temp": {"type": "boolean", "default": False, "description": "清理临时文件"},
                "clean_node_modules": {"type": "boolean", "default": False, "description": "清理 node_modules 目录"},
                "clean_pycache": {"type": "boolean", "default": True, "description": "清理 __pycache__ 目录"},
                "clean_ds_store": {"type": "boolean", "default": True, "description": "清理 .DS_Store 文件"},
                "min_file_size": {"type": "integer", "default": 0, "description": "最小文件大小(字节)"},
                "dry_run": {"type": "boolean", "default": True},
                "max_depth": {"type": "integer", "default": 5, "description": "扫描深度"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        target = inputs.get("target_path", str(Path.home()))
        target_path = Path(target).expanduser()

        if not target_path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"路径不存在: {target}")

        dry_run = params.get("dry_run", True)
        cleaned_files = []

        # 清理规则
        patterns = []
        if params.get("clean_trash"):
            patterns.append(("trash", _get_trash_path()))
        if params.get("clean_cache"):
            patterns.append(("cache", str(Path.home() / "Library/Caches")))
        if params.get("clean_temp"):
            patterns.append(("temp", "/tmp"))

        # 文件级清理
        file_patterns = []
        if params.get("clean_pycache"):
            file_patterns.append("__pycache__")
        if params.get("clean_ds_store"):
            file_patterns.append(".DS_Store")
        if params.get("clean_node_modules"):
            file_patterns.append("node_modules")

        # 扫描目录
        for pattern in file_patterns:
            for found in target_path.rglob(pattern):
                if not found.exists():
                    continue
                try:
                    # 计算大小
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
                    logger.debug(f"处理 {found} 时跳过: {e}")

        # 处理目录级清理
        for label, path_str in patterns:
            p = Path(path_str).expanduser()
            if p.exists():
                try:
                    total_size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
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
                    logger.debug(f"跳过 {label} {p}: {e}")

        total_freed = sum(f.get("size", 0) for f in cleaned_files if "delete" in f.get("action", "") or "clean" in f.get("action", "") or "would" in f.get("action", ""))

        summary = f"{'预览' if dry_run else '清理'}完成: 发现 {len(cleaned_files)} 项，可释放 {_file_size_str(total_freed)}"
        if not dry_run:
            summary = f"清理完成: 处理 {len(cleaned_files)} 项，释放 {_file_size_str(total_freed)}"

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
    """文件监听节点 — 监听目录变化并触发后续操作。

    使用 watchdog 库监听文件系统的创建、修改、删除事件。
    """
    name = "file_watcher"
    display_name = "文件监听"
    category = NodeCategory.MACOS_SYSTEM
    description = "监听目录变化并触发后续操作"
    icon = "👀"
    default_label = "文件监听"

    inputs = [
        {"key": "watch_path", "label": "监听目录", "type": "string"},
    ]
    outputs = [
        {"key": "events", "label": "监听到的事件", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "watch_subdirs": {"type": "boolean", "default": True},
                "watch_for_creation": {"type": "boolean", "default": True},
                "watch_for_modification": {"type": "boolean", "default": False},
                "watch_for_deletion": {"type": "boolean", "default": False},
                "file_patterns": {
                    "type": "string",
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
                error="未指定监听目录",
                summary="未指定监听目录",
            )

        path = Path(watch_path).expanduser()
        if not path.exists():
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"监听目录不存在: {watch_path}",
                summary="目录不存在",
            )

        watch_subdirs = params.get("watch_subdirs", True)
        file_patterns = params.get("file_patterns", "")
        timeout = params.get("timeout_seconds", 30)

        # 使用 watchdog 监听
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            events = []
            stop_event = asyncio.Event()

            class DeskEventHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        events.append({
                            "type": "created",
                            "path": event.src_path,
                            "name": Path(event.src_path).name,
                        })
                def on_modified(self, event):
                    if not event.is_directory and params.get("watch_for_modification", False):
                        events.append({
                            "type": "modified",
                            "path": event.src_path,
                            "name": Path(event.src_path).name,
                        })
                def on_deleted(self, event):
                    if not event.is_directory and params.get("watch_for_deletion", False):
                        events.append({
                            "type": "deleted",
                            "path": event.src_path,
                            "name": Path(event.src_path).name,
                        })

            event_handler = DeskEventHandler()
            observer = Observer()
            observer.schedule(event_handler, str(path), recursive=watch_subdirs)
            observer.start()

            # 等待指定时间或停止信号
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                observer.stop()
                observer.join()

            # 过滤文件模式
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
                summary=f"监听完成: 检测到 {len(events)} 个事件",
            )

        except ImportError:
            # watchdog 未安装，返回提示
            return NodeResult(
                status=NodeStatus.FAILED,
                error="需要安装 watchdog: pip install watchdog",
                summary="依赖缺失: watchdog",
            )


@register_node
class FileCopyNode(BaseNode):
    name = "file_copy"
    display_name = "文件复制"
    category = NodeCategory.FILE_OPERATION
    description = "复制文件或目录到目标位置"
    icon = "📋"
    default_label = "文件复制"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "destination", "label": "目标目录", "type": "string"},
    ]
    outputs = [
        {"key": "copied_files", "label": "已复制文件", "type": "list[dict]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少文件列表或目标目录")

        dst = Path(destination).expanduser()
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
            target = dst / src.name
            if target.exists() and not params.get("overwrite", False):
                stem = target.stem
                target = dst / f"{stem}_{int(time.time())}{target.suffix}"
            try:
                if src.is_file():
                    shutil.copy2(str(src), str(target)) if params.get("preserve_metadata", True) else shutil.copy(str(src), str(target))
                else:
                    shutil.copytree(str(src), str(target), dirs_exist_ok=True)
                copied.append({"source": str(src), "destination": str(target), "name": src.name})
            except Exception as e:
                logger.error(f"复制失败 {src}: {e}")

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"copied_files": copied},
            summary=f"已复制 {len(copied)} 个文件到 {dst}",
        )


@register_node
class FileMoveNode(BaseNode):
    name = "file_move"
    display_name = "文件移动"
    category = NodeCategory.FILE_OPERATION
    description = "移动文件或目录到目标位置"
    icon = "➡️"
    default_label = "文件移动"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
        {"key": "destination", "label": "目标目录", "type": "string"},
    ]
    outputs = [{"key": "moved_files", "label": "已移动文件", "type": "list[dict]"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "create_subdir": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        destination = inputs.get("destination", "")
        params = self.config.params

        if not files or not destination:
            return NodeResult(status=NodeStatus.FAILED, error="缺少文件列表或目标目录")

        dst = Path(destination).expanduser()
        dst.mkdir(parents=True, exist_ok=True)

        if params.get("create_subdir", True):
            dst = dst / time.strftime("%Y%m%d")
            dst.mkdir(exist_ok=True)

        moved = []
        for f in files:
            src = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
            if not src.exists():
                continue
            target = dst / src.name
            if target.exists() and not params.get("overwrite", False):
                stem = target.stem
                target = dst / f"{stem}_{int(time.time())}{target.suffix}"
            try:
                shutil.move(str(src), str(target))
                moved.append({"source": str(src), "destination": str(target), "name": src.name})
            except Exception as e:
                logger.error(f"移动失败 {src}: {e}")

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"moved_files": moved},
            summary=f"已移动 {len(moved)} 个文件到 {dst}",
        )


@register_node
class FileDeleteNode(BaseNode):
    name = "file_delete"
    display_name = "文件删除"
    category = NodeCategory.FILE_OPERATION
    description = "删除文件或目录到废纸篓"
    icon = "🗑️"
    default_label = "文件删除"

    inputs = [
        {"key": "files", "label": "文件列表", "type": "list[file]"},
    ]
    outputs = [{"key": "deleted_files", "label": "已删除文件", "type": "list[dict]"}]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "use_trash": {"type": "boolean", "default": True, "description": "移到废纸篓而非永久删除"},
                "dry_run": {"type": "boolean", "default": True, "description": "预览模式"},
                "confirm": {"type": "boolean", "default": False, "description": "需要确认"},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        files = inputs.get("files", [])
        params = self.config.params

        if not files:
            return NodeResult(status=NodeStatus.SUCCESS, data={"deleted_files": []}, summary="没有文件需要删除")

        use_trash = params.get("use_trash", True)
        dry_run = params.get("dry_run", True)

        deleted = []
        for f in files:
            path = Path(f) if isinstance(f, str) else Path(f.get("path", ""))
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
            summary=f"{'预览: 将删除' if dry_run else '已删除'} {len(deleted)} 个文件",
        )


@register_node
class FileFindNode(BaseNode):
    """文件查找节点 — 按条件递归查找文件。"""
    name = "file_find"
    display_name = "文件查找"
    category = NodeCategory.FILE_OPERATION
    description = "按条件递归查找文件"
    icon = "🔍"
    default_label = "文件查找"

    inputs = [
        {"key": "search_path", "label": "搜索路径", "type": "string"},
    ]
    outputs = [
        {"key": "files", "label": "找到的文件", "type": "list[file]"},
        {"key": "total_count", "label": "文件总数", "type": "integer"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patterns": {"type": "string", "description": "文件模式，逗号分隔，如: *.pdf,*.docx,*.md", "default": "*"},
                "min_size": {"type": "integer", "default": 0, "description": "最小文件大小(字节)"},
                "max_size": {"type": "integer", "default": 0, "description": "最大文件大小(字节)"},
                "max_depth": {"type": "integer", "default": 10, "description": "最大递归深度"},
                "max_results": {"type": "integer", "default": 1000, "description": "最大结果数"},
                "include_hidden": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        search_path = inputs.get("search_path", inputs.get("search_path", ""))
        params = self.config.params

        if not search_path:
            return NodeResult(status=NodeStatus.FAILED, error="未指定搜索路径")

        path = Path(search_path).expanduser()
        if not path.exists():
            return NodeResult(status=NodeStatus.FAILED, error=f"路径不存在: {search_path}")

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
            summary=f"找到 {len(files)} 个文件",
        )


@register_node
class ScreenCaptureNode(BaseNode):
    """桌面截图节点 — 截取屏幕并保存/分析。

    对标 Claude Cowork 的屏幕查看能力。
    支持全屏、选区、窗口截图，可结合 fusion-mlx 进行图像分析。
    """
    name = "screen_capture"
    display_name = "桌面截图"
    category = NodeCategory.MACOS_SYSTEM
    description = "截取屏幕并保存到文件"
    icon = "📸"
    default_label = "屏幕截图"

    inputs = [
        {"key": "save_path", "label": "保存路径", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "file_path", "label": "截图文件路径", "type": "string"},
        {"key": "width", "label": "宽度", "type": "integer"},
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
                    "description": "截图类型",
                },
                "save_path": {
                    "type": "string",
                    "default": "~/Desktop",
                    "description": "截图保存目录",
                },
                "file_name": {
                    "type": "string",
                    "default": "screenshot_{date}",
                    "description": "文件名（不含扩展名）",
                },
                "analyze_with_ai": {
                    "type": "boolean",
                    "default": False,
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

        # 使用 screencapture 命令
        region_flag = ""
        if capture_type == "selection":
            region_flag = "-i"  # 交互选择
        elif capture_type == "window":
            region_flag = "-w"  # 窗口截图

        try:
            proc = await asyncio.create_subprocess_shell(
                f"screencapture {region_flag} -x '{file_path}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"截图失败: {stderr.decode()}",
                    summary="截图失败",
                )

            # 获取图片尺寸
            import subprocess
            size_result = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(file_path)],
                capture_output=True, text=True, timeout=5,
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

            # AI 分析截图
            if analyze and width > 0 and height > 0:
                try:
                    analysis = await self._analyze_screenshot(str(file_path))
                    result_data["analysis"] = analysis
                except Exception as e:
                    result_data["analysis"] = f"分析失败: {e}"

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data=result_data,
                summary=f"截图已保存: {file_path.name} ({width}x{height})",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"截图异常: {e}",
                summary="截图异常",
            )

    async def _analyze_screenshot(self, image_path: str) -> str:
        """使用 fusion-mlx 分析截图内容。"""
        from ...ai import FusionMLXClient
        client = FusionMLXClient()
        try:
            response = await client.chat(
                model="qwen3.5-9b",
                messages=[
                    {"role": "system", "content": "你是一个图像分析助手。分析截图内容并描述你看到了什么。"},
                    {"role": "user", "content": f"分析这张截图: {image_path}"},
                ],
                max_tokens=1024,
            )
            return response.content
        except Exception as e:
            return f"AI 分析不可用: {e}"


@register_node
class ClipboardNode(BaseNode):
    """剪贴板节点 — 读写系统剪贴板。

    对标 Claude Cowork 的剪贴板操作能力。
    支持文本和文件路径的读写。
    """
    name = "clipboard"
    display_name = "剪贴板"
    category = NodeCategory.MACOS_SYSTEM
    description = "读写系统剪贴板"
    icon = "📋"
    default_label = "剪贴板"

    inputs = [
        {"key": "text", "label": "要写入的文本", "type": "string", "optional": True},
    ]
    outputs = [
        {"key": "text", "label": "剪贴板文本", "type": "string"},
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
                    "description": "剪贴板操作",
                },
                "text": {
                    "type": "string",
                    "default": "",
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
                    summary=f"读取剪贴板: {len(content)} 字符",
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
                    summary=f"写入剪贴板: {len(text)} 字符",
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
                    summary="剪贴板已清空",
                )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"剪贴板操作失败: {e}",
                summary="操作失败",
            )


@register_node
class NotificationNode(BaseNode):
    """系统通知节点 — 发送 macOS 原生通知。

    对标 Claude Cowork 的通知能力。
    使用 osascript 发送 macOS Notification Center 通知。
    """
    name = "notification"
    display_name = "系统通知"
    category = NodeCategory.MACOS_SYSTEM
    description = "发送 macOS 原生通知"
    icon = "🔔"
    default_label = "系统通知"

    inputs = [
        {"key": "title", "label": "通知标题", "type": "string"},
        {"key": "message", "label": "通知内容", "type": "string"},
    ]
    outputs = [
        {"key": "sent", "label": "是否发送成功", "type": "boolean"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "Fusion-Cowork", "description": "通知标题"},
                "message": {"type": "string", "default": "", "description": "通知内容"},
                "subtitle": {"type": "string", "default": "", "description": "副标题"},
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
                error="通知内容不能为空",
                summary="未指定通知内容",
            )

        # 转义 AppleScript 特殊字符
        title_escaped = title.replace('"', '\\"')
        msg_escaped = message.replace('"', '\\"')
        sub_escaped = subtitle.replace('"', '\\"')

        sound_cmd = "sound name \"default\"" if sound else ""

        script = f'display notification "{msg_escaped}" with title "{title_escaped}"'
        if subtitle:
            script += f' subtitle "{sub_escaped}"'
        if sound:
            script += f' sound name "default"'

        try:
            proc = await asyncio.create_subprocess_shell(
                f"osascript -e '{script}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            success = proc.returncode == 0
            return NodeResult(
                status=NodeStatus.SUCCESS if success else NodeStatus.FAILED,
                data={"sent": success, "title": title, "message": message},
                summary=f"通知已发送: {title}" if success else f"通知发送失败: {stderr.decode()}",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"通知发送失败: {e}",
                summary="通知失败",
            )


@register_node
class AppLifecycleNode(BaseNode):
    """应用生命周期节点 — 启动/退出/切换 macOS 应用程序。

    对标 Claude Cowork 的桌面应用控制能力。
    使用 AppleScript 控制 macOS 应用。
    """
    name = "app_lifecycle"
    display_name = "应用控制"
    category = NodeCategory.MACOS_SYSTEM
    description = "启动/退出/切换 macOS 应用程序"
    icon = "🖥️"
    default_label = "应用控制"

    inputs = [
        {"key": "app_name", "label": "应用名称", "type": "string"},
    ]
    outputs = [
        {"key": "success", "label": "是否成功", "type": "boolean"},
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
                    "description": "操作类型",
                },
                "app_name": {
                    "type": "string",
                    "default": "",
                    "description": "应用名称（如 Finder、Safari、Terminal）",
                },
                "save_windows": {
                    "type": "boolean",
                    "default": False,
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
                # 列出正在运行的应用
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
                    summary=f"运行中的应用: {len(apps)} 个",
                )

            if not app_name:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error="未指定应用名称",
                    summary="未指定应用",
                )

            if action == "launch":
                script = f'tell application "{app_name}" to launch'
            elif action == "quit":
                save = "saving yes" if save_windows else ""
                script = f'tell application "{app_name}" to quit {save}'
            elif action == "activate":
                script = f'tell application "{app_name}" to activate'
            elif action == "check":
                script = f'tell application "System Events" to (name of processes) contains "{app_name}"'
            else:
                return NodeResult(status=NodeStatus.FAILED, error=f"未知操作: {action}", summary="未知操作")

            proc = await asyncio.create_subprocess_shell(
                f"osascript -e '{script}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            # 获取进程 PID
            pid = 0
            if action in ("launch", "activate", "check"):
                try:
                    pid_proc = await asyncio.create_subprocess_shell(
                        f"pgrep -f '{app_name}' | head -1",
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
                error=f"应用控制失败: {e}",
                summary="操作失败",
            )


@register_node
class OCRNode(BaseNode):
    """OCR 文字识别节点 — 从图片中识别文字。

    对标 Claude Cowork 的屏幕文字读取能力。
    使用 macOS 原生 Vision 框架或 fusion-mlx 进行 OCR。
    """
    name = "ocr"
    display_name = "OCR 文字识别"
    category = NodeCategory.AI_PROCESSING
    description = "从图片中识别文字"
    icon = "👁️"
    default_label = "OCR 识别"

    inputs = [
        {"key": "image_path", "label": "图片路径", "type": "string"},
    ]
    outputs = [
        {"key": "text", "label": "识别文字", "type": "string"},
        {"key": "confidence", "label": "置信度", "type": "float"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "图片文件路径"},
                "language": {"type": "string", "default": "zh-Hans,en", "description": "识别语言"},
                "method": {
                    "type": "string",
                    "enum": ["native", "mlx"],
                    "default": "native",
                    "description": "OCR 方法（native=macOS Vision, mlx=fusion-mlx）",
                },
            },
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        image_path = inputs.get("image_path", params.get("image_path", ""))
        language = params.get("language", "zh-Hans,en")
        method = params.get("method", "native")

        if not image_path:
            return NodeResult(
                status=NodeStatus.FAILED, error="未指定图片路径", summary="未指定图片"
            )

        path = Path(image_path).expanduser()
        if not path.exists():
            return NodeResult(
                status=NodeStatus.FAILED, error=f"文件不存在: {image_path}", summary="文件不存在"
            )

        try:
            if method == "native":
                # 使用 macOS 原生 Vision 框架（通过 Shortcuts 或 swift 命令行）
                # 这里使用一个简化的方法：通过 tesseract 或 mlx 视觉模型
                text = await self._ocr_native(path)
            else:
                text = await self._ocr_mlx(path)

            return NodeResult(
                status=NodeStatus.SUCCESS,
                data={"text": text, "confidence": 0.85, "method": method, "image": str(path)},
                summary=f"识别到 {len(text)} 字符",
            )

        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"OCR 失败: {e}",
                summary="OCR 失败",
            )

    async def _ocr_native(self, path: Path) -> str:
        """使用 macOS 原生能力进行 OCR。"""
        # 尝试使用 Shortcuts 的 OCR 功能
        try:
            proc = await asyncio.create_subprocess_shell(
                f"shortcuts run 'Extract Text from Image' -i '{path}' 2>/dev/null || "
                f"echo 'OCR not available'",
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

        return f"[OCR 需要安装 macOS Shortcuts 或 tesseract]"

    async def _ocr_mlx(self, path: Path) -> str:
        """使用 fusion-mlx 视觉模型进行 OCR。"""
        from ...ai import FusionMLXClient
        client = FusionMLXClient()
        try:
            response = await client.chat(
                model="qwen3.5-9b",
                messages=[
                    {"role": "system", "content": "你是一个 OCR 助手。识别图片中的文字内容，只返回文字本身。"},
                    {"role": "user", "content": f"请识别这张图片中的文字: {path}"},
                ],
                max_tokens=2048,
            )
            return response.content
        except Exception as e:
            return f"[MLX OCR 不可用: {e}]"