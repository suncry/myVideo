import AppKit
import UniformTypeIdentifiers

// Use AppKit's event loop so asynchronous Launch Services completion is delivered.
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let workspace = NSWorkspace.shared
let bundleID = "com.colliderli.iina"
let repair = CommandLine.arguments.dropFirst().first == "repair"
let extensions = Array(CommandLine.arguments.dropFirst(2))
let iina = workspace.urlForApplication(withBundleIdentifier: bundleID)
var rows = [[String: Any]]()
var position = 0
func handler(_ type: UTType) -> String {
    guard let url = workspace.urlForApplication(toOpen: type) else { return "" }
    return Bundle(url: url)?.bundleIdentifier ?? ""
}
func finish() {
    let result: [String: Any] = ["installed": iina != nil, "associations": rows]
    let data = try! JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    exit(0)
}
func next() {
    guard position < extensions.count else { finish(); return }
    let ext = extensions[position]
    position += 1
    guard let type = UTType(filenameExtension: ext) else {
        rows.append(["extension": ext, "uti": "", "before": "", "after": "", "status": -1])
        next(); return
    }
    let before = handler(type)
    func record(_ error: Error?) {
        rows.append(["extension": ext, "uti": type.identifier, "before": before,
                     "after": handler(type), "status": (error as NSError?)?.code ?? 0])
        // A denied request is reported, never worked around or repeatedly retried.
        if error != nil { finish() } else { next() }
    }
    if repair, let iina = iina, before.lowercased() != bundleID {
        workspace.setDefaultApplication(at: iina, toOpen: type) { error in
            DispatchQueue.main.async { record(error) }
        }
    } else { record(nil) }
}
DispatchQueue.main.async { next() }
application.run()
