// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FusionBrowser",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [],
    targets: [
        // 主浏览器应用
        .executableTarget(
            name: "FusionBrowser",
            dependencies: ["BridgeServer"],
            path: "Sources/Browser",
            resources: [
                .process("Resources"),
            ]
        ),
        // 桥接服务层（与 Python 后端通信）
        .target(
            name: "BridgeServer",
            path: "Sources/BridgeServer"
        ),
    ]
)