// Minimal macOS Vision OCR helper for Tally.
//
// Reads an image path from argv[1], runs VNRecognizeTextRequest at the accurate
// recognition level, and prints the recognized lines to stdout (one observation
// per line, in reading order). This keeps financial screenshots on-device: the
// pixels never leave the machine. The Python side compiles this once with swiftc
// and then invokes the cached binary via subprocess.

import Foundation
import Vision
import CoreImage
import AppKit

func loadCGImage(_ path: String) -> CGImage? {
    // NSImage handles PNG, JPEG, HEIC, and most everything the Wallet app exports.
    guard let img = NSImage(contentsOfFile: path) else { return nil }
    var rect = NSRect(x: 0, y: 0, width: img.size.width, height: img.size.height)
    return img.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: vision_ocr <image-path>\n".data(using: .utf8)!)
    exit(2)
}

guard let cg = loadCGImage(args[1]) else {
    FileHandle.standardError.write("could not load image\n".data(using: .utf8)!)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("vision request failed: \(error)\n".data(using: .utf8)!)
    exit(4)
}

guard let results = request.results else {
    exit(0)
}

// Reconstruct visual rows from bounding boxes. Vision returns observations in
// reading order, but a wide gap between a left-aligned merchant and a
// right-aligned amount (exactly the Apple Card list layout) makes it group the
// whole left column first and the whole right column second. That would split
// every merchant from its amount. Instead we cluster observations by vertical
// position (boundingBox is normalized with origin bottom-left), sort each
// cluster left to right, and emit one text line per on-screen row so the
// merchant and its amount stay together.
struct Obs {
    let text: String
    let x: Double
    let y: Double
    let h: Double
}

var items: [Obs] = []
for obs in results {
    guard let top = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox
    items.append(Obs(text: top.string, x: Double(bb.minX), y: Double(bb.midY), h: Double(bb.height)))
}
items.sort { $0.y > $1.y }  // top of the image first

var rows: [[Obs]] = []
for it in items {
    if let ref = rows.last?.first {
        let tol = max(ref.h, it.h) * 0.6
        if abs(it.y - ref.y) <= tol {
            rows[rows.count - 1].append(it)
            continue
        }
    }
    rows.append([it])
}

var out = ""
for var row in rows {
    row.sort { $0.x < $1.x }
    out += row.map { $0.text }.joined(separator: "   ") + "\n"
}
FileHandle.standardOutput.write(out.data(using: .utf8)!)
