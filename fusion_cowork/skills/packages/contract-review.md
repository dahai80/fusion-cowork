---
name: contract-review
command: /contract-review
title: 合同审查
title_en: Contract review
description: 本地审查合同, 依据知识库审查要点逐条产出带引用的备忘
description_en: Local contract review producing a clause-cited memo grounded in KB playbook hits
version: "1.0"
category: legal
aliases: [合同审查]
allowed-tools: [fetch_url, file_input, file_output]
handler: contract_review
parameters:
  type: object
  required: [contract_path]
  properties:
    contract_path:
      type: string
      title: 合同文件
      title_en: Contract file
      description: ~/Documents/contract.md
    kb_id:
      type: string
      title: 知识库 ID (审查要点)
      title_en: Knowledge base ID (playbook)
      description: 留空则仅按指令审查
    playbook_query:
      type: string
      title: 检索问题
      title_en: Retrieval query
      default: 合同审查要点
    output_dir:
      type: string
      title: 输出目录
      title_en: Output directory
      description: 默认与合同同目录
    model:
      type: string
      title: 模型
      title_en: Model
      description: 默认 FUSION_MLX_MODEL
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
