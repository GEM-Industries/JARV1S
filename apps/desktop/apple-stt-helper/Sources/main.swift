import Foundation

let config = HelperConfig.fromEnvironment()
guard !config.token.isEmpty else {
    fputs("JARV1SSpeechHelper requires JARVIS_SPEECH_TOKEN\n", stderr)
    exit(1)
}
let server = SpeechWebSocketServer(config: config)
do {
    try server.start()
    fputs("JARV1SSpeechHelper listening on \(config.listenHost):\(config.port)\n", stderr)
    RunLoop.main.run()
} catch {
    fputs("JARV1SSpeechHelper failed to start: \(error)\n", stderr)
    exit(1)
}
