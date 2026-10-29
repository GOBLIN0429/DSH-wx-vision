import ApplicationServices
import AppKit
import Foundation

// Target geometry for the WeChat window.
//
// Picked to fit inside the main display (1637x1024 pt) with margin for the menu
// bar, and large enough that OCR stays accurate — at 280x380 the glyphs were too
// small and recall fell apart, while 688x796 already worked well.
let DEF_X = 820.0
let DEF_Y = 60.0
let DEF_W = 800.0
let DEF_H = 940.0

// Drift tolerance: WeChat snaps to its own increments and the user drags the
// window around, so exact equality is the wrong test.
let TOL = 4.0

func axAttr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func frameOf(_ win: AXUIElement) -> CGRect? {
    guard let pvRaw = axAttr(win, kAXPositionAttribute),
          let svRaw = axAttr(win, kAXSizeAttribute) else { return nil }
    guard CFGetTypeID(pvRaw) == AXValueGetTypeID(),
          CFGetTypeID(svRaw) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero
    var sz = CGSize.zero
    guard AXValueGetValue(pvRaw as! AXValue, .cgPoint, &p),
          AXValueGetValue(svRaw as! AXValue, .cgSize, &sz) else { return nil }
    return CGRect(x: p.x, y: p.y, width: sz.width, height: sz.height)
}

func wechat() -> (pid_t, AXUIElement)? {
    for app in NSWorkspace.shared.runningApplications {
        let name = app.localizedName ?? ""
        let bundle = app.bundleIdentifier ?? ""
        guard name.contains("微信") || bundle == "com.tencent.xinWeChat" else { continue }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        guard let w = axAttr(axApp, kAXMainWindowAttribute) else { continue }
        guard CFGetTypeID(w) == AXUIElementGetTypeID() else { continue }
        return (app.processIdentifier, w as! AXUIElement)
    }
    return nil
}

func setFrame(_ win: AXUIElement, _ target: CGRect) {
    // Position first: growing the window while it sits low can push its bottom
    // edge off-screen, and the compositor then refuses the size.
    var p = CGPoint(x: target.origin.x, y: target.origin.y)
    if let v = AXValueCreate(.cgPoint, &p) {
        AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, v)
    }
    usleep(120_000)
    var s = CGSize(width: target.size.width, height: target.size.height)
    if let v = AXValueCreate(.cgSize, &s) {
        AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, v)
    }
    usleep(180_000)
}

let a = CommandLine.arguments
var target = CGRect(
    x: a.count > 4 ? Double(a[1]) ?? DEF_X : DEF_X,
    y: a.count > 4 ? Double(a[2]) ?? DEF_Y : DEF_Y,
    width: a.count > 4 ? Double(a[3]) ?? DEF_W : DEF_W,
    height: a.count > 4 ? Double(a[4]) ?? DEF_H : DEF_H
)
let ensureOnly = a.contains("--ensure")

// Clamp to the usable desktop. AX coordinates start at the top-left of the
// primary display, while NSScreen reports from the bottom-left, so the menu
// bar and the Dock both have to be translated before they can bound the frame.
if let scr = NSScreen.main {
    let sf = scr.frame
    let vf = scr.visibleFrame
    let topInset = sf.height - (vf.origin.y + vf.height)
    let bottomLimit = topInset + vf.height
    if target.origin.y < topInset { target.origin.y = topInset }
    if target.origin.x < 0 { target.origin.x = 0 }
    if target.origin.y + target.size.height > bottomLimit {
        target.size.height = bottomLimit - target.origin.y
    }
    if target.origin.x + target.size.width > sf.width {
        target.size.width = sf.width - target.origin.x
    }
}

guard let (pid, win) = wechat() else {
    print("{\"ok\": false, \"error\": \"wechat window not found\"}")
    exit(1)
}

guard let before = frameOf(win) else {
    print("{\"ok\": false, \"error\": \"cannot read window frame\"}")
    exit(1)
}

let matches = abs(before.origin.x - target.origin.x) <= TOL
    && abs(before.origin.y - target.origin.y) <= TOL
    && abs(before.width - target.width) <= TOL
    && abs(before.height - target.height) <= TOL

var fixed = false
if !matches && !ensureOnly {
    setFrame(win, target)
    fixed = true
}

guard let after = frameOf(win) else { exit(1) }

let ok = abs(after.origin.x - target.origin.x) <= TOL
    && abs(after.origin.y - target.origin.y) <= TOL
    && abs(after.width - target.width) <= TOL
    && abs(after.height - target.height) <= TOL

let out: [String: Any] = [
    "ok": ok,
    "pid": Int(pid),
    "target": [target.origin.x, target.origin.y, target.size.width, target.size.height],
    "before": [before.origin.x, before.origin.y, before.size.width, before.size.height],
    "after": [after.origin.x, after.origin.y, after.size.width, after.size.height],
    "adjusted": fixed,
    "already_ok": matches,
]
if let d = try? JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]),
   let s = String(data: d, encoding: .utf8) {
    print(s)
}
exit(ok ? 0 : 2)
