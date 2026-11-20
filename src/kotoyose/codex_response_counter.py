"""Count only provider Responses SSE completion evidence, without retaining content."""

import hashlib

import h11

from .codex_request_policy import parse_json


class ResponseCounter:
    def __init__(self):
        self.ids = set()
        self.requests = 0
        self.pending = 0
        self.uncertain = False

    def begin(self):
        self.requests += 1
        self.pending += 1
        return _ResponseEvidence(self)

    def value(self):
        if not self.requests or self.pending or self.uncertain:
            return None
        return len(self.ids)


class _ResponseEvidence:
    """A single upstream request; failure to parse evidence never affects forwarding."""
    def __init__(self, counter):
        self.counter = counter
        self.parser = h11.Connection(h11.CLIENT, max_incomplete_event_size=65536)
        self.parser.send(h11.Request(method='POST', target='/', headers=[('host', 'provider')]))
        self.parser.send(h11.EndOfMessage())
        self.status = None
        self.buffer = bytearray()
        self.terminal = False
        self.completed_id = None
        self.invalid = False
        self.finished = False

    def feed_http(self, data):
        if self.invalid or self.finished:
            return
        try:
            self.parser.receive_data(data)
            while True:
                event = self.parser.next_event()
                if event in (h11.NEED_DATA, h11.PAUSED):
                    break
                if isinstance(event, h11.Response):
                    self.status = event.status_code
                elif isinstance(event, h11.Data):
                    self.feed_body(event.data)
                elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                    break
        except Exception:
            self.invalid = True

    def feed_body(self, data):
        try:
            self._feed_body(data)
        except Exception:
            self.invalid = True
            self.buffer.clear()

    def _feed_body(self, data):
        if self.invalid or self.status != 200:
            return
        self.buffer.extend(data)
        if len(self.buffer) > 2 * 1024 * 1024:
            self.invalid = True
            self.buffer.clear()
            return
        self.buffer[:] = self.buffer.replace(b'\r\n', b'\n')
        while b'\n\n' in self.buffer:
            record, _, remaining = self.buffer.partition(b'\n\n')
            self.buffer[:] = remaining
            lines = record.split(b'\n')
            data_lines = [line[5:].lstrip(b' ') for line in lines if line.startswith(b'data:')]
            event_lines = [line[6:].lstrip(b' ') for line in lines if line.startswith(b'event:')]
            if len(event_lines) > 1:
                self.invalid = True
                return
            if not data_lines:
                continue
            data = b'\n'.join(data_lines)
            if data == b'[DONE]':
                continue
            try:
                value = parse_json(data)
                if (not isinstance(value, dict) or not isinstance(value.get('type'), str)
                        or event_lines and event_lines[0] != value['type'].encode('ascii')):
                    raise ValueError
                if value['type'] == 'response.completed':
                    response = value.get('response')
                    if not isinstance(response, dict) or response.get('status') != 'completed' or not isinstance(response.get('id'), str) or not response['id']:
                        raise ValueError
                    response_id = hashlib.sha256(response['id'].encode()).digest()
                    if self.terminal and self.completed_id != response_id:
                        raise ValueError
                    self.completed_id = response_id
                    self.counter.ids.add(response_id)
                    self.terminal = True
                elif value['type'] in {'response.failed', 'response.incomplete'}:
                    if self.terminal and self.completed_id is not None:
                        raise ValueError
                    self.terminal = True
            except (ValueError, UnicodeError, TypeError):
                self.invalid = True
                self.buffer.clear()
                return

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self.counter.pending -= 1
        if (self.invalid or self.status is None or self.status == 200 and not self.terminal
                or self.status not in (200, 429)):
            self.counter.uncertain = True
        self.buffer.clear()
