---
name: /spreadsheet-recon
title: 导出对账
title_en: Spreadsheet reconciliation
description: 预算工作簿 vs 导出目录逐科目对账, 产出差异工作簿并标记超阈值项
description_en: Reconcile a budget workbook against an exports directory, flagging items over threshold
category: office
aliases: [导出对账]
handler: builtin:/spreadsheet-recon
argument_template: "{budget_path} {exports_dir} {threshold_pct}"
params:
  - name: budget_path
    label: 预算工作簿
    label_en: Budget workbook
    type: file
    required: true
    placeholder: ~/finance/budget.xlsx
  - name: exports_dir
    label: 导出目录
    label_en: Exports directory
    type: dir
    required: true
    placeholder: ~/finance/exports
  - name: threshold_pct
    label: 超阈值百分比
    label_en: Threshold percent
    type: float
    default: 10
---

将预算工作簿的每个科目与实际导出的累计金额逐项比对, 产出对账工作簿 (差异列 + 超阈值标红),
并列出未对齐的预算科目与无预算的实报科目。全程本地确定性计算。
