"""TelemProto/1.0 collector state machine.

:class:`SensorTable` receives decoded datagrams plus a source address and
returns replies and events. It owns registration, sequence accounting, liveness
and status transitions, but performs no I/O and writes no output; that belongs
to the collector loop added in the next step.

User-facing descriptions are written in Brazilian Portuguese; identifiers and
comments are in English.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

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
