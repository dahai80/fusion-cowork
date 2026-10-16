# Fusion Cowork Web

fusion-cowork 的 DAG 工作流画布 UI 包 — 工作流构建器 Web 版 (union-web PRD N-6, issue #94)。

## 范围

- 工作流列表: 运行 / 编辑 / 删除 (危险操作红色确认 + 删除类输入名称二次确认)
- 桌面端 DAG 画布编辑: 节点面板 (来自 `/api/nodes`)、拖拽定位、连线、节点参数编辑器 (含 schema 提示)
- 移动端只读 (`devices: ["desktop", "mobile-readonly"]`): 隐藏节点面板/写操作, 顶部只读横幅
- 运行视图: 实时状态轮询 (可见 5s / 后台 Tab 自动降频 30s)、逐步骤结果、停止
- 内嵌模式 (`?embedded=1` / iframe) 隐藏自有导航 — 统一 Shell 管外壳 (spec S7)

## 技术栈 (Spec §10.1 基线)

React 18 + Vite 5 + TypeScript + Tailwind (preset 来自 `@fusion/web-ui`) + Bun。画布: `@xyflow/react`。

## 命令

```bash
bun install
bun run dev        # vite dev server :11464 (代理 /api /health → :11461)
bun run build      # typecheck + vite build → dist/
bun run typecheck
bun run lint       # biome check src
```

## 后端

UI 消费 cowork web API (`fusion-cowork web serve`, 端口 11461):
节点目录 / 工作流定义 CRUD / 运行生命周期 (提交 / 轮询 / 停止)。
同一服务内置 `web/dist` 静态托管。

鉴权: `FUSION_COWORK_WEB_API_KEY` — gateway `/shell/fusion-cowork/` 代理注入模块密钥; UI 本身不持有密钥 (spec S8)。独立模式密钥接缝 (issue #96): 运行时解析 — `window.__FUSION_COWORK_WEB_API_KEY__` > `localStorage['fusion-cowork-web-api-key']` > 构建期 `VITE_API_KEY` (遗留回退)。无 key 则省略鉴权头 (keyless serve 姿态)。

## Manifest

`manifest.json` 遵循 union-web PRD §5.1 (id `fusion-cowork`, category
`platform`, entry `/shell/fusion-cowork/`, devices `["desktop",
"mobile-readonly"]`)。

## Vendor 包

`@fusion/web-sdk` 与 `@fusion/web-ui` 以 `vendor/@fusion/` 引入 (与 fusion-web-shell 仓 src 逐字节一致, 无私有分叉)。
