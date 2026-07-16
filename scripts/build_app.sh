#!/bin/bash
# Fusion-Desk macOS .app 打包脚本
# 将 Fusion-Desk CLI + 内嵌浏览器打包为 macOS 原生 .app
# V0.2 特性：macOS 原生应用打包

set -e

APP_NAME="Fusion-Desk"
APP_VERSION="0.2.0"
APP_IDENTITY="com.fusion.desk"
BUILD_DIR="build"
APP_DIR="${BUILD_DIR}/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"

echo "🔨 构建 Fusion-Desk ${APP_VERSION} macOS .app"

# 1. 清理旧构建
rm -rf "${BUILD_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"

# 2. 构建 Python 虚拟环境
echo "  📦 打包 Python 环境..."
python3 -m venv "${RESOURCES_DIR}/venv"
source "${RESOURCES_DIR}/venv/bin/activate"
pip install --quiet -e ".[web]" 2>/dev/null
deactivate

# 3. 复制 Fusion-Desk 源码
echo "  📂 复制程序文件..."
cp -r fusion_desk "${RESOURCES_DIR}/"
cp -r browser "${RESOURCES_DIR}/" 2>/dev/null || true
cp pyproject.toml "${RESOURCES_DIR}/"
cp README.md "${RESOURCES_DIR}/"

# 4. 构建启动脚本
echo "  📝 创建启动脚本..."
cat > "${MACOS_DIR}/${APP_NAME}" << 'SCRIPT'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RESOURCES="${DIR}/../Resources"
export PATH="${RESOURCES}/venv/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="${RESOURCES}:${PYTHONPATH}"

# 启动 Fusion-Desk CLI
cd "${RESOURCES}"
exec "${RESOURCES}/venv/bin/fusion-desk" "$@"
SCRIPT
chmod +x "${MACOS_DIR}/${APP_NAME}"

# 5. 创建 Info.plist
echo "  📋 创建 Info.plist..."
cat > "${CONTENTS_DIR}/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${APP_IDENTITY}</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleVersion</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${APP_VERSION}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
</dict>
</plist>
PLIST

# 6. 创建应用图标（占位符）
echo "  🖼️  创建应用图标..."
# 生成一个简单的 SVG 图标
cat > "${RESOURCES_DIR}/AppIcon.svg" << 'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#1a1a2e"/>
      <stop offset="100%" style="stop-color:#0f3460"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="100" fill="url(#bg)"/>
  <text x="256" y="280" font-family="Arial" font-size="200" font-weight="bold" fill="white" text-anchor="middle">FD</text>
  <text x="256" y="380" font-family="Arial" font-size="40" fill="rgba(255,255,255,0.6)" text-anchor="middle">Fusion-Desk</text>
</svg>
SVG

# 尝试将 SVG 转换为 ICNS（如果可用）
if command -v svg2icns &>/dev/null; then
    svg2icns "${RESOURCES_DIR}/AppIcon.svg" "${RESOURCES_DIR}/AppIcon.icns" 2>/dev/null || true
fi

# 7. 构建 Swift 浏览器（如果存在）
if [ -f "browser/Package.swift" ]; then
    echo "  🖥️  构建内嵌浏览器..."
    cd browser
    swift build -c release 2>/dev/null && echo "    浏览器构建成功" || echo "    ⚠️ 浏览器构建失败"
    if [ -f ".build/release/FusionBrowser" ]; then
        cp ".build/release/FusionBrowser" "${MACOS_DIR}/FusionBrowser"
        chmod +x "${MACOS_DIR}/FusionBrowser"
    fi
    cd ..
fi

# 8. 创建启动桌面快捷方式
echo "  🚀 创建快捷方式..."
LAUNCHER_SCRIPT="${BUILD_DIR}/start_fusion_desk.sh"
cat > "${LAUNCHER_SCRIPT}" << 'SCRIPT'
#!/bin/bash
open "$(dirname "$0")/Fusion-Desk.app"
SCRIPT
chmod +x "${LAUNCHER_SCRIPT}"

echo ""
echo "✅ 打包完成!"
echo "   📍 ${APP_DIR}"
echo "   📏 大小: $(du -sh "${APP_DIR}" | cut -f1)"
echo ""
echo "   🚀 启动: open ${APP_DIR}"
echo "   💻 CLI: ${APP_DIR}/Contents/MacOS/Fusion-Desk"