"""TelemProto/1.0 sensor client.

Implements the sensor FSM from ``docs/PROTOCOLO.md`` section 7: registration
(REG/ACK with retries), periodic DATA readings and graceful BYE shutdown.

Identifiers, comments and docstrings are in English. User-facing strings
(logs and CLI errors) are in Brazilian Portuguese.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from .protocol import (
    Ack,
    Bye,
    Data,
    Error,
    ErrorCode,
    ProtocolError,
    Register,
    decode,
    encode,
    format_value,
)

# Client constants (docs/PROTOCOLO.md section 9).
REG_TIMEOUT = 1.0
REG_ATTEMPTS = 4
BYE_TIMEOUT = 0.5
BYE_ATTEMPTS = 3
NAME_IN_USE_GRACE_MIN = 15.0
NAME_IN_USE_GRACE_FACTOR = 3.0
NAME_IN_USE_GRACE_MARGIN = 2.0

VALUE_MIN = -50.0
VALUE_MAX = 150.0

_ID_RE = re.compile(r"[A-Za-z0-9]{1,16}", re.ASCII)


@dataclass(frozen=True, slots=True)
class SensorConfig:
    """Command-line configuration of the sensor (PLAN section 7.2)."""

    host: str = "127.0.0.1"
    port: int = 6000
    sensor_id: str = ""
    interval: int = 5
    loss: float = 0.0
    base: float = 21.5
    wander: float = 0.4
    seed: int | None = None
    count: int = 0


class SensorClient:
    """Sensor client FSM: register, send readings, exit gracefully."""

    def __init__(self, config: SensorConfig) -> None:
        self.config = config
        self.session = secrets.token_hex(4)
        self.seq = 1
        self.value = float(config.base)
        self.registered = False
        self._rng = random.Random(config.seed)
        self._logger = logging.getLogger("telemproto.sensor")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self._logger.addHandler(handler)
        self._sock: socket.socket | None = None
        self.next_data: float | None = None
        self.data_start: float | None = None
        self.data_n: int = 0
        self.next_reg: float = 0.0
        self.reg_waiting: bool = False
        self.reg_deadline: float = 0.0
        self.reg_attempts: int = 0
        self.name_in_use_since: float | None = None
        self.readings_sent: int = 0

    # -- lifecycle ------------------------------------------------------

    def run(self) -> int:
        """Create the connected UDP socket and run the client FSM."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((self.config.host, self.config.port))
        except OSError as exc:
            self._error(
                "falha ao abrir socket %s:%s: %s",
                self.config.host,
                self.config.port,
                exc,
            )
            return 3

        self._sock = sock
        self.readings_sent = 0
        self.next_data = None
        self.data_start = None
        self.data_n = 0
        self.next_reg = 0.0
        self.reg_waiting = False
        self.reg_deadline = 0.0
        self.reg_attempts = 0
        self.name_in_use_since = None

        code = 0
        try:
            try:
                code = self._loop(sock)
            except KeyboardInterrupt:
                self._info("interrompido pelo usuario")
                code = 0
        finally:
            if self.registered:
                try:
                    self._bye(sock)
                except KeyboardInterrupt:
                    pass
            self._close()
        return code

    # ---------------------------------------------------------

    def _loop(self, sock: socket.socket) -> int:
        """Main event loop. Returns the process exit code."""

        while True:
            now = time.monotonic()

            # NAME_IN_USE grace window: exit 5 when it ends.
            if self.name_in_use_since is not None:
                grace = (
                    max(
                        NAME_IN_USE_GRACE_MIN,
                        NAME_IN_USE_GRACE_FACTOR * self.config.interval,
                    )
                    + NAME_IN_USE_GRACE_MARGIN
                )
                if now - self.name_in_use_since >= grace:
                    self._warn(
                        "carencia de NAME_IN_USE esgotada para id=%s; saindo",
                        self.config.sensor_id,
                    )
                    return 5

            if (
                not self.registered
                and not self.reg_waiting
                and now >= self.next_reg
            ):
                self._send_reg(sock)
                self.reg_waiting = True
                self.reg_attempts = 1
                self.reg_deadline = now + REG_TIMEOUT
                continue

            if (
                self.registered
                and self.next_data is not None
                and now >= self.next_data
            ):
                if self._drain_not_registered(sock):
                    self.registered = False
                    self.next_reg = now
                    continue
                self._send_data(sock)
                self.readings_sent += 1
                self.seq += 1
                if self.config.count > 0 and self.readings_sent >= self.config.count:
                    break
                self.data_n += 1
                assert self.data_start is not None
                self.next_data = (
                    self.data_start + self.data_n * self.config.interval
                )
                while self.next_data <= now:
                    self.data_n += 1
                    self.next_data = (
                        self.data_start + self.data_n * self.config.interval
                    )
                continue

            if self.registered:
                deadline = self.next_data
            elif self.reg_waiting:
                deadline = self.reg_deadline
            else:
                deadline = self.next_reg
            timeout = None if deadline is None else max(0.0, deadline - now)
            sock.settimeout(timeout)
            raw = self._recv_once(sock)
            now = time.monotonic()

            if raw is not None:
                exit_code = self._process_incoming(raw, now)
                if exit_code is not None:
                    return exit_code
                continue

            # Timeout fired.
            if self.reg_waiting and now >= self.reg_deadline:
                if self.reg_attempts < REG_ATTEMPTS:
                    self._send_reg(sock)
                    self.reg_attempts += 1
                    self.reg_deadline = now + REG_TIMEOUT
                else:
                    self._warn(
                        "sem ACK de REG apos %d tentativas; nova tentativa no proximo tick",
                        REG_ATTEMPTS,
                    )
                    self.reg_waiting = False
                    self.reg_attempts = 0
                    self.next_reg = now + self.config.interval

        return 0

    # -- message handling --------------------------------------

    def _process_incoming(
        self, raw: bytes, now: float
    ) -> int | None:
        """Process one received datagram.

        Returns an exit code to terminate the process, or None to continue.
        """

        try:
            message = decode(raw)
        except ProtocolError:
            return None

        if isinstance(message, Ack):
            if (
                not self.registered
                and message.acked == "REG"
                and message.sensor_id == self.config.sensor_id
                and message.session == self.session
            ):
                self.registered = True
                self.reg_waiting = False
                self.reg_attempts = 0
                self.name_in_use_since = None
                self._info(
                    "registrado id=%s sessao=%s",
                    self.config.sensor_id,
                    self.session,
                )
                # First DATA immediately after the ACK.
                self.data_start = now
                self.next_data = now
                self.data_n = 0
            # Stale ACKs from other sessions or ACK|BYE are ignored here.
            return None

        if not isinstance(message, Error):
            return None

        self._warn(
            "erro recebido: %s %s",
            message.code.value,
            message.description,
        )
        if message.code == ErrorCode.BAD_INTERVAL:
            self._error(
                "intervalo invalido (%s); saindo",
                self.config.interval,
            )
            return 2
        if message.code == ErrorCode.NAME_IN_USE:
            if self.name_in_use_since is None:
                self.name_in_use_since = now
            # Abort the current REG cycle and retry every interval.
            self.registered = False
            self.reg_waiting = False
            self.reg_attempts = 0
            self.next_reg = now + self.config.interval
            return None
        if message.code == ErrorCode.NOT_REGISTERED and self.registered:
            self.registered = False
            self.next_reg = now
        return None

    # -- socket helpers ---------------------------------------------------

    def _recv_once(self, sock: socket.socket) -> bytes | None:
        """Receive one datagram, or None on timeout/error."""
        try:
            return sock.recv(65535)
        except socket.timeout:
            return None
        except (ConnectionRefusedError, OSError) as exc:
            self._error("erro de socket: %s", exc)
            return None

    def _drain_not_registered(self, sock: socket.socket) -> bool:
        """Non-blocking drain; True if a NOT_REGISTERED error was observed."""
        sock.settimeout(0.0)
        try:
            while True:
                try:
                    raw = sock.recv(65535)
                except BlockingIOError:
                    return False
                except (ConnectionRefusedError, OSError):
                    return False
                try:
                    message = decode(raw)
                except ProtocolError:
                    continue
                if (
                    isinstance(message, Error)
                    and message.code == ErrorCode.NOT_REGISTERED
                ):
                    return True
        finally:
            sock.settimeout(None)

    # -- sending -----------------------------------------------

    def _send_reg(self, sock: socket.socket) -> None:
        payload = encode(
            Register(self.config.sensor_id, self.config.interval, self.session)
        )
        try:
            sock.send(payload)
        except OSError as exc:
            self._error("falha ao enviar REG: %s", exc)
            return
        self._info(
            "enviado REG id=%s intervalo=%s sessao=%s",
            self.config.sensor_id,
            self.config.interval,
            self.session,
        )

    def _send_data(self, sock: socket.socket) -> None:
        now_wall = int(time.time())
        message = Data(
            self.config.sensor_id,
            self.session,
            self.seq,
            now_wall,
            self.value,
        )
        payload = encode(message)
        dropped = self._rng.random() < self.config.loss
        try:
            if not dropped:
                sock.send(payload)
                self._info(
                    "enviado DATA seq=%s valor=%s",
                    self.seq,
                    format_value(self.value),
                )
            else:
                self._info(
                    "descartado DATA seq=%s (perda simulada)",
                    self.seq,
                )
        except OSError as exc:
            self._error("falha ao enviar DATA: %s", exc)
            return
        # Random walk from base, clamped to [-50, 150], 3 decimal places.
        step = self._rng.uniform(-self.config.wander, self.config.wander)
        self.value = min(VALUE_MAX, max(VALUE_MIN, self.value + step))
        self.value = round(self.value, 3)

    def _bye(self, sock: socket.socket) -> None:
        """Send BYE up to BYE_ATTEMPTS, then exit regardless of ACK."""
        payload = encode(Bye(self.config.sensor_id, self.session))
        for attempt in range(1, BYE_ATTEMPTS + 1):
            try:
                sock.send(payload)
            except OSError as exc:
                self._error("falha ao enviar BYE: %s", exc)
            sock.settimeout(BYE_TIMEOUT)
            try:
                raw = sock.recv(65535)
            except socket.timeout:
                self._debug(
                    "sem ACK de BYE (tentativa %d/%d)", attempt, BYE_ATTEMPTS
                )
                continue
            except OSError as exc:
                self._error("erro ao aguardar ACK de BYE: %s", exc)
                continue
            try:
                message = decode(raw)
            except ProtocolError:
                continue
            if (
                isinstance(message, Ack)
                and message.acked == "BYE"
                and message.sensor_id == self.config.sensor_id
                and message.session == self.session
            ):
                self._info("BYE confirmado")
                return
        self._info(
            "BYE sem confirmacao apos %d tentativas; encerrando de qualquer forma",
            BYE_ATTEMPTS,
        )

    # -- logging / closure ------------------------------------

    def _info(self, msg: str, *args: object) -> None:
        self._logger.info(msg, *args)

    def _warn(self, msg: str, *args: object) -> None:
        self._logger.warning(msg, *args)

    def _error(self, msg: str, *args: object) -> None:
        self._logger.error(msg, *args)

    def _debug(self, msg: str, *args: object) -> None:
        self._logger.debug(msg, *args)

    def _close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            pass
        finally:
            self._sock = None


