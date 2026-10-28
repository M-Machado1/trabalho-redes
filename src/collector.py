"""TelemProto/1.0 collector.

The module has two layers:

* :class:`SensorTable` receives decoded datagrams plus a source address and
  returns replies and events. It owns registration, sequence accounting,
  liveness and status transitions, but performs no I/O and writes no output.
* :class:`Collector` owns the socket, the ``selectors`` loop, the ``ERR``
  rate limiter, table rendering, logging and the command line.

User-facing messages and log lines are written in Brazilian Portuguese;
identifiers and comments are in English.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import selectors
import signal
import socket
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Sequence

from .protocol import (
    Ack,
    Address,
    Bye,
    Data,
    Error,
    ErrorCode,
    ProtocolError,
    Register,
    VALUE_MAX,
    VALUE_MIN,
    decode,
    encode,
    format_value,
)

SEQUENCE_WINDOW = 64


class SensorStatus(str, Enum):
    """Liveness dimension of a registered sensor."""

    ACTIVE = "active"
    SILENT = "silent"
    DEPARTED = "departed"


@dataclass(frozen=True, slots=True)
class TableConfig:
    max_sensors: int = 256
    max_readings: int = 120
    offline_min: float = 15.0
    offline_factor: float = 3.0
    release_after_silence: float = 0.0
    retention: float = 60.0


@dataclass(frozen=True, slots=True)
class Reading:
    seq: int
    timestamp: int
    value: float


@dataclass(frozen=True, slots=True)
class SensorRow:
    """Immutable snapshot of one sensor row."""

    sensor_id: str
    session: str
    addr: Address
    interval: int
    status: SensorStatus
    last_seq: int | None
    last_value: float | None
    last_value_ts: int | None
    age: float
    received: int
    lost: int
    dup: int
    late: int
    readings: tuple[Reading, ...]


@dataclass(frozen=True, slots=True)
class Reply:
    payload: bytes
    addr: Address
    kind: Literal["ack", "err"]


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    fields: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandleResult:
    replies: tuple[Reply, ...] = ()
    events: tuple[Event, ...] = ()


@dataclass(slots=True)
class _Row:
    """Mutable row kept by the table."""

    sensor_id: str
    session: str
    addr: Address
    interval: int
    status: SensorStatus
    first_seen: float
    registered_at: float
    last_seen_mono: float
    last_seen_wall: float
    departed_at: float | None = None
    last_seq: int | None = None
    received: int = 0
    lost: int = 0
    dup: int = 0
    late: int = 0
    last_value: float | None = None
    last_value_ts: int | None = None
    recent: deque[int] = field(
        default_factory=lambda: deque(maxlen=SEQUENCE_WINDOW)
    )
    recent_set: set[int] = field(default_factory=set)
    readings: deque[Reading] = field(default_factory=lambda: deque(maxlen=0))


class SensorTable:
    """Registration, sequence accounting and liveness detection."""

    def __init__(self, config: TableConfig | None = None) -> None:
        self.config = config or TableConfig()
        self._rows: dict[str, _Row] = {}

    # -- public API -----------------------------------------------------

    def handle(self, raw: bytes, addr: Address) -> HandleResult:
        """Process one datagram. This method never raises."""

        try:
            message = decode(raw)
        except ProtocolError as exc:
            return self._error(addr, exc.code, exc.description)

        try:
            if isinstance(message, Register):
                return self._register(message, addr)
            if isinstance(message, Data):
                return self._data(message, addr)
            if isinstance(message, Bye):
                return self._bye(message, addr)
            return self._error(
                addr,
                ErrorCode.BAD_REQUEST,
                "mensagem nao esperada pelo coletor",
            )
        except Exception as exc:  # noqa: BLE001 - isolation per datagram (RF-15)
            return self._error(
                addr,
                ErrorCode.SERVER_ERROR,
                "falha interna no coletor",
                (
                    "SERVER_ERROR",
                    {
                        "code": ErrorCode.SERVER_ERROR.value,
                        "addr": format_addr(addr),
                        "error": type(exc).__name__,
                    },
                ),
            )

    def expire(self) -> tuple[Event, ...]:
        """Apply liveness and retention timeouts. Call it on every tick."""

        now = time.monotonic()
        events: list[Event] = []
        for row in list(self._rows.values()):
            if row.status is SensorStatus.ACTIVE:
                limit = self._offline_after(row)
                if now - row.last_seen_mono >= limit:
                    row.status = SensorStatus.SILENT
                    events.append(
                        Event(
                            "SILENT",
                            {"id": row.sensor_id, "after": round(limit, 3)},
                        )
                    )
            elif row.status is SensorStatus.DEPARTED:
                if (
                    row.departed_at is not None
                    and now - row.departed_at >= self.config.retention
                ):
                    del self._rows[row.sensor_id]
                    events.append(
                        Event(
                            "EXPIRED",
                            {
                                "id": row.sensor_id,
                                "session": row.session,
                            },
                        )
                    )
        return tuple(events)

    def snapshot(self) -> tuple[SensorRow, ...]:
        """Return rows ordered by sensor id, with age from ``monotonic()``."""

        now = time.monotonic()
        return tuple(
            SensorRow(
                sensor_id=row.sensor_id,
                session=row.session,
                addr=row.addr,
                interval=row.interval,
                status=row.status,
                last_seq=row.last_seq,
                last_value=row.last_value,
                last_value_ts=row.last_value_ts,
                age=now - row.last_seen_mono,
                received=row.received,
                lost=row.lost,
                dup=row.dup,
                late=row.late,
                readings=tuple(row.readings),
            )
            for row in sorted(
                self._rows.values(), key=lambda item: item.sensor_id
            )
        )

    # -- message handlers ----------------------------------------------

    def _register(self, message: Register, addr: Address) -> HandleResult:
        now_mono = time.monotonic()
        now_wall = time.time()
        events: list[Event] = []
        row = self._rows.get(message.sensor_id)

        if row is None:
            if len(self._rows) >= self.config.max_sensors:
                victim = self._pick_victim()
                if victim is None:
                    return self._error(
                        addr,
                        ErrorCode.SERVER_ERROR,
                        "tabela de sensores cheia",
                        (
                            "REG",
                            {
                                "id": message.sensor_id,
                                "result": "tabela_cheia",
                            },
                        ),
                    )
                del self._rows[victim.sensor_id]
                events.append(
                    Event(
                        "EVICT",
                        {
                            "id": victim.sensor_id,
                            "reason": victim.status.value,
                        },
                    )
                )
            new_row = self._create(
                message, addr, now_mono, now_wall
            )
            events.append(
                self._reg_event(new_row, result="ok", idempotent=False)
            )
            return HandleResult(
                (self._ack(new_row, "REG"),), tuple(events)
            )

        if row.session == message.session:
            previous = row.status
            row.addr = addr
            row.interval = message.interval
            row.last_seen_mono = now_mono
            row.last_seen_wall = now_wall
            if previous is not SensorStatus.ACTIVE:
                row.status = SensorStatus.ACTIVE
                row.departed_at = None
                events.append(
                    Event(
                        "REACTIVATED",
                        {
                            "id": row.sensor_id,
                            "session": row.session,
                            "previous": previous.value,
                        },
                    )
                )
            events.append(
                self._reg_event(row, result="ok", idempotent=True)
            )
            return HandleResult((self._ack(row, "REG"),), tuple(events))

        if row.status is SensorStatus.ACTIVE:
            return self._error(
                addr,
                ErrorCode.NAME_IN_USE,
                "identificador em uso por sessao ativa",
                ("REG", {"id": message.sensor_id, "result": "name_in_use"}),
            )

        if row.status is SensorStatus.SILENT:
            released_at = (
                row.last_seen_mono
                + self._offline_after(row)
                + self.config.release_after_silence
            )
            if now_mono < released_at:
                return self._error(
                    addr,
                    ErrorCode.NAME_IN_USE,
                    "identificador em uso por sessao ativa",
                    (
                        "REG",
                        {
                            "id": message.sensor_id,
                            "result": "name_in_use",
                        },
                    ),
                )

        events.append(
            Event(
                "REPLACED",
                {
                    "id": row.sensor_id,
                    "previous_session": row.session,
                    "previous_status": row.status.value,
                    "received": row.received,
                    "lost": row.lost,
                    "dup": row.dup,
                    "late": row.late,
                },
            )
        )
        del self._rows[row.sensor_id]
        new_row = self._create(message, addr, now_mono, now_wall)
        events.append(
            self._reg_event(new_row, result="ok", idempotent=False)
        )
        return HandleResult((self._ack(new_row, "REG"),), tuple(events))

    def _data(self, message: Data, addr: Address) -> HandleResult:
        row = self._rows.get(message.sensor_id)
        if row is None or row.session != message.session:
            return self._error(
                addr, ErrorCode.NOT_REGISTERED, "sensor nao registrado"
            )
        if not VALUE_MIN <= message.value <= VALUE_MAX:
            return self._error(
                addr,
                ErrorCode.VALUE_OUT_OF_RANGE,
                "valor fora da faixa permitida",
            )

        now_mono = time.monotonic()
        now_wall = time.time()
        row.addr = addr
        previous = row.status
        row.status = SensorStatus.ACTIVE
        row.departed_at = None
        row.last_seen_mono = now_mono
        row.last_seen_wall = now_wall

        events: list[Event] = [
            Event(
                "DATA",
                {
                    "id": row.sensor_id,
                    "seq": message.seq,
                    "value": message.value,
                },
            )
        ]
        if previous is not SensorStatus.ACTIVE:
            events.append(
                Event(
                    "REACTIVATED",
                    {
                        "id": row.sensor_id,
                        "session": row.session,
                        "previous": previous.value,
                    },
                )
            )

        last = row.last_seq
        if last is None:
            row.received += 1
            row.last_seq = message.seq
            self._accept(row, message)
        elif message.seq == last or message.seq in row.recent_set:
            row.dup += 1
            events.append(
                Event(
                    "DUP",
                    {
                        "id": row.sensor_id,
                        "seq": message.seq,
                        "dup": row.dup,
                    },
                )
            )
        elif message.seq < last:
            row.late += 1
            events.append(
                Event(
                    "LATE",
                    {
                        "id": row.sensor_id,
                        "seq": message.seq,
                        "late": row.late,
                    },
                )
            )
        else:
            gap = message.seq - last - 1
            if gap > 0:
                row.lost += gap
                events.append(
                    Event(
                        "LOSS",
                        {
                            "id": row.sensor_id,
                            "from_seq": last,
                            "to_seq": message.seq,
                            "gap": gap,
                            "lost": row.lost,
                        },
                    )
                )
            row.received += 1
            row.last_seq = message.seq
            self._accept(row, message)

        return HandleResult((), tuple(events))

    def _bye(self, message: Bye, addr: Address) -> HandleResult:
        row = self._rows.get(message.sensor_id)
        if row is None:
            return HandleResult(
                events=(
                    Event(
                        "BYE_IGNORED",
                        {
                            "id": message.sensor_id,
                            "session": message.session,
                            "reason": "desconhecido",
                        },
                    ),
                )
            )
        if row.session != message.session:
            return HandleResult(
                events=(
                    Event(
                        "BYE_IGNORED",
                        {
                            "id": message.sensor_id,
                            "session": message.session,
                            "reason": "sessao divergente",
                        },
                    ),
                )
            )

        row.status = SensorStatus.DEPARTED
        row.departed_at = time.monotonic()
        row.last_seen_mono = row.departed_at
        row.last_seen_wall = time.time()
        row.addr = addr
        return HandleResult(
            (self._ack(row, "BYE"),),
            (Event("BYE", {"id": row.sensor_id, "session": row.session}),),
        )

    # -- helpers --------------------------------------------------------

    def _create(
        self,
        message: Register,
        addr: Address,
        now_mono: float,
        now_wall: float,
    ) -> _Row:
        row = _Row(
            sensor_id=message.sensor_id,
            session=message.session,
            addr=addr,
            interval=message.interval,
            status=SensorStatus.ACTIVE,
            first_seen=now_wall,
            registered_at=now_wall,
            last_seen_mono=now_mono,
            last_seen_wall=now_wall,
            readings=deque(maxlen=max(0, self.config.max_readings)),
        )
        self._rows[row.sensor_id] = row
        return row

    def _accept(self, row: _Row, message: Data) -> None:
        row.last_value = message.value
        row.last_value_ts = message.timestamp
        row.readings.append(
            Reading(
                seq=message.seq,
                timestamp=message.timestamp,
                value=message.value,
            )
        )
        if (
            row.recent.maxlen is not None
            and len(row.recent) == row.recent.maxlen
        ):
            row.recent_set.discard(row.recent[0])
        row.recent.append(message.seq)
        row.recent_set.add(message.seq)

    def _pick_victim(self) -> _Row | None:
        """Pick the oldest departed row, then the oldest silent one."""

        departed = [
            row
            for row in self._rows.values()
            if row.status is SensorStatus.DEPARTED
        ]
        if departed:
            return min(departed, key=lambda row: row.departed_at or 0.0)
        silent = [
            row
            for row in self._rows.values()
            if row.status is SensorStatus.SILENT
        ]
        if silent:
            return min(silent, key=lambda row: row.last_seen_mono)
        return None

    def _offline_after(self, row: _Row) -> float:
        return max(
            self.config.offline_min,
            self.config.offline_factor * row.interval,
        )

    def _reg_event(
        self, row: _Row, *, result: str, idempotent: bool
    ) -> Event:
        return Event(
            "REG",
            {
                "id": row.sensor_id,
                "session": row.session,
                "addr": format_addr(row.addr),
                "interval": row.interval,
                "result": result,
                "idempotent": idempotent,
            },
        )

    def _ack(self, row: _Row, acked: Literal["REG", "BYE"]) -> Reply:
        payload = encode(Ack(acked, row.sensor_id, row.session))
        return Reply(payload=payload, addr=row.addr, kind="ack")

    def _error(
        self,
        addr: Address,
        code: ErrorCode,
        description: str,
        event: tuple[str, dict[str, object]] | None = None,
    ) -> HandleResult:
        payload = encode(Error(code, description))
        name, fields = event or (
            "PROTOCOL_ERROR",
            {"code": code.value, "addr": format_addr(addr)},
        )
        return HandleResult(
            replies=(Reply(payload=payload, addr=addr, kind="err"),),
            events=(Event(name, fields),),
        )


def format_addr(addr: Address) -> str:
    return f"{addr[0]}:{addr[1]}"


# -- collector I/O layer -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Command-line configuration of the collector (PLAN §7.1)."""

    host: str = "0.0.0.0"
    port: int = 6000
    max_sensors: int = 256
    max_readings: int = 120
    offline_min: float = 15.0
    offline_factor: float = 3.0
    release_after_silence: float = 0.0
    retention: float = 60.0
    refresh: float = 1.0
    drop_ack: float = 0.0
    seed: int | None = None
    json: bool = False
    log_file: str | None = None
    verbose: bool = False

    def table_config(self) -> TableConfig:
        return TableConfig(
            max_sensors=self.max_sensors,
            max_readings=self.max_readings,
            offline_min=self.offline_min,
            offline_factor=self.offline_factor,
            release_after_silence=self.release_after_silence,
            retention=self.retention,
        )


