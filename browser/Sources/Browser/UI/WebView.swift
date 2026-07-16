import SwiftUI
import WebKit

/// WKWebView 的 SwiftUI 封装
struct FusionWebView: NSViewRepresentable {
    @ObservedObject var tab: BrowserTab

    func makeNSView(context: Context) -> WKWebView {
        guard let webView = tab.webView else {
            let config = WKWebViewConfiguration()
            let wv = WKWebView(frame: .zero, configuration: config)
            return wv
        }
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {
        // WebView 状态由 TabManager 管理
    }
}