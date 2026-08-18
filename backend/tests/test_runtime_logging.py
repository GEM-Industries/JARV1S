import logging
import os
import time

from core.llm.prompt_dump import _cleanup_prompt_dumps
from services.log_buffer import (
    HumanReadableContextFormatter,
    LogContextFilter,
    RollingLogBuffer,
    log_context,
)


def test_rolling_log_buffer_is_bounded_structured_and_sanitized() -> None:
    buffer = RollingLogBuffer(maxlen=2)
    logger = logging.getLogger("test.runtime-logging")
    logger.handlers = [buffer]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("discarded")
    with log_context(turn_id="turn-1", task_id="task\n1"):
        logger.info(
            'request api_key="private-value" Authorization: Bearer abc.def\r\nnext'
        )
    logger.warning("password=hunter2")

    snapshot = buffer.snapshot()
    assert len(snapshot) == 2
    assert snapshot[0]["context"] == {"turn_id": "turn-1", "task_id": "task 1"}
    assert "private-value" not in snapshot[0]["message"]
    assert "abc.def" not in snapshot[0]["message"]
    assert "\n" not in snapshot[0]["message"]
    assert "hunter2" not in snapshot[1]["message"]


def test_human_formatter_appends_only_safe_context() -> None:
    formatter = HumanReadableContextFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "token=%s",
        ("private-token",),
        None,
    )

    with log_context(instance_id="instance-1"):
        LogContextFilter().filter(record)

    rendered = formatter.format(record)
    assert rendered == "INFO token=[REDACTED] [instance_id=instance-1]"
    assert record.args == ("private-token",)


def test_prompt_dump_cleanup_enforces_age_and_count(tmp_path) -> None:
    now = time.time()
    old = tmp_path / "old.txt"
    old.write_text("old")
    os.utime(old, (now - 3 * 24 * 60 * 60, now - 3 * 24 * 60 * 60))

    for index in range(4):
        path = tmp_path / f"recent-{index}.txt"
        path.write_text(str(index))
        os.utime(path, (now + index, now + index))
    unrelated = tmp_path / "keep.json"
    unrelated.write_text("{}")

    _cleanup_prompt_dumps(tmp_path, max_files=2, max_age_days=1)

    assert not old.exists()
    assert sorted(path.name for path in tmp_path.glob("*.txt")) == [
        "recent-2.txt",
        "recent-3.txt",
    ]
    assert unrelated.exists()
