import Foundation

/// 安全沙箱策略 — 控制网页可访问的本地资源
///
/// 规则：
/// 1. 私有协议禁止外网访问
/// 2. 网页 JS 禁止任意外网请求
/// 3. 本地文件访问白名单机制
/// 4. 所有 AI 调用强制走本地 fusion-mlx
final class SecurityPolicy: @unchecked Sendable {
    static let shared = SecurityPolicy()

    /// 文件访问白名单
    private let allowedPaths: [String] = [
        NSHomeDirectory() + "/.fusion",
        NSHomeDirectory() + "/Desktop",
        NSHomeDirectory() + "/Documents",
        NSHomeDirectory() + "/Downloads",
    ]

    /// 允许的文件扩展名
    private let allowedExtensions: Set<String> = [
        "txt", "md", "json", "yaml", "yml", "toml",
        "py", "js", "ts", "rs", "go", "swift",
        "pdf", "doc", "docx", "csv",
        "png", "jpg", "jpeg", "gif", "svg",
    ]

    /// 检查路径是否在白名单内
    func isPathAllowed(_ url: URL) -> Bool {
        let resolvedPath = url.resolvingSymlinksInPath().path

        // 检查是否在允许的目录下
        for allowed in allowedPaths {
            let resolvedAllowed = (allowed as NSString).expandingTildeInPath
            if resolvedPath.hasPrefix(resolvedAllowed) {
                // 检查扩展名
                let ext = url.pathExtension.lowercased()
                if ext.isEmpty || allowedExtensions.contains(ext) {
                    return true
                }
            }
        }
        return false
    }

    /// 检查 URL 是否可加载
    func isURLAllowed(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased() else { return false }

        switch scheme {
        case "fusion":
            return true  // 私有协议完全允许
        case "http", "https":
            // 只允许本地服务
            guard let host = url.host?.lowercased() else { return false }
            let allowedHosts = ["localhost", "127.0.0.1"]
            return allowedHosts.contains(host)
        case "file":
            return isPathAllowed(url)
        case "data", "blob":
            return true  // 内联资源允许
        default:
            return false
        }
    }

    /// 检查 JS 调用是否安全
    func isActionAllowed(_ action: String) -> Bool {
        let allowedActions: Set<String> = [
            "chat", "run", "embed", "kb_query", "list_models",
            "read_file", "write_file", "run_automation",
            "ping",
        ]
        return allowedActions.contains(action)
    }
}