# -- command line -----------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.sensor",
        description="Sensor de telemetria TelemProto/1.0 sobre UDP.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="endereco do coletor"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6000,
        metavar="PORTA",
        help="porta UDP do coletor",
    )
    parser.add_argument(
        "--id",
        required=True,
        dest="sensor_id",
        metavar="ID",
        help="identificador do sensor (1 a 16 caracteres alfanumericos)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        metavar="S",
        help="segundos entre leituras (1..3600)",
    )
    parser.add_argument(
        "--loss",
        type=float,
        default=0.0,
        metavar="P",
        help="probabilidade de descartar um datagrama de DATA (0..1)",
    )
    parser.add_argument(
        "--base",
        type=float,
        default=21.5,
        metavar="V",
        help="valor inicial do passeio aleatorio",
    )
    parser.add_argument(
        "--wander",
        type=float,
        default=0.4,
        metavar="V",
        help="passo maximo do passeio aleatorio",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="semente para geracao de valores e perda",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        metavar="N",
        help="numero de leituras; 0 = ilimitado",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> SensorConfig:
    """Validate parsed arguments and build the sensor configuration."""

    errors: list[str] = []
    if not _ID_RE.fullmatch(args.sensor_id):
        errors.append("--id deve ter 1 a 16 caracteres alfanumericos")
    if not 1 <= args.interval <= 3600:
        errors.append("--interval deve ficar entre 1 e 3600 segundos")
    if not 0.0 <= args.loss <= 1.0:
        errors.append("--loss deve ficar entre 0 e 1")
    if errors:
        for error in errors:
            print(f"erro: {error}", file=sys.stderr)
        raise SystemExit(2)
    return SensorConfig(
        host=args.host,
        port=args.port,
        sensor_id=args.sensor_id,
        interval=args.interval,
        loss=args.loss,
        base=args.base,
        wander=args.wander,
        seed=args.seed,
        count=args.count,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    client = SensorClient(config)
    return client.run()


if __name__ == "__main__":
    raise SystemExit(main())
