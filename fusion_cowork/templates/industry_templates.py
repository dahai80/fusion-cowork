"""Industry automation templates — out-of-the-box templates for specific industries.

V0.3 features:
- Design industry templates (UI/UX, design asset organization, material management)
- Development industry templates (code cleanup, project init, log analysis)
- Data industry templates (data cleaning, format conversion, report generation)
- Ops industry templates (system monitoring, log rotation, backup management)
"""

from __future__ import annotations

from typing import Any, Dict, List

# Industry template definitions
INDUSTRY_TEMPLATES: List[Dict[str, Any]] = [
    # ── Design industry ──
    {
        "id": "design_asset_organizer",
        "name": "设计素材自动整理",  # i18n: user-facing
        "industry": "设计",  # i18n: user-facing
        "description": "按类型/项目自动分类设计素材文件（PSD/AI/Sketch/Figma 导出）",  # i18n: user-facing
        "icon": "🎨",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["设计", "素材", "分类", "归档"],  # i18n: user-facing
        "workflow": {
            "name": "设计素材自动整理",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {
                            "path": "~/Desktop/设计素材",  # i18n: user-facing
                            "file_patterns": "*.psd,*.ai,*.sketch,*.fig,*.png,*.jpg,*.svg",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "file_classifier",
                    "config": {
                        "params": {"move_to_subdirs": True, "target_base": "~/Desktop/设计素材_已归档"}  # i18n: user-facing
                    },  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    {
        "id": "design_batch_export",
        "name": "设计稿批量导出",  # i18n: user-facing
        "industry": "设计",  # i18n: user-facing
        "description": "批量导出设计稿为指定格式，自动生成预览图",  # i18n: user-facing
        "icon": "🖼️",
        "difficulty": "中等",  # i18n: user-facing
        "tags": ["设计", "导出", "批量"],  # i18n: user-facing
        "workflow": {
            "name": "设计稿批量导出",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {
                        "params": {"path": "~/Desktop/设计稿", "file_patterns": "*.sketch,*.fig"}  # i18n: user-facing
                    },  # i18n: user-facing
                },
                {
                    "id": "n2",
                    "name": "file_copy",
                    "config": {
                        "params": {"destination": "~/Desktop/设计稿_导出", "create_subdir": True}  # i18n: user-facing
                    },  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    # ── Development industry ──
    {
        "id": "dev_code_cleanup",
        "name": "项目代码清理",  # i18n: user-facing
        "industry": "开发",  # i18n: user-facing
        "description": "清理项目中的临时文件、缓存、编译产物",  # i18n: user-facing
        "icon": "💻",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["开发", "清理", "项目"],  # i18n: user-facing
        "workflow": {
            "name": "项目代码清理",  # i18n: user-facing
            "nodes": [
                {"id": "n1", "name": "file_input", "config": {"params": {"path": "~/Projects", "recursive": True}}},
                {
                    "id": "n2",
                    "name": "disk_cleaner",
                    "config": {
                        "params": {
                            "clean_pycache": True,
                            "clean_ds_store": True,
                            "clean_node_modules": True,
                            "dry_run": True,
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    {
        "id": "dev_log_analyzer",
        "name": "日志智能分析",  # i18n: user-facing
        "industry": "开发",  # i18n: user-facing
        "description": "AI 自动分析日志文件，提取错误和异常，生成分析报告",  # i18n: user-facing
        "icon": "🔍",
        "difficulty": "中等",  # i18n: user-facing
        "needs_ai": True,
        "tags": ["开发", "日志", "AI", "分析"],  # i18n: user-facing
        "workflow": {
            "name": "日志智能分析",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {"params": {"path": "~/Desktop/logs", "file_patterns": "*.log,*.log.*"}},
                },
                {
                    "id": "n2",
                    "name": "ai_summarize",
                    "config": {"params": {"model": "", "extract_keywords": True, "extract_conclusions": True}},
                },
                {
                    "id": "n3",
                    "name": "file_output",
                    "config": {
                        "params": {"output_path": "~/Desktop/日志分析报告", "format": "markdown"}  # i18n: user-facing
                    },  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
    },
    {
        "id": "dev_project_init",
        "name": "项目初始化脚手架",  # i18n: user-facing
        "industry": "开发",  # i18n: user-facing
        "description": "自动创建项目目录结构、README、.gitignore、License",  # i18n: user-facing
        "icon": "🚀",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["开发", "项目", "初始化"],  # i18n: user-facing
        "workflow": {
            "name": "项目初始化脚手架",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "shell_exec",
                    "config": {
                        "params": {"command": "mkdir -p src tests docs", "workdir": "~/Desktop/新项目"}  # i18n: user-facing
                    },  # i18n: user-facing
                },
                {
                    "id": "n2",
                    "name": "file_output",
                    "config": {
                        "params": {
                            "output_path": "~/Desktop/新项目",  # i18n: user-facing
                            "file_name": "README",
                            "format": "markdown",
                        }  # i18n: user-facing
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    # ── Data industry ──
    {
        "id": "data_csv_cleaner",
        "name": "CSV 数据清洗",  # i18n: user-facing
        "industry": "数据",  # i18n: user-facing
        "description": "自动清洗 CSV 数据：去重、格式标准化、空值处理",  # i18n: user-facing
        "icon": "📊",
        "difficulty": "中等",  # i18n: user-facing
        "tags": ["数据", "CSV", "清洗", "批量"],  # i18n: user-facing
        "workflow": {
            "name": "CSV 数据清洗",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {"params": {"path": "~/Desktop/data", "file_patterns": "*.csv"}},
                },
                {
                    "id": "n2",
                    "name": "python_repl",
                    "config": {
                        "params": {
                            "code": "import pandas as pd; df = pd.read_csv('input.csv'); df = df.drop_duplicates().fillna('N/A'); df.to_csv('output_clean.csv', index=False)",
                            "timeout": 30,
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "file_output",
                    "config": {"params": {"output_path": "~/Desktop/清洗结果", "format": "csv"}},  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
    },
    {
        "id": "data_format_converter",
        "name": "数据格式批量转换",  # i18n: user-facing
        "industry": "数据",  # i18n: user-facing
        "description": "批量转换数据格式（CSV↔JSON↔Excel↔Markdown 表格）",  # i18n: user-facing
        "icon": "🔄",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["数据", "转换", "批量"],  # i18n: user-facing
        "workflow": {
            "name": "数据格式批量转换",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {"params": {"path": "~/Desktop/data", "file_patterns": "*.csv,*.json"}},
                },
                {
                    "id": "n2",
                    "name": "file_output",
                    "config": {"params": {"output_path": "~/Desktop/转换结果", "format": "csv"}},  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    # ── Ops industry ──
    {
        "id": "ops_system_monitor",
        "name": "系统状态巡检",  # i18n: user-facing
        "industry": "运维",  # i18n: user-facing
        "description": "自动收集系统状态（磁盘/内存/CPU/网络），生成巡检报告",  # i18n: user-facing
        "icon": "🖥️",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["运维", "监控", "巡检", "报告"],  # i18n: user-facing
        "workflow": {
            "name": "系统状态巡检",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "shell_exec",
                    "config": {
                        "params": {
                            "command": "df -h && echo '---' && free -h && echo '---' && top -l 1 -n 0",
                            "timeout": 10,
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "file_output",
                    "config": {
                        "params": {
                            "output_path": "~/Desktop/巡检报告",  # i18n: user-facing
                            "file_name": "系统巡检_{date}",  # i18n: user-facing
                            "format": "markdown",
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    {
        "id": "ops_backup_manager",
        "name": "重要文件自动备份",  # i18n: user-facing
        "industry": "运维",  # i18n: user-facing
        "description": "定时备份指定目录到备份位置，支持增量备份",  # i18n: user-facing
        "icon": "💾",
        "difficulty": "简单",  # i18n: user-facing
        "tags": ["运维", "备份", "定时"],  # i18n: user-facing
        "workflow": {
            "name": "重要文件自动备份",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "file_input",
                    "config": {"params": {"path": "~/Documents/重要文件", "recursive": True}},  # i18n: user-facing
                },
                {
                    "id": "n2",
                    "name": "file_copy",
                    "config": {
                        "params": {
                            "destination": "~/Backups/重要文件",  # i18n: user-facing
                            "preserve_metadata": True,
                            "create_subdir": True,
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    {
        "id": "ops_disk_alert",
        "name": "磁盘空间预警",  # i18n: user-facing
        "industry": "运维",  # i18n: user-facing
        "description": "监控磁盘使用率，超过阈值时自动清理并发送通知",  # i18n: user-facing
        "icon": "⚠️",
        "difficulty": "中等",  # i18n: user-facing
        "tags": ["运维", "磁盘", "监控", "告警"],  # i18n: user-facing
        "workflow": {
            "name": "磁盘空间预警",  # i18n: user-facing
            "nodes": [
                {
                    "id": "n1",
                    "name": "shell_exec",
                    "config": {
                        "params": {"command": "df -h / | tail -1 | awk '{print $5}' | sed 's/%//'", "timeout": 5}
                    },
                },
                {
                    "id": "n2",
                    "name": "disk_cleaner",
                    "config": {"params": {"clean_cache": True, "clean_temp": True, "dry_run": True}},
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
]


def get_industry_templates(industry: str = "") -> List[Dict[str, Any]]:
    """Get the industry template list.

    Args:
        industry: Industry name (empty string returns all)

    Returns:
        List of templates
    """
    if not industry:
        return list(INDUSTRY_TEMPLATES)
    return [t for t in INDUSTRY_TEMPLATES if t.get("industry") == industry]


def get_industries() -> List[str]:
    """Get all industry categories."""
    industries = set()
    for t in INDUSTRY_TEMPLATES:
        ind = t.get("industry", "其他")  # i18n: user-facing
        industries.add(ind)
    return sorted(industries)
