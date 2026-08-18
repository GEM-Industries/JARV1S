#if JARVIS_HAS_SPEECH_ANALYZER
import AVFoundation
import Foundation
import Speech

@available(macOS 26.0, *)
actor AppleSpeechEngine: SpeechEngine {
    private var activeSession: UtteranceSession?

    func currentStatus() async -> HelperStatus {
        if let permission = permissionStatusIfNeeded() {
            return permission
        }

        do {
            guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) else {
                return HelperStatus(
                    ready: false,
                    state: "unsupported",
                    detail: "On-device speech does not support this Mac's language yet."
                )
            }
            if await localeIsInstalled(locale) {
                return HelperStatus(
                    ready: true,
                    state: "ready",
                    detail: "Apple Speech ready"
                )
            }
            let transcriber = makeTranscriber(locale: locale)
            if try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) != nil {
                return HelperStatus(
                    ready: false,
                    state: "needs_assets",
                    detail: "On-device speech needs a one-time download."
                )
            }
            return HelperStatus(
                ready: true,
                state: "ready",
                detail: "Apple Speech ready"
            )
        } catch {
            return HelperStatus(
                ready: false,
                state: "unavailable",
                detail: error.localizedDescription
            )
        }
    }

    func prepare() async -> HelperStatus {
        if activeSession != nil {
            return HelperStatus(
                ready: false,
                state: "unavailable",
                detail: "Cannot prepare while an utterance is in progress."
            )
        }

        let authStatus = await withCheckedContinuation { (continuation: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        guard authStatus == .authorized else {
            return HelperStatus(
                ready: false,
                state: "needs_permission",
                detail: "Speech Recognition permission was not granted."
            )
        }

        do {
            guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) else {
                return HelperStatus(
                    ready: false,
                    state: "unsupported",
                    detail: "On-device speech does not support this Mac's language yet."
                )
            }
            let transcriber = makeTranscriber(locale: locale)
            if !(await localeIsInstalled(locale)) {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                    try await request.downloadAndInstall()
                }
            }
            guard await localeIsInstalled(locale) else {
                return HelperStatus(
                    ready: false,
                    state: "needs_assets",
                    detail: "Could not finish the on-device speech download. Check the network and try again."
                )
            }
            guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
                return HelperStatus(
                    ready: false,
                    state: "unavailable",
                    detail: "On-device speech has no compatible audio format."
                )
            }
            let analyzer = makeAnalyzer(transcriber)
            try await analyzer.prepareToAnalyze(in: format)
            return HelperStatus(
                ready: true,
                state: "ready",
                detail: "Apple Speech ready"
            )
        } catch {
            return HelperStatus(
                ready: false,
                state: "unavailable",
                detail: "Prepare failed: \(error.localizedDescription)"
            )
        }
    }

    func startUtterance(
        sampleRate: Int,
        onPartial: @escaping @Sendable (String) -> Void,
        onFinal: @escaping @Sendable (String) -> Void
    ) async throws {
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            throw SpeechEngineError.permissionDenied
        }
        guard activeSession == nil else { throw SpeechEngineError.busy }
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) else {
            throw SpeechEngineError.unsupported
        }
        guard await localeIsInstalled(locale) else {
            throw SpeechEngineError.assetsMissing
        }

        let session = try await UtteranceSession(
            sampleRate: sampleRate,
            locale: locale,
            onPartial: onPartial,
            onFinal: onFinal
        )
        activeSession = session
    }

    func feedPCM(_ data: Data) async throws {
        guard let session = activeSession else { throw SpeechEngineError.notStarted }
        try await session.feedPCM(data)
    }

    func finalizeUtterance() async throws -> String {
        guard let session = activeSession else { throw SpeechEngineError.notStarted }
        let text = try await session.finalize()
        activeSession = nil
        return text
    }

    func cancelUtterance() async {
        await activeSession?.cancel()
        activeSession = nil
    }
}

@available(macOS 26.0, *)
private func permissionStatusIfNeeded() -> HelperStatus? {
    switch SFSpeechRecognizer.authorizationStatus() {
    case .denied, .restricted:
        return HelperStatus(
            ready: false,
            state: "needs_permission",
            detail: "Enable Speech Recognition for JARV1S Speech Helper in System Settings."
        )
    case .notDetermined:
        return HelperStatus(
            ready: false,
            state: "needs_permission",
            detail: "Speech Recognition permission has not been granted yet."
        )
    case .authorized:
        return nil
    @unknown default:
        return nil
    }
}

@available(macOS 26.0, *)
private func localeIdentifier(_ locale: Locale) -> String {
    locale.identifier(.bcp47)
}

@available(macOS 26.0, *)
private func localeIsInstalled(_ locale: Locale) async -> Bool {
    let installed = await SpeechTranscriber.installedLocales
    let target = localeIdentifier(locale)
    return installed.contains { localeIdentifier($0) == target }
}

@available(macOS 26.0, *)
private func makeTranscriber(locale: Locale) -> SpeechTranscriber {
    SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
}

@available(macOS 26.0, *)
private func makeAnalyzer(_ transcriber: SpeechTranscriber) -> SpeechAnalyzer {
    let options = SpeechAnalyzer.Options(
        priority: .userInitiated,
        modelRetention: .lingering
    )
    return SpeechAnalyzer(modules: [transcriber], options: options)
}

@available(macOS 26.0, *)
private actor UtteranceSession {
    private let analyzer: SpeechAnalyzer
    private let inputBuilder: AsyncStream<AnalyzerInput>.Continuation
    private let converter: PCMConverter
    private let onPartial: @Sendable (String) -> Void
    private let onFinal: @Sendable (String) -> Void
    private var resultsTask: Task<Void, Never>?
    private var finalText = ""
    private var volatileText = ""
    private var finished = false

    init(
        sampleRate: Int,
        locale: Locale,
        onPartial: @escaping @Sendable (String) -> Void,
        onFinal: @escaping @Sendable (String) -> Void
    ) async throws {
        self.onPartial = onPartial
        self.onFinal = onFinal

        let transcriber = makeTranscriber(locale: locale)
        let analyzer = makeAnalyzer(transcriber)
        let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber])
        guard let format else {
            throw SpeechEngineError.internalFailure("No compatible audio format for SpeechAnalyzer")
        }

        let (inputSequence, inputBuilder) = AsyncStream.makeStream(of: AnalyzerInput.self)
        try await analyzer.prepareToAnalyze(in: format)
        try await analyzer.start(inputSequence: inputSequence)

        self.analyzer = analyzer
        self.inputBuilder = inputBuilder
        self.converter = try PCMConverter(sourceSampleRate: Double(sampleRate), destination: format)

        resultsTask = Task { [weak self] in
            guard let self else { return }
            await self.consumeResults(transcriber)
        }
    }

    private func consumeResults(_ transcriber: SpeechTranscriber) async {
        do {
            for try await result in transcriber.results {
                let text = String(result.text.characters)
                if result.isFinal {
                    finalText += text
                    volatileText = ""
                    let snapshot = finalText.trimmingCharacters(in: .whitespacesAndNewlines)
                    onFinal(snapshot)
                } else {
                    volatileText = text
                    let snapshot = (finalText + volatileText)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    onPartial(snapshot)
                }
            }
        } catch {
            // Results stream ended or cancelled.
        }
    }

    func feedPCM(_ data: Data) async throws {
        guard !finished else { return }
        let buffer = try converter.convert(pcm16Mono: data)
        inputBuilder.yield(AnalyzerInput(buffer: buffer))
    }

    func finalize() async throws -> String {
        guard !finished else {
            return (finalText + volatileText).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        finished = true
        inputBuilder.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        await resultsTask?.value
        let snapshot = (finalText + volatileText).trimmingCharacters(in: .whitespacesAndNewlines)
        if !snapshot.isEmpty {
            onFinal(snapshot)
        }
        return snapshot
    }

    func cancel() async {
        guard !finished else { return }
        finished = true
        inputBuilder.finish()
        await analyzer.cancelAndFinishNow()
        resultsTask?.cancel()
        await resultsTask?.value
    }
}

@available(macOS 26.0, *)
private final class PCMConverter: @unchecked Sendable {
    private let converter: AVAudioConverter
    private let destinationFormat: AVAudioFormat
    private let sourceFormat: AVAudioFormat

    init(sourceSampleRate: Double, destination: AVAudioFormat) throws {
        guard let source = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sourceSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            throw SpeechEngineError.internalFailure("Could not create source audio format")
        }
        guard let converter = AVAudioConverter(from: source, to: destination) else {
            throw SpeechEngineError.internalFailure("Could not create audio converter")
        }
        self.sourceFormat = source
        self.destinationFormat = destination
        self.converter = converter
    }

    func convert(pcm16Mono data: Data) throws -> AVAudioPCMBuffer {
        let frameCount = AVAudioFrameCount(data.count / 2)
        guard frameCount > 0 else {
            throw SpeechEngineError.internalFailure("Empty audio chunk")
        }
        guard let input = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: frameCount) else {
            throw SpeechEngineError.internalFailure("Could not allocate source buffer")
        }
        input.frameLength = frameCount
        data.withUnsafeBytes { raw in
            if let base = raw.baseAddress, let dest = input.int16ChannelData?[0] {
                dest.update(from: base.assumingMemoryBound(to: Int16.self), count: Int(frameCount))
            }
        }

        let ratio = destinationFormat.sampleRate / sourceFormat.sampleRate
        let outFrames = AVAudioFrameCount(Double(frameCount) * ratio) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: destinationFormat, frameCapacity: outFrames) else {
            throw SpeechEngineError.internalFailure("Could not allocate destination buffer")
        }

        var error: NSError?
        var consumed = false
        let status = converter.convert(to: output, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return input
        }
        if let error {
            throw error
        }
        if status == .error {
            throw SpeechEngineError.internalFailure("Audio conversion failed")
        }
        return output
    }
}
#endif
