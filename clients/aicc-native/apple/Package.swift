// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AICCNative",
    platforms: [.macOS(.v15), .iOS(.v18)],
    products: [
        .library(name: "AICCNativeCore", targets: ["AICCNativeCore"]),
        .executable(name: "AICCNativeApp", targets: ["AICCNativeApp"]),
    ],
    targets: [
        .target(name: "AICCNativeCore", resources: [.process("Resources")]),
        .executableTarget(name: "AICCNativeApp", dependencies: ["AICCNativeCore"]),
        .testTarget(name: "AICCNativeCoreTests", dependencies: ["AICCNativeCore"]),
    ]
)
