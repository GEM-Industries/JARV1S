import Foundation

protocol SpeechEngine: AnyObject {
    func currentStatus() async -> HelperStatus
    func prepare() async -> HelperStatus
    func startUtterance(
        sampleRate: Int,
        onPartial: @escaping @Sendable (String) -> Void,
        onFinal: @escaping @Sendable (String) -> Void
    ) async throws
    func feedPCM(_ data: Data) async throws
    func finalizeUtterance() async throws -> String
    func cancelUtterance() async
}

func makeSpeechEngine() -> SpeechEngine {
#if JARVIS_HAS_SPEECH_ANALYZER
    if #available(macOS 26.0, *) {
        return AppleSpeechEngine()
    }
#endif
    return UnsupportedSpeechEngine()
}

/// Used when SpeechAnalyzer is unavailable (older OS/SDK).
final class UnsupportedSpeechEngine: SpeechEngine {
    func currentStatus() async -> HelperStatus {
        HelperStatus(
            ready: false,
            state: "unsupported",
            detail: "On-device Speech requires macOS 26 or later with SpeechAnalyzer."
        )
    }

    func prepare() async -> HelperStatus {
        await currentStatus()
    }

    func startUtterance(
        sampleRate: Int,
        onPartial: @escaping @Sendable (String) -> Void,
        onFinal: @escaping @Sendable (String) -> Void
    ) async throws {
        throw SpeechEngineError.unsupported
    }

    func feedPCM(_ data: Data) async throws {
        throw SpeechEngineError.unsupported
    }

    func finalizeUtterance() async throws -> String {
        throw SpeechEngineError.unsupported
    }

    func cancelUtterance() async {}
}

enum SpeechEngineError: Error, LocalizedError {
    case unsupported
    case busy
    case notStarted
    case permissionDenied
    case assetsMissing
    case internalFailure(String)

    var errorDescription: String? {
        switch self {
        case .unsupported:
            return "SpeechAnalyzer is not available on this system."
        case .busy:
            return "Another utterance is already in progress."
        case .notStarted:
            return "No active utterance."
        case .permissionDenied:
            return "Speech Recognition permission is required."
        case .assetsMissing:
            return "On-device speech needs a one-time download."
        case .internalFailure(let message):
            return message
        }
    }
}
