"""Logging for the app and uvicorn together, set up once (CODING_GUIDELINES §1.9, §2.8).

Everything goes through one handler on the root logger. uvicorn's loggers lose their own
handlers and propagate to it, so every line has the same format, and none of them can print
what §1.9 forbids:
- a logged traceback keeps its frames but never an exception's message, which can quote
  personal data (psycopg's includes Postgres's DETAIL, e.g. `Key (email)=(...)`);
- uvicorn's access lines leave out the query string.
"""

import logging
import logging.config
import sys
import traceback
from types import TracebackType
from typing import Any

HANDLER_NAME = "countertalk"
WITHHELD = "(message not logged: it can hold personal data)"


def frames_only(error: BaseException) -> str:
    """A traceback with every frame and each exception's type, but none of their messages."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        cause = current.__cause__
        current = (
            cause if cause is not None or current.__suppress_context__ else current.__context__
        )
    parts = []
    for exc in reversed(chain):
        if parts:
            parts.append("\nwhich led to:\n")
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_list(traceback.extract_tb(exc.__traceback__)))
        name = type(exc).__qualname__
        module = type(exc).__module__
        parts.append(f"{name if module == 'builtins' else f'{module}.{name}'}: {WITHHELD}\n")
    return "".join(parts).rstrip("\n")


class FramesOnlyFormatter(logging.Formatter):
    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        return frames_only(ei[1]) if ei[1] is not None else ""


class DropQueryString(logging.Filter):
    """uvicorn's access lines carry the full path; a query string can hold names or text."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


_base_record_factory = logging.getLogRecordFactory()


def _frames_only_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """Every record carries its traceback as frames only, before any handler sees it.

    So a handler that isn't ours (an error tracker, uvicorn with its own --log-config) can't
    print an exception's message either: there's no exception left on the record to format.
    """
    record = _base_record_factory(*args, **kwargs)
    if record.exc_info and record.exc_info[1] is not None:
        record.exc_text = frames_only(record.exc_info[1])
        record.exc_info = None
    return record


def configure_logging() -> None:
    """Safe to call again: it replaces the configuration rather than adding to it."""
    logging.setLogRecordFactory(_frames_only_record)
    uvicorn_logger: dict[str, Any] = {"handlers": [], "propagate": True, "level": "INFO"}
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "class": "countertalk.logs.FramesOnlyFormatter",
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                },
            },
            "filters": {"drop_query_string": {"()": DropQueryString}},
            "handlers": {
                HANDLER_NAME: {
                    "class": "logging.StreamHandler",
                    "formatter": "plain",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {"handlers": [HANDLER_NAME], "level": "INFO"},
            "loggers": {
                "uvicorn": uvicorn_logger,
                "uvicorn.error": uvicorn_logger,
                "uvicorn.access": {**uvicorn_logger, "filters": ["drop_query_string"]},
            },
        }
    )


def install_frames_only_excepthook() -> None:
    """An uncaught exception, such as a failed migration in the alembic CLI, prints like a log."""

    def hook(exc_type: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
        sys.stderr.write(frames_only(error) + "\n")

    sys.excepthook = hook
