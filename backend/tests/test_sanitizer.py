from core.turns.sanitizer import sanitize_assistant_output, sanitize_tts_text


def test_strips_bare_leading_reasoning_label_from_stream_chunk():
    assert sanitize_assistant_output("thought\n\n") == ""


def test_strips_doubled_leading_reasoning_label_from_stream_chunk():
    assert sanitize_assistant_output("thought thought\n\n") == ""


def test_strips_leading_reasoning_label_before_tool_call():
    assert sanitize_assistant_output("thought\n\n<tool_call>await jarvis.system.think()</tool_call>") == (
        "<tool_call>await jarvis.system.think()</tool_call>"
    )


def test_preserves_normal_text_that_starts_with_similar_word():
    assert sanitize_assistant_output("Thoughtful answer coming up.") == "Thoughtful answer coming up."


def test_preserves_inline_label_words_when_model_leaks_angle_brackets():
    text = "The <analysis> is on screen. This is about buying a <code> editor."

    assert sanitize_assistant_output(text) == "The analysis is on screen. This is about buying a code editor."


def test_preserves_inline_provider_channel_labels_as_words():
    text = (
        "I'll let you know when the <|channel|>analysis is ready. "
        "It treats information as a <|channel|>content for governance."
    )

    assert sanitize_assistant_output(text) == (
        "I'll let you know when the analysis is ready. "
        "It treats information as a content for governance."
    )


def test_strips_leading_provider_channel_label_as_control_marker():
    assert sanitize_assistant_output("<|channel|>analysis The answer is ready.") == "The answer is ready."


def test_strips_leading_xml_style_control_label_without_inserting_word():
    assert sanitize_assistant_output("<analysis>The answer is ready.") == "The answer is ready."


def test_strips_wrapping_xml_style_control_labels():
    assert sanitize_assistant_output("<final>Done.</final>") == "Done."


def test_preserves_inline_balanced_tags_as_literal_text():
    text = "Use <code>print()</code> as the example."

    assert sanitize_assistant_output(text) == text


def test_strips_generated_tool_result_blocks_from_assistant_text():
    text = (
        "<tool_result>\n"
        "Added 'Upload brand guidelines to Canva for Boulder Society SendFest'\n"
        "</_result>\n"
        "<tool_result>\n"
        "Added 'Create Boulder Society SendFest post'\n"
        "</_result>\n"
        "I've split those tasks and set the reminder."
    )

    assert sanitize_assistant_output(text) == "I've split those tasks and set the reminder."


def test_strips_tool_result_block_between_spoken_sentences():
    text = "Done. <tool_result>internal output</tool_result> All set."

    assert sanitize_assistant_output(text) == "Done. All set."


def test_tts_text_strips_markdown_formatting():
    text = "**Interest** is not the same as _approval_. `Listen-to-verify` helps."

    assert sanitize_tts_text(text) == "Interest is not the same as approval. Listen-to-verify helps."


def test_tts_text_strips_structural_markdown_but_keeps_words():
    text = "### Reset mechanism\n\n- Use your feet on the floor.\n1. Listen first.\n> Then speak."

    assert sanitize_tts_text(text) == "Reset mechanism\nUse your feet on the floor.\nListen first.\nThen speak."


def test_tts_text_preserves_link_text():
    assert sanitize_tts_text("Read [Cartesia docs](https://docs.cartesia.ai).") == "Read Cartesia docs."
