import SwiftUI

@main
struct FusionBrowserApp: App {
    @StateObject private var tabManager = TabManager()

    var body: some Scene {
        WindowGroup {
            BrowserView()
                .environmentObject(tabManager)
                .frame(minWidth: 800, minHeight: 600)
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("新标签页") {
                    tabManager.addTab(url: "fusion://start/")
                }
                .keyboardShortcut("t", modifiers: .command)
            }
        }
    }
}