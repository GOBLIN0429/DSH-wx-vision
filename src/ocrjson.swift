import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else { print("[]"); exit(0) }
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print("[]"); exit(0) }
let W = Double(cg.width), H = Double(cg.height)
let mode = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "accurate"
let req = VNRecognizeTextRequest()
req.recognitionLevel = (mode == "fast") ? .fast : .accurate
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.usesLanguageCorrection = true
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
var out: [[String: Any]] = []
for o in req.results ?? [] {
    guard let c = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    out.append([
        "x": Int(b.origin.x * W),
        "y": Int((1 - b.origin.y - b.height) * H),
        "w": Int(b.width * W),
        "h": Int(b.height * H),
        "t": c.string,
        "c": (c.confidence * 1000).rounded() / 1000
    ])
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
FileHandle.standardOutput.write(data)
