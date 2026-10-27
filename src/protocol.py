"""TelemProto/1.0 wire codec.

This module only converts between bytes and message objects. It knows nothing
about the network, sensors or collector state. Grammar and validation order follow
``docs/PROTOCOLO.md`` section 8:

    size -> encoding -> structure/version -> field syntax -> state -> value range

The last two steps belong to the collector table, not to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias

Address: TypeAlias = tuple[str, int]

VERSION = 1
MAX_DATAGRAM = 512
MAX_ID_LEN = 16
SESSION_LEN = 8
INTERVAL_MIN = 1
INTERVAL_MAX = 3600
VALUE_MIN = -50.0
VALUE_MAX = 150.0
ERR_MAX_BYTES = 64

_ID_RE = re.compile(r"[A-Za-z0-9]{1,16}", re.ASCII)
_SESSION_RE = re.compile(r"[0-9a-f]{8}", re.ASCII)
_INTERVAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,3})", re.ASCII)
_SEQ_RE = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)
_TIMESTAMP_RE = re.compile(r"[0-9]{1,10}", re.ASCII)
_VALUE_RE = re.compile(
    r"-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,3})?", re.ASCII
)
_DESCRIPTION_RE = re.compile(r"[\x20-\x7b\x7d-\x7e]{1,40}", re.ASCII)

_FIELD_COUNTS = {"REG": 5, "DATA": 7, "BYE": 4, "ACK": 5, "ERR": 4}


class ErrorCode(str, Enum):
    """``err-code`` values defined by TelemProto/1.0."""

    BAD_REQUEST = "BAD_REQUEST"
    TOO_LONG = "TOO_LONG"
    NOT_REGISTERED = "NOT_REGISTERED"
    NAME_IN_USE = "NAME_IN_USE"
    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    BAD_INTERVAL = "BAD_INTERVAL"
    SERVER_ERROR = "SERVER_ERROR"


class ProtocolError(Exception):
    """Raised when a datagram cannot be decoded.

    ``description`` is user-facing and therefore written in Brazilian
    Portuguese; it is safe to put on the wire inside an ``ERR`` message.
    """

    def __init__(self, code: ErrorCode, description: str) -> None:
        super().__init__(f"{code.value}: {description}")
        self.code = code
        self.description = description


@dataclass(frozen=True, slots=True)
class Register:
    sensor_id: str
    interval: int
    session: str


@dataclass(frozen=True, slots=True)
class Data:
    sensor_id: str
    session: str
    seq: int
    timestamp: int
    value: float


@dataclass(frozen=True, slots=True)
class Bye:
    sensor_id: str
    session: str


@dataclass(frozen=True, slots=True)
class Ack:
    acked: Literal["REG", "BYE"]
    sensor_id: str
    session: str


@dataclass(frozen=True, slots=True)
class Error:
    code: ErrorCode
    description: str


Message: TypeAlias = Register | Data | Bye | Ack | Error


def decode(raw: bytes) -> Message:
    """Decode one datagram into a message object.

    Raises :class:`ProtocolError` for every invalid input, in the order defined
    by the protocol specification.
    """

    data = bytes(raw)
    if len(data) > MAX_DATAGRAM:
        raise ProtocolError(ErrorCode.TOO_LONG, "datagrama excede 512 bytes")
    if not data:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "mensagem vazia")

    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "codificacao invalida") from None
    if any(not "\x20" <= char <= "\x7e" for char in text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "codificacao invalida")

    fields = text.split("|")
    kind = fields[0]
    expected = _FIELD_COUNTS.get(kind)
    if expected is None:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "tipo de mensagem desconhecido")
    if len(fields) != expected:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "contagem de campos invalida")
    if fields[1] != str(VERSION):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "versao nao suportada")

    if kind == "REG":
        return Register(
            sensor_id=_parse_id(fields[2]),
            interval=_parse_interval(fields[3]),
            session=_parse_session(fields[4]),
        )
    if kind == "DATA":
        return Data(
            sensor_id=_parse_id(fields[2]),
            session=_parse_session(fields[3]),
            seq=_parse_seq(fields[4]),
            timestamp=_parse_timestamp(fields[5]),
            value=_parse_value(fields[6]),
        )
    if kind == "BYE":
        return Bye(
            sensor_id=_parse_id(fields[2]),
            session=_parse_session(fields[3]),
        )
    if kind == "ACK":
        return Ack(
            acked=_parse_acked(fields[2]),
            sensor_id=_parse_id(fields[3]),
            session=_parse_session(fields[4]),
        )
    return Error(
        code=_parse_error_code(fields[2]),
        description=_parse_description(fields[3]),
    )


def encode(message: Message) -> bytes:
    """Encode a message object into one datagram payload.

    Values that cannot be represented on the wire raise :class:`ValueError`,
    since they indicate a programming error rather than bad network input.
    """

    if isinstance(message, Register):
        if not INTERVAL_MIN <= message.interval <= INTERVAL_MAX:
            raise ValueError(f"interval out of range: {message.interval!r}")
        return (
            f"REG|{VERSION}|{_text_field(message.sensor_id)}|"
            f"{message.interval}|{_text_field(message.session)}"
        ).encode("ascii")
    if isinstance(message, Data):
        if message.seq < 1 or message.timestamp < 0:
            raise ValueError("seq and timestamp must be non-negative")
        return (
            f"DATA|{VERSION}|{_text_field(message.sensor_id)}|"
            f"{_text_field(message.session)}|{message.seq}|"
            f"{message.timestamp}|{format_value(message.value)}"
        ).encode("ascii")
    if isinstance(message, Bye):
        return (
            f"BYE|{VERSION}|{_text_field(message.sensor_id)}|"
            f"{_text_field(message.session)}"
        ).encode("ascii")
    if isinstance(message, Ack):
        if message.acked not in ("REG", "BYE"):
            raise ValueError(f"invalid acked type: {message.acked!r}")
        return (
            f"ACK|{VERSION}|{message.acked}|"
            f"{_text_field(message.sensor_id)}|{_text_field(message.session)}"
        ).encode("ascii")
    if isinstance(message, Error):
        description = message.description.replace("|", "/")[:40] or "erro"
        return (
            f"ERR|{VERSION}|{message.code.value}|"
            f"{description}"
        ).encode("ascii", "replace")
    raise TypeError(f"unsupported message: {message!r}")


def format_value(value: float) -> str:
    """Format a reading with up to three decimal places and no exponent."""

    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _parse_id(text: str) -> str:
    if not _ID_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "identificador invalido")
    return text


def _parse_session(text: str) -> str:
    if not _SESSION_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "sessao invalida")
    return text


def _parse_interval(text: str) -> int:
    if not _INTERVAL_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "intervalo invalido")
    interval = int(text)
    if not INTERVAL_MIN <= interval <= INTERVAL_MAX:
        raise ProtocolError(
            ErrorCode.BAD_INTERVAL, "intervalo fora da faixa 1..3600"
        )
    return interval


def _parse_seq(text: str) -> int:
    if not _SEQ_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "numero de sequencia invalido")
    return int(text)


def _parse_timestamp(text: str) -> int:
    if not _TIMESTAMP_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "timestamp invalido")
    return int(text)


def _parse_value(text: str) -> float:
    if not _VALUE_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "valor invalido")
    return float(text)


def _parse_acked(text: str) -> Literal["REG", "BYE"]:
    if text == "REG":
        return "REG"
    if text == "BYE":
        return "BYE"
    raise ProtocolError(ErrorCode.BAD_REQUEST, "confirmacao invalida")


def _parse_error_code(text: str) -> ErrorCode:
    try:
        return ErrorCode(text)
    except ValueError:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST, "codigo de erro desconhecido"
        ) from None


def _parse_description(text: str) -> str:
    if not _DESCRIPTION_RE.fullmatch(text):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "descricao de erro invalida")
    return text


def _text_field(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "|" in value
        or any(not "\x20" <= char <= "\x7e" for char in value)
    ):
        raise ValueError(f"invalid protocol field: {value!r}")
    return value
