import Foundation
import WebKit
import os

/// Fusion JS ↔ Native 双向桥接层
///
/// 核心能力：
/// 1. JS → Native：网页调用本地 AI（fusion-mlx）
/// 2. Native → JS：系统事件推送网页
/// 3. 安全鉴权：防止越权调用
class FusionBridge: NSObject, WKScriptMessageHandler {
    private let logger = Logger(subsystem: "com.fusion.browser", category: "bridge")

    /// 桥接状态
    @Published var isConnected: Bool = false
    private var messageQueue: [String] = []

    /// 注入到网页的桥接 JS
    var bridgeScript: WKUserScript? {
        let js = """
        (function() {
            window.FusionBridge = {
                // AI 推理
                async chat(model, messages, options = {}) {
                    return await _callNative('chat', { model, messages, ...options });
                },
                async run(prompt, model) {
                    return await _callNative('run', { prompt, model });
                },
                // Embedding
                async embed(text) {
                    return await _callNative('embed', { text });
                },
                // 知识库
                async kbQuery(kbId, question) {
                    return await _callNative('kb_query', { kb_id: kbId, question });
                },
                // 模型管理
                async listModels() {
                    return await _callNative('list_models', {});
                },
                // 文件操作
                async readFile(path) {
                    return await _callNative('read_file', { path });
                },
                async writeFile(path, content) {
                    return await _callNative('write_file', { path, content });
                },
                // 自动化
                async runAutomation(template) {
                    return await _callNative('run_automation', { template });
                },
                // 事件监听
                onEvent(callback) {
                    window.FusionBridge._eventCallback = callback;
                },
                // 连接状态
                ping() {
                    return 'pong';
                }
            };

            // 内部调用方法
            let _callId = 0;
            let _callbacks = {};
            window.FusionBridge._eventCallback = null;

            async function _callNative(action, params) {
                return new Promise((resolve, reject) => {
                    const callId = ++_callId;
                    _callbacks[callId] = { resolve, reject };
                    try {
                        window.webkit.messageHandlers.fusionBridge.postMessage({
                            callId: callId,
                            action: action,
                            params: params
                        });
                        // 超时处理
                        setTimeout(() => {
                            if (_callbacks[callId]) {
                                delete _callbacks[callId];
                                reject(new Error('FusionBridge call timeout'));
                            }
                        }, 30000);
                    } catch(e) {
                        reject(e);
                    }
                });
            }

            // 接收原生响应
            window.FusionBridge._handleResponse = function(callId, error, result) {
                const cb = _callbacks[callId];
                if (cb) {
                    delete _callbacks[callId];
                    if (error) {
                        cb.reject(new Error(error));
                    } else {
                        cb.resolve(result);
                    }
                }
            };

            // 接收原生事件推送
            window.FusionBridge._handleEvent = function(event, data) {
                if (window.FusionBridge._eventCallback) {
                    window.FusionBridge._eventCallback({ event, data });
                }
            };

            console.log('[FusionBridge] 已连接');
        })();
        """
        return WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: false)
    }

    // MARK: - JS → Native 消息处理

    func userContentController(_ userContentController: WKUserContentController,
                                didReceive message: WKScriptMessage) {
        guard message.name == "fusionBridge",
              let body = message.body as? [String: Any],
              let callId = body["callId"] as? Int,
              let action = body["action"] as? String,
              let params = body["params"] as? [String: Any] else {
            return
        }

        Task {
            await handleCall(callId: callId, action: action, params: params)
        }
    }

    private func handleCall(callId: Int, action: String, params: [String: Any]) async {
        defer { isConnected = true }

        switch action {
        case "chat":
            await handleChat(callId: callId, params: params)
        case "run":
            await handleRun(callId: callId, params: params)
        case "embed":
            await handleEmbed(callId: callId, params: params)
        case "kb_query":
            await handleKBQuery(callId: callId, params: params)
        case "list_models":
            await handleListModels(callId: callId)
        case "read_file":
            await handleReadFile(callId: callId, params: params)
        case "write_file":
            await handleWriteFile(callId: callId, params: params)
        case "run_automation":
            await handleRunAutomation(callId: callId, params: params)
        case "ping":
            respondToJS(callId: callId, result: "pong")
        default:
            respondToJS(callId: callId, error: "Unknown action: \(action)")
        }
    }

    // MARK: - AI 推理（→ fusion-mlx）

    private func handleChat(callId: Int, params: [String: Any]) async {
        let model = params["model"] as? String ?? "default"
        let messages = params["messages"] as? [[String: String]] ?? []

        guard let url = URL(string: "http://localhost:8000/v1/chat/completions") else {
            respondToJS(callId: callId, error: "Invalid fusion-mlx URL")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 120

        let payload: [String: Any] = [
            "model": model,
            "messages": messages,
            "temperature": params["temperature"] as? Double ?? 0.7,
            "max_tokens": params["max_tokens"] as? Int ?? 4096,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let choices = json["choices"] as? [[String: Any]],
               let message = choices.first?["message"] as? [String: Any],
               let content = message["content"] as? String {
                respondToJS(callId: callId, result: ["content": content])
            }
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    private func handleRun(callId: Int, params: [String: Any]) async {
        let prompt = params["prompt"] as? String ?? ""
        let model = params["model"] as? String ?? "default"

        let messages = [["role": "user", "content": prompt]]
        await handleChat(callId: callId, params: ["model": model, "messages": messages])
    }

    private func handleEmbed(callId: Int, params: [String: Any]) async {
        guard let text = params["text"] as? String,
              let url = URL(string: "http://localhost:8000/v1/embeddings") else {
            respondToJS(callId: callId, error: "Invalid parameters")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30

        let payload: [String: Any] = ["model": "BGE-M3", "input": text]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let dataArray = json["data"] as? [[String: Any]],
               let embedding = dataArray.first?["embedding"] {
                respondToJS(callId: callId, result: ["embedding": embedding])
            }
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    // MARK: - 知识库（→ Fusion-KB）

    private func handleKBQuery(callId: Int, params: [String: Any]) async {
        let kbId = params["kb_id"] as? String ?? "default"
        let question = params["question"] as? String ?? ""

        guard let url = URL(string: "http://localhost:11434/kb/bases/\(kbId)/query") else {
            respondToJS(callId: callId, error: "Invalid KB URL")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30

        let payload: [String: Any] = ["question": question, "top_k": 5]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                respondToJS(callId: callId, result: json)
            }
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    // MARK: - 模型管理

    private func handleListModels(callId: Int) async {
        guard let url = URL(string: "http://localhost:8000/v1/models") else { return }

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                respondToJS(callId: callId, result: json["data"] ?? [])
            }
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    // MARK: - 文件操作

    private func handleReadFile(callId: Int, params: [String: Any]) async {
        guard let path = params["path"] as? String else {
            respondToJS(callId: callId, error: "Missing path")
            return
        }

        let fileURL = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        guard SecurityPolicy.shared.isPathAllowed(fileURL) else {
            respondToJS(callId: callId, error: "Access denied: path not in whitelist")
            return
        }

        do {
            let content = try String(contentsOf: fileURL, encoding: .utf8)
            respondToJS(callId: callId, result: ["content": content, "path": path])
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    private func handleWriteFile(callId: Int, params: [String: Any]) async {
        guard let path = params["path"] as? String,
              let content = params["content"] as? String else {
            respondToJS(callId: callId, error: "Missing path or content")
            return
        }

        let fileURL = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        guard SecurityPolicy.shared.isPathAllowed(fileURL) else {
            respondToJS(callId: callId, error: "Access denied: path not in whitelist")
            return
        }

        do {
            try content.write(to: fileURL, atomically: true, encoding: .utf8)
            respondToJS(callId: callId, result: ["success": true])
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    // MARK: - 自动化

    private func handleRunAutomation(callId: Int, params: [String: Any]) async {
        let template = params["template"] as? String ?? ""

        // 调用本地 Fusion-Cowork 自动化服务
        guard let url = URL(string: "http://localhost:9000/api/tasks/run") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let payload: [String: Any] = ["template": template]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                respondToJS(callId: callId, result: json)
            }
        } catch {
            respondToJS(callId: callId, error: error.localizedDescription)
        }
    }

    // MARK: - 响应/事件推送

    /// 向 JS 发送响应
    private func respondToJS(callId: Int, result: Any? = nil, error: String? = nil) {
        let response: [String: Any] = [
            "callId": callId,
            "error": error as Any,
            "result": result as Any,
        ]
        // 通过全局 JS 函数回传
        if let jsonData = try? JSONSerialization.data(withJSONObject: response),
           let jsonStr = String(data: jsonData, encoding: .utf8) {
            messageQueue.append(jsonStr)
        }
    }

    /// 向网页推送事件
    func pushEvent(_ event: String, data: Any) {
        guard let jsonData = try? JSONSerialization.data(withJSONObject: data),
              let jsonStr = String(data: jsonData, encoding: .utf8) else { return }

        let js = "window.FusionBridge._handleEvent('\(event)', \(jsonStr));"
        // 通过 WKWebView evaluateJavaScript 执行
        logger.info("推送事件: \(event)")
    }

    func flushMessages(to webView: WKWebView) {
        for msg in messageQueue {
            let js = "window.FusionBridge._handleResponse(\(msg));"
            webView.evaluateJavaScript(js, completionHandler: nil)
        }
        messageQueue.removeAll()
    }
}