@dataclass(slots=True)
class _Bucket:
    """Token bucket used by :class:`ErrorLimiter`."""

    tokens: float
    updated: float

    def refill(self, now: float, capacity: float, rate: float) -> None:
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(capacity, self.tokens + elapsed * rate)
        self.updated = now


class ErrorLimiter:
    """Limit ``ERR`` replies per source IP and globally (RF-13, D-08).

    Each source IP gets a bucket of three tokens refilled at one token per
    second, so a burst of bad datagrams produces at most three replies and
    sustained abuse gets one reply per second. A global bucket of twenty
    tokens refilled at twenty per second caps the whole collector. The table
    of per-IP buckets is an LRU with 1024 entries.
    """

    def __init__(
        self,
        per_ip_capacity: float = 3.0,
        per_ip_rate: float = 1.0,
        global_capacity: float = 20.0,
        global_rate: float = 20.0,
        max_ips: int = 1024,
    ) -> None:
        self.per_ip_capacity = per_ip_capacity
        self.per_ip_rate = per_ip_rate
        self.global_capacity = global_capacity
        self.global_rate = global_rate
        self.max_ips = max_ips
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._global = _Bucket(tokens=global_capacity, updated=0.0)
        self._started = False

    def allow(self, ip: str, now: float) -> bool:
        """Consume one token and report whether the reply may be sent."""

        if not self._started:
            self._global.updated = now
            self._started = True
        self._global.refill(now, self.global_capacity, self.global_rate)
        if self._global.tokens < 1.0:
            return False

        bucket = self._buckets.get(ip)
        if bucket is None:
            if len(self._buckets) >= self.max_ips:
                self._buckets.popitem(last=False)
            bucket = _Bucket(tokens=self.per_ip_capacity, updated=now)
            self._buckets[ip] = bucket
        else:
            bucket.refill(now, self.per_ip_capacity, self.per_ip_rate)
            self._buckets.move_to_end(ip)

        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        self._global.tokens -= 1.0
        return True


