#!/usr/bin/env python3
"""Publish one JSON message over MQTT 3.1.1 without an external client library."""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
from typing import Final


DEFAULT_TOPIC: Final = "v1/devices/me/telemetry"


def _mqtt_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65_535:
        raise ValueError("MQTT string exceeds 65535 encoded bytes")
    return struct.pack("!H", len(encoded)) + encoded


def _remaining_length(length: int) -> bytes:
    if not 0 <= length <= 268_435_455:
        raise ValueError("MQTT remaining length is out of range")
    result = bytearray()
    while True:
        encoded = length % 128
        length //= 128
        if length:
            encoded |= 0x80
        result.append(encoded)
        if not length:
            return bytes(result)


def _packet(packet_type: int, body: bytes) -> bytes:
    return bytes([packet_type]) + _remaining_length(len(body)) + body


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = connection.recv(size - len(value))
        if not chunk:
            raise ConnectionError("MQTT connection closed before packet completed")
        value.extend(chunk)
    return bytes(value)


def _recv_packet(connection: socket.socket) -> tuple[int, bytes]:
    packet_type = _recv_exact(connection, 1)[0]
    multiplier = 1
    remaining = 0
    for _ in range(4):
        encoded = _recv_exact(connection, 1)[0]
        remaining += (encoded & 0x7F) * multiplier
        if encoded & 0x80 == 0:
            return packet_type, _recv_exact(connection, remaining)
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


def publish(
    *,
    host: str,
    port: int,
    username: str,
    topic: str,
    payload: bytes,
    client_id: str,
    timeout_seconds: float,
) -> None:
    connect_body = (
        _mqtt_string("MQTT")
        + bytes([4, 0x82])
        + struct.pack("!H", 30)
        + _mqtt_string(client_id)
        + _mqtt_string(username)
    )
    packet_id = 1
    publish_body = (
        _mqtt_string(topic)
        + struct.pack("!H", packet_id)
        + payload
    )
    with socket.create_connection(
        (host, port),
        timeout=timeout_seconds,
    ) as connection:
        connection.settimeout(timeout_seconds)
        connection.sendall(_packet(0x10, connect_body))
        packet_type, connack = _recv_packet(connection)
        if packet_type != 0x20 or connack != b"\x00\x00":
            return_code = connack[1] if len(connack) > 1 else None
            raise ConnectionError(
                f"MQTT broker rejected CONNECT with code {return_code}"
            )
        connection.sendall(_packet(0x32, publish_body))
        packet_type, puback = _recv_packet(connection)
        if packet_type != 0x40 or puback != struct.pack("!H", packet_id):
            raise ConnectionError("MQTT broker did not acknowledge PUBLISH")
        connection.sendall(b"\xe0\x00")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--username-env", required=True)
    parser.add_argument("--payload-json", required=True)
    parser.add_argument("--client-id", default=f"lilies-publisher-{os.getpid()}")
    parser.add_argument("--timeout-seconds", type=float, default=10)
    args = parser.parse_args()

    username = os.environ.get(args.username_env, "")
    if not username:
        raise SystemExit(f"environment variable {args.username_env!r} is empty")
    value = json.loads(args.payload_json)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    publish(
        host=args.host,
        port=args.port,
        username=username,
        topic=args.topic,
        payload=payload,
        client_id=args.client_id,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "status": "published",
                "host": args.host,
                "port": args.port,
                "topic": args.topic,
                "payload_bytes": len(payload),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
