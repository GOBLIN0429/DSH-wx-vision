import CoreGraphics
import Foundation
let kw = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "微信"
guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { exit(1) }
for w in info {
    let owner = (w[kCGWindowOwnerName as String] as? String) ?? ""
    let layer = (w[kCGWindowLayer as String] as? Int) ?? -1
    guard layer == 0, owner.contains(kw) else { continue }
    let pid = (w[kCGWindowOwnerPID as String] as? Int) ?? -1
    let wid = (w[kCGWindowNumber as String] as? Int) ?? -1
    print("pid=\(pid) wid=\(wid) owner=\(owner)")
}
