import Foundation
import LocalAuthentication
import AppKit

if CommandLine.arguments.contains("--watch-lock") {
    let signal: (Notification) -> Void = { _ in
        FileHandle.standardOutput.write(Data("lock\n".utf8))
    }
    DistributedNotificationCenter.default().addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main, using: signal)
    NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main, using: signal)
    NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.sessionDidResignActiveNotification, object: nil, queue: .main, using: signal)
    let owner = getppid()
    _ = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in
        if getppid() != owner { exit(0) }
    }
    RunLoop.main.run()
    exit(0)
}

let context = LAContext()
context.localizedCancelTitle = "取消"
context.touchIDAuthenticationAllowableReuseDuration = 0
var error: NSError?
let biometric = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error)
let available = context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error)
func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value), let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}
if CommandLine.arguments.contains("--probe") {
    emit(["available": available, "biometrics": biometric, "type": context.biometryType.rawValue])
    exit(0)
}
if !available {
    emit(["success": false, "message": "系统身份验证不可用，请检查 Mac 的 Touch ID 与密码设置。"])
    exit(1)
}
// macOS presents Touch ID / Apple Watch first, with the OS login password as recovery.
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "验证身份以打开影库私密模式") { success, error in
    emit(["success": success, "message": success ? "" : (error?.localizedDescription ?? "验证未完成")])
    exit(success ? 0 : 1)
}
RunLoop.main.run()
