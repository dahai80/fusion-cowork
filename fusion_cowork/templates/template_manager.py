"""Fusion-Cowork 内置模板 — 开箱即用的自动化模板。

每个模板是一个预定义的工作流定义，用户可一键运行。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine.workflow import Workflow

logger = logging.getLogger(__name__)

# 内置模板定义
BUILTIN_TEMPLATES = [
    {
        "id": "desktop_daily_cleanup",
        "name": "桌面每日规整",
        "description": "每天自动规整桌面文件，按类型分类到子文件夹",
        "category": "桌面清理",
        "tags": ["桌面", "清理", "分类", "日常"],
        "icon": "🧹",
        "difficulty": "简单",
        "estimated_time": "30秒",
        "workflow": {
            "name": "桌面每日规整",
            "description": "按文件类型自动规整桌面文件",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": False,
                            "file_patterns": "*",
                            "include_hidden": False,
                            "sort_by": "type"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "desktop_clean",
                    "config": {
                        "params": {
                            "organize_by_type": True,
                            "organize_by_date": False,
                            "remove_old_files": False,
                            "skip_hidden": True,
                            "dry_run": False
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"}
            ]
        }
    },
    {
        "id": "download_organizer",
        "name": "下载文件夹自动归档",
        "description": "自动整理下载文件夹，按类型归档、去重、清理过期文件",
        "category": "文件整理",
        "tags": ["下载", "归档", "去重", "清理"],
        "icon": "📥",
        "difficulty": "简单",
        "estimated_time": "1分钟",
        "workflow": {
            "name": "下载文件夹自动归档",
            "description": "自动整理下载文件夹，按类型归档、去重",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Downloads",
                            "recursive": False,
                            "file_patterns": "*",
                            "sort_by": "date",
                            "sort_order": "desc"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "download_organizer",
                    "config": {
                        "params": {
                            "organize_by_type": True,
                            "deduplicate": True,
                            "clean_old_files": False,
                            "target_dir": "~/Documents/Downloads_Archive",
                            "dry_run": False
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"}
            ]
        }
    },
    {
        "id": "pdf_summarizer",
        "name": "PDF/文档批量总结",
        "description": "批量总结 PDF、Word、Markdown 文档，提取关键词和结论，生成汇总报告",
        "category": "AI 处理",
        "tags": ["PDF", "文档", "摘要", "AI", "批量"],
        "icon": "📝",
        "difficulty": "中等",
        "estimated_time": "2-5分钟",
        "needs_ai": True,
        "workflow": {
            "name": "文档批量AI摘要",
            "description": "批量总结文档并生成汇总报告",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": True,
                            "file_patterns": "*.pdf,*.docx,*.md,*.txt",
                            "sort_by": "name"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "ai_summarize",
                    "config": {
                        "params": {
                            "model": "",
                            "summary_length": "medium",
                            "extract_keywords": True,
                            "extract_conclusions": True,
                            "generate_report": True
                        }
                    }
                },
                {
                    "id": "n3",
                    "name": "file_output",
                    "config": {
                        "params": {
                            "output_path": "~/Desktop/Output",
                            "file_name": "文档摘要报告_{date}",
                            "format": "markdown"
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"}
            ]
        }
    },
    {
        "id": "file_classify_organize",
        "name": "AI 智能文件分类归档",
        "description": "AI 根据文件内容语义自动分类并归档到对应文件夹",
        "category": "AI 处理",
        "tags": ["AI", "分类", "归档", "智能"],
        "icon": "🏷️",
        "difficulty": "中等",
        "estimated_time": "1-3分钟",
        "needs_ai": True,
        "workflow": {
            "name": "AI 智能文件分类归档",
            "description": "AI 根据文件内容语义自动分类并归档",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": False,
                            "file_patterns": "*",
                            "sort_by": "name"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "ai_classify",
                    "config": {
                        "params": {
                            "model": "",
                            "classify_by_content": True,
                            "max_files_per_batch": 10
                        }
                    }
                },
                {
                    "id": "n3",
                    "name": "file_classifier",
                    "config": {
                        "params": {
                            "move_to_subdirs": True,
                            "target_base": "~/Desktop",
                            "dry_run": False
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"}
            ]
        }
    },
    {
        "id": "batch_ai_rename",
        "name": "AI 批量智能重命名",
        "description": "AI 根据文件内容智能生成规范的文件名，批量重命名",
        "category": "AI 处理",
        "tags": ["AI", "重命名", "批量", "智能"],
        "icon": "✏️",
        "difficulty": "中等",
        "estimated_time": "1-2分钟",
        "needs_ai": True,
        "workflow": {
            "name": "AI 批量智能重命名",
            "description": "AI 根据内容智能生成规范文件名",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": False,
                            "file_patterns": "*",
                            "sort_by": "name"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "ai_generate_name",
                    "config": {
                        "params": {
                            "model": "",
                            "use_content": True,
                            "lowercase": False,
                            "replace_spaces": True
                        }
                    }
                },
                {
                    "id": "n3",
                    "name": "file_batch_rename",
                    "config": {
                        "params": {
                            "dry_run": True
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"}
            ]
        }
    },
    {
        "id": "disk_cleanup",
        "name": "磁盘空间清理",
        "description": "扫描并清理缓存、临时文件、__pycache__、.DS_Store 等垃圾文件",
        "category": "系统维护",
        "tags": ["磁盘", "清理", "缓存", "系统"],
        "icon": "💾",
        "difficulty": "简单",
        "estimated_time": "1-3分钟",
        "workflow": {
            "name": "磁盘空间清理",
            "description": "扫描并清理系统垃圾文件",
            "nodes": [
                {
                    "id": "n1",
                    "name": "disk_cleaner",
                    "config": {
                        "params": {
                            "clean_trash": False,
                            "clean_cache": True,
                            "clean_temp": True,
                            "clean_pycache": True,
                            "clean_ds_store": True,
                            "clean_node_modules": False,
                            "dry_run": True,
                            "max_depth": 5
                        }
                    }
                }
            ],
            "edges": []
        }
    },
    {
        "id": "project_docs_collect",
        "name": "项目资料一键归集",
        "description": "递归扫描项目目录，按类型归集文档、代码、图片等资料",
        "category": "文件整理",
        "tags": ["项目", "归集", "整理", "批量"],
        "icon": "📂",
        "difficulty": "简单",
        "estimated_time": "30秒",
        "workflow": {
            "name": "项目资料一键归集",
            "description": "按类型归集项目资料",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": True,
                            "file_patterns": "*.pdf,*.docx,*.md,*.txt,*.py,*.js,*.ts,*.json,*.yaml,*.yml,*.toml,*.png,*.jpg,*.svg",
                            "sort_by": "type"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "file_classifier",
                    "config": {
                        "params": {
                            "move_to_subdirs": True,
                            "target_base": "~/Desktop/项目归集",
                            "dry_run": False
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"}
            ]
        }
    },
    {
        "id": "duplicate_finder",
        "name": "重复文件扫描清理",
        "description": "扫描指定目录中的重复文件，识别并清理",
        "category": "系统维护",
        "tags": ["去重", "清理", "重复文件"],
        "icon": "🔍",
        "difficulty": "中等",
        "estimated_time": "1-5分钟",
        "workflow": {
            "name": "重复文件扫描清理",
            "description": "扫描并清理重复文件",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": True,
                            "file_patterns": "*",
                            "sort_by": "size",
                            "sort_order": "desc"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "download_organizer",
                    "config": {
                        "params": {
                            "organize_by_type": False,
                            "deduplicate": True,
                            "clean_old_files": False,
                            "dry_run": True
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"}
            ]
        }
    },
    {
        "id": "log_cleaner",
        "name": "日志文件自动清洗",
        "description": "扫描并清理日志文件，汇总生成清理报告",
        "category": "系统维护",
        "tags": ["日志", "清洗", "清理", "系统"],
        "icon": "📋",
        "difficulty": "简单",
        "estimated_time": "30秒",
        "workflow": {
            "name": "日志文件自动清洗",
            "description": "扫描并清理日志文件",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": True,
                            "file_patterns": "*.log,*.log.*",
                            "sort_by": "date",
                            "sort_order": "desc"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "file_delete",
                    "config": {
                        "params": {
                            "use_trash": True,
                            "dry_run": True
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"}
            ]
        }
    },
    {
        "id": "image_batch_rename",
        "name": "图片批量重命名分类",
        "description": "批量重命名图片文件并按类型分类",
        "category": "文件整理",
        "tags": ["图片", "重命名", "分类", "批量"],
        "icon": "🖼️",
        "difficulty": "简单",
        "estimated_time": "30秒",
        "workflow": {
            "name": "图片批量重命名分类",
            "description": "批量重命名图片并按类型分类",
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop",
                            "recursive": False,
                            "file_patterns": "*.jpg,*.jpeg,*.png,*.gif,*.bmp,*.webp,*.svg,*.heic",
                            "sort_by": "name"
                        }
                    }
                },
                {
                    "id": "n2",
                    "name": "file_batch_rename",
                    "config": {
                        "params": {
                            "pattern": "IMG_{date}_{index}",
                            "start_index": 1,
                            "padding": 4,
                            "lowercase": True,
                            "replace_spaces": True,
                            "dry_run": True
                        }
                    }
                },
                {
                    "id": "n3",
                    "name": "file_classifier",
                    "config": {
                        "params": {
                            "move_to_subdirs": True,
                            "target_base": "~/Desktop/图片",
                            "dry_run": False
                        }
                    }
                }
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"}
            ]
        }
    }
]


class TemplateManager:
    """模板管理器 — 管理内置模板和用户自定义模板。

    功能：
    - 内置模板列表
    - 模板搜索/推荐（调用 AI）
    - 模板加载为工作流
    - 用户自定义模板保存/加载
    """

    def __init__(self, custom_dir: str = ""):
        self._custom_dir = Path(custom_dir).expanduser() if custom_dir else Path.home() / ".fusion-cowork" / "templates"
        self._custom_dir.mkdir(parents=True, exist_ok=True)

    def list_templates(
        self,
        category: str = "",
        tag: str = "",
        needs_ai: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """列出模板。

        Args:
            category: 按分类筛选
            tag: 按标签筛选
            needs_ai: 按是否需要 AI 筛选

        Returns:
            list[dict]: 模板列表
        """
        templates = list(BUILTIN_TEMPLATES)

        # 加载用户自定义模板
        for tpl_file in self._custom_dir.glob("*.json"):
            try:
                tpl = json.loads(tpl_file.read_text(encoding="utf-8"))
                templates.append(tpl)
            except Exception as e:
                logger.warning(f"加载自定义模板失败 {tpl_file}: {e}")

        # 筛选
        if category:
            templates = [t for t in templates if t.get("category", "") == category]
        if tag:
            templates = [t for t in templates if tag in t.get("tags", [])]
        if needs_ai is not None:
            templates = [t for t in templates if t.get("needs_ai", False) == needs_ai]

        return templates

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """获取指定模板的详细信息。"""
        for tpl in self.list_templates():
            if tpl.get("id") == template_id:
                return tpl
        return None

    def template_to_workflow(self, template_id: str) -> Optional[Workflow]:
        """将模板加载为可执行的工作流。

        Args:
            template_id: 模板 ID

        Returns:
            Optional[Workflow]: 工作流实例，如果模板不存在则返回 None
        """
        tpl = self.get_template(template_id)
        if not tpl:
            logger.error(f"模板不存在: {template_id}")
            return None

        workflow_data = tpl.get("workflow", {})
        wf = Workflow.from_dict(workflow_data)
        wf.id = f"tpl_{template_id}"
        return wf

    def save_custom_template(self, template: Dict[str, Any]) -> bool:
        """保存用户自定义模板。"""
        try:
            tpl_id = template.get("id", f"custom_{int(__import__('time').time())}")
            tpl_file = self._custom_dir / f"{tpl_id}.json"
            tpl_file.write_text(
                json.dumps(template, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"已保存自定义模板: {tpl_id}")
            return True
        except Exception as e:
            logger.error(f"保存自定义模板失败: {e}")
            return False

    def delete_custom_template(self, template_id: str) -> bool:
        """删除自定义模板。"""
        tpl_file = self._custom_dir / f"{template_id}.json"
        if tpl_file.exists():
            tpl_file.unlink()
            return True
        return False

    def get_categories(self) -> List[str]:
        """获取所有模板分类。"""
        categories = set()
        for tpl in self.list_templates():
            cat = tpl.get("category", "其他")
            categories.add(cat)
        return sorted(categories)

    def search_templates(self, query: str) -> List[Dict[str, Any]]:
        """搜索模板（按名称和描述匹配）。"""
        query = query.lower()
        results = []
        for tpl in self.list_templates():
            if (query in tpl.get("name", "").lower()
                    or query in tpl.get("description", "").lower()
                    or any(query in tag.lower() for tag in tpl.get("tags", []))):
                results.append(tpl)
        return results
