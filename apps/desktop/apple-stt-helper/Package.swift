// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JARV1SSpeechHelper",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "JARV1SSpeechHelper", targets: ["JARV1SSpeechHelper"]),
    ],
    targets: [
        .executableTarget(
            name: "JARV1SSpeechHelper",
            path: "Sources"
        ),
    ]
)