_STATUS_LABELS = {
    SensorStatus.ACTIVE: "Online",
    SensorStatus.SILENT: "Offline",
    SensorStatus.DEPARTED: "Saiu",
}

_TABLE_HEADERS = (
    "ID",
    "SITUACAO",
    "ENDERECO",
    "INTERVALO",
    "SEQ",
    "VALOR",
    "IDADE",
    "PERDIDOS",
    "DUP",
    "ATRASADOS",
)
_TABLE_RIGHT_ALIGNED = frozenset(range(3, len(_TABLE_HEADERS)))


def render_table(rows: Sequence[SensorRow], now: float) -> str:
    """Render one snapshot of the table as plain text.

    ``now`` is the wall-clock instant used in the title; per-row ages come
    from :meth:`SensorTable.snapshot`. The output has no ANSI escapes: the
    caller decides whether to clear the screen.
    """

    clock = time.strftime("%H:%M:%S", time.localtime(now))
    lines = [f"TelemProto/1.0 - coletor UDP - {clock} - {len(rows)} sensor(es)"]
    cells = [
        (
            row.sensor_id,
            _STATUS_LABELS[row.status],
            format_addr(row.addr),
            str(row.interval),
            "-" if row.last_seq is None else str(row.last_seq),
            "-" if row.last_value is None else format_value(row.last_value),
            f"{row.age:.1f}s",
            str(row.lost),
            str(row.dup),
            str(row.late),
        )
        for row in rows
    ]
    widths = [
        max(
            [len(_TABLE_HEADERS[index])]
            + [len(row[index]) for row in cells]
        )
        for index in range(len(_TABLE_HEADERS))
    ]

    def format_row(values: Sequence[str]) -> str:
        parts = []
        for index, value in enumerate(values):
            if index in _TABLE_RIGHT_ALIGNED:
                parts.append(value.rjust(widths[index]))
            else:
                parts.append(value.ljust(widths[index]))
        return "  ".join(parts).rstrip()

    lines.append(format_row(_TABLE_HEADERS))
    lines.append("  ".join("-" * width for width in widths))
    if cells:
        lines.extend(format_row(row) for row in cells)
    else:
        lines.append("(nenhum sensor registrado)")
    return "\n".join(lines)


