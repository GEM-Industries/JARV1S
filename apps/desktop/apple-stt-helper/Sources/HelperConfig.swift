import Foundation

struct HelperConfig {
    let listenHost: String
    let port: UInt16
    let token: String

    static func fromEnvironment() -> HelperConfig {
        let hostRaw = ProcessInfo.processInfo.environment["JARVIS_SPEECH_HOST"] ?? "127.0.0.1"
        // Loopback only — never expose SpeechAnalyzer over the LAN.
        let host = (hostRaw == "127.0.0.1" || hostRaw == "::1") ? hostRaw : "127.0.0.1"
        let portRaw = ProcessInfo.processInfo.environment["JARVIS_SPEECH_PORT"] ?? "9091"
        let token = ProcessInfo.processInfo.environment["JARVIS_SPEECH_TOKEN"] ?? ""
        let port = UInt16(portRaw) ?? 9091
        return HelperConfig(listenHost: host, port: port, token: token)
    }
}

struct HelperStatus: Sendable {
    var ready: Bool
    var state: String
    var detail: String?

    func jsonObject() -> [String: Any] {
        var payload: [String: Any] = [
            "type": "status",
            "ready": ready,
            "state": state,
        ]
        if let detail {
            payload["detail"] = detail
        }
        return payload
    }
}
