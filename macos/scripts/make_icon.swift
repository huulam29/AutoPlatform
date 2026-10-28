// Draws the app icon (a rounded square with the "flowchart" SF Symbol) into an .iconset folder.
//   swift macos/scripts/make_icon.swift <out.iconset>
// then: iconutil -c icns <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func render(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
        hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(px)
    let inset = s * 0.09
    let rect = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let path = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.22, yRadius: rect.width * 0.22)
    NSGradient(
        starting: NSColor(calibratedRed: 0.18, green: 0.36, blue: 0.95, alpha: 1),
        ending: NSColor(calibratedRed: 0.42, green: 0.20, blue: 0.85, alpha: 1))!
        .draw(in: path, angle: -60)
    let config = NSImage.SymbolConfiguration(pointSize: s * 0.42, weight: .semibold)
        .applying(.init(paletteColors: [.white]))
    if let sym = NSImage(systemSymbolName: "flowchart", accessibilityDescription: nil)?
        .withSymbolConfiguration(config)
    {
        let sz = sym.size
        sym.draw(in: NSRect(x: (s - sz.width) / 2, y: (s - sz.height) / 2, width: sz.width, height: sz.height))
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try render(base).write(to: out.appending(path: "icon_\(base)x\(base).png"))
    try render(base * 2).write(to: out.appending(path: "icon_\(base)x\(base)@2x.png"))
}
print("wrote \(out.path)")
