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
        "description": "每议题一次运行: 提案打标 -> 同步 G1 决策任务 -> G1 提案闸门 -> 实施记录 -> 关联 PR 检查 (零 LLM 证据闸门, 无 PR 阻塞: 先提 PR 再进验收, 重跑复查) -> 同步 G4 验收任务 (带 run_id + PR/CI 证据) -> G4 验收闸门 -> accepted 打标 -> 同步 G5 发布任务 -> G5 发布闸门 -> rollout 打标。G1/G4/G5 均自动同步 agent-studio 审批任务, 决策经平台收件箱或签名 webhook 自动续跑。见 architecture/multi-repo-automation-setup-guide-1104.md",
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
                    # issue #294: G1 决策同步 agent-studio — 平台收件箱可见, webhook 续跑本 run
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
                    # issue #306: G4 前置证据闸门 — 纯 REST 读 (零 LLM)。issue 无关联 PR 时
                    # 节点失败阻塞 (错误 "先提 PR 再进验收"), PR 出现后从本步骤重跑即复查;
                    # PR 链接 + CI 状态经 ${result.*} 进入 n4 同步 payload, 审批卡可见。
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
                                    # issue #306: 证据 — n4a 的 linked_pr 结果 (edge 默认端口
                                    # 并入 inputs, ${result.*} 从上游输出取值)
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
                    # issue #294: G5 决策同步 agent-studio (发布闸门前, 平台侧可审批)
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
        "name": "治理-批次审计 (state:approved 巡检)",
        "description": "按 state:approved 标签列出试点仓库议题 -> G 闸门人工复核 -> 审计结论回写议题评论。逐议题派生生命周期 run 需编排层支持, 见 architecture/multi-repo-automation-setup-guide-1104.md",
        "category": "治理",
        "tags": ["治理", "审计", "批次", "G-gate"],
        "icon": "📋",
        "difficulty": "进阶",
        "estimated_time": "人工决定",
        # issue #287: 模板级参数 (批次审计: 仓库 + 巡检标签); issue #294: labels 可从 GUI 表单传入
        "params": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "目标仓库 (owner/name)", "default": "org/repo"},
                "issue_number": {"type": "integer", "description": "议题编号", "default": 1},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "巡检标签 (issue_list 过滤用)",
                    "default": ["state:approved"],
                },
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
                            # issue #294: labels 走模板参数 (整串占位符保留数组类型)
                            "labels": "${param.labels}",
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
    # issue #307: product_lifecycle — full delivery pipeline orchestration.
    # Zero-LLM orchestration: LLM seats run as studio_run nodes; deterministic
    # stages (scaffold / issue creation / PR evidence gate / labels) are plain
    # nodes. Exactly 3 human gates (A PRD / B arch / C acceptance, batch
    # verdicts per #309).
    {
        "id": "product_lifecycle",
        "name": "交付-产品生命周期 (A/B/C 三闸门)",
        "description": "全交付管线: BA 出 PRD -> 闸门 A -> 架构 -> 闸门 B -> 脚手架 ai-<slug> -> 建任务 issue (state:ready) -> claude_dispatch 派发 coder 会话 (PR-only 契约, 预算上限) -> 关联 PR 机器验收 -> Env/Mock 代理 -> 闸门 C 人工验收 (支持逐项 batch 决定) -> accepted 打标 + 证据归档。LLM 座位经 studio_run 调 agent-studio graph 或 agent (issue #320, 默认 delivery-* seat agents)。",
        "category": "交付",
        "tags": ["交付", "管线", "闸门", "脚手架", "派发"],
        "icon": "🏭",
        "difficulty": "进阶",
        "estimated_time": "人工决定",
        "params": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "仓库 slug, 生成 ai-<slug>", "default": "demo"},
                "repo": {"type": "string", "description": "任务 issue 所在仓库 (owner/name)", "default": "org/repo"},
                "task_title": {"type": "string", "description": "任务 issue 标题", "default": "Implement v0.1 scope"},
                "task_prompt": {
                    "type": "string",
                    "description": "claude_dispatch 任务指令",
                    # issue #362: 交付契约要求 PR 正文带关闭关键词 — linked_pr 机器验收按
                    # pr_link_strength=closing 判交付, 仅提及 (hygiene PR) 会被阻塞
                    "default": (
                        "Implement the assigned task per PRD/architecture docs; deliver via PR with self-test "
                        "evidence. The PR body must link this issue with a closing keyword (Closes #N)."
                    ),
                },
                # issue #361: 重跑幂等 — 已有脚手架目录默认拒绝, 传 true 复写脚手架文件
                # (既有产出保留; 空目录始终自动复用)
                "scaffold_overwrite": {
                    "type": "boolean",
                    "description": "重跑时复写已有脚手架目录 (repo_scaffold overwrite=true; 空目录自动复用, 既有产出保留)",
                    "default": False,
                },
                # issue #362: PR 链接强度闸门 — 仅提及 issue 的 PR (hygiene/aside) 阻塞验收
                "require_closing_link": {
                    "type": "boolean",
                    "description": "linked_pr 仅接受关闭关键词关联的 PR (Closes/Fixes/Resolves #N), 仅提及即阻塞",
                    "default": True,
                },
                "ba_graph_id": {
                    "type": "string",
                    "description": "agent-studio BA 代理 graph ID (与 ba_agent_id 二选一)",
                    "default": "",
                },
                "ba_agent_id": {
                    "type": "string",
                    "description": "agent-studio BA 代理 agent ID (与 ba_graph_id 二选一; Builder 部署的 seat agent)",
                    "default": "delivery-ba",
                },
                "arch_graph_id": {
                    "type": "string",
                    "description": "agent-studio 架构代理 graph ID (与 arch_agent_id 二选一)",
                    "default": "",
                },
                "arch_agent_id": {
                    "type": "string",
                    "description": "agent-studio 架构代理 agent ID (与 arch_graph_id 二选一)",
                    "default": "delivery-architect",
                },
                "env_graph_id": {
                    "type": "string",
                    "description": "agent-studio Env/Mock 代理 graph ID (与 env_agent_id 二选一)",
                    "default": "",
                },
                "env_agent_id": {
                    "type": "string",
                    "description": "agent-studio Env/Mock 代理 agent ID (与 env_graph_id 二选一)",
                    "default": "delivery-env-mock",
                },
                # issue #358: 1800s 会硬杀真实交付会话 — 3600s 起步, 可用
                # FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 环境变量全局调整
                "dispatch_timeout": {
                    "type": "number",
                    "description": "派发墙钟上限 (秒, 缺省 3600, 可用 FUSION_COWORK_CLAUDE_DISPATCH_TIMEOUT 覆盖)",
                    "default": 3600,
                },
                "dispatch_max_tokens": {"type": "number", "description": "派发 token 上限", "default": 200000},
                # issue #363/#373: studio_run 座位轮次预算 — studio 默认 8 轮会饿死多产物角色
                # (env-mock/architect), 经 spec.max_turns 显式传入; live 证据 24 轮对
                # 内容密集座位仍耗尽 (run_e5d4d34da020), 缺省提到 48
                "seat_max_turns": {
                    "type": "number",
                    "description": "studio_run 座位轮次预算 (spec.max_turns; 缺省 48, studio 默认 8 轮会饿死多产物角色)",
                    "default": 48,
                },
                # issue #413: 架构评审闸门 (gate B) 在 intake 时二选一 — 缺省不评审 (自动通过),
                # review_arch=true 时闸 B 照常暂停等人工批准
                "review_arch": {
                    "type": "boolean",
                    "description": "是否需要架构评审 (闸门 B); 缺省 false = 自动通过, true = 暂停等人工批准",
                    "default": False,
                },
                # issue #409: studio_run 上游等待可配 — 并发管线 mlx 争抢下 900s 固定等待不够
                "studio_wait_timeout": {
                    "type": "number",
                    "description": "studio_run 各座位 sync 等待终态上限 (秒; 缺省 1800)",
                    "default": 1800,
                },
                # issue #387: 合成 E2E — 三开关缺省全关 = 生产行为; 合成启动传
                # seat_model=replay-dev + gates_human=false + node_dry_run=true,
                # 管线零真实模型 token、零 GitHub 写、无人工闸门
                "seat_model": {
                    "type": "string",
                    "description": "座位模型钉选 (spec.model; 缺省空 = 按 agent manifest 选模; 合成 E2E 用 replay-dev)",
                    "default": "",
                },
                "gates_human": {
                    "type": "boolean",
                    "description": "人工闸门 A/C 是否需要评审 (闸 B 由 review_arch 独立控制); 缺省 true, 合成 E2E 传 false",
                    "default": True,
                },
                "node_dry_run": {
                    "type": "boolean",
                    "description": "GitHub/派发节点合成模式 (确定性伪结果, 零网络); 缺省 false, 合成 E2E 传 true",
                    "default": False,
                },
                # issue #422: 历史项目比对 — match 节点参数; candidates 缺省空 = 永远判 new
                # (现行为), 候选由操作员/上游维护
                "match_candidates": {
                    "type": "array",
                    "description": "候选项目列表 [{slug, repo, capabilities, stack, customer, health}]",
                    "default": [],
                },
                "match_candidates_file": {
                    "type": "string",
                    "description": "候选 JSON 文件路径 (与 match_candidates 合并; 缺省空)",
                    "default": "",
                },
                "match_threshold": {
                    "type": "number",
                    "description": "extend/base 判定分数阈值 (缺省 0.6)",
                    "default": 0.6,
                },
                "own_customer": {
                    "type": "string",
                    "description": "本客户标识 (candidate.customer 相等才判 extend; 缺省空 = 全部自有)",
                    "default": "",
                },
                "required_capabilities": {
                    "type": "array",
                    "description": "需求要求的能力清单 (gaps = 要求 - 候选已具备)",
                    "default": [],
                },
                # issue #431: 客户设计稿 — design_workspace 空 = 无设计稿, 行为与旧版完全一致;
                # 有设计稿时 design_context 经 desigma-mcp (stdio) 导出结构 + 代码, 注入 coder prompt
                "design_workspace": {
                    "type": "string",
                    "description": "客户设计稿工作区路径 (.desigma 目录); 空 = 无设计稿 (现行为)",
                    "default": "",
                },
                "design_command": {
                    "type": "string",
                    "description": "desigma-mcp 命令 (basename 须在 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",
                    "default": "desigma-mcp",
                },
                "design_frame_id": {
                    "type": "number",
                    "description": "导出的设计 frame 节点 id; 0 = 取首个节点",
                    "default": 0,
                },
                "design_languages": {
                    "type": "array",
                    "description": "导出语言 (缺省 react + tailwind)",
                    "default": ["react", "tailwind"],
                },
                "design_timeout": {
                    "type": "number",
                    "description": "desigma-mcp 单请求超时 (秒)",
                    "default": 60,
                },
            },
            # issue #320: 座位 ID 二选一 — 装配时显式 graph_id 优先, 清空 agent 缺省 (反之亦然)
            "seat_pairs": [
                ["ba_graph_id", "ba_agent_id"],
                ["arch_graph_id", "arch_agent_id"],
                ["env_graph_id", "env_agent_id"],
            ],
            "required": ["slug", "repo", "task_title"],
        },
        "workflow": {
            "name": "交付-产品生命周期",
            "description": "PRD gate A -> design context -> arch gate B -> scaffold -> dispatch -> PR evidence -> env/mock -> gate C -> accepted",
            "nodes": [
                {
                    "id": "n1",
                    "name": "studio_run",
                    "config": {
                        "params": {
                            "graph_id": "${param.ba_graph_id}",
                            "agent_id": "${param.ba_agent_id}",
                            "input": "Write the PRD for product ${param.slug}. Output the PRD document content.",
                            "mode": "sync",
                            # issue #363: 显式轮次预算 — studio 默认 8 轮饿死多产物角色
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: 座位模型钉选 (合成 E2E 钉 replay-dev; 空 = 上游选模)
                            "model": "${param.seat_model}",
                            # issue #409: 上游等待可配 (缺省 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    "id": "n2",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "闸门 A: PRD 评审",
                            "details": "批准 PRD 后进入架构设计; 拒绝则 BA 代理按评审意见修订",
                            # issue #387: gates_human=false (合成 E2E) 闸门 A 自动通过
                            "require_review": "${param.gates_human}",
                            # issue #328: 拒绝 → 修订回路 — 回到 n1 (BA) 带评审意见重跑
                            "on_reject": "revise",
                            "revision_node": "n1",
                        }
                    },
                },
                {
                    # issue #431: 设计上下文 — 闸门 A (需求确认) 后挂载; workspace 空 = 无设计稿
                    # 空转成功 (design_section 空串, coder prompt 逐字节不变); 有设计稿时导出
                    # 结构 + 代码, design_section 以完整段落拼在 n7 coder prompt 尾部 (实现红线)
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
                            # issue #346: 规范交接 — 下方传入 BA 产物的 artifact id (agent-studio
                            # run result artifacts[0] 为 id 字符串), 下游座位按 id 取 PRD, 不猜路径
                            "input": "Write the architecture/plan for product ${param.slug} based on the approved PRD below.\n\nPRD artifact id (canonical reference — read the PRD by this artifact id, never guess file paths):\n${step.n1.result.artifacts.0}\n\nPRD document:\n${step.n1.result.last_result.report}",
                            "mode": "sync",
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: 座位模型钉选 (合成 E2E 钉 replay-dev; 空 = 上游选模)
                            "model": "${param.seat_model}",
                            # issue #409: 上游等待可配 (缺省 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    # issue #422: 历史项目比对 — 在闸门 B (确认点) 之前自动运行, 闸 B 暂停时
                    # 运行视图展示判定卡 (分数/缺口), 操作员可改判 (override_output 改向指令)
                    # 后再批准; extend 复用客户已有仓 (增量扩展), base 提炼公共能力后新仓继承
                    # (绝不写他客户仓, 绝不泄露他客户项目名), new 现行为
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
                            "prompt": "闸门 B: 架构评审",
                            "details": "批准架构后脚手架 + 派发; 拒绝则架构代理按评审意见修订",
                            # issue #413: review_arch=false (缺省) → 闸门自动通过不进 HITL
                            "require_review": "${param.review_arch}",
                            # issue #328: 拒绝 → 修订回路 — 回到 n3 (架构) 带评审意见重跑
                            "on_reject": "revise",
                            "revision_node": "n3",
                        }
                    },
                },
                {
                    # issue #361: scaffold_overwrite 模板参数透传 — 重跑管线不再被已有工作树硬阻塞
                    # issue #422: verdict=extend 时 repo_scaffold 转为 clone 已有仓 (增量扩展)
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
                            "labels": ["state:ready"],
                            # issue #387: 合成 E2E dry_run — 确定性伪 issue, 零 GitHub 写
                            "dry_run": "${param.node_dry_run}",
                        }
                    },
                },
                {
                    "id": "n7",
                    "name": "claude_dispatch",
                    "config": {
                        # issue #358: 看门狗 (#257 默认 1800s) 必须晚于节点内部超时触发,
                        # 否则节点被取消、detach 语义 (#358) 永远走不到 — 3600 + 300s 余量。
                        # dispatch_timeout 显式调大超过 3900 时需同步上调此处
                        "timeout": 3900,
                        "params": {
                            "repo": "${param.repo}",
                            "allowed_repos": ["${param.repo}"],
                            "issue_number": "${result.number}",
                            # issue #431: design_section 空串 (无设计稿) 时 prompt 逐字节不变;
                            # 有设计稿时完整设计契约段落拼在尾部 (结构/token 以导出为准, fd-design-lint 外不偏离)
                            "prompt": "${param.task_prompt}${step.n4d.design_section}",
                            "context_links": ["${result.repo_path}/CLAUDE.md"],
                            "cwd": "${result.repo_path}",
                            "timeout": "${param.dispatch_timeout}",
                            "max_tokens": "${param.dispatch_max_tokens}",
                            # issue #387: 合成 E2E dry_run — 不 spawn 会话, 确定性伪 PR
                            "dry_run": "${param.node_dry_run}",
                        },
                    },
                },
                {
                    # issue #362: 机器验收按关闭关键词判交付 — hygiene/aside PR (仅提及 issue) 阻塞
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
                            # issue #387: 合成 E2E dry_run — 确定性伪关联 PR, 零 GitHub 读
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
                            # issue #346: env pack 同样收规范 artifact id 交接 (PRD + 架构)
                            "input": "Prepare the env/mock pack for product ${param.slug} per the PRD and architecture below.\n\nPRD artifact id (canonical reference):\n${step.n1.result.artifacts.0}\n\nArchitecture artifact id (canonical reference):\n${step.n3.result.artifacts.0}\n\nPRD document:\n${step.n1.result.last_result.report}\n\nArchitecture document:\n${step.n3.result.last_result.report}",
                            "mode": "sync",
                            "max_turns": "${param.seat_max_turns}",
                            # issue #387: 座位模型钉选 (合成 E2E 钉 replay-dev; 空 = 上游选模)
                            "model": "${param.seat_model}",
                            # issue #409: 上游等待可配 (缺省 1800s)
                            "timeout": "${param.studio_wait_timeout}",
                        }
                    },
                },
                {
                    "id": "n10",
                    "name": "approval_gate",
                    "config": {
                        "params": {
                            "prompt": "闸门 C: 人工验收",
                            "details": "支持逐项 batch 决定 (items[]); 全拒 = 任务回退, 混合 = 带逐项结论继续",
                            # issue #387: gates_human=false (合成 E2E) 闸门 C 自动通过
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
                            # issue #387: 合成 E2E dry_run — 零 GitHub 写
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
                            "issue_number": "${result.number}",
                            "body": "Gate C accepted — PR ${result.pr_url} evidence archived (run ${run_id})",
                            # issue #387: 合成 E2E dry_run — 零 GitHub 写
                            "dry_run": "${param.node_dry_run}",
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
        # issue #320: 座位 ID 二选一 — 显式值优先, 另一侧清空, 避免 studio_run 校验双设冲突
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
