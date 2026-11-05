"""Fusion-Cowork 内置模板 — 开箱即用的自动化模板。

每个模板是一个预定义的工作流定义，用户可一键运行。
"""

from __future__ import annotations

import json
import logging
import re
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
                            "sort_by": "type",
                        }
                    },
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
                            "dry_run": False,
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
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
                            "sort_order": "desc",
                        }
                    },
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
                            "dry_run": False,
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
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
                            "sort_by": "name",
                        }
                    },
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
                            "generate_report": True,
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "file_output",
                    "config": {
                        "params": {
                            "output_path": "~/Desktop/Output",
                            "file_name": "文档摘要报告_{date}",
                            "format": "markdown",
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
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
                        "params": {"path": "~/Desktop", "recursive": False, "file_patterns": "*", "sort_by": "name"}
                    },
                },
                {
                    "id": "n2",
                    "name": "ai_classify",
                    "config": {"params": {"model": "", "classify_by_content": True, "max_files_per_batch": 10}},
                },
                {
                    "id": "n3",
                    "name": "file_classifier",
                    "config": {"params": {"move_to_subdirs": True, "target_base": "~/Desktop", "dry_run": False}},
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
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
                        "params": {"path": "~/Desktop", "recursive": False, "file_patterns": "*", "sort_by": "name"}
                    },
                },
                {
                    "id": "n2",
                    "name": "ai_generate_name",
                    "config": {
                        "params": {"model": "", "use_content": True, "lowercase": False, "replace_spaces": True}
                    },
                },
                {"id": "n3", "name": "file_batch_rename", "config": {"params": {"dry_run": True}}},
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
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
                            "max_depth": 5,
                        }
                    },
                }
            ],
            "edges": [],
        },
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
                            "sort_by": "type",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "file_classifier",
                    "config": {
                        "params": {"move_to_subdirs": True, "target_base": "~/Desktop/项目归集", "dry_run": False}
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
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
                            "sort_order": "desc",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "download_organizer",
                    "config": {
                        "params": {
                            "organize_by_type": False,
                            "deduplicate": True,
                            "clean_old_files": False,
                            "dry_run": True,
                        }
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
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
                            "sort_order": "desc",
                        }
                    },
                },
                {"id": "n2", "name": "file_delete", "config": {"params": {"use_trash": True, "dry_run": True}}},
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
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
                            "sort_by": "name",
                        }
                    },
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
                            "dry_run": True,
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "file_classifier",
                    "config": {"params": {"move_to_subdirs": True, "target_base": "~/Desktop/图片", "dry_run": False}},
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
    },
    {
        "id": "weekly_readout_pptx",
        "name": "周度数据读出 (PPT)",
        "description": "从 JSON 数据源生成周度读出 deck: 环比对比, 超 10% 变动自动打标 (issue #152)",
        "category": "办公交付",
        "tags": ["PPT", "周报", "环比", "读出"],
        "icon": "📽️",
        "difficulty": "简单",
        "estimated_time": "10秒",
        "needs_ai": False,
        "workflow": {
            "name": "周度数据读出",
            "description": "本期 vs 上期对比, 超阈值变动打标, 生成可编辑 deck",
            "nodes": [
                {
                    "id": "n1",
                    "name": "pptx_write",
                    "config": {
                        "params": {
                            "output_path": "~/Desktop/Output",
                            "file_name": "weekly_readout_{date}",
                            "overwrite": True,
                            "spec": json.dumps(
                                {
                                    "title": "周度数据读出",
                                    "subtitle": "本期 vs 上期 (>10% 变动打标)",
                                    "slides": [
                                        {
                                            "layout": "chart",
                                            "title": "核心指标对比",
                                            "chart": {
                                                "type": "column",
                                                "categories": ["营收", "订单", "活跃", "转化率"],
                                                "series": [
                                                    {"name": "上期", "values": [100, 90, 180, 3.2]},
                                                    {"name": "本期", "values": [128, 84, 205, 3.0]},
                                                ],
                                            },
                                        }
                                    ],
                                    "period_compare": {
                                        "metric": "指标",
                                        "categories": ["营收", "订单", "活跃", "转化率"],
                                        "previous": [100, 90, 180, 3.2],
                                        "current": [128, 84, 205, 3.0],
                                        "threshold_pct": 10,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        }
                    },
                }
            ],
            "edges": [],
        },
    },
    # issue #283: governance G1-G5 gate pipeline (multi-repo automation setup guide)
    {
        "id": "governance_issue_lifecycle",
        "name": "治理-议题生命周期 (G1-G4-G5 闸门)",
        "description": "每议题一次运行: 提案打标 -> G1 提案闸门 -> 实施记录 -> 同步 agent-studio 审批任务 (带 run_id) -> G4 验收闸门 -> accepted 打标 -> G5 发布闸门 -> rollout 打标。审批经平台收件箱或签名 webhook 自动续跑。见 architecture/multi-repo-automation-setup-guide-1104.md",
        "category": "治理",
        "tags": ["治理", "闸门", "审批", "G-gate", "多仓库"],
        "icon": "🚦",
        "difficulty": "进阶",
        "estimated_time": "人工决定",
        # issue #287: 模板级参数 — 装配时经 template_to_workflow(param_values=...) 替换
        "params": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "目标仓库 (owner/name)", "default": "org/repo"},
                "issue_number": {"type": "integer", "description": "议题编号", "default": 1},
                "approvals_url": {
                    "type": "string",
                    "description": "agent-studio 审批任务同步端点",
                    "default": "http://127.0.0.1:11442/v1/agent-platform/approvals",
                },
            },
            "required": ["repo", "issue_number"],
        },
        "workflow": {
            "name": "治理-议题生命周期",
            "description": "G1-G5 gate pipeline for one issue",
            "nodes": [
                {
                    "id": "n1",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_set_labels",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "labels": ["state:proposal"],
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "G1 提案闸门: 批准后开始实施",
                            "details": "确认议题方案与标签状态机 (state:proposal -> approved)",
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_comment",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "body": "G1 approved — implementation started (branch/PR tracked)",
                        }
                    },
                },
                {
                    "id": "n4",
                    "name": "http_request",
                    "config": {
                        "params": {
                            "method": "POST",
                            "url": "${param.approvals_url}",
                            # issue #287: 鉴权头从环境注入 (${env.VAR}), 未配置时节点侧
                            # 丢弃该头 — 模板绝不硬编码密钥
                            "headers": {"x-api-key": "${env.FUSION_AGENT_STUDIO_API_KEY}"},
                            "body": {
                                # issue #291: approvals API 必填字段 — 缺 requester 直接 400;
                                # requester/assigned_to 必须不同 (平台禁止自审批, 422)
                                "title": "G4 acceptance ${param.repo}#${param.issue_number}",
                                "requester": "cowork-agent",
                                "assigned_to": "owner",
                                # issue #293: agent-studio 只持久化嵌套 payload 对象作任务参数
                                # (顶层 run_id/gate/kind 被丢弃, 决策 webhook 路由不回 run)
                                "payload": {
                                    "run_id": "${run_id}",
                                    "gate": "G4",
                                    "kind": "acceptance",
                                },
                            },
                            "timeout": 10,
                        }
                    },
                },
                {
                    "id": "n5",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "G4 验收闸门: 批准后标记 accepted",
                            "details": "agent-studio 审批任务已同步; 决定经 webhook 自动续跑本 run",
                        }
                    },
                },
                {
                    "id": "n6",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_set_labels",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "labels": ["state:accepted"],
                        }
                    },
                },
                {
                    "id": "n7",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "G5 发布闸门: 批准后进入 rollout",
                            "details": "最终发布确认",
                        }
                    },
                },
                {
                    "id": "n8",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_set_labels",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "labels": ["state:rollout"],
                        }
                    },
                },
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"},
                {"source_id": "n3", "target_id": "n4"},
                {"source_id": "n4", "target_id": "n5"},
                {"source_id": "n5", "target_id": "n6"},
                {"source_id": "n6", "target_id": "n7"},
                {"source_id": "n7", "target_id": "n8"},
            ],
        },
    },
    {
        "id": "governance_batch_audit",
        "name": "治理-批次审计 (state:approved 巡检)",
        "description": "按 state:approved 标签列出试点仓库议题 -> G 闸门人工复核 -> 审计结论回写议题评论。逐议题派生生命周期 run 需编排层支持, 见 architecture/multi-repo-automation-setup-guide-1104.md",
        "category": "治理",
        "tags": ["治理", "审计", "批次", "G-gate"],
        "icon": "📋",
        "difficulty": "进阶",
        "estimated_time": "人工决定",
        # issue #287: 模板级参数 (批次审计只需要仓库)
        "params": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "目标仓库 (owner/name)", "default": "org/repo"},
                "issue_number": {"type": "integer", "description": "议题编号", "default": 1},
            },
            "required": ["repo"],
        },
        "workflow": {
            "name": "治理-批次审计",
            "description": "Batch audit over state:approved issues",
            "nodes": [
                {
                    "id": "n1",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_list",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "labels": ["state:approved"],
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "批次审计闸门: 复核 state:approved 议题清单",
                            "details": "确认入验收批次的议题范围",
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_comment",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "body": "Batch audit approved — issue accepted into acceptance batch",
                        }
                    },
                },
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"},
            ],
        },
    },
]


