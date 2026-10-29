import AppKit
import Foundation
guard CommandLine.arguments.count > 1, let pid = pid_t(CommandLine.arguments[1]) else {
    print("usage: activate <pid>"); exit(1)
}
if let app = NSRunningApplication(processIdentifier: pid) {
    let ok = app.activate(options: [.activateIgnoringOtherApps])
    print("activated:", ok, "name:", app.localizedName ?? "?")
    // raise the main window too
    let axApp = AXUIElementCreateApplication(pid)
    var win: AnyObject?
    if AXUIElementCopyAttributeValue(axApp, kAXMainWindowAttribute as CFString, &win) == .success {
        AXUIElementPerformAction(win as! AXUIElement, kAXRaiseAction as CFString)
        print("raised main window")
    }
} else {
    print("no app for pid")
}