def clear_console() -> None:
    """Clear the terminal before redrawing the table.

    ``cls`` is used on Windows because not every Windows console understands
    ANSI escape sequences; on POSIX the standard ANSI clear sequence is enough.
    When stdout is redirected, nothing is cleared, so the table snapshots stay
    append-only in the file.
    """

    if not sys.stdout.isatty():
        return
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


class _LogFormatter(logging.Formatter):
    """``hh:mm:ss.mmm LEVEL message`` as required by REQUISITOS §8.2."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{stamp}.{int(record.msecs):03d} {record.levelname} {message}"


def format_event(event: Event) -> str:
    """Render one event as ``NAME key=value ...`` for the log."""

    parts = [event.name]
    for key, value in event.fields.items():
        parts.append(f"{key}={_format_log_value(value)}")
    return " ".join(parts)


def _format_log_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    if value is None:
        return "-"
    return str(value)


_EVENT_LEVELS = {
    "DATA": logging.DEBUG,
    "PROTOCOL_ERROR": logging.WARNING,
    "SERVER_ERROR": logging.ERROR,
    "ERR_SUPPRESSED": logging.WARNING,
}


class Collector:
    """UDP socket, event loop, output and command-line glue."""

    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.table = SensorTable(config.table_config())
        self.limiter = ErrorLimiter()
        self.rng = random.Random(config.seed)
        self.logger = logging.getLogger("telemproto.collector")
        self._sock: socket.socket | None = None
        self._stop = False

    # -- lifecycle ------------------------------------------------------

    def run(self) -> int:
        """Bind, serve until interrupted, print the summary and close."""

        try:
            self._configure_logging()
        except OSError as exc:
            print(
                f"erro: falha ao abrir o arquivo de log: {exc}",
                file=sys.stderr,
            )
            return 4

        try:
            self._sock = self._bind()
        except OSError as exc:
            self.logger.error(
                "falha ao abrir socket host=%s porta=%s erro=%s",
                self.config.host,
                self.config.port,
                exc,
            )
            return 3

        self._install_signal_handlers()
        code = 0
        try:
            self._log_startup()
            self._loop()
        except KeyboardInterrupt:
            self.logger.info("interrompido pelo usuario")
        except Exception:
            self.logger.exception("erro interno fora do laco de datagramas")
            code = 4
        finally:
            self._close()
            summary = self._summary()
            self._publish(Event("SUMMARY", summary))
            if not self.config.json:
                self._write(_summary_line(summary))
            self.logger.info("coletor encerrado")
        return code

    def _bind(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.config.host, self.config.port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    def _close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        finally:
            self._sock = None

    def _configure_logging(self) -> None:
        formatter = _LogFormatter()
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers.clear()
        self.logger.setLevel(
            logging.DEBUG if self.config.verbose else logging.INFO
        )
        self.logger.propagate = False
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        self.logger.addHandler(stream)
        if self.config.log_file:
            file_handler = logging.FileHandler(
                self.config.log_file, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
        except (AttributeError, OSError, ValueError):
            pass

    def _on_signal(self, signum: int, frame: object) -> None:
        self._stop = True

    # -- event loop -----------------------------------------------------

    def _loop(self) -> None:
        assert self._sock is not None
        selector = selectors.DefaultSelector()
        selector.register(self._sock, selectors.EVENT_READ)
        try:
            next_render = time.monotonic()
            while not self._stop:
                timeout = max(0.0, next_render - time.monotonic())
                selector.select(timeout)
                self._drain()
                for event in self.table.expire():
                    self._publish(event)
                now = time.monotonic()
                if now >= next_render:
                    self._render()
                    next_render = now + self.config.refresh
        finally:
            selector.close()

    def _drain(self) -> None:
        assert self._sock is not None
        for _ in range(64):
            try:
                raw, addr = self._sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError as exc:
                self.logger.warning("falha ao receber datagrama erro=%s", exc)
                return
            self._handle(raw, addr)

    def _handle(self, raw: bytes, addr: Address) -> None:
        result = self.table.handle(raw, addr)
        for event in result.events:
            self._publish(event)
        now = time.monotonic()
        for reply in result.replies:
            self._send(reply, now)

    def _send(self, reply: Reply, now: float) -> None:
        assert self._sock is not None
        code = self._reply_code(reply) if reply.kind == "err" else None
        if reply.kind == "err" and not self.limiter.allow(
            reply.addr[0], now
        ):
            self._publish(
                Event(
                    "ERR_SUPPRESSED",
                    {"addr": format_addr(reply.addr), "code": code},
                )
            )
            return
        if self.rng.random() < self.config.drop_ack:
            fields: dict[str, object] = {
                "addr": format_addr(reply.addr),
                "kind": reply.kind,
            }
            if code is not None:
                fields["code"] = code
            self._publish(Event("DROP", fields))
            return
        try:
            self._sock.sendto(reply.payload, reply.addr)
        except OSError as exc:
            self.logger.warning(
                "falha ao enviar resposta addr=%s erro=%s",
                format_addr(reply.addr),
                exc,
            )
            return
        if code is not None:
            self._publish(
                Event(
                    "ERR_SENT",
                    {"addr": format_addr(reply.addr), "code": code},
                )
            )

    @staticmethod
    def _reply_code(reply: Reply) -> str:
        try:
            message = decode(reply.payload)
        except ProtocolError:
            return "?"
        return message.code.value if isinstance(message, Error) else "?"

    # -- output ---------------------------------------------------------

    def _log_startup(self) -> None:
        assert self._sock is not None
        addr = self._sock.getsockname()
        self.logger.info("escutando endereco=%s porta=%s", addr[0], addr[1])
        if self.config.port == 0:
            self.logger.info("porta efetiva=%s", addr[1])
        self.logger.debug(
            "configuracao max_sensors=%s max_readings=%s refresh=%s "
            "drop_ack=%s",
            self.config.max_sensors,
            self.config.max_readings,
            self.config.refresh,
            self.config.drop_ack,
        )

    def _render(self) -> None:
        if self.config.json:
            return
        clear_console()
        text = render_table(self.table.snapshot(), time.time())
        self._write(text)

    def _publish(self, event: Event) -> None:
        self.logger.log(
            _EVENT_LEVELS.get(event.name, logging.INFO), format_event(event)
        )
        if not self.config.json:
            return
        if event.name == "DATA" and not self.config.verbose:
            return
        record: dict[str, object] = {
            "ts": round(time.time(), 3),
            "event": event.name,
        }
        record.update(event.fields)
        self._write(json.dumps(record, ensure_ascii=False, default=str))

    def _summary(self) -> dict[str, object]:
        rows = self.table.snapshot()
        online = sum(row.status is SensorStatus.ACTIVE for row in rows)
        offline = sum(row.status is SensorStatus.SILENT for row in rows)
        departed = sum(row.status is SensorStatus.DEPARTED for row in rows)
        return {
            "sensors": len(rows),
            "online": online,
            "offline": offline,
            "departed": departed,
            "received": sum(row.received for row in rows),
            "lost": sum(row.lost for row in rows),
            "dup": sum(row.dup for row in rows),
            "late": sum(row.late for row in rows),
        }

    @staticmethod
    def _write(text: str) -> None:
        sys.stdout.write(text if text.endswith("\n") else f"{text}\n")
        sys.stdout.flush()


def _summary_line(summary: dict[str, object]) -> str:
    return (
        "Resumo: sensores={sensors} online={online} offline={offline} "
        "saiu={departed} recebidos={received} perdidos={lost} "
        "duplicados={dup} atrasados={late}"
    ).format(**summary)


# -- command line --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.collector",
        description="Coletor de telemetria TelemProto/1.0 sobre UDP.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="endereco de bind"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6000,
        metavar="PORTA",
        help="porta UDP; 0 escolhe uma porta livre",
    )
    parser.add_argument(
        "--max-sensors",
        type=int,
        default=256,
        metavar="N",
        help="limite de sensores na tabela",
    )
    parser.add_argument(
        "--max-readings",
        type=int,
        default=120,
        metavar="N",
        help="historico de leituras por sensor",
    )
    parser.add_argument(
        "--offline-min",
        type=float,
        default=15.0,
        metavar="S",
        help="piso do timeout de silencio em segundos",
    )
    parser.add_argument(
        "--offline-factor",
        type=float,
        default=3.0,
        metavar="X",
        help="multiplicador do intervalo declarado",
    )
    parser.add_argument(
        "--release-after-silence",
        type=float,
        default=0.0,
        metavar="S",
        help="espera extra para liberar um identificador sem BYE",
    )
    parser.add_argument(
        "--retention",
        type=float,
        default=60.0,
        metavar="S",
        help="tempo de retencao de uma linha com BYE",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=1.0,
        metavar="S",
        help="periodo de redesenho da tabela",
    )
    parser.add_argument(
        "--drop-ack",
        type=float,
        default=0.0,
        metavar="P",
        help="probabilidade de descartar ACK/ERR (demonstracao)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="semente de --drop-ack",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="eventos em NDJSON no stdout, sem tabela",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="ARQUIVO",
        help="arquivo adicional de log",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="nivel DEBUG, incluindo cada DATA",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> CollectorConfig:
    """Validate parsed arguments and build the collector configuration."""

    if not args.host.strip():
        raise ValueError("endereco de bind vazio")
    if not 0 <= args.port <= 65535:
        raise ValueError("porta fora da faixa 0..65535")
    if args.max_sensors < 1:
        raise ValueError("--max-sensors deve ser pelo menos 1")
    if args.max_readings < 0:
        raise ValueError("--max-readings nao pode ser negativo")
    if args.offline_min < 0:
        raise ValueError("--offline-min nao pode ser negativo")
    if args.offline_factor < 0:
        raise ValueError("--offline-factor nao pode ser negativo")
    if args.release_after_silence < 0:
        raise ValueError("--release-after-silence nao pode ser negativo")
    if args.retention < 0:
        raise ValueError("--retention nao pode ser negativo")
    if args.refresh <= 0:
        raise ValueError("--refresh deve ser maior que zero")
    if not 0.0 <= args.drop_ack <= 1.0:
        raise ValueError("--drop-ack deve ficar entre 0 e 1")
    return CollectorConfig(
        host=args.host,
        port=args.port,
        max_sensors=args.max_sensors,
        max_readings=args.max_readings,
        offline_min=args.offline_min,
        offline_factor=args.offline_factor,
        release_after_silence=args.release_after_silence,
        retention=args.retention,
        refresh=args.refresh,
        drop_ack=args.drop_ack,
        seed=args.seed,
        json=args.json,
        log_file=args.log_file,
        verbose=args.verbose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    return Collector(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
