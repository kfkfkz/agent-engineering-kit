"""Shared streaming PNG validation for screenshot evidence and static previews."""
from __future__ import annotations

import struct
from typing import Callable
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_PIXELS = 100_000_000
MAX_ROW_BYTES = 16 * 1024 * 1024


class PngValidationError(ValueError):
    pass


def fail(message: str) -> None:
    raise PngValidationError(message)


def validate_png_reader(read: Callable[[int], bytes], file_size: int, *,
                        max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    """Validate a PNG stream with bounded chunk/scanline buffers; caller owns reader."""
    if file_size > MAX_FILE_BYTES:
        fail("PNG exceeds 200 MiB")

    def read_exact(length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            block = read(min(64 * 1024, length - len(output)))
            if not block:
                fail("truncated PNG")
            output.extend(block)
        return bytes(output)

    if read_exact(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        fail("not a PNG signature")
    consumed = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int, int] | None = None
    seen_idat = False
    ended_idat = False
    seen_iend = False
    palette_entries = 0
    transparency = False
    decompressor: zlib.decompressobj | None = None
    row_size = row_fill = rows = channels = 0
    color = depth = 0

    def accept_output(piece: bytes) -> None:
        nonlocal row_fill, rows
        offset = 0
        while offset < len(piece):
            need = row_size + 1 - row_fill
            count = min(need, len(piece) - offset)
            if row_fill == 0 and piece[offset] not in {0, 1, 2, 3, 4}:
                fail("invalid PNG scanline filter")
            row_fill += count
            offset += count
            if row_fill == row_size + 1:
                rows += 1
                if ihdr is None or rows > ihdr[1]:
                    fail("PNG decompressed stream exceeds declared image")
                row_fill = 0

    def inflate(block: bytes) -> None:
        assert decompressor is not None
        if decompressor.eof and block:
            fail("data follows PNG zlib stream")
        pending = block
        while True:
            limit = row_size + 1 - row_fill
            try:
                piece = decompressor.decompress(pending, limit)
            except zlib.error as exc:
                fail(f"PNG zlib stream invalid: {exc}")
            pending = decompressor.unconsumed_tail
            accept_output(piece)
            if decompressor.eof and (pending or decompressor.unused_data):
                fail("data follows PNG zlib stream")
            if decompressor.eof:
                break
            if pending:
                continue
            if len(piece) == limit:
                # Drain any output already buffered by zlib, still bounded to one row.
                pending = b""
                block = b""
                continue
            break

    while consumed < file_size:
        if file_size - consumed < 12:
            fail("truncated PNG chunk")
        header = read_exact(8)
        consumed += 8
        size = struct.unpack_from(">I", header, 0)[0]
        kind = header[4:8]
        if size + 4 > file_size - consumed:
            fail("truncated PNG chunk payload")
        if ihdr is None and kind != b"IHDR":
            fail("IHDR must be the first PNG chunk")
        crc = zlib.crc32(kind)
        if kind == b"IHDR":
            if ihdr is not None or size != 13:
                fail("duplicate or invalid IHDR")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            combos = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (width < 1 or height < 1 or width * height > max_pixels
                    or color not in combos or depth not in combos[color]
                    or compression != 0 or filtering != 0 or interlace != 0):
                fail("unsupported or unsafe IHDR dimensions/format")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            row_size = (width * channels * depth + 7) // 8
            if row_size > MAX_ROW_BYTES:
                fail("PNG row exceeds 16 MiB")
            ihdr = (width, height, depth, color, interlace)
            decompressor = zlib.decompressobj()
        elif kind == b"PLTE":
            if seen_idat or palette_entries or color in {0, 4} or size == 0 or size % 3 or size > 768:
                fail("invalid PLTE placement or length")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            palette_entries = size // 3
            if color == 3 and palette_entries > (1 << depth):
                fail("PLTE contains more entries than indexed bit depth allows")
        elif kind == b"tRNS":
            if seen_idat or transparency or color not in {0, 2, 3}:
                fail("invalid tRNS placement or color type")
            if ((color == 0 and size != 2) or (color == 2 and size != 6)
                    or (color == 3 and (not palette_entries or size > palette_entries))):
                fail("invalid tRNS length")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            transparency = True
        elif kind == b"IDAT":
            if ended_idat or (color == 3 and not palette_entries):
                fail("invalid IDAT order or missing indexed palette")
            seen_idat = True
            remaining = size
            while remaining:
                block = read_exact(min(64 * 1024, remaining))
                remaining -= len(block)
                crc = zlib.crc32(block, crc)
                inflate(block)
        else:
            if seen_idat:
                ended_idat = True
            if kind == b"IEND":
                if size != 0 or not seen_idat or seen_iend:
                    fail("invalid IEND")
            elif kind[:1].isupper():
                fail("unknown critical PNG chunk")
            remaining = size
            while remaining:
                block = read_exact(min(64 * 1024, remaining))
                remaining -= len(block)
                crc = zlib.crc32(block, crc)
        consumed += size
        expected_crc = struct.unpack(">I", read_exact(4))[0]
        consumed += 4
        if crc & 0xFFFFFFFF != expected_crc:
            fail("PNG chunk CRC mismatch")
        if kind == b"IEND":
            seen_iend = True
            if consumed != file_size or read(1):
                fail("trailing bytes after IEND")
            break
    if ihdr is None or not seen_idat or not seen_iend or decompressor is None:
        fail("PNG is missing a required chunk")
    if color == 3 and not palette_entries:
        fail("indexed PNG is missing PLTE")
    if row_fill != 0 or rows != ihdr[1] or not decompressor.eof or decompressor.unused_data:
        fail("PNG zlib/scanline stream is incomplete or has trailing data")
    return ihdr[0], ihdr[1]
