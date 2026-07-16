import Foundation
import WebKit

/// Fusion 私有协议处理器 — 拦截 fusion:// 协议并渲染本地页面
///
/// 支持的协议：
/// - fusion://start/      — 起始页
/// - fusion://kb/         — 知识库管理页面
/// - fusion://model/      — 模型管理页面
/// - fusion://automation/ — 自动化工作流编辑器
/// - fusion://terminal/   — 终端界面
final class ProtocolHandler: @unchecked Sendable {
    static let shared = ProtocolHandler()

    private let resourcePaths: [String: String] = [
        "start": "start",
        "kb": "kb",
        "model": "model",
        "automation": "automation",
        "terminal": "terminal",
    ]

    /// 处理 fusion:// 协议请求
    /// - Returns: HTML 字符串，nil 表示未知协议
    func handle(url: String) -> String? {
        // 解析协议路径
        let path = url
            .replacingOccurrences(of: "fusion://", with: "")
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))

        let page = path.isEmpty ? "start" : path.components(separatedBy: "/").first ?? "start"

        guard let resourceKey = resourcePaths[page] else {
            return notFoundPage(path: url)
        }

        return generatePage(resourceKey: resourceKey, path: path)
    }

    /// 生成页面 HTML
    private func generatePage(resourceKey: String, path: String) -> String {
        let title = pageTitle(for: resourceKey)
        let icon = pageIcon(for: resourceKey)

        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>\(title) — Fusion Browser</title>
            <style>
                * { margin: 0; padding: 0; box-sizing: border-box; }
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
                    color: #e0e0e0;
                    min-height: 100vh;
                    display: flex;
                    flex-direction: column;
                }
                .header {
                    padding: 40px 20px 20px;
                    text-align: center;
                }
                .header h1 { font-size: 28px; font-weight: 600; }
                .header p { color: #888; margin-top: 8px; font-size: 14px; }
                .content {
                    flex: 1;
                    padding: 20px;
                    max-width: 800px;
                    margin: 0 auto;
                    width: 100%;
                }
                .card {
                    background: rgba(255,255,255,0.05);
                    border-radius: 12px;
                    padding: 20px;
                    margin-bottom: 16px;
                    border: 1px solid rgba(255,255,255,0.1);
                    transition: all 0.2s;
                }
                .card:hover { background: rgba(255,255,255,0.08); }
                .card h3 { font-size: 16px; margin-bottom: 8px; }
                .card p { font-size: 13px; color: #999; }
                .status-bar {
                    padding: 12px 20px;
                    background: rgba(0,0,0,0.3);
                    font-size: 12px;
                    color: #666;
                    display: flex;
                    justify-content: space-between;
                }
                .badge {
                    display: inline-block;
                    padding: 2px 8px;
                    border-radius: 4px;
                    font-size: 11px;
                    background: rgba(0,200,100,0.2);
                    color: #4caf50;
                }
                .fusion-bridge-status {
                    position: fixed;
                    bottom: 20px;
                    right: 20px;
                    padding: 8px 16px;
                    background: rgba(0,0,0,0.6);
                    border-radius: 20px;
                    font-size: 12px;
                    color: #4caf50;
                    border: 1px solid rgba(76,175,80,0.3);
                }
                @media (prefers-color-scheme: light) {
                    body { background: #f5f5f7; color: #1d1d1f; }
                    .card { background: rgba(0,0,0,0.03); border-color: rgba(0,0,0,0.1); }
                    .status-bar { background: rgba(0,0,0,0.05); }
                }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>\(icon) \(title)</h1>
                <p>Fusion Browser — 专为本地 AI 自动化而生的内嵌浏览器引擎</p>
            </div>
            <div class="content" id="app">
                <div class="card">
                    <h3>🔄 正在加载...</h3>
                    <p>通过 FusionBridge 连接本地 AI 服务</p>
                </div>
            </div>
            <div class="status-bar">
                <span>\(path)</span>
                <span><span class="badge">🔒 本地离线</span>  Fusion-MLX 原生</span>
            </div>
            <div class="fusion-bridge-status" id="bridgeStatus">🟢 FusionBridge 已连接</div>
            <script>
                // 页面专用逻辑
                const page = '\(resourceKey)';
                document.addEventListener('DOMContentLoaded', async () => {
                    try {
                        const pong = await window.FusionBridge.ping();
                        document.getElementById('bridgeStatus').textContent = '🟢 FusionBridge 已连接';
                    } catch(e) {
                        document.getElementById('bridgeStatus').textContent = '🔴 FusionBridge 未连接';
                    }
                    loadPageContent(page);
                });

                async function loadPageContent(page) {
                    const app = document.getElementById('app');
                    try {
                        const models = await window.FusionBridge.listModels();
                        if (page === 'start') {
                            app.innerHTML = `
                                <div class="card">
                                    <h3>🤖 可用模型</h3>
                                    <p>${models.length > 0 ? models.map(m => m.id || m.model).join(', ') : '暂无模型'}</p>
                                </div>
                                <div class="card">
                                    <h3>📚 知识库</h3>
                                    <p>通过 fusion://kb/ 访问知识库管理</p>
                                </div>
                                <div class="card">
                                    <h3>⚡ 快速开始</h3>
                                    <p>输入 URL 或使用 fusion:// 协议访问本地页面</p>
                                </div>
                            `;
                        } else {
                            app.innerHTML = `<div class="card"><h3>📄 ${page} 页面</h3><p>内容待加载...</p></div>`;
                        }
                    } catch(e) {
                        app.innerHTML = `<div class="card"><h3>⚠️ 桥接未连接</h3><p>请确保 fusion-mlx 正在运行</p></div>`;
                    }
                }
            </script>
        </body>
        </html>
        """
    }

    private func notFoundPage(path: String) -> String {
        return """
        <!DOCTYPE html>
        <html><head><title>404 — Fusion Browser</title>
        <style>body { font-family: -apple-system, sans-serif; padding: 40px; text-align: center; color: #666; }</style>
        </head><body>
        <h1>404</h1>
        <p>未知协议路径: \(path)</p>
        <p>支持的协议: fusion://start/, fusion://kb/, fusion://model/, fusion://automation/, fusion://terminal/</p>
        </body></html>
        """
    }

    private func pageTitle(for key: String) -> String {
        switch key {
        case "start": return "Fusion 起始页"
        case "kb": return "知识库管理"
        case "model": return "模型管理"
        case "automation": return "自动化工作流"
        case "terminal": return "终端"
        default: return "Fusion"
        }
    }

    private func pageIcon(for key: String) -> String {
        switch key {
        case "start": return "🏠"
        case "kb": return "📚"
        case "model": return "🤖"
        case "automation": return "⚡"
        case "terminal": return "💻"
        default: return "🔗"
        }
    }
}