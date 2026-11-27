"""Fusion-Cowork built-in templates — out-of-the-box automation templates.

Each template is a predefined workflow definition that users can run with one click.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine.workflow import Workflow

logger = logging.getLogger(__name__)

# Built-in template definitions
BUILTIN_TEMPLATES = [
    {
        "id": "desktop_daily_cleanup",
        "name": "桌面每日规整",  # i18n: user-facing
        "description": "每天自动规整桌面文件，按类型分类到子文件夹",  # i18n: user-facing
        "category": "桌面清理",  # i18n: user-facing
        "tags": ["桌面", "清理", "分类", "日常"],  # i18n: user-facing
        "icon": "🧹",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "30秒",  # i18n: user-facing
        "workflow": {
            "name": "桌面每日规整",  # i18n: user-facing
            "description": "按文件类型自动规整桌面文件",  # i18n: user-facing
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
        "name": "下载文件夹自动归档",  # i18n: user-facing
        "description": "自动整理下载文件夹，按类型归档、去重、清理过期文件",  # i18n: user-facing
        "category": "文件整理",  # i18n: user-facing
        "tags": ["下载", "归档", "去重", "清理"],  # i18n: user-facing
        "icon": "📥",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "1分钟",  # i18n: user-facing
        "workflow": {
            "name": "下载文件夹自动归档",  # i18n: user-facing
            "description": "自动整理下载文件夹，按类型归档、去重",  # i18n: user-facing
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
        "name": "PDF/文档批量总结",  # i18n: user-facing
        "description": "批量总结 PDF、Word、Markdown 文档，提取关键词和结论，生成汇总报告",  # i18n: user-facing
        "category": "AI 处理",  # i18n: user-facing
        "tags": ["PDF", "文档", "摘要", "AI", "批量"],  # i18n: user-facing
        "icon": "📝",
        "difficulty": "中等",  # i18n: user-facing
        "estimated_time": "2-5分钟",  # i18n: user-facing
        "needs_ai": True,
        "workflow": {
            "name": "文档批量AI摘要",  # i18n: user-facing
            "description": "批量总结文档并生成汇总报告",  # i18n: user-facing
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
                            "file_name": "文档摘要报告_{date}",  # i18n: user-facing
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
        "name": "AI 智能文件分类归档",  # i18n: user-facing
        "description": "AI 根据文件内容语义自动分类并归档到对应文件夹",  # i18n: user-facing
        "category": "AI 处理",  # i18n: user-facing
        "tags": ["AI", "分类", "归档", "智能"],  # i18n: user-facing
        "icon": "🏷️",
        "difficulty": "中等",  # i18n: user-facing
        "estimated_time": "1-3分钟",  # i18n: user-facing
        "needs_ai": True,
        "workflow": {
            "name": "AI 智能文件分类归档",  # i18n: user-facing
            "description": "AI 根据文件内容语义自动分类并归档",  # i18n: user-facing
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
        "name": "AI 批量智能重命名",  # i18n: user-facing
        "description": "AI 根据文件内容智能生成规范的文件名，批量重命名",  # i18n: user-facing
        "category": "AI 处理",  # i18n: user-facing
        "tags": ["AI", "重命名", "批量", "智能"],  # i18n: user-facing
        "icon": "✏️",
        "difficulty": "中等",  # i18n: user-facing
        "estimated_time": "1-2分钟",  # i18n: user-facing
        "needs_ai": True,
        "workflow": {
            "name": "AI 批量智能重命名",  # i18n: user-facing
            "description": "AI 根据内容智能生成规范文件名",  # i18n: user-facing
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
        "name": "磁盘空间清理",  # i18n: user-facing
        "description": "扫描并清理缓存、临时文件、__pycache__、.DS_Store 等垃圾文件",  # i18n: user-facing
        "category": "系统维护",  # i18n: user-facing
        "tags": ["磁盘", "清理", "缓存", "系统"],  # i18n: user-facing
        "icon": "💾",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "1-3分钟",  # i18n: user-facing
        "workflow": {
            "name": "磁盘空间清理",  # i18n: user-facing
            "description": "扫描并清理系统垃圾文件",  # i18n: user-facing
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
        "name": "项目资料一键归集",  # i18n: user-facing
        "description": "递归扫描项目目录，按类型归集文档、代码、图片等资料",  # i18n: user-facing
        "category": "文件整理",  # i18n: user-facing
        "tags": ["项目", "归集", "整理", "批量"],  # i18n: user-facing
        "icon": "📂",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "30秒",  # i18n: user-facing
        "workflow": {
            "name": "项目资料一键归集",  # i18n: user-facing
            "description": "按类型归集项目资料",  # i18n: user-facing
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
                        "params": {
                            "move_to_subdirs": True,
                            "target_base": "~/Desktop/项目归集",  # i18n: user-facing
                            "dry_run": False,
                        }  # i18n: user-facing
                    },
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    },
    {
        "id": "duplicate_finder",
        "name": "重复文件扫描清理",  # i18n: user-facing
        "description": "扫描指定目录中的重复文件，识别并清理",  # i18n: user-facing
        "category": "系统维护",  # i18n: user-facing
        "tags": ["去重", "清理", "重复文件"],  # i18n: user-facing
        "icon": "🔍",
        "difficulty": "中等",  # i18n: user-facing
        "estimated_time": "1-5分钟",  # i18n: user-facing
        "workflow": {
            "name": "重复文件扫描清理",  # i18n: user-facing
            "description": "扫描并清理重复文件",  # i18n: user-facing
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
        "name": "日志文件自动清洗",  # i18n: user-facing
        "description": "扫描并清理日志文件，汇总生成清理报告",  # i18n: user-facing
        "category": "系统维护",  # i18n: user-facing
        "tags": ["日志", "清洗", "清理", "系统"],  # i18n: user-facing
        "icon": "📋",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "30秒",  # i18n: user-facing
        "workflow": {
            "name": "日志文件自动清洗",  # i18n: user-facing
            "description": "扫描并清理日志文件",  # i18n: user-facing
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
        "name": "图片批量重命名分类",  # i18n: user-facing
        "description": "批量重命名图片文件并按类型分类",  # i18n: user-facing
        "category": "文件整理",  # i18n: user-facing
        "tags": ["图片", "重命名", "分类", "批量"],  # i18n: user-facing
        "icon": "🖼️",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "30秒",  # i18n: user-facing
        "workflow": {
            "name": "图片批量重命名分类",  # i18n: user-facing
            "description": "批量重命名图片并按类型分类",  # i18n: user-facing
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
                    "config": {
                        "params": {
                            "move_to_subdirs": True,
                            "target_base": "~/Desktop/图片",  # i18n: user-facing
                            "dry_run": False,
                        }  # i18n: user-facing
                    },  # i18n: user-facing
                },
            ],
            "edges": [{"source_id": "n1", "target_id": "n2"}, {"source_id": "n2", "target_id": "n3"}],
        },
    },
    {
        "id": "weekly_readout_pptx",
        "name": "周度数据读出 (PPT)",  # i18n: user-facing
        "description": "从 JSON 数据源生成周度读出 deck: 环比对比, 超 10% 变动自动打标 (issue #152)",  # i18n: user-facing
        "category": "办公交付",  # i18n: user-facing
        "tags": ["PPT", "周报", "环比", "读出"],  # i18n: user-facing
        "icon": "📽️",
        "difficulty": "简单",  # i18n: user-facing
        "estimated_time": "10秒",  # i18n: user-facing
        "needs_ai": False,
        "workflow": {
            "name": "周度数据读出",  # i18n: user-facing
            "description": "本期 vs 上期对比, 超阈值变动打标, 生成可编辑 deck",  # i18n: user-facing
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
                                    "title": "周度数据读出",  # i18n: user-facing
                                    "subtitle": "本期 vs 上期 (>10% 变动打标)",  # i18n: user-facing
                                    "slides": [
                                        {
                                            "layout": "chart",
                                            "title": "核心指标对比",  # i18n: user-facing
                                            "chart": {
                                                "type": "column",
                                                "categories": ["营收", "订单", "活跃", "转化率"],  # i18n: user-facing
                                                "series": [
                                                    {
                                                        "name": "上期",  # i18n: user-facing
                                                        "values": [100, 90, 180, 3.2],
                                                    },  # i18n: user-facing
                                                    {
                                                        "name": "本期",  # i18n: user-facing
                                                        "values": [128, 84, 205, 3.0],
                                                    },  # i18n: user-facing
                                                ],
                                            },
                                        }
                                    ],
                                    "period_compare": {
                                        "metric": "指标",  # i18n: user-facing
                                        "categories": ["营收", "订单", "活跃", "转化率"],  # i18n: user-facing
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
        "name": "治理-议题生命周期 (G1-G4-G5 闸门)",  # i18n: user-facing
        "description": "每议题一次运行: 提案打标 -> 同步 G1 决策任务 -> G1 提案闸门 -> 实施记录 -> 关联 PR 检查 (零 LLM 证据闸门, 无 PR 阻塞: 先提 PR 再进验收, 重跑复查) -> 同步 G4 验收任务 (带 run_id + PR/CI 证据) -> G4 验收闸门 -> accepted 打标 -> 同步 G5 发布任务 -> G5 发布闸门 -> rollout 打标。G1/G4/G5 均自动同步 agent-studio 审批任务, 决策经平台收件箱或签名 webhook 自动续跑。见 architecture/multi-repo-automation-setup-guide-1104.md",  # i18n: user-facing
        "category": "治理",  # i18n: user-facing
        "tags": ["治理", "闸门", "审批", "G-gate", "多仓库"],  # i18n: user-facing
        "icon": "🚦",
        "difficulty": "进阶",  # i18n: user-facing
        "estimated_time": "人工决定",  # i18n: user-facing
        # issue #287: template-level params — substituted at assembly time via template_to_workflow(param_values=...)
        "params": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "目标仓库 (owner/name)",  # i18n: user-facing
                    "default": "org/repo",
                },  # i18n: user-facing
                "issue_number": {"type": "integer", "description": "议题编号", "default": 1},  # i18n: user-facing
                "approvals_url": {
                    "type": "string",
                    "description": "agent-studio 审批任务同步端点",  # i18n: user-facing
                    # product-level call (roadmap §6): template param default —
                    # the approval task store lives in the agent-studio daemon.
                    "default": "http://127.0.0.1:11442/v1/agent-platform/approvals",
                },
            },
            "required": ["repo", "issue_number"],
        },
        "workflow": {
            "name": "治理-议题生命周期",  # i18n: user-facing
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
                    # issue #294: sync G1 decision to agent-studio — visible in platform inbox, webhook resumes this run
                    "id": "n1b",
                    "name": "http_request",
                    "config": {
                        "params": {
                            "method": "POST",
                            "url": "${param.approvals_url}",
                            "headers": {"x-api-key": "${env.FUSION_AGENT_STUDIO_API_KEY}"},
                            "body": {
                                "title": "G1 proposal ${param.repo}#${param.issue_number}",
                                "requester": "cowork-agent",
                                "assigned_to": "owner",
                                "payload": {
                                    "run_id": "${run_id}",
                                    "gate": "G1",
                                    "kind": "proposal",
                                },
                            },
                            "timeout": 10,
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "G1 提案闸门: 批准后开始实施",  # i18n: user-facing
                            "details": "确认议题方案与标签状态机 (state:proposal -> approved)",  # i18n: user-facing
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
                    # issue #306: G4 pre-evidence gate — pure REST read (zero LLM). When the issue has no
                    # linked PR the node fails and blocks (error "file a PR before acceptance"); once the
                    # PR appears, rerunning from this step re-checks. PR link + CI status flow into the
                    # n4 sync payload via ${result.*}, visible on the approval card.
                    "id": "n4a",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "linked_pr",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${param.issue_number}",
                            "require": True,
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
                            # issue #287: auth header injected from environment (${env.VAR}); when unset
                            # the node drops the header — templates never hardcode secrets
                            "headers": {"x-api-key": "${env.FUSION_AGENT_STUDIO_API_KEY}"},
                            "body": {
                                # issue #291: approvals API required fields — missing requester is a bare 400;
                                # requester/assigned_to must differ (platform forbids self-approval, 422)
                                "title": "G4 acceptance ${param.repo}#${param.issue_number}",
                                "requester": "cowork-agent",
                                "assigned_to": "owner",
                                # issue #293: agent-studio persists only the nested payload object as task params
                                # (top-level run_id/gate/kind are dropped; the decision webhook does not route back to the run)
                                "payload": {
                                    "run_id": "${run_id}",
                                    "gate": "G4",
                                    "kind": "acceptance",
                                    # issue #306: evidence — linked_pr result from n4a (edge default port
                                    # merged into inputs; ${result.*} reads upstream outputs)
                                    "evidence": {
                                        "pr_url": "${result.pr_url}",
                                        "ci_status": "${result.ci_status}",
                                    },
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
                            "prompt": "G4 验收闸门: 批准后标记 accepted",  # i18n: user-facing
                            "details": "agent-studio 审批任务已同步; 决定经 webhook 自动续跑本 run",  # i18n: user-facing
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
                    # issue #294: sync G5 decision to agent-studio (before the rollout gate, approvable on the platform side)
                    "id": "n6b",
                    "name": "http_request",
                    "config": {
                        "params": {
                            "method": "POST",
                            "url": "${param.approvals_url}",
                            "headers": {"x-api-key": "${env.FUSION_AGENT_STUDIO_API_KEY}"},
                            "body": {
                                "title": "G5 rollout ${param.repo}#${param.issue_number}",
                                "requester": "cowork-agent",
                                "assigned_to": "owner",
                                "payload": {
                                    "run_id": "${run_id}",
                                    "gate": "G5",
                                    "kind": "rollout",
                                },
                            },
                            "timeout": 10,
                        }
                    },
                },
                {
                    "id": "n7",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "G5 发布闸门: 批准后进入 rollout",  # i18n: user-facing
                            "details": "最终发布确认",  # i18n: user-facing
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
                {"source_id": "n1", "target_id": "n1b"},
                {"source_id": "n1b", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"},
                {"source_id": "n3", "target_id": "n4a"},
                {"source_id": "n4a", "target_id": "n4"},
                {"source_id": "n4", "target_id": "n5"},
                {"source_id": "n5", "target_id": "n6"},
                {"source_id": "n6", "target_id": "n6b"},
                {"source_id": "n6b", "target_id": "n7"},
                {"source_id": "n7", "target_id": "n8"},
            ],
        },
    },
    {
        "id": "governance_batch_audit",
        "name": "治理-批次审计 (state:approved 巡检)",  # i18n: user-facing
        "description": "按 state:approved 标签列出试点仓库议题 -> G 闸门人工复核 -> 审计结论回写议题评论。逐议题派生生命周期 run 需编排层支持, 见 architecture/multi-repo-automation-setup-guide-1104.md",  # i18n: user-facing
        "category": "治理",  # i18n: user-facing
        "tags": ["治理", "审计", "批次", "G-gate"],  # i18n: user-facing
        "icon": "📋",
        "difficulty": "进阶",  # i18n: user-facing
        "estimated_time": "人工决定",  # i18n: user-facing
        # issue #287: template-level params (batch audit: repo + audit label); issue #294: labels can be passed from the GUI form
        "params": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "目标仓库 (owner/name)",  # i18n: user-facing
                    "default": "org/repo",
                },  # i18n: user-facing
                "issue_number": {"type": "integer", "description": "议题编号", "default": 1},  # i18n: user-facing
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "巡检标签 (issue_list 过滤用)",  # i18n: user-facing
                    "default": ["state:approved"],
                },
            },
            "required": ["repo"],
        },
        "workflow": {
            "name": "治理-批次审计",  # i18n: user-facing
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
                            # issue #294: labels via template param (whole-string placeholder keeps array type)
                            "labels": "${param.labels}",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "批次审计闸门: 复核 state:approved 议题清单",  # i18n: user-facing
                            "details": "确认入验收批次的议题范围",  # i18n: user-facing
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
    # issue #307: product_lifecycle — full delivery pipeline orchestration.
    # Zero-LLM orchestration: LLM seats run as studio_run nodes; deterministic
    # stages (scaffold / issue creation / PR evidence gate / labels) are plain
    # nodes. Exactly 3 human gates (A PRD / B arch / C acceptance, batch
    # verdicts per #309).
    {
        "id": "product_lifecycle",
        # issue #485: workflow-definition ids registered at web-serve seed time —
        # external callers (fusion-site factory) kick off runs by these ids
        "web_aliases": ["product_lifecycle_agents"],
        "name": "交付-产品生命周期 (A/B/C 三闸门)",  # i18n: user-facing
        "description": "全交付管线: BA 出 PRD -> 闸门 A -> 架构 -> 闸门 B -> 脚手架 ai-<slug> -> 建任务 issue (state:ready) -> claude_dispatch 派发 coder 会话 (PR-only 契约, 预算上限) -> 关联 PR 机器验收 -> Env/Mock 代理 -> 闸门 C 人工验收 (支持逐项 batch 决定) -> accepted 打标 + 证据归档。LLM 座位经 studio_run 调 agent-studio graph 或 agent (issue #320, 默认 delivery-* seat agents)。",  # i18n: user-facing
        "category": "交付",  # i18n: user-facing
        "tags": ["交付", "管线", "闸门", "脚手架", "派发"],  # i18n: user-facing
        "icon": "🏭",
        "difficulty": "进阶",  # i18n: user-facing
        "estimated_time": "人工决定",  # i18n: user-facing
        "params": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "仓库 slug, 生成 ai-<slug>",  # i18n: user-facing
                    "default": "demo",
                },  # i18n: user-facing
                "repo": {
                    "type": "string",
                    "description": "任务 issue 所在仓库 (owner/name)",  # i18n: user-facing
                    "default": "org/repo",
                },  # i18n: user-facing
                "task_title": {
                    "type": "string",
                    "description": "任务 issue 标题",  # i18n: user-facing
                    "default": "Implement v0.1 scope",
                },  # i18n: user-facing
                "task_prompt": {
                    "type": "string",
                    "description": "claude_dispatch 任务指令",  # i18n: user-facing
                    # issue #362: delivery contract requires a closing keyword in the PR body — linked_pr machine
                    # acceptance judges delivery by pr_link_strength=closing; mention-only (hygiene PR) blocks
                    "default": (
                        "Implement the assigned task per PRD/architecture docs; deliver via PR with self-test "
                        "evidence. The PR body must link this issue with a closing keyword (Closes #N)."
                    ),
                },
                # issue #361: rerun idempotency — existing scaffold dir rejected by default; pass true to overwrite
                # scaffold files (existing artifacts preserved; empty dirs are always reused)
                "scaffold_overwrite": {
                    "type": "boolean",
                    "description": "重跑时复写已有脚手架目录 (repo_scaffold overwrite=true; 空目录自动复用, 既有产出保留)",  # i18n: user-facing
                    "default": False,
                },
                # issue #362: PR link-strength gate — mention-only PRs (hygiene/aside) block acceptance
                "require_closing_link": {
                    "type": "boolean",
                    "description": "linked_pr 仅接受关闭关键词关联的 PR (Closes/Fixes/Resolves #N), 仅提及即阻塞",  # i18n: user-facing
                    "default": True,
                },
                "ba_graph_id": {
                    "type": "string",
                    "description": "agent-studio BA 代理 graph ID (与 ba_agent_id 二选一)",  # i18n: user-facing
                    "default": "",
                },
                "ba_agent_id": {
                    "type": "string",
                    "description": "agent-studio BA 代理 agent ID (与 ba_graph_id 二选一; Builder 部署的 seat agent)",  # i18n: user-facing
                    "default": "delivery-ba",
                },
                "arch_graph_id": {
                    "type": "string",
                    "description": "agent-studio 架构代理 graph ID (与 arch_agent_id 二选一)",  # i18n: user-facing
                    "default": "",
                },
                "arch_agent_id": {
                    "type": "string",
                    "description": "agent-studio 架构代理 agent ID (与 arch_graph_id 二选一)",  # i18n: user-facing
                    "default": "delivery-architect",
                },
                "env_graph_id": {
                    "type": "string",
                    "description": "agent-studio Env/Mock 代理 graph ID (与 env_agent_id 二选一)",  # i18n: user-facing
                    "default": "",
                },
                "env_agent_id": {
                    "type": "string",
                    "description": "agent-studio Env/Mock 代理 agent ID (与 env_graph_id 二选一)",  # i18n: user-facing
                    "default": "delivery-env-mock",
                },
                # issue #358: 1800s hard-kills real delivery sessions — start at 3600s; tune globally via the
                # FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT env var
                "dispatch_timeout": {
                    "type": "number",
                    "description": "派发墙钟上限 (秒, 缺省 3600, 可用 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 覆盖)",  # i18n: user-facing
                    "default": 3600,
                },
                "dispatch_max_tokens": {
                    "type": "number",
                    "description": "派发 token 上限",  # i18n: user-facing
                    "default": 200000,
                },  # i18n: user-facing
                # issue #363/#373: studio_run seat turn budget — studio default 8 turns starves multi-artifact
                # roles (env-mock/architect); passed explicitly via spec.max_turns. Live evidence: 24 turns
                # still exhausted for content-dense seats (run_e5d4d34da020), default raised to 48
                "seat_max_turns": {
                    "type": "number",
                    "description": "studio_run 座位轮次预算 (spec.max_turns; 缺省 48, studio 默认 8 轮会饿死多产物角色)",  # i18n: user-facing
                    "default": 48,
                },
                # issue #413: arch review gate (gate B) chosen at intake — default no review (auto-pass);
                # with review_arch=true gate B still pauses for manual approval
                "review_arch": {
                    "type": "boolean",
                    "description": "是否需要架构评审 (闸门 B); 缺省 false = 自动通过, true = 暂停等人工批准",  # i18n: user-facing
                    "default": False,
                },
                # issue #409: studio_run upstream wait is configurable — fixed 900s wait not enough under concurrent pipeline mlx contention
                "studio_wait_timeout": {
                    "type": "number",
                    "description": "studio_run 各座位 sync 等待终态上限 (秒; 缺省 1800)",  # i18n: user-facing
                    "default": 1800,
                },
                # issue #387: synthetic E2E — all three switches default off = production behavior; synthetic
                # launches pass seat_model=replay-dev + gates_human=false + node_dry_run=true:
                # zero real-model tokens, zero GitHub writes, no human gates
                "seat_model": {
                    "type": "string",
                    "description": "座位模型钉选 (spec.model; 缺省空 = 按 agent manifest 选模; 合成 E2E 用 replay-dev)",  # i18n: user-facing
                    "default": "",
                },
                "gates_human": {
                    "type": "boolean",
                    "description": "人工闸门 A/C 是否需要评审 (闸 B 由 review_arch 独立控制); 缺省 true, 合成 E2E 传 false",  # i18n: user-facing
                    "default": True,
                },
                "node_dry_run": {
                    "type": "boolean",
                    "description": "GitHub/派发节点合成模式 (确定性伪结果, 零网络); 缺省 false, 合成 E2E 传 true",  # i18n: user-facing
                    "default": False,
                },
                # issue #422: historical project match — match node params; empty candidates default = always
                # verdict new (current behavior); candidates maintained by operator/upstream
                "match_candidates": {
                    "type": "array",
                    "description": "候选项目列表 [{slug, repo, capabilities, stack, customer, health}]",  # i18n: user-facing
                    "default": [],
                },
                "match_candidates_file": {
                    "type": "string",
                    "description": "候选 JSON 文件路径 (与 match_candidates 合并; 缺省空)",  # i18n: user-facing
                    "default": "",
                },
                "match_threshold": {
                    "type": "number",
                    "description": "extend/base 判定分数阈值 (缺省 0.6)",  # i18n: user-facing
                    "default": 0.6,
                },
                "own_customer": {
                    "type": "string",
                    "description": "本客户标识 (candidate.customer 相等才判 extend; 缺省空 = 全部自有)",  # i18n: user-facing
                    "default": "",
                },
                "required_capabilities": {
                    "type": "array",
                    "description": "需求要求的能力清单 (gaps = 要求 - 候选已具备)",  # i18n: user-facing
                    "default": [],
                },
                # issue #431: customer design assets — empty design_workspace = no design, behavior identical
                # to before; with assets, design_context exports structure + code via desigma-mcp (stdio)
                # into the coder prompt
                "design_workspace": {
                    "type": "string",
                    "description": "客户设计稿工作区路径 (.desigma 目录); 空 = 无设计稿 (现行为)",  # i18n: user-facing
                    "default": "",
                },
                "design_command": {
                    "type": "string",
                    "description": "desigma-mcp 命令 (basename 须在 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",  # i18n: user-facing
                    "default": "desigma-mcp",
                },
                "design_frame_id": {
                    "type": "number",
                    "description": "导出的设计 frame 节点 id; 0 = 取首个节点",  # i18n: user-facing
                    "default": 0,
                },
                "design_languages": {
                    "type": "array",
                    "description": "导出语言 (缺省 react + tailwind)",  # i18n: user-facing
                    "default": ["react", "tailwind"],
                },
                "design_timeout": {
                    "type": "number",
                    "description": "desigma-mcp 单请求超时 (秒)",  # i18n: user-facing
                    "default": 60,
                },
                # issue #452: demo contract gate — demo_gate=false makes n13 a successful no-op (current
                # behavior); true validates the three deliverables (docker-compose.demo.yml / demo.yaml /
                # stub pack), missing items = delivery failure; register=true registers with the executor demo API
                "demo_gate": {
                    "type": "boolean",
                    "description": "是否启用演示契约门 (AGENTS.md rule 7); 缺省 false = 跳过",  # i18n: user-facing
                    "default": False,
                },
                "demo_deliverable_dir": {
                    "type": "string",
                    "description": "交付物仓库/worktree 路径 (缺省空 = 用脚手架目录 business/<repo>)",  # i18n: user-facing
                    "default": "",
                },
                "demo_check_script": {
                    "type": "string",
                    "description": "meta repo check-demo-runnable.py 路径 (可选, 全局跑, 仅本仓库违规判失败)",  # i18n: user-facing
                    "default": "",
                },
                "demo_register": {
                    "type": "boolean",
                    "description": "契约通过后向 executor demo API 注册样例 (缺省 false)",  # i18n: user-facing
                    "default": False,
                },
                "demo_executor_url": {
                    "type": "string",
                    "description": "executor demo API base (缺省空 = FUSION_EXECUTOR_URL 环境变量)",  # i18n: user-facing
                    "default": "",
                },
            },
            # issue #320: seat ID either-or — at assembly an explicit graph_id wins and clears the agent default (and vice versa)
            "seat_pairs": [
                ["ba_graph_id", "ba_agent_id"],
                ["arch_graph_id", "arch_agent_id"],
                ["env_graph_id", "env_agent_id"],
            ],
            "required": ["slug", "repo", "task_title"],
        },
        "workflow": {
            "name": "交付-产品生命周期",  # i18n: user-facing
            "description": "PRD gate A -> design context -> arch gate B -> scaffold -> dispatch -> PR evidence -> env/mock -> gate C -> accepted",
            "nodes": [
                {
                    "id": "n1",
                    "name": "studio_run",
                    "config": {
                        "params": {
                            "graph_id": "${param.ba_graph_id}",
                            "agent_id": "${param.ba_agent_id}",
                            # issue #443: seat input must carry the original requirement — with only a slug the
                            # BA would invent an unrelated PRD; task_title/task_prompt are the authoritative text
                            "input": (
                                "Write the PRD for product ${param.slug}. Output the PRD document content.\n\n"
                                "Customer requirement (authoritative — design exactly this scope, do not invent):\n"
                                "Title: ${param.task_title}\n\n"
                                "${param.task_prompt}"
                            ),
                            "mode": "sync",
                            # issue #363: explicit turn budget — studio default 8 turns starves multi-artifact roles
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: seat model pinning (synthetic E2E pins replay-dev; empty = upstream picks)
                            "model": "${param.seat_model}",
                            # issue #409: upstream wait configurable (default 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "闸门 A: PRD 评审",  # i18n: user-facing
                            "details": "批准 PRD 后进入架构设计; 拒绝则 BA 代理按评审意见修订",  # i18n: user-facing
                            # issue #387: gates_human=false (synthetic E2E) auto-passes gate A
                            "require_review": "${param.gates_human}",
                            # issue #328: reject -> revision loop — back to n1 (BA) rerun with review feedback
                            "on_reject": "revise",
                            "revision_node": "n1",
                        }
                    },
                },
                {
                    # issue #431: design context — mounted after gate A (requirement confirmation); empty
                    # workspace = no design, no-op success (design_section empty string, coder prompt
                    # byte-identical); with assets it exports structure + code and appends design_section
                    # as a full section to the n7 coder prompt (implementation constraints)
                    "id": "n4d",
                    "name": "design_context",
                    "config": {
                        "params": {
                            "workspace": "${param.design_workspace}",
                            "command": "${param.design_command}",
                            "frame_id": "${param.design_frame_id}",
                            "languages": "${param.design_languages}",
                            "timeout": "${param.design_timeout}",
                        }
                    },
                },
                {
                    "id": "n3",
                    "name": "studio_run",
                    "config": {
                        "params": {
                            "graph_id": "${param.arch_graph_id}",
                            "agent_id": "${param.arch_agent_id}",
                            # issue #346: canonical handoff — pass the BA artifact id below (agent-studio
                            # run result artifacts[0] is an id string); downstream seats fetch the PRD by
                            # id, never guess paths
                            # issue #478: seats like replay-dev produce no artifacts so artifacts.0 would
                            # break — |fallback degrades; with artifacts present the canonical id handoff
                            # is unchanged, else inline report fallback
                            "input": "Write the architecture/plan for product ${param.slug} based on the approved PRD below.\n\nPRD artifact id (canonical reference — read the PRD by this artifact id, never guess file paths; may be absent):\n${step.n1.result.artifacts.0|(no artifact id — use the PRD document below)}\n\nPRD document:\n${step.n1.result.last_result.report}",
                            "mode": "sync",
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: seat model pinning (synthetic E2E pins replay-dev; empty = upstream picks)
                            "model": "${param.seat_model}",
                            # issue #409: upstream wait configurable (default 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    # issue #422: historical match — runs automatically before gate B (confirmation point);
                    # when gate B pauses, the run view shows a verdict card (score/gaps) and the operator
                    # can override (override_output redirect directive) before approving; extend reuses the
                    # customer's existing repo (incremental extension), base distills shared capabilities
                    # into a new inheriting repo (never writes other customers' repos, never leaks their
                    # project names), new = current behavior
                    "id": "n4m",
                    "name": "historical_match",
                    "config": {
                        "params": {
                            "requirement": "Task: ${param.task_title}\n${param.task_prompt}",
                            "candidates": "${param.match_candidates}",
                            "candidates_file": "${param.match_candidates_file}",
                            "threshold": "${param.match_threshold}",
                            "own_customer": "${param.own_customer}",
                            "required_capabilities": "${param.required_capabilities}",
                        }
                    },
                },
                {
                    "id": "n4",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "闸门 B: 架构评审",  # i18n: user-facing
                            "details": "批准架构后脚手架 + 派发; 拒绝则架构代理按评审意见修订",  # i18n: user-facing
                            # issue #413: review_arch=false (default) -> gate auto-passes without entering HITL
                            "require_review": "${param.review_arch}",
                            # issue #328: reject -> revision loop — back to n3 (arch) rerun with review feedback
                            "on_reject": "revise",
                            "revision_node": "n3",
                        }
                    },
                },
                {
                    # issue #361: scaffold_overwrite template param passthrough — rerun no longer hard-blocked
                    # by an existing worktree
                    # issue #422: with verdict=extend repo_scaffold clones the existing repo instead (incremental extension)
                    "id": "n5",
                    "name": "repo_scaffold",
                    "config": {
                        "params": {
                            "slug": "${param.slug}",
                            "overwrite": "${param.scaffold_overwrite}",
                            "verdict": "${step.n4m.verdict}",
                            "extend_repo": "${step.n4m.matched_repo}",
                        }
                    },
                },
                {
                    "id": "n6",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_create",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "title": "${param.task_title}",
                            "body": "Task for ${param.slug}. Scaffold: ${result.repo_path}. Deliver via PR; self-test evidence required.",
                            # issue #427: extension traceability — extend-verdict runs carry the extension label
                            # (github_api filters empty strings; non-extension runs keep the same label set)
                            "labels": ["state:ready", "${step.n4m.extension_label}"],
                            # issue #387: synthetic E2E dry_run — deterministic fake issue, zero GitHub writes
                            "dry_run": "${param.node_dry_run}",
                        }
                    },
                },
                {
                    "id": "n7",
                    "name": "claude_dispatch",
                    "config": {
                        # issue #358: the watchdog (#257, default 1800s) must fire after the node's internal
                        # timeout, otherwise the node is cancelled and the detach semantics (#358) are never
                        # reached — 3600 + 300s margin. If dispatch_timeout is raised above 3900, raise this too
                        "timeout": 3900,
                        "params": {
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${result.number}",
                            # issue #431: with an empty design_section string (no design) the prompt stays
                            # byte-identical; with assets the full design-contract section is appended
                            # (structure/tokens per export, no drift outside fd-design-lint)
                            "prompt": "${param.task_prompt}${step.n4d.design_section}${step.n4m.extension_contract}",
                            "context_links": ["${result.repo_path}/CLAUDE.md"],
                            "cwd": "${result.repo_path}",
                            "timeout": "${param.dispatch_timeout}",
                            "max_tokens": "${param.dispatch_max_tokens}",
                            # issue #387: synthetic E2E dry_run — no session spawned, deterministic fake PR
                            "dry_run": "${param.node_dry_run}",
                        },
                    },
                },
                {
                    # issue #362: machine acceptance judges delivery by closing keyword — mention-only PRs (hygiene/aside) block
                    "id": "n8",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "linked_pr",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${result.number}",
                            "require": True,
                            "require_closing_link": "${param.require_closing_link}",
                            # issue #387: synthetic E2E dry_run — deterministic fake linked PR, zero GitHub reads
                            "dry_run": "${param.node_dry_run}",
                        }
                    },
                },
                {
                    "id": "n9",
                    "name": "studio_run",
                    "config": {
                        "params": {
                            "graph_id": "${param.env_graph_id}",
                            "agent_id": "${param.env_agent_id}",
                            # issue #346: env pack also receives canonical artifact id handoff (PRD + architecture)
                            # issue #478: falls back to inline report handoff when artifacts are empty (|fallback)
                            "input": "Prepare the env/mock pack for product ${param.slug} per the PRD and architecture below.\n\nPRD artifact id (canonical reference; may be absent):\n${step.n1.result.artifacts.0|(no artifact id — use the PRD document below)}\n\nArchitecture artifact id (canonical reference; may be absent):\n${step.n3.result.artifacts.0|(no artifact id — use the architecture document below)}\n\nPRD document:\n${step.n1.result.last_result.report}\n\nArchitecture document:\n${step.n3.result.last_result.report}",
                            "mode": "sync",
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: seat model pinning (synthetic E2E pins replay-dev; empty = upstream picks)
                            "model": "${param.seat_model}",
                            # issue #409: upstream wait configurable (default 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    "id": "n10",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "闸门 C: 人工验收",  # i18n: user-facing
                            "details": "支持逐项 batch 决定 (items[]); 全拒 = 任务回退, 混合 = 带逐项结论继续",  # i18n: user-facing
                            # issue #387: gates_human=false (synthetic E2E) auto-passes gate C
                            "require_review": "${param.gates_human}",
                        }
                    },
                },
                {
                    "id": "n11",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_set_labels",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${result.number}",
                            "labels": ["state:accepted"],
                            # issue #387: synthetic E2E dry_run — zero GitHub writes
                            "dry_run": "${param.node_dry_run}",
                        }
                    },
                },
                {
                    "id": "n12",
                    "name": "github_api",
                    "config": {
                        "params": {
                            "op": "issue_comment",
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            # issue #478: the ${result.*} shorthand resolves against the nearest upstream
                            # (n11 issue_set_labels) whose envelope has no number — reference the
                            # issue_create node n6's result.number explicitly
                            "issue_number": "${step.n6.result.number}",
                            "body": "Gate C accepted — PR ${result.pr_url} evidence archived (run ${run_id})",
                            # issue #387: synthetic E2E dry_run — zero GitHub writes
                            "dry_run": "${param.node_dry_run}",
                        }
                    },
                },
                {
                    # issue #452: demo contract gate — demo_gate=false is a successful no-op; true validates
                    # the three deliverables, missing items = delivery failure (AGENTS.md rule 7)
                    "id": "n13",
                    "name": "demo_contract",
                    "config": {
                        "params": {
                            "enabled": "${param.demo_gate}",
                            "deliverable_dir": "${param.demo_deliverable_dir}",
                            "slug": "${param.slug}",
                            "check_script": "${param.demo_check_script}",
                            "register": "${param.demo_register}",
                            "executor_url": "${param.demo_executor_url}",
                        }
                    },
                },
            ],
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n4d"},
                {"source_id": "n4d", "target_id": "n3"},
                {"source_id": "n3", "target_id": "n4m"},
                {"source_id": "n4m", "target_id": "n4"},
                {"source_id": "n4", "target_id": "n5"},
                {"source_id": "n4m", "target_id": "n5"},
                {"source_id": "n5", "target_id": "n6"},
                {"source_id": "n6", "target_id": "n7"},
                {"source_id": "n5", "target_id": "n7"},
                {"source_id": "n7", "target_id": "n8"},
                {"source_id": "n6", "target_id": "n8"},
                {"source_id": "n8", "target_id": "n9"},
                {"source_id": "n9", "target_id": "n10"},
                {"source_id": "n10", "target_id": "n11"},
                {"source_id": "n6", "target_id": "n11"},
                {"source_id": "n11", "target_id": "n12"},
                {"source_id": "n6", "target_id": "n12"},
                {"source_id": "n8", "target_id": "n12"},
                {"source_id": "n12", "target_id": "n13"},
            ],
        },
    },
]


def _substitute_params(value: Any, params: Dict[str, Any]) -> Any:
    # issue #287: recursively substitute ${param.X} placeholders in node config — unmatched
    # placeholders stay literal (WARNING logged for observability); failing assembly when a
    # known param is missing is more honest than silently shipping org/repo.
    if isinstance(value, str):
        # whole-string placeholder -> keep the param's original type (issue_number 42 stays int, not the string "42")
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
    """Template manager — manages built-in and user custom templates.

    Features:
    - Built-in template listing
    - Template search/recommendation (AI-backed)
    - Load template as workflow
    - User custom template save/load
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
        """List templates.

        Args:
            category: Filter by category
            tag: Filter by tag
            needs_ai: Filter by whether AI is needed

        Returns:
            list[dict]: Template list
        """
        templates = list(BUILTIN_TEMPLATES)

        # Load user custom templates
        for tpl_file in self._custom_dir.glob("*.json"):
            try:
                tpl = json.loads(tpl_file.read_text(encoding="utf-8"))
                templates.append(tpl)
            except Exception as e:
                logger.warning(f"Failed to load custom template {tpl_file}: {e}")

        # Filter
        if category:
            templates = [t for t in templates if t.get("category", "") == category]
        if tag:
            templates = [t for t in templates if tag in t.get("tags", [])]
        if needs_ai is not None:
            templates = [t for t in templates if t.get("needs_ai", False) == needs_ai]

        return templates

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed info for a specific template."""
        for tpl in self.list_templates():
            if tpl.get("id") == template_id:
                return tpl
        return None

    def template_to_workflow(
        self, template_id: str, param_values: Optional[Dict[str, Any]] = None
    ) -> Optional[Workflow]:
        """Load a template as an executable workflow.

        Args:
            template_id: Template ID
            param_values: issue #287 template-level params — ${param.X} placeholders in
                node config are replaced with these values at assembly time
                (repo/issue_number/approvals_url etc.)

        Returns:
            Optional[Workflow]: Workflow instance, or None if the template does not exist
        """
        tpl = self.get_template(template_id)
        if not tpl:
            logger.error(f"Template not found: {template_id}")
            return None

        workflow_data = tpl.get("workflow", {})
        # issue #287: template param assembly — schema defaults first, explicit param_values override;
        # placeholders never linger in node config even when no value is passed
        effective = {
            key: (spec or {}).get("default")
            for key, spec in (tpl.get("params", {}).get("properties") or {}).items()
            if (spec or {}).get("default") is not None
        }
        effective.update({k: v for k, v in (param_values or {}).items() if v is not None})
        # issue #320: seat ID either-or — explicit value wins and the other side is cleared, avoiding studio_run double-set validation conflicts
        for pair in tpl.get("params", {}).get("seat_pairs") or []:
            if len(pair) == 2 and all(p in effective for p in pair):
                first, second = pair
                if str(effective.get(first) or "").strip():
                    effective[second] = ""
                elif str(effective.get(second) or "").strip():
                    effective[first] = ""
        if effective:
            workflow_data = _substitute_params(workflow_data, effective)
        wf = Workflow.from_dict(workflow_data)
        wf.id = f"tpl_{template_id}"
        return wf

    def save_custom_template(self, template: Dict[str, Any]) -> bool:
        """Save a user custom template."""
        try:
            tpl_id = template.get("id", f"custom_{int(__import__('time').time())}")
            tpl_file = self._custom_dir / f"{tpl_id}.json"
            tpl_file.write_text(
                json.dumps(template, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"Custom template saved: {tpl_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save custom template: {e}")
            return False

    def delete_custom_template(self, template_id: str) -> bool:
        """Delete a custom template."""
        tpl_file = self._custom_dir / f"{template_id}.json"
        if tpl_file.exists():
            tpl_file.unlink()
            return True
        return False

    def get_categories(self) -> List[str]:
        """Get all template categories."""
        categories = set()
        for tpl in self.list_templates():
            cat = tpl.get("category", "其他")  # i18n: user-facing
            categories.add(cat)
        return sorted(categories)

    def search_templates(self, query: str) -> List[Dict[str, Any]]:
        """Search templates (matches name and description)."""
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
