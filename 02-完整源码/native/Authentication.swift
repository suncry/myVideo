// Loaded by the GUI process: the system prompt belongs to the visible YingKu app.
import Foundation
import LocalAuthentication
import AppKit

private let stateLock = NSLock()
private var activeContext: LAContext?
private var state: Int32 = -1
private var generation = 0

@_cdecl("yingku_auth_available")
public func authAvailable() -> Int32 {
    let context = LAContext()
    return context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) ? 1 : 0
}

@_cdecl("yingku_auth_begin")
public func authBegin() -> Int32 {
    precondition(Thread.isMainThread)
    authCancel()
    NSApplication.shared.activate(ignoringOtherApps: true)
    let context = LAContext()
    context.localizedCancelTitle = "取消"
    context.touchIDAuthenticationAllowableReuseDuration = 0
    guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else { return -2 }
    stateLock.lock()
    generation += 1
    let request = generation
    activeContext = context
    state = 0
    stateLock.unlock()
    context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "验证身份以进入隐私模式") { success, _ in
        stateLock.lock()
        if generation == request { state = success ? 1 : -1 }
        stateLock.unlock()
    }
    return 0
}

@_cdecl("yingku_auth_status")
public func authStatus() -> Int32 {
    stateLock.lock()
    defer { stateLock.unlock() }
    return state
}

@_cdecl("yingku_auth_cancel")
public func authCancel() {
    stateLock.lock()
    generation += 1
    let context = activeContext
    activeContext = nil
    state = -1
    stateLock.unlock()
    context?.invalidate()
}
