import SwiftUI

struct BrowserView: View {
    @EnvironmentObject private var tabManager: TabManager
    @State private var urlText: String = "fusion://start/"
    @State private var showSidebar: Bool = true

    var body: some View {
        HSplitView {
            // 左侧边栏
            if showSidebar {
                SidebarView()
                    .frame(minWidth: 200, maxWidth: 300)
            }

            // 主内容区
            VStack(spacing: 0) {
                // 工具栏
                ToolbarView(urlText: $urlText, showSidebar: $showSidebar)

                // 标签页
                TabBarView()

                // 网页内容
                ZStack {
                    if let tab = tabManager.activeTab {
                        FusionWebView(tab: tab)
                    } else {
                        EmptyStateView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                // 状态栏
                StatusBarView()
            }
        }
    }
}

// MARK: - 工具栏

struct ToolbarView: View {
    @EnvironmentObject private var tabManager: TabManager
    @Binding var urlText: String
    @Binding var showSidebar: Bool

    var body: some View {
        HStack(spacing: 8) {
            // 侧边栏切换
            Button(action: { showSidebar.toggle() }) {
                Image(systemName: "sidebar.left")
            }
            .buttonStyle(.borderless)

            // 后退
            Button(action: { tabManager.activeTab?.goBack() }) {
                Image(systemName: "chevron.left")
            }
            .buttonStyle(.borderless)
            .disabled(!(tabManager.activeTab?.canGoBack ?? false))

            // 前进
            Button(action: { tabManager.activeTab?.goForward() }) {
                Image(systemName: "chevron.right")
            }
            .buttonStyle(.borderless)
            .disabled(!(tabManager.activeTab?.canGoForward ?? false))

            // 刷新
            Button(action: { tabManager.activeTab?.reload() }) {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)

            // 地址栏
            TextField("输入 URL 或搜索...", text: $urlText)
                .textFieldStyle(.roundedBorder)
                .onSubmit {
                    tabManager.activeTab?.load(url: urlText)
                }
                .font(.system(size: 13))

            // 加载指示器
            if tabManager.activeTab?.isLoading == true {
                ProgressView()
                    .scaleEffect(0.7)
                    .frame(width: 16)
            }

            // 新标签页
            Button(action: { tabManager.addTab(url: "fusion://start/") }) {
                Image(systemName: "plus")
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color(NSColor.controlBackgroundColor))
    }
}

// MARK: - 标签栏

struct TabBarView: View {
    @EnvironmentObject private var tabManager: TabManager

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 0) {
                ForEach(tabManager.tabs) { tab in
                    TabItemView(tab: tab)
                }
            }
        }
        .frame(height: 32)
        .background(Color(NSColor.windowBackgroundColor))
    }
}

struct TabItemView: View {
    @EnvironmentObject private var tabManager: TabManager
    @ObservedObject var tab: BrowserTab
    @State private var hovered = false

    var body: some View {
        HStack(spacing: 4) {
            // 图标
            if let icon = tab.icon {
                Image(nsImage: icon)
                    .resizable()
                    .frame(width: 14, height: 14)
            } else {
                Image(systemName: "globe")
                    .font(.caption)
            }

            // 标题
            Text(tab.title)
                .font(.system(size: 12))
                .lineLimit(1)

            // 关闭按钮
            Button(action: { tabManager.closeTab(tab) }) {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
            }
            .buttonStyle(.borderless)
            .opacity(hovered ? 1 : 0.3)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(tab.isActive ? Color(NSColor.selectedControlColor) : Color.clear)
        .cornerRadius(4)
        .onHover { hovered = $0 }
        .onTapGesture { tabManager.activateTab(tab) }
    }
}

// MARK: - 侧边栏

struct SidebarView: View {
    @EnvironmentObject private var tabManager: TabManager

    var body: some View {
        List {
            Section("Fusion 生态") {
                SidebarItem(title: "首页", icon: "house", url: "fusion://start/")
                SidebarItem(title: "知识库", icon: "books.vertical", url: "fusion://kb/")
                SidebarItem(title: "模型管理", icon: "cpu", url: "fusion://model/")
                SidebarItem(title: "自动化", icon: "gearshape.2", url: "fusion://automation/")
                SidebarItem(title: "终端", icon: "terminal", url: "fusion://terminal/")
            }

            Section("最近访问") {
                ForEach(tabManager.history.prefix(10), id: \.self) { url in
                    SidebarItem(title: url, icon: "clock", url: url)
                }
            }
        }
        .listStyle(.sidebar)
    }
}

struct SidebarItem: View {
    let title: String
    let icon: String
    let url: String
    @EnvironmentObject private var tabManager: TabManager

    var body: some View {
        HStack {
            Image(systemName: icon)
                .frame(width: 20)
            Text(title)
                .font(.system(size: 13))
            Spacer()
        }
        .padding(.vertical, 2)
        .onTapGesture {
            tabManager.addTab(url: url)
        }
    }
}

// MARK: - 空状态

struct EmptyStateView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "globe")
                .font(.system(size: 48))
                .foregroundColor(.secondary)
            Text("Fusion Browser")
                .font(.title2)
                .fontWeight(.medium)
            Text("专为本地 AI 自动化而生的内嵌浏览器引擎")
                .foregroundColor(.secondary)
            Text("fusion://start/ · fusion://kb/ · fusion://model/ · fusion://automation/")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - 状态栏

struct StatusBarView: View {
    @EnvironmentObject private var tabManager: TabManager

    var body: some View {
        HStack {
            if let tab = tabManager.activeTab {
                Text(tab.statusMessage)
                    .font(.system(size: 11))
                    .foregroundColor(.secondary)
                Spacer()
                if let bridge = tab.bridge {
                    Text("AI: \(bridge.isConnected ? "已连接" : "未连接")")
                        .font(.system(size: 11))
                        .foregroundColor(bridge.isConnected ? .green : .secondary)
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 2)
        .background(Color(NSColor.controlBackgroundColor))
    }
}