def _substitute_params(value: Any, params: Dict[str, Any]) -> Any:
    # issue #287: 递归替换节点配置里的 ${param.X} 占位符 — 未命中的占位符保留字面量
    # (记 WARNING 可观测), 已知参数缺失时装配即失败比静默 org/repo 上线更诚实。
    if isinstance(value, str):
        # 整串占位符 → 保留参数原始类型 (issue_number 42 保持 int, 不变 "42" 字符串)
        m = re.fullmatch(r"\$\{(param\.[A-Za-z_][A-Za-z0-9_]*)\}", value)
        if m and m.group(1)[6:] in params:
            return params[m.group(1)[6:]]

        def _repl(m):
            key = m.group(1)
            if key.startswith("param.") and key[6:] in params:
                val = params[key[6:]]
                return json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
            return m.group(0)

        return re.sub(r"\$\{(param\.[A-Za-z_][A-Za-z0-9_]*)\}", _repl, value)
    if isinstance(value, dict):
        return {k: _substitute_params(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_params(v, params) for v in value]
    return value


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

    def template_to_workflow(
        self, template_id: str, param_values: Optional[Dict[str, Any]] = None
    ) -> Optional[Workflow]:
        """将模板加载为可执行的工作流。

        Args:
            template_id: 模板 ID
            param_values: issue #287 模板级参数 — 节点配置里的 ${param.X} 占位符
                在装配时用这里的值替换 (repo/issue_number/approvals_url 等)

        Returns:
            Optional[Workflow]: 工作流实例，如果模板不存在则返回 None
        """
        tpl = self.get_template(template_id)
        if not tpl:
            logger.error(f"模板不存在: {template_id}")
            return None

        workflow_data = tpl.get("workflow", {})
        # issue #287: 模板参数装配 — schema defaults 先落底, 显式 param_values 覆盖;
        # 未传值时占位符也不会残留在节点配置里
        effective = {
            key: (spec or {}).get("default")
            for key, spec in (tpl.get("params", {}).get("properties") or {}).items()
            if (spec or {}).get("default") is not None
        }
        effective.update({k: v for k, v in (param_values or {}).items() if v is not None})
        if effective:
            workflow_data = _substitute_params(workflow_data, effective)
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
            if (
                query in tpl.get("name", "").lower()
                or query in tpl.get("description", "").lower()
                or any(query in tag.lower() for tag in tpl.get("tags", []))
            ):
                results.append(tpl)
        return results
