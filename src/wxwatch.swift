import ScreenCaptureKit
import CoreGraphics
import CoreMedia
import Foundation
import AppKit

// MARK: - Configuration

let UNREAD_POLL_MS = 500          // L1: AX unread poll (measured 0.108 ms per read)
let STREAM_FPS = 2                // L2: frames per second from the compositor
let EMIT_MIN_GAP_MS = 700.0       // suppress bursts of change events
let REANCHOR_SECONDS = 20.0       // re-resolve window/menu-extra occasionally

// --until-change: print the event that fired and exit at once, so the caller
// can start its own clock the moment a change is detected instead of waiting
// for a fixed watch window to expire.
let UNTIL_CHANGE = CommandLine.arguments.contains("--until-change")

// Hash only the message list: skip the header band and, importantly, the
// compose box — a blinking caret there would fire a "changed" event every
// second and defeat the whole point.
let REGION_X0 = 0.12              // right of the icon rail / chat list
let REGION_Y0 = 0.06              // below the title bar
let REGION_Y1 = 0.80              // above the compose separator

// The chat list is watched separately: a change there means SOME conversation
// received a message, and WeChat floats that conversation to the top — which is
// how the watcher learns which group to open without cycling through them.
let LIST_X0 = 0.02
let LIST_X1 = 0.36
let LIST_Y0 = 0.05
let LIST_Y1 = 0.82

// MARK: - Helpers

func nowStamp() -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: Date())
}

func emit(_ dict: [String: Any]) {
    var d = dict
    d["t"] = nowStamp()
    if let data = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

func wechatWindows() -> [SCWindow] {
    // filled by the async refresh below
    return currentWindows
}

var currentWindows: [SCWindow] = []

func pickWindow() -> SCWindow? {
    currentWindows.first { w in
        (w.owningApplication?.applicationName ?? "").contains("微信") && w.frame.width > 200
    }
}

// MARK: - Accessibility (L1)

func axAttr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func findMenuExtra(_ el: AXUIElement, _ depth: Int) -> AXUIElement? {
    if depth > 6 { return nil }
    if (axAttr(el, kAXSubroleAttribute) as? String) == "AXMenuExtra" { return el }
    if let kids = axAttr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for k in kids { if let r = findMenuExtra(k, depth + 1) { return r } }
    }
    return nil
}

func wechatPID() -> pid_t? {
    for app in NSWorkspace.shared.runningApplications {
        let n = app.localizedName ?? ""
        let b = app.bundleIdentifier ?? ""
        if n.contains("微信") || b == "com.tencent.xinWeChat" {
            if app.activationPolicy != .prohibited { return app.processIdentifier }
        }
    }
    return nil
}

final class UnreadProbe {
    private var extra: AXUIElement?
    private var pid: pid_t?

    func reanchor() -> Bool {
        guard let p = wechatPID() else { return false }
        pid = p
        let app = AXUIElementCreateApplication(p)
        extra = findMenuExtra(app, 0)
        return extra != nil
    }

    /// Returns the raw menu-extra title, e.g. " 30" or "" when nothing unread.
    func read() -> String? {
        guard let e = extra else { return nil }
        return (axAttr(e, kAXTitleAttribute) as? String)
    }
}

// MARK: - Frame hashing (L2)

func sparseHash(_ pixelBuffer: CVPixelBuffer, x0f: Double = REGION_X0,
                y0f: Double = REGION_Y0, y1f: Double = REGION_Y1) -> UInt64 {
    CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else { return 0 }
    let fullH = CVPixelBufferGetHeight(pixelBuffer)
    let fullW = CVPixelBufferGetWidth(pixelBuffer)
    let bpr = CVPixelBufferGetBytesPerRow(pixelBuffer)

    // Hash only the message-list rectangle. The compose box is skipped on
    // purpose: its blinking caret would otherwise register as a change about
    // once a second.
    let y0 = Int(Double(fullH) * y0f)
    let y1 = Int(Double(fullH) * y1f)
    let x0 = Int(Double(fullW) * x0f) * 4

    var hash: UInt64 = 1469598103934665603
    var y = y0
    while y < y1 {
        let row = base.advanced(by: y * bpr)
        let bytes = row.assumingMemoryBound(to: UInt8.self)
        var x = x0
        let w = fullW * 4
        while x < w {
            hash = (hash ^ UInt64(bytes[x])) &* 1099511628211
            x += 29
        }
        y += 3
    }
    return hash
}

