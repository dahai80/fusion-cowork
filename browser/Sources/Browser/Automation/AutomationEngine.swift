import Foundation
import WebKit

/// 网页自动化引擎 — 静默网页加载、元素识别、自动操作
///
/// 与 FusionBridge 配合，实现 AI 辅助的网页自动化能力。
/// 所有 AI 调用通过 Bridge 下沉到 fusion-mlx。
class AutomationEngine {
    private weak var webView: WKWebView?
    private weak var bridge: FusionBridge?

    /// 自动化任务队列
    private var taskQueue: [AutomationTask] = []
    private var isRunning = false

    init(webView: WKWebView, bridge: FusionBridge) {
        self.webView = webView
        self.bridge = bridge
    }

    /// 执行自动化任务
    func execute(_ task: AutomationTask) async -> AutomationResult {
        taskQueue.append(task)
        if !isRunning { await runNextTask() }
        return AutomationResult(success: true, message: "任务已加入队列")
    }

    /// 静默加载页面
    func loadPage(url: String, timeout: TimeInterval = 30) async -> AutomationResult {
        guard let webView = webView else {
            return AutomationResult(success: false, message: "WebView 不可用")
        }

        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                if url.hasPrefix("fusion://") {
                    if let html = ProtocolHandler.shared.handle(url: url) {
                        webView.loadHTMLString(html, baseURL: URL(string: "fusion://local/"))
                        continuation.resume(returning: AutomationResult(success: true, message: "本地页面加载完成"))
                    }
                } else if let webURL = URL(string: url) {
                    let request = URLRequest(url: webURL)
                    webView.load(request)
                    continuation.resume(returning: AutomationResult(success: true, message: "页面加载中: \(url)"))
                }
            }
        }
    }

    /// 获取页面文本内容
    func getPageText() async -> String {
        guard let webView = webView else { return "" }
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                webView.evaluateJavaScript("document.body.innerText") { result, _ in
                    continuation.resume(returning: result as? String ?? "")
                }
            }
        }
    }

    /// 获取页面 HTML
    func getPageHTML() async -> String {
        guard let webView = webView else { return "" }
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                webView.evaluateJavaScript("document.documentElement.outerHTML") { result, _ in
                    continuation.resume(returning: result as? String ?? "")
                }
            }
        }
    }

    /// 点击元素（CSS 选择器）
    func clickElement(selector: String) async -> AutomationResult {
        guard let webView = webView else {
            return AutomationResult(success: false, message: "WebView 不可用")
        }
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                webView.evaluateJavaScript("""
                    (function() {
                        const el = document.querySelector('\(selector)');
                        if (el) { el.click(); return 'clicked'; }
                        return 'not found';
                    })();
                """) { result, _ in
                    let success = (result as? String) == "clicked"
                    continuation.resume(returning: AutomationResult(
                        success: success,
                        message: success ? "已点击: \(selector)" : "未找到元素: \(selector)"
                    ))
                }
            }
        }
    }

    /// 输入文本
    func fillInput(selector: String, value: String) async -> AutomationResult {
        guard let webView = webView else {
            return AutomationResult(success: false, message: "WebView 不可用")
        }
        let escapedValue = value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "'", with: "\\'")
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                webView.evaluateJavaScript("""
                    (function() {
                        const el = document.querySelector('\(selector)');
                        if (el) {
                            el.value = '\(escapedValue)';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            return 'filled';
                        }
                        return 'not found';
                    })();
                """) { result, _ in
                    let success = (result as? String) == "filled"
                    continuation.resume(returning: AutomationResult(
                        success: success,
                        message: success ? "已输入: \(selector)" : "未找到元素: \(selector)"
                    ))
                }
            }
        }
    }

    /// 截图
    func takeScreenshot() async -> Data? {
        guard let webView = webView else { return nil }
        return await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                webView.takeSnapshot(with: nil) { image, _ in
                    if let image = image {
                        let data = image.tiffRepresentation
                        continuation.resume(returning: data)
                    } else {
                        continuation.resume(returning: nil)
                    }
                }
            }
        }
    }

    /// AI 辅助元素识别（通过 fusion-mlx 分析页面内容）
    func aiFindElement(description: String) async -> String? {
        let pageText = await getPageText()
        let prompt = """
        根据以下网页内容，找出符合描述的元素CSS选择器。
        描述: \(description)
        网页内容: \(pageText.prefix(2000))
        只返回CSS选择器，不要其他内容。
        """

        // 通过 bridge 调用 fusion-mlx
        // 返回选择器供 clickElement/fillInput 使用
        return nil  // 待实现
    }

    private func runNextTask() async {
        isRunning = true
        while !taskQueue.isEmpty {
            let task = taskQueue.removeFirst()
            // 执行任务
        }
        isRunning = false
    }
}

/// 自动化任务定义
struct AutomationTask {
    let id = UUID()
    let type: TaskType
    let params: [String: Any]

    enum TaskType {
        case loadPage
        case click
        case fillInput
        case extractText
        case screenshot
        case aiAnalyze
    }
}

/// 自动化结果
struct AutomationResult {
    let success: Bool
    let message: String
    let data: [String: Any]?

    init(success: Bool, message: String, data: [String: Any]? = nil) {
        self.success = success
        self.message = message
        self.data = data
    }
}