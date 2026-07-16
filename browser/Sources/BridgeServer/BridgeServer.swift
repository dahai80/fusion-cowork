import Foundation
import Network

/// 桥接服务器 — 与 Python 后端通信的 HTTP 服务
///
/// 监听本地端口，接收来自 Fusion-Desk Python 后端的命令：
/// - 打开 URL
/// - 执行自动化任务
/// - 查询浏览器状态
/// - 关闭浏览器
@available(macOS 14.0, *)
public final class BridgeServer: @unchecked Sendable {
    private var listener: NWListener?
    private var port: UInt16
    private let onRequestLock = NSLock()
    private var _onRequest: ((String) -> String)?

    public init(port: UInt16 = 9234) {
        self.port = port
    }

    /// 启动服务器
    public func start() throws {
        let params = NWParameters.tcp
        listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
        listener?.newConnectionHandler = { [weak self] connection in
            connection.start(queue: .main)
            self?.handleConnection(connection)
        }
        listener?.start(queue: .main)
        print("[BridgeServer] 已启动，监听端口: \(port)")
    }

    /// 停止服务器
    public func stop() {
        listener?.cancel()
        listener = nil
        print("[BridgeServer] 已停止")
    }

    /// 设置请求处理器
    public func setRequestHandler(_ handler: @escaping (String) -> String) {
        onRequestLock.withLock { _onRequest = handler }
    }

    private func handleConnection(_ connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, _, _ in
            guard let data = data, let request = String(data: data, encoding: .utf8) else {
                connection.cancel()
                return
            }

            let handler = self?.onRequestLock.withLock { self?._onRequest }
            let response = handler?(request) ?? "{\"error\":\"no handler\"}"
            connection.send(content: response.data(using: .utf8), completion: .contentProcessed({ _ in
                connection.cancel()
            }))
        }
    }
}