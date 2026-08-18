import Foundation
import Network

final class SpeechWebSocketServer {
    private let config: HelperConfig
    private let engine: SpeechEngine
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "dev.jarv1s.host.speech.server")
    // NWConnection handlers retain sessions weakly, so the server owns active sessions.
    private var sessions: [ObjectIdentifier: ClientSession] = [:]

    init(config: HelperConfig, engine: SpeechEngine = makeSpeechEngine()) {
        self.config = config
        self.engine = engine
    }

    func start() throws {
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        let websocketOptions = NWProtocolWebSocket.Options()
        websocketOptions.autoReplyPing = true
        parameters.defaultProtocolStack.applicationProtocols.insert(websocketOptions, at: 0)

        guard let port = NWEndpoint.Port(rawValue: config.port) else {
            throw SpeechEngineError.internalFailure("Invalid port \(config.port)")
        }
        parameters.requiredLocalEndpoint = .hostPort(
            host: NWEndpoint.Host(config.listenHost),
            port: port
        )
        let listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.stateUpdateHandler = { state in
            if case let .failed(error) = state {
                fputs("JARV1SSpeechHelper listener failed: \(error)\n", stderr)
            }
        }
        listener.start(queue: queue)
        self.listener = listener
    }

    private func accept(_ connection: NWConnection) {
        let session = ClientSession(connection: connection, config: config, engine: engine, queue: queue)
        let id = ObjectIdentifier(session)
        sessions[id] = session
        Task {
            await session.start(onClose: { [weak self] in
                self?.queue.async { self?.sessions.removeValue(forKey: id) }
            })
        }
    }
}

private actor ClientSession {
    private let connection: NWConnection
    private let config: HelperConfig
    private let engine: SpeechEngine
    private let queue: DispatchQueue
    private var utteranceActive = false
    private var closed = false
    private var onClose: (@Sendable () -> Void)?

    init(connection: NWConnection, config: HelperConfig, engine: SpeechEngine, queue: DispatchQueue) {
        self.connection = connection
        self.config = config
        self.engine = engine
        self.queue = queue
    }

    func start(onClose: @escaping @Sendable () -> Void) {
        self.onClose = onClose
        connection.stateUpdateHandler = { [weak self] state in
            Task { await self?.handleConnectionState(state) }
        }
        connection.start(queue: queue)
    }

    private func handleConnectionState(_ state: NWConnection.State) async {
        switch state {
        case .ready:
            receiveNext()
        case .failed, .cancelled:
            await cleanup()
        default:
            break
        }
    }

    private func receiveNext() {
        connection.receiveMessage { [weak self] content, context, _isComplete, error in
            Task {
                await self?.handleMessage(content: content, context: context, error: error)
            }
        }
    }

    private func handleMessage(
        content: Data?,
        context: NWConnection.ContentContext?,
        error: NWError?
    ) async {
        guard !closed else { return }
        if let error {
            fputs("JARV1SSpeechHelper receive error: \(error)\n", stderr)
            await cleanup()
            return
        }
        if let content, !content.isEmpty {
            let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                as? NWProtocolWebSocket.Metadata
            if metadata?.opcode == .binary {
                await handleBinary(content)
            } else {
                await handleText(content)
            }
        }
        if !closed {
            receiveNext()
        }
    }

    private func handleText(_ data: Data) async {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            sendJSON(["type": "error", "detail": "Invalid JSON message"])
            return
        }
        let type = (object["type"] as? String ?? "").lowercased()
        guard authorize(object) else { return }
        switch type {
        case "status":
            let status = await engine.currentStatus()
            sendJSON(status.jsonObject())
        case "prepare":
            let status = await engine.prepare()
            sendJSON(status.jsonObject())
        case "start":
            let sampleRate = object["sample_rate"] as? Int ?? 16_000
            utteranceActive = true
            do {
                try await engine.startUtterance(
                    sampleRate: sampleRate,
                    onPartial: { [weak self] text in
                        Task { await self?.sendJSON(["type": "partial", "text": text]) }
                    },
                    onFinal: { [weak self] text in
                        Task { await self?.sendJSON(["type": "final", "text": text]) }
                    }
                )
                sendJSON(["type": "started"])
            } catch {
                utteranceActive = false
                sendJSON(["type": "error", "detail": error.localizedDescription])
            }
        case "finalize":
            do {
                let text = try await engine.finalizeUtterance()
                if !text.isEmpty {
                    sendJSON(["type": "final", "text": text])
                }
                sendJSON(["type": "done"])
            } catch {
                sendJSON(["type": "error", "detail": error.localizedDescription])
                sendJSON(["type": "done"])
            }
            utteranceActive = false
        case "cancel":
            await engine.cancelUtterance()
            utteranceActive = false
        default:
            sendJSON(["type": "error", "detail": "Unknown message type: \(type)"])
        }
    }

    private func handleBinary(_ data: Data) async {
        guard utteranceActive else { return }
        do {
            try await engine.feedPCM(data)
        } catch {
            sendJSON(["type": "error", "detail": error.localizedDescription])
        }
    }

    private func authorize(_ object: [String: Any]) -> Bool {
        guard !config.token.isEmpty else { return true }
        if let token = object["token"] as? String, token == config.token {
            return true
        }
        sendJSON(["type": "error", "detail": "Unauthorized"])
        return false
    }

    private func sendJSON(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: []) else { return }
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "text", metadata: [metadata])
        connection.send(content: data, contentContext: context, isComplete: true, completion: .contentProcessed { _ in })
    }

    private func cleanup() async {
        guard !closed else { return }
        closed = true
        if utteranceActive {
            await engine.cancelUtterance()
            utteranceActive = false
        }
        connection.cancel()
        let close = onClose
        onClose = nil
        close?()
    }
}
