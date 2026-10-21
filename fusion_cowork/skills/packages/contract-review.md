---
name: /contract-review
title: 合同审查
title_en: Contract review
description: 本地审查合同, 依据知识库审查要点逐条产出带引用的备忘
description_en: Local contract review producing a clause-cited memo grounded in KB playbook hits
category: legal
aliases: [合同审查]
handler: contract_review
params:
  - name: contract_path
    label: 合同文件
    label_en: Contract file
    type: file
    required: true
    placeholder: ~/Documents/contract.md
  - name: kb_id
    label: 知识库 ID (审查要点)
    label_en: Knowledge base ID (playbook)
    type: string
    placeholder: 留空则仅按指令审查
  - name: playbook_query
    label: 检索问题
    label_en: Retrieval query
    type: string
    default: 合同审查要点
  - name: output_dir
    label: 输出目录
    label_en: Output directory
    type: dir
    placeholder: 默认与合同同目录
  - name: model
    label: 模型
    label_en: Model
    type: string
    placeholder: 默认 FUSION_MLX_MODEL
output_template: |
  ## 风险条款
  | 条款 | 原文摘录 | 风险等级 | 依据 | 建议 |
  |---|---|---|---|---|

  ## 缺失条款
  ## 结论
---

你是企业法务合同审查助手。审查下面这份合同, 并按输出模板给出结论:

1. 逐条列出高风险 / 中风险条款, 引用知识库要点的编号 ([P1]、[P2]...)。
2. 指出合同中缺失的常见保护条款。
3. 每条风险给出可执行的修改建议。
4. 只依据给定合同与审查要点作答; 不要编造合同中没有的条款。
5. 全部内容在本机处理, 不得调用外部网络服务。
