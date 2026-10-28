// swift-tools-version: 6.0
// agentline for macOS: a SwiftUI app that runs the agentline engine locally.
// `AgentlineCore` holds everything testable without a UI (models, API client, backend launch
// spec, Keychain, notification de-duplication); `Agentline` is the SwiftUI app.

import PackageDescription

let package = Package(
    name: "Agentline",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "Agentline", targets: ["Agentline"]),
    ],
    targets: [
        .target(name: "AgentlineCore"),
        .executableTarget(name: "Agentline", dependencies: ["AgentlineCore"]),
        .testTarget(
            name: "AgentlineCoreTests",
            dependencies: ["AgentlineCore"],
            resources: [.copy("Fixtures")]
        ),
    ],
    swiftLanguageModes: [.v6]
)
