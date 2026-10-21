---
name: /weekly-readout
title: 周报
title_en: Weekly readout
description: 汇总本机生命周期事件, 生成每周运行 / 失败统计与可选摘要
description_en: Summarize local lifecycle events into a weekly run/failure readout
category: productivity
aliases: [周报]
handler: weekly_readout
params:
  - name: days
    label: 统计天数
    label_en: Days
    type: int
    default: 7
  - name: source_dir
    label: 生命周期日志目录
    label_en: Lifecycle log directory
    type: dir
    placeholder: 默认 FUSION_COWORK_LIFECYCLE_DIR
  - name: output_dir
    label: 输出目录
    label_en: Output directory
    type: dir
    placeholder: 默认 ~/.fusion-cowork/reports
  - name: narrative
    label: 生成文字摘要
    label_en: Generate narrative
    type: bool
    default: false
---

根据本机生命周期事件统计最近一段时间的运行与失败情况, 按域与事件类型汇总,
列出失败项。启用文字摘要时用本机模型写一段简短管理摘要。
