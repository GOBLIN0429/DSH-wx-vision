import CoreGraphics
import Foundation

guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
    print("no window info"); exit(1)
}
let filter = CommandLine.arguments.count > 1 ? CommandLine.arguments[1].lowercased() : nil
for w in info {
    let owner = (w[kCGWindowOwnerName as String] as? String) ?? ""
    let title = (w[kCGWindowName as String] as? String) ?? ""
    let layer = (w[kCGWindowLayer as String] as? Int) ?? -1
    guard layer == 0 else { continue }
    if let f = filter, !(owner.lowercased().contains(f) || title.lowercased().contains(f)) { continue }
    var b = CGRect.zero
    if let bd = w[kCGWindowBounds as String] as? [String: Any] {
        b = CGRect(x: bd["X"] as? Double ?? 0, y: bd["Y"] as? Double ?? 0,
                   width: bd["Width"] as? Double ?? 0, height: bd["Height"] as? Double ?? 0)
    }
    let wid = (w[kCGWindowNumber as String] as? Int) ?? -1
    if owner.isEmpty { continue }
    print("id=\(wid) owner=\"\(owner)\" title=\"\(title)\" rect=\(Int(b.origin.x)),\(Int(b.origin.y)),\(Int(b.width)),\(Int(b.height))")
}
