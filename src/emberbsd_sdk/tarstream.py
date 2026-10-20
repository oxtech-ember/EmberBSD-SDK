# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). Byte-preserving tar record traversal.
"""Read raw tar records without extraction or automatic PAX/sparse transforms.

The stdlib validates each fixed-size header and its numeric fields. This layer
keeps raw path, link and type bytes. Consumers own PAX merge, member-type, path,
size and payload policies. No input name is used as a filesystem path here.
"""

from dataclasses import dataclass
import tarfile

from .errors import SDKError

TAR_STREAM_API = 1
TAR_HEADER_FACTS_API = 1
CHUNK = 1024 * 1024
BLOCK = 512


def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not isinstance(chunk, bytes) or len(chunk) > size - len(data):
            raise SDKError('tar stream returned invalid bytes')
        if not chunk:
            raise SDKError('truncated tar record')
        data.extend(chunk)
    return bytes(data)


class _Payload:
    def __init__(self, stream, size):
        self.stream = stream
        self.remaining = size
        self.active = True

    def read(self, size):
        if not self.active:
            raise SDKError('tar record is no longer active')
        if type(size) is not int or not 0 <= size <= CHUNK:
            raise SDKError('tar read size must be between zero and one MiB')
        data = _read_exact(self.stream, min(size, self.remaining))
        self.remaining -= len(data)
        return data


@dataclass(frozen=True)
class TarRecord:
    name: bytes
    linkname: bytes
    typeflag: bytes
    size: int
    payload: object
    uid: int | None = None
    gid: int | None = None
    uname: bytes | None = None
    gname: bytes | None = None

    def read(self, size):
        return self.payload.read(size)


def tar_records(stream):
    """Yield raw headers with bounded payload reads; drain skipped payload.

    A zero header terminates traversal. The producer wrapper drains the rest.
    Do not retain a record for reading after advancing this iterator.
    """
    while True:
        first = stream.read(BLOCK)
        if not isinstance(first, bytes) or len(first) > BLOCK:
            raise SDKError('tar stream returned invalid header bytes')
        if not first:
            return
        header = first + _read_exact(stream, BLOCK - len(first))
        if header == bytes(BLOCK):
            return
        try:
            info = tarfile.TarInfo.frombuf(header, 'ascii', 'surrogateescape')
        except (tarfile.HeaderError, ValueError) as exc:
            raise SDKError(f'invalid tar header: {exc}') from exc
        if info.size < 0:
            raise SDKError('negative tar payload size')
        name = header[:100].split(b'\0', 1)[0]
        prefix = header[345:500].split(b'\0', 1)[0]
        kind = header[156:157]
        if prefix and kind not in tarfile.GNU_TYPES:
            name = prefix + b'/' + name
        payload = _Payload(stream, info.size)
        try:
            yield TarRecord(name, header[157:257].split(b'\0', 1)[0],
                            kind, info.size, payload, info.uid, info.gid,
                            header[265:297].split(b'\0', 1)[0],
                            header[297:329].split(b'\0', 1)[0])
            while payload.read(CHUNK):
                pass
            _read_exact(stream, (-info.size) % BLOCK)
        finally:
            payload.active = False


def pax_records(data, *, limit=CHUNK):
    """Parse byte-counted PAX values without applying or decoding them.

    Later duplicates replace earlier values. Empty values remain visible.
    Sparse keys are returned intact; a consumer can reject them before merging.
    """
    if not isinstance(data, bytes) or type(limit) is not int or limit < 0:
        raise SDKError('PAX input must be bytes with a nonnegative limit')
    if len(data) > limit:
        raise SDKError('oversized PAX header')
    values = {}
    pos = 0
    while pos < len(data):
        space = data.find(b' ', pos)
        digits = data[pos:space] if space >= 0 else b''
        if not digits or digits[:1] == b'0' or not digits.isdigit():
            raise SDKError('malformed PAX record length')
        try:
            length = int(digits)
        except ValueError as exc:
            raise SDKError('malformed PAX record length') from exc
        prefix = len(digits) + 1
        if length > len(data) - pos or length <= prefix + 2:
            raise SDKError('truncated PAX record')
        record = data[space + 1:pos + length]
        if not record.endswith(b'\n') or b'=' not in record:
            raise SDKError('malformed PAX value')
        key, value = record[:-1].split(b'=', 1)
        values[key] = value
        pos += length
    return values
