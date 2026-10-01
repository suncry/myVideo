import AppKit
import CoreFoundation
import ApplicationServices

let bundleID = "com.colliderli.iina"
let command = CommandLine.arguments.dropFirst().first ?? ""
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}
func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, key as CFString, &value)
    return value
}
func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}
func recentMenu(_ element: AXUIElement, depth: Int = 0) -> AXUIElement? {
    guard depth < 5 else { return nil }
    let title = attribute(element, kAXTitleAttribute) as? String ?? ""
    if ["打开最近文件", "打開最近使用的檔案", "打开最近使用的文件", "Open Recent"].contains(title) {
        return children(element).first
    }
    for child in children(element) {
        if let match = recentMenu(child, depth: depth + 1) { return match }
    }
    return nil
}
func clearRecents(_ player: NSRunningApplication, deadline: Date) {
    let element = AXUIElementCreateApplication(player.processIdentifier)
    if let bar = attribute(element, kAXMenuBarAttribute),
       let menu = recentMenu(bar as! AXUIElement) {
        let items = children(menu)
        let entries = items.filter {
            let title = attribute($0, kAXTitleAttribute) as? String ?? ""
            return !title.isEmpty && !["清除菜单", "清除選單", "Clear Menu"].contains(title)
        }
        if entries.isEmpty { print("cleared"); exit(0) }
        for item in items {
            let title = attribute(item, kAXTitleAttribute) as? String ?? ""
            if ["清除菜单", "清除選單", "Clear Menu"].contains(title) {
                if (attribute(item, kAXEnabledAttribute) as? Bool) == false { print("cleared"); exit(0) }
                if AXUIElementPerformAction(item, kAXPressAction as CFString) == .success {
                    // Re-read the menu after IINA has processed the action.
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { clearRecents(player, deadline: deadline) }
                    return
                }
            }
        }
    }
    guard Date() < deadline else { fail("IINA 最近打开菜单未能清空。") }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { clearRecents(player, deadline: deadline) }
}
if command == "request-access" {
    let trusted = AXIsProcessTrusted()
    print(trusted ? "authorized" : "permission-needed")
} else if command == "clear-recents" {
    guard AXIsProcessTrusted() else {
        fail("请在系统设置 → 隐私与安全性 → 辅助功能中允许影库，才能自动清空 IINA 的最近打开菜单。")
    }
    let deadline = Date(timeIntervalSinceNow: 8)
    if let player = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == bundleID }) {
        DispatchQueue.main.async { clearRecents(player, deadline: deadline) }
    } else if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.hides = true
        NSWorkspace.shared.openApplication(at: url, configuration: config) { player, error in
            guard let player = player else { fail("无法打开 IINA 以清理最近打开菜单。") }
            DispatchQueue.main.async { clearRecents(player, deadline: deadline) }
        }
    } else { print("not installed"); exit(0) }
    DispatchQueue.main.asyncAfter(deadline: .now() + 10) { fail("IINA 最近打开菜单清理超时。") }
    application.run()
} else if command == "stop" {
    let players = NSWorkspace.shared.runningApplications.filter { $0.bundleIdentifier == bundleID }
    for player in players where !player.isTerminated {
        if !player.terminate() { fail("IINA 拒绝退出，播放记录尚未清理。") }
    }
    let deadline = Date(timeIntervalSinceNow: 15)
    while players.contains(where: { !$0.isTerminated }) && Date() < deadline {
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
    }
    guard players.allSatisfy({ $0.isTerminated }) else { fail("IINA 尚未退出，播放记录尚未清理。") }
    print("stopped")
} else if command == "clear-preferences" {
    guard !NSWorkspace.shared.runningApplications.contains(where: { $0.bundleIdentifier == bundleID }) else {
        fail("IINA 仍在运行，播放记录尚未清理。")
    }
    let keys = ["iinaLastPlayedFilePath", "iinaLastPlayedFilePosition", "recentDocuments"]
    for host in [kCFPreferencesAnyHost, kCFPreferencesCurrentHost] {
        for key in keys {
            CFPreferencesSetValue(key as CFString, nil, bundleID as CFString, kCFPreferencesCurrentUser, host)
        }
        guard CFPreferencesSynchronize(bundleID as CFString, kCFPreferencesCurrentUser, host) else {
            fail("IINA 上次播放记录未能保存清理结果。")
        }
    }
    CFPreferencesAppSynchronize(bundleID as CFString)
    guard keys.allSatisfy({ CFPreferencesCopyAppValue($0 as CFString, bundleID as CFString) == nil }) else {
        fail("IINA 上次播放记录仍有残留。")
    }
    print("cleared")
} else { fail("Unsupported operation") }
