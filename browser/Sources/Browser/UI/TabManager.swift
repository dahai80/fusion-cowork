import Foundation
import SwiftUI
import WebKit

/// 浏览器标签页
class BrowserTab: ObservableObject, Identifiable {
    let id = UUID()
    @Published var title: String = "新标签页"
    @Published var url: String = "fusion://start/"
    @Published var icon: NSImage? = nil
    @Published var isLoading: Bool = false
    @Published var canGoBack: Bool = false
    @Published var canGoForward: Bool = false
    @Published var isActive: Bool = false
    @Published var statusMessage: String = "就绪"
    @Published var progress: Double = 0.0

    var webView: WKWebView?
    var bridge: FusionBridge?
    var automation: AutomationEngine?

    func load(url: String) {
        self.url = url
        guard let webView = webView else { return }

        if url.hasPrefix("fusion://") {
            // 私有协议 — 本地渲染
            if let html = ProtocolHandler.shared.handle(url: url) {
                webView.loadHTMLString(html, baseURL: URL(string: "fusion://local/"))
                statusMessage = "本地页面加载完成"
            }
        } else if let webURL = URL(string: url) {
            let request = URLRequest(url: webURL)
            webView.load(request)
            statusMessage = "加载中..."
        }
    }

    func reload() {
        load(url: url)
    }

    func goBack() {
        webView?.goBack()
    }

    func goForward() {
        webView?.goForward()
    }
}

/// 标签页管理器
@MainActor
class TabManager: ObservableObject {
    @Published var tabs: [BrowserTab] = []
    @Published var activeTab: BrowserTab?
    @Published var history: [String] = []

    init() {
        addTab(url: "fusion://start/")
    }

    func addTab(url: String) {
        // 创建 WebView 配置
        let config = WKWebViewConfiguration()
        let userContentController = WKUserContentController()

        // 注册桥接处理器
        let bridge = FusionBridge()
        userContentController.add(bridge, name: "fusionBridge")

        // 注入桥接 JS
        if let bridgeJS = bridge.bridgeScript {
            userContentController.addUserScript(bridgeJS)
        }

        config.userContentController = userContentController
        config.websiteDataStore = WKWebsiteDataStore.nonPersistent()

        // 创建 WebView
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.customUserAgent = "FusionBrowser/0.1 (Apple Silicon; macOS; MLX)"

        // 创建标签
        let tab = BrowserTab()
        tab.webView = webView
        tab.bridge = bridge
        tab.automation = AutomationEngine(webView: webView, bridge: bridge)

        // 设置导航代理
        let navigationDelegate = FusionNavigationDelegate(tab: tab, tabManager: self)
        webView.navigationDelegate = navigationDelegate
        webView.uiDelegate = FusionUIDelegate(tab: tab)

        // 加载 URL
        tab.load(url: url)

        tabs.append(tab)
        activateTab(tab)
    }

    func activateTab(_ tab: BrowserTab) {
        activeTab?.isActive = false
        tab.isActive = true
        activeTab = tab
        updateNavigationState(tab)
    }

    func closeTab(_ tab: BrowserTab) {
        tabs.removeAll { $0.id == tab.id }
        if activeTab?.id == tab.id {
            activeTab = tabs.last
            activeTab?.isActive = true
        }
    }

    func updateNavigationState(_ tab: BrowserTab) {
        tab.canGoBack = tab.webView?.canGoBack ?? false
        tab.canGoForward = tab.webView?.canGoForward ?? false
    }

    func addToHistory(_ url: String) {
        if !url.hasPrefix("fusion://") {
            history.insert(url, at: 0)
            if history.count > 100 { history = Array(history.prefix(100)) }
        }
    }
}

// MARK: - WKWebView 导航代理

@MainActor
class FusionNavigationDelegate: NSObject, WKNavigationDelegate {
    weak var tab: BrowserTab?
    weak var tabManager: TabManager?

    init(tab: BrowserTab, tabManager: TabManager) {
        self.tab = tab
        self.tabManager = tabManager
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        tab?.isLoading = true
        tab?.statusMessage = "加载中..."
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        tab?.isLoading = false
        tab?.title = webView.title ?? "无标题"
        tab?.url = webView.url?.absoluteString ?? ""
        tab?.statusMessage = "完成"
        tab?.progress = 1.0
        tabManager?.updateNavigationState(tab!)
        if let url = webView.url?.absoluteString {
            tabManager?.addToHistory(url)
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        tab?.isLoading = false
        tab?.statusMessage = "加载失败: \(error.localizedDescription)"
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        tab?.isLoading = true
    }
}

// MARK: - WKWebView UI 代理

@MainActor
class FusionUIDelegate: NSObject, WKUIDelegate {
    weak var tab: BrowserTab?

    init(tab: BrowserTab) {
        self.tab = tab
    }

    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        // 新窗口在当前标签打开
        if let url = navigationAction.request.url {
            tab?.load(url: url.absoluteString)
        }
        return nil
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        tab?.statusMessage = "JS Alert: \(message)"
        completionHandler()
    }
}