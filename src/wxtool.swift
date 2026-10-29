import CoreGraphics
import Foundation

let src = CGEventSource(stateID: .hidSystemState)

func click(_ x: Double, _ y: Double) {
    let p = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(120_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(60_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
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
