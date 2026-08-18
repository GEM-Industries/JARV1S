from jarvis_satellite.protocol import (
    MessageType,
    decode_audio,
    encode_audio,
    ping_message,
    playback_end_message,
    user_audio_message,
)


def test_user_audio_message_uses_existing_ws_contract():
    message = user_audio_message(b"\x01\x02")

    assert message.type == MessageType.USER_AUDIO.value
    assert message.data == {"audio": "AQI=", "encoding": "base64"}
    assert message.as_dict()["type"] == "user_audio"


def test_playback_end_message_uses_backend_message_type():
    message = playback_end_message()

    assert message.type == "audio.playback_end"
    assert message.data == {}


def test_ping_message_includes_timestamp():
    message = ping_message()

    assert message.type == "system.ping"
    assert isinstance(message.data["timestamp"], int)


def test_audio_base64_roundtrip():
    payload = b"abc123"

    assert decode_audio(encode_audio(payload)) == payload
