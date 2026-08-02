# 架构合规整改计划

审计日期: 2026-08-02
关联 Issue: #16
违规等级: P0
合规评级: D

层级定位: 三、基础核心产品 - 云端协同沙箱
核心职责: 提供多人协作会话和协同编辑能力

违规项与整改:

1. desk.mlx.start/stop (desk_rpc.py:415-435) - 删除, fusion-mlx生命周期由系统级服务管理 - P0-S1
2. orchestrator/ Agent编排框架 - AgentOrchestrator移至网关/编排层, cowork改为轻量代理 - P0-S1
3. templates/industry_templates.py - 行业模板改为插件机制, 从核心代码剥离 - P0-S2
4. space/与fusion-projects重叠 - 明确边界: projects管项目资产生命周期, cowork管协作会话生命周期 - P0-S2
5. pyproject.toml描述不一致 - 修正为云端协同沙箱 - P0-S2
6. CapabilityMatrix竞品对比 - 移除benchmark/竞品对比代码 - P0-S3

整改阶段:
P0-S1: 删除desk.mlx.start/stop, AgentOrchestrator移至编排层
P0-S2: 行业模板插件化, 明确space/边界, 修正产品描述
P0-S3: 清理竞品对比代码

合规标准: fusion-cowork应只包含协作会话管理/实时协同编辑/会话历史/权限管理/轻量Agent代理, 不应包含基础设施管理/Agent编排框架/行业模板/竞品对比
