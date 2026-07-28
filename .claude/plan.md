# M3 实施计划：生态扩展（插件系统 + 技能机制 + Chrome CDP）

## 目标

PRD M3 阶段三项核心功能的完整实现，含测试和 CLI 集成。

---

## 1. 插件系统 (W7)

### 新增文件
- `fusion_desk/plugins/__init__.py` — 导出 PluginLoader, PluginManifest
- `fusion_desk/plugins/loader.py` — PluginLoader 核心实现
- `fusion_desk/plugins/manifest.py` — PluginManifest 数据结构

### 核心设计

PluginManifest dataclass: name, version, description, author, nodes (list[str]), dependencies (list[str]), entry_point (str, default "plugin")

PluginLoader:
- _plugins_dir: Path (~/.fusion-desk/plugins/)
- _loaded: Dict[str, PluginManifest]
- discover() -> List[PluginManifest]
- load(name) -> List[BaseNode] — 动态导入 plugin.py，扫描 BaseNode 子类，注册到 NodeRegistry
- load_all() -> Dict[str, List[BaseNode]]
- unload(name) -> bool — 从 NodeRegistry 注销
- install(path) -> bool — 从目录/zip 安装
- uninstall(name) -> bool — 卸载并删除文件
- list_plugins() -> List[PluginManifest]

### 插件目录约定
~/.fusion-desk/plugins/my_plugin/manifest.json + plugin.py

manifest.json: {name, version, description, author, nodes, dependencies}

### CLI: fusion-desk plugin install/list/load/unload/uninstall

---

## 2. 技能机制 (W8)

### 新增文件
- `fusion_desk/skills/__init__.py`
- `fusion_desk/skills/registry.py` — SkillRegistry + Skill
- `fusion_desk/skills/builtin.py` — 6 个内置技能

### 核心设计

Skill dataclass: name, description, handler (async callable), category, aliases

SkillRegistry:
- _skills: Dict[str, Skill]
- register(skill)
- execute(name, args) -> Any
- list_skills(category) -> List[Skill]
- search(query) -> List[Skill]

### 内置技能: /cleanup, /classify, /screenshot, /search, /organize, /diskclean

### CLI: fusion-desk skill list/run/search
### MCP: MCPServer 新增 skill_list, skill_run 工具

---

## 3. Chrome CDP (W9)

### 新增文件
- `fusion_desk/nodes/browser/cdp_client.py` — CDPClient (WebSocket)
- `fusion_desk/nodes/browser/cdp_nodes.py` — 10 个 CDP 节点

### CDPClient: connect(port=9222), disconnect(), send(method, params)
高级 API: navigate, get_a11y_tree, click, fill, screenshot, evaluate_js, emulate_viewport, list_network_requests, list_console_messages

### 10 个 CDP 节点
cdp_navigate, cdp_snapshot, cdp_click, cdp_fill, cdp_fill_form, cdp_screenshot, cdp_evaluate, cdp_emulate, cdp_network, cdp_console

### CLI: fusion-desk cdp connect/navigate/snapshot/screenshot/status

---

## 4. 测试: tests/test_m3.py — 约 35-40 tests

## 5. 文件变更
新增 8 个文件，修改 4 个文件 (cli.py, __init__.py, mcp_server.py, README.md)

## 6. 实施顺序: 插件 → 技能 → CDP → 测试 → 文档

## 7. 技术决策
- CDP 通信: websockets 库 (可选依赖)
- 插件安装: zipfile 标准库
- 技能 handler: async (args: str) -> Any
- CDP 测试: mock CDPClient，不依赖真实 Chrome
