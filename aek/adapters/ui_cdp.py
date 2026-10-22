"""Bounded private CDP pipes, not a browser launcher or an execution grant."""
from __future__ import annotations

import json
import math
import os
import selectors
import stat
import time

MAX_PROTOCOL_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 2000
METHODS = frozenset({
    'Browser.getVersion', 'Target.getTargets', 'Target.attachToTarget',
    'Page.enable', 'Emulation.setScriptExecutionDisabled', 'Network.enable',
    'Network.setBlockedURLs', 'Emulation.setDeviceMetricsOverride',
    'Emulation.setEmulatedMedia', 'Page.getFrameTree', 'Page.setDocumentContent',
    'DOM.getDocument', 'Page.getLayoutMetrics', 'Page.captureScreenshot', 'Browser.close',
})


class CdpError(ValueError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CdpError('CDP_PROTOCOL_ERROR')
        result[key] = value
    return result


def _constant(_value):
    raise CdpError('CDP_PROTOCOL_ERROR')


class CdpPipe:
    """Own both supplied descriptors, including on initialization failure.

    A provider must create these private pipes under its own approved process
    scope. No TCP, path, process launch or runtime adoption happens here.
    """
    def __init__(self, read_fd, write_fd, *, timeout_seconds=35,
                 max_bytes=MAX_PROTOCOL_BYTES, max_events=MAX_EVENTS):
        self._read, self._write, self._selector = read_fd, write_fd, None
        self._closed = False
        self._pending = bytearray()
        self._sequence = 0
        self.events_seen = 0
        self.received_bytes = self.sent_bytes = 0
        try:
            if (os.name != 'posix' or type(read_fd) is not int or type(write_fd) is not int
                    or read_fd < 0 or write_fd < 0 or read_fd == write_fd
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 35
                    or type(max_bytes) is not int or not 64 <= max_bytes <= MAX_PROTOCOL_BYTES
                    or type(max_events) is not int or not 0 <= max_events <= MAX_EVENTS):
                raise CdpError('CDP_UNAVAILABLE')
            if not all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (read_fd, write_fd)):
                raise CdpError('CDP_UNAVAILABLE')
            self._max_bytes, self._max_events = max_bytes, max_events
            self._deadline = time.monotonic() + timeout_seconds
            os.set_blocking(read_fd, False)
            os.set_blocking(write_fd, False)
            self._selector = selectors.DefaultSelector()
            self._selector.register(read_fd, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._selector is not None:
            self._selector.close()
        for fd in {value for value in (self._read, self._write) if type(value) is int and value >= 0}:
            try:
                os.close(fd)
            except OSError:
                pass
        self._pending.clear()

    def rpc(self, method, params=None, session=None):
        if self._closed:
            raise CdpError('CDP_CLOSED')
        try:
            if (type(method) is not str or method not in METHODS
                    or (params is not None and type(params) is not dict)
                    or (session is not None and (type(session) is not str or not 1 <= len(session) <= 256))):
                raise CdpError('CDP_COMMAND_REJECTED')
            self._sequence += 1
            request = {'id': self._sequence, 'method': method, 'params': {} if params is None else params}
            if session is not None:
                request['sessionId'] = session
            raw = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\0'
            if len(raw) + self.sent_bytes > self._max_bytes:
                raise CdpError('CDP_LIMIT_EXCEEDED')
            offset, response = 0, None
            self._selector.register(self._write, selectors.EVENT_WRITE)
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpError('CDP_TIMEOUT')
                for key, _mask in self._selector.select(remaining):
                    if key.fd == self._write:
                        try:
                            written = os.write(self._write, raw[offset:offset + 65536])
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise CdpError('CDP_PROTOCOL_ERROR')
                        offset += written
                        self.sent_bytes += written
                        if offset == len(raw):
                            self._selector.unregister(self._write)
                        continue
                    try:
                        chunk = os.read(self._read, min(65536, self._max_bytes - self.received_bytes + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise CdpError('CDP_PIPE_CLOSED')
                    self.received_bytes += len(chunk)
                    self._pending.extend(chunk)
                    if self.received_bytes > self._max_bytes:
                        raise CdpError('CDP_LIMIT_EXCEEDED')
                    while b'\0' in self._pending:
                        boundary = self._pending.index(0)
                        frame = bytes(self._pending[:boundary])
                        del self._pending[:boundary + 1]
                        message = json.loads(frame.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
                        if type(message) is not dict:
                            raise CdpError('CDP_PROTOCOL_ERROR')
                        if 'id' in message:
                            if (type(message['id']) is not int or message['id'] != self._sequence
                                    or message.get('sessionId') != session or response is not None
                                    or offset != len(raw) or 'error' in message
                                    or set(message) - {'id', 'result', 'sessionId'}
                                    or type(message.get('result')) is not dict):
                                raise CdpError('CDP_PROTOCOL_ERROR')
                            response = message['result']
                        else:
                            if (type(message.get('method')) is not str
                                    or type(message.get('params', {})) is not dict
                                    or set(message) - {'method', 'params', 'sessionId'}):
                                raise CdpError('CDP_PROTOCOL_ERROR')
                            self.events_seen += 1
                            if self.events_seen > self._max_events:
                                raise CdpError('CDP_LIMIT_EXCEEDED')
                if response is not None:
                    if time.monotonic() >= self._deadline:
                        raise CdpError('CDP_TIMEOUT')
                    return response
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            self.close()
            if isinstance(exc, CdpError):
                raise
            raise CdpError('CDP_PROTOCOL_ERROR') from None
