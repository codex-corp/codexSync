import logging
import json
import os
import platform
import re
from datetime import date, timedelta
from pathlib import Path
import uuid
import zipfile

from .models import LoggingConfig


def configure_logging(cfg: LoggingConfig, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if cfg.file:
        _ensure_parent(cfg.file)
        handlers.append(
            _DailySizeRotatingFileHandler(
                base_file=cfg.file,
                retention_days=cfg.retention_days,
                archive_mode=cfg.archive_mode,
                max_bytes=cfg.max_file_size_mb * 1024 * 1024,
                machine_name=_safe_log_component(cfg.machine_id or platform.node()),
            )
        )

    formatter = _build_formatter(cfg.format)
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _build_formatter(format_name: str) -> logging.Formatter:
    value = format_name.lower().strip()
    if value == "json":
        return _JsonFormatter()
    if value == "logfmt":
        return _LogfmtFormatter()
    return logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


class _LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        return " ".join(f"{key}={_quote_logfmt(str(value))}" for key, value in fields.items())


def _quote_logfmt(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class _DailySizeRotatingFileHandler(logging.Handler):
    def __init__(
        self,
        base_file: Path,
        retention_days: int,
        archive_mode: str,
        max_bytes: int,
        machine_name: str,
    ) -> None:
        super().__init__()
        self._base_file = base_file
        self._retention_days = retention_days
        self._archive_mode = archive_mode
        self._max_bytes = max_bytes
        self._machine_name = machine_name
        self._stream = None
        self._current_date = date.today()
        self._current_index = self._discover_current_index(self._current_date)
        self._open_stream()
        self._archive_stale_text_logs()
        self._prune_old_logs()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) + "\n"
            self._rollover_if_needed(msg)
            if self._stream is not None:
                self._stream.write(msg)
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()

    @property
    def _stem(self) -> str:
        return self._base_file.stem

    @property
    def _suffix(self) -> str:
        return self._base_file.suffix if self._base_file.suffix else ".log"

    def _log_path(self, day: date, index: int) -> Path:
        day_str = day.isoformat()
        prefix = f"{self._stem}-{self._machine_name}-{day_str}"
        if index == 0:
            name = f"{prefix}{self._suffix}"
        else:
            name = f"{prefix}.{index}{self._suffix}"
        return self._base_file.parent / name

    def _open_stream(self) -> None:
        path = self._log_path(self._current_date, self._current_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")

    def _current_log_path(self) -> Path:
        return self._log_path(self._current_date, self._current_index)

    def _rollover_if_needed(self, pending_msg: str) -> None:
        today = date.today()
        pending_size = len(pending_msg.encode("utf-8"))
        path = self._current_log_path()
        current_size = path.stat().st_size if path.exists() else 0

        need_new_day = today != self._current_date
        need_size_rollover = (current_size + pending_size) > self._max_bytes
        if not need_new_day and not need_size_rollover:
            return

        if self._stream is not None:
            self._stream.close()
            self._stream = None

        if self._archive_mode == "zip":
            self._archive_log_file(path)

        if need_new_day:
            self._current_date = today
            self._current_index = self._discover_current_index(today)
        else:
            self._current_index = self._next_available_index(self._current_date, self._current_index + 1)

        self._open_stream()
        self._archive_stale_text_logs()
        self._prune_old_logs()

    def _next_available_index(self, day: date, start: int) -> int:
        idx = max(start, 0)
        while self._log_path(day, idx).exists():
            idx += 1
        return idx

    def _discover_current_index(self, day: date) -> int:
        indexes = [idx for _, idx in self._iter_matching_text_logs() if _ == day]
        if not indexes:
            return 0
        idx = max(indexes)
        candidate = self._log_path(day, idx)
        if candidate.exists() and candidate.stat().st_size >= self._max_bytes:
            return self._next_available_index(day, idx + 1)
        return idx

    def _iter_matching_text_logs(self) -> list[tuple[date, int]]:
        result: list[tuple[date, int]] = []
        for path in self._base_file.parent.glob(f"{self._stem}-{self._machine_name}-*{self._suffix}"):
            parsed = self._parse_text_log_name(path.name)
            if parsed is not None:
                result.append(parsed)
        return result

    def _iter_matching_zip_logs(self) -> list[tuple[Path, date]]:
        result: list[tuple[Path, date]] = []
        for path in self._base_file.parent.glob(f"{self._stem}-{self._machine_name}-*{self._suffix}.zip"):
            parsed = self._parse_zip_log_name(path.name)
            if parsed is not None:
                result.append((path, parsed))
        return result

    def _parse_text_log_name(self, name: str) -> tuple[date, int] | None:
        suffix = re.escape(self._suffix)
        stem = re.escape(self._stem)
        machine = re.escape(self._machine_name)
        pattern = re.compile(rf"^{stem}-{machine}-(\d{{4}}-\d{{2}}-\d{{2}})(?:\.(\d+))?{suffix}$")
        match = pattern.match(name)
        if not match:
            return None
        day_raw, idx_raw = match.groups()
        try:
            day = date.fromisoformat(day_raw)
        except ValueError:
            return None
        idx = int(idx_raw) if idx_raw else 0
        return day, idx

    def _parse_zip_log_name(self, name: str) -> date | None:
        suffix = re.escape(self._suffix)
        stem = re.escape(self._stem)
        machine = re.escape(self._machine_name)
        pattern = re.compile(rf"^{stem}-{machine}-(\d{{4}}-\d{{2}}-\d{{2}})(?:\.(\d+))?{suffix}\.zip$")
        match = pattern.match(name)
        if not match:
            return None
        day_raw = match.group(1)
        try:
            return date.fromisoformat(day_raw)
        except ValueError:
            return None

    def _archive_stale_text_logs(self) -> None:
        """Archive text logs of earlier days.

        Only earlier days: a same-day file with another index may be the one a
        second codexSync process (a scheduled task beside the window) is
        writing right now.
        """
        if self._archive_mode != "zip":
            return
        current = self._current_log_path().resolve()
        today = date.today()
        for path in self._base_file.parent.glob(f"{self._stem}-{self._machine_name}-*{self._suffix}"):
            if not path.is_file():
                continue
            if path.resolve() == current:
                continue
            parsed = self._parse_text_log_name(path.name)
            if parsed is None or parsed[0] >= today:
                continue
            self._archive_log_file(path)

    def _archive_log_file(self, source: Path) -> None:
        """Move ``source`` into a zip, or leave it alone if it is still in use.

        The file is claimed by renaming it first. On Windows that fails while
        another process has it open, and the archive is skipped until a later
        rollover; zipping it anyway and then failing to delete it produced a
        new zip of the same file on every line the other process wrote (CS-318).
        """
        if not source.exists() or not source.is_file():
            return
        claimed = source.with_name(f"{source.name}.{uuid.uuid4().hex[:8]}.archiving")
        try:
            os.replace(source, claimed)
        except OSError:
            return
        zip_path = source.with_name(f"{source.name}.zip")
        counter = 1
        while zip_path.exists():
            zip_path = source.with_name(f"{source.name}.{counter}.zip")
            counter += 1
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(claimed, arcname=source.name)
        claimed.unlink(missing_ok=True)

    def _prune_old_logs(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = date.today() - timedelta(days=self._retention_days)

        for path in self._base_file.parent.glob(f"{self._stem}-{self._machine_name}-*{self._suffix}"):
            parsed = self._parse_text_log_name(path.name)
            if parsed is None:
                continue
            day, _ = parsed
            if day < cutoff:
                path.unlink(missing_ok=True)

        for path, day in self._iter_matching_zip_logs():
            if day < cutoff:
                path.unlink(missing_ok=True)


def _safe_log_component(raw: str | None) -> str:
    if not raw:
        return "unknown-machine"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip())
    cleaned = cleaned.strip("-.")
    return cleaned or "unknown-machine"