// MARK: - Stream sink

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    var onFrame: ((UInt64, UInt64) -> Void)?   // (listHash, conversationHash)
    var onStop: ((String) -> Void)?

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid else { return }
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        onFrame?(sparseHash(pb, x0f: LIST_X0, y0f: LIST_Y0, y1f: LIST_Y1),
                 sparseHash(pb))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onStop?(error.localizedDescription)
    }
}

// MARK: - Main

_ = NSApplication.shared
NSApp.setActivationPolicy(.accessory)

let probe = UnreadProbe()
let sink = Sink()

var lastUnread: String? = nil
var lastEmittedHash: UInt64? = nil
var lastEmitAt = Date.distantPast
var frameCount = 0
var streamRunning = false
var currentStream: SCStream?

func startStream() async {
    do {
        currentWindows = try await SCShareableContent
            .excludingDesktopWindows(false, onScreenWindowsOnly: true).windows
    } catch {
        emit(["ev": "error", "stage": "shareableContent", "msg": error.localizedDescription])
        return
    }
    guard let win = pickWindow() else {
        emit(["ev": "error", "stage": "window", "msg": "wechat window not found"])
        return
    }

    let f = win.frame
    // SCK refuses a first capture with a global sourceRect, so take the whole
    // window and restrict the hash instead — the cost is identical anyway
    // (measured: ~100 ms regardless of output size).
    let filter = SCContentFilter(desktopIndependentWindow: win)
    let cfg = SCStreamConfiguration()
    cfg.width = Int(f.width * 2)
    cfg.height = Int(f.height * 2)
    cfg.showsCursor = false
    cfg.queueDepth = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(STREAM_FPS))

    let stream = SCStream(filter: filter, configuration: cfg, delegate: sink)
    do {
        try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "wxwatch.cap"))
        try await stream.startCapture()
        currentStream = stream
        streamRunning = true
        emit(["ev": "stream", "state": "started",
              "window": [Int(f.width), Int(f.height)],
              "fps": STREAM_FPS])
    } catch {
        emit(["ev": "error", "stage": "startCapture", "msg": error.localizedDescription])
    }
}

var lastListHash: UInt64?
var lastConvHash: UInt64?

sink.onFrame = { listHash, convHash in
    frameCount += 1
    let now = Date()
    let dt = now.timeIntervalSince(lastEmitAt) * 1000

    if let prev = lastListHash, prev != listHash, dt >= EMIT_MIN_GAP_MS {
        lastEmitAt = now
        lastListHash = listHash
        lastConvHash = convHash
        emit(["ev": "change", "src": "list",
              "hash": String(listHash, radix: 16), "prev": String(prev, radix: 16)])
        if UNTIL_CHANGE { exit(0) }
        return
    }
    lastListHash = listHash

    if let prev = lastConvHash, prev != convHash, dt >= EMIT_MIN_GAP_MS {
        lastEmitAt = now
        lastConvHash = convHash
        emit(["ev": "change", "src": "frame",
              "hash": String(convHash, radix: 16), "prev": String(prev, radix: 16)])
        if UNTIL_CHANGE { exit(0) }
        return
    }
    lastConvHash = convHash
}

sink.onStop = { msg in
    streamRunning = false
    emit(["ev": "stream", "state": "stopped", "msg": msg])
}

// L1 timer
if !probe.reanchor() {
    emit(["ev": "error", "stage": "menuextra", "msg": "wechat menu extra not found (accessibility?)"])
}

let pollTimer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "wxwatch.ax"))
pollTimer.schedule(deadline: .now(), repeating: .milliseconds(UNREAD_POLL_MS))
pollTimer.setEventHandler {
    if let cur = probe.read() {
        if let prev = lastUnread, prev != cur {
            lastUnread = cur
            lastEmitAt = Date()
            emit(["ev": "change", "src": "unread", "from": prev.trimmingCharacters(in: .whitespaces),
                  "to": cur.trimmingCharacters(in: .whitespaces)])
            if UNTIL_CHANGE { exit(0) }
        } else if lastUnread == nil {
            lastUnread = cur
            emit(["ev": "unread", "value": cur.trimmingCharacters(in: .whitespaces)])
        }
    } else {
        _ = probe.reanchor()
    }
}
pollTimer.resume()

emit(["ev": "ready", "poll_ms": UNREAD_POLL_MS, "fps": STREAM_FPS])

Task {
    await startStream()
    while true {
        try? await Task.sleep(nanoseconds: UInt64(REANCHOR_SECONDS * 1_000_000_000))
        if !streamRunning {
            emit(["ev": "stream", "state": "restarting"])
            await startStream()
        }
        _ = probe.reanchor()
    }
}

RunLoop.current.run()
