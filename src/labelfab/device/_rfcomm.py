"""RFCOMM addressing for interpreters built without ``bluetooth/bluetooth.h``.

``AF_BLUETOOTH`` is a *kernel* feature, but CPython gates two compile-time things on
the BlueZ headers: the ``socket.AF_BLUETOOTH``/``BTPROTO_RFCOMM`` constants, and the
``sockaddr_rc`` marshalling inside ``getsockaddrarg()``. Distro pythons are built with
those headers; relocatable ones (python-build-standalone, which is what the ``.deb``
ships) are not, so ``sock.connect((mac, channel))`` raises even though the kernel
underneath is perfectly willing.

Only the address encoding is missing, and it is nine bytes of stable kernel ABI. So
build ``sockaddr_rc`` here and hand it to libc ``connect(2)`` directly. Everything
after that -- ``sendall``, ``recv``, ``settimeout``, ``close`` -- is family-agnostic
and needs no shim, which is why this module is the whole of the workaround.

The alternative considered and rejected was compiling CPython from source with
``libbluetooth-dev``: it costs the glibc-only property of the shipped interpreter
(``_ssl`` and friends become undeclared shared-library dependencies on whatever the
build container had), and it puts a full CPython build inside the QEMU-emulated arm64
release leg.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import socket
import sys

#: From ``<sys/socket.h>`` and ``<bluetooth/bluetooth.h>``. Hardcoded because the
#: interpreters this module exists for are precisely the ones lacking the constants.
AF_BLUETOOTH = 31
BTPROTO_RFCOMM = 3

#: ``sizeof(struct sockaddr_rc)`` -- 2 (family) + 6 (bdaddr) + 1 (channel) + 1 tail
#: pad. The kernel rejects anything shorter with ``EINVAL``, so this is load-bearing
#: rather than cosmetic.
SOCKADDR_RC_SIZE = 10

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def sockaddr_rc(mac: str, channel: int) -> bytes:
    """Encode ``struct sockaddr_rc``.

    .. code-block:: c

        struct sockaddr_rc {
            sa_family_t rc_family;   /* u16, host byte order */
            bdaddr_t    rc_bdaddr;   /* 6 bytes, little-endian: reversed vs display */
            uint8_t     rc_channel;
        };

    The byte reversal is the part that bites: BlueZ prints addresses most-significant
    octet first and stores them least-significant first.
    """
    try:
        raw_mac = bytes.fromhex(mac.replace(":", ""))
    except ValueError as exc:
        raise ValueError(f"not a bluetooth address: {mac!r}") from exc
    if len(raw_mac) != 6:
        raise ValueError(f"not a bluetooth address: {mac!r}")
    if not 0 <= channel <= 255:
        raise ValueError(f"RFCOMM channel out of range: {channel}")
    return AF_BLUETOOTH.to_bytes(2, sys.byteorder) + raw_mac[::-1] + bytes([channel]) + b"\x00"


def has_native_support() -> bool:
    """Whether the running interpreter can address RFCOMM without this shim."""
    return hasattr(socket, "AF_BLUETOOTH")


def socket_rfcomm() -> socket.socket:
    """An RFCOMM stream socket, with or without the constants being present.

    ``socket()`` takes plain integers, so this needs no shim at all -- the family only
    has to be *known to the kernel*, not to the interpreter.
    """
    return socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)


def connect(sock: socket.socket, mac: str, channel: int, timeout_s: float) -> None:
    """``connect(2)`` to an RFCOMM peer, with a timeout, via libc.

    ``socket.settimeout()`` puts the fd in ``O_NONBLOCK`` and emulates blocking inside
    the socket module -- machinery a raw libc call does not participate in. So do the
    connect the way the socket module does internally: go non-blocking, expect
    ``EINPROGRESS``, wait for writability, then read ``SO_ERROR`` for the real verdict.
    A bare blocking connect would ignore ``timeout_s`` and hang for the kernel's own
    RFCOMM timeout instead, which is far longer than a print job should ever wait.
    """
    addr = sockaddr_rc(mac, channel)
    buf = ctypes.create_string_buffer(addr, SOCKADDR_RC_SIZE)
    sock.setblocking(False)
    if _libc.connect(sock.fileno(), buf, SOCKADDR_RC_SIZE) != 0:
        err = ctypes.get_errno()
        # EINTR belongs with the other two rather than in a retry: a connect interrupted
        # by a signal still completes asynchronously, and calling connect() again would
        # get EALREADY. Waiting for writability is the correct move for all three.
        if err not in (errno.EINPROGRESS, errno.EALREADY, errno.EINTR):
            raise OSError(err, os.strerror(err))
        if not select.select([], [sock], [], timeout_s)[1]:
            raise TimeoutError(
                f"connecting to {mac} on RFCOMM channel {channel} timed out after {timeout_s}s"
            )
        # Writability only means the attempt finished, not that it succeeded.
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            raise OSError(err, os.strerror(err))
    sock.settimeout(timeout_s)
