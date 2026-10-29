import CoreGraphics
import Foundation

let src = CGEventSource(stateID: .hidSystemState)

// Modifier keys can be left "down" in the HID state by an earlier synthetic
// keystroke. With Control down, a click becomes a secondary click on macOS, and
// WeChat answers with a context menu — which is exactly what stray clicks into
// the chat list started doing. Release them all before clicking, and pin empty
// flags on the mouse events so they cannot inherit anything.
func releaseModifiers() {
    for code: CGKeyCode in [55, 54, 56, 60, 58, 61, 59, 62] {  // cmd ctrl shift caps opt rcmd rshift ropt
        for down in [true, false] where down == false {
            let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
            e?.flags = []
            e?.post(tap: .cghidEventTap)
        }
    }
    usleep(40_000)
}

func click(_ x: Double, _ y: Double) {
    releaseModifiers()
    let p = CGPoint(x: x, y: y)
    let move = CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)
    move?.flags = []
    move?.post(tap: .cghidEventTap)
    usleep(120_000)
    let down = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
    down?.flags = []
    down?.post(tap: .cghidEventTap)
    usleep(60_000)
    let up = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
    up?.flags = []
    up?.post(tap: .cghidEventTap)
    usleep(150_000)
}

func key(_ code: CGKeyCode, flags: CGEventFlags = []) {
    let d = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)
    d?.flags = flags
    d?.post(tap: .cghidEventTap)
    usleep(40_000)
    let u = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)
    u?.flags = flags
    u?.post(tap: .cghidEventTap)
    usleep(60_000)
}

let a = CommandLine.arguments
guard a.count >= 2 else { print("usage: wxtool click X Y | paste | enter | type CODE"); exit(1) }
switch a[1] {
case "pos":
    // Where the cursor actually is, in the same space CGEvent accepts.
    if let e = CGEvent(source: nil) {
        print("cursor=\(e.location.x),\(e.location.y)")
    }
case "move":
    if a.count >= 4, let x = Double(a[2]), let y = Double(a[3]) {
        let p = CGPoint(x: x, y: y)
        let m = CGEvent(mouseEventSource: src, mouseType: .mouseMoved,
                        mouseCursorPosition: p, mouseButton: .left)
        m?.flags = []
        m?.post(tap: .cghidEventTap)
        usleep(200_000)
        if let e = CGEvent(source: nil) {
            print("requested=\(x),\(y) actual=\(e.location.x),\(e.location.y)")
        }
    }
case "release":
    releaseModifiers(); print("released")
case "click":
    guard a.count >= 4, let x = Double(a[2]), let y = Double(a[3]) else { print("bad coords"); exit(1) }
    click(x, y); print("clicked \(x),\(y)")
case "paste":
    key(9, flags: .maskCommand)   // Cmd+V
    print("pasted")
case "enter":
    key(36)                        // Return
    print("enter")
case "escape":
    key(53); print("escape")
case "selectall":
    key(0, flags: .maskCommand); print("selectall")
case "delete":
    key(51); print("delete")
case "clear":
    key(0, flags: .maskCommand); usleep(80_000); key(51); print("clear")
default:
    print("unknown")
}
