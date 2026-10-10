import http.server
import urllib.parse
import uuid


def verify(root):
    # The ledger lives in the trusted verifier, not in candidate-written files or reports.
    ledger = {}
    ledger_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            key = urllib.parse.parse_qs(parsed.query).get("key", [None])[0]
            if parsed.path != "/status" or not isinstance(key, str):
                self.send_json({"error": "INVALID"}, 400)
                return
            with ledger_lock:
                receipts = ledger.get(key, [])
                self.send_json({"receipt": receipts[-1] if receipts else None})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/commit" or not 0 < length <= 4096:
                    raise ValueError
                key = json.loads(self.rfile.read(length))["key"]
                if not isinstance(key, str) or not 0 < len(key) <= 128:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.send_json({"error": "INVALID"}, 400)
                return
            receipt = uuid.uuid4().hex
            with ledger_lock:
                ledger.setdefault(key, []).append(receipt)
            self.send_json({"receipt": receipt})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    service = f"http://127.0.0.1:{server.server_address[1]}"
    key = "resume-" + uuid.uuid4().hex
    try:
        expect(root, {"service": service, "key": key, "crash_after_commit": True}, {"error": "INTERRUPTED"})
        with ledger_lock:
            assert len(ledger.get(key, [])) == 1
            receipt = ledger[key][0]
        # Each probe starts a fresh process with no local memory/checkpoint to trust.
        expect(root, {"service": service, "key": key}, {"receipt": receipt, "reused": True})
        expect(root, {"service": service, "key": key}, {"receipt": receipt, "reused": True})
        with ledger_lock:
            assert ledger[key] == [receipt]
        new_key = "new-" + uuid.uuid4().hex
        observed = probe(root, {"service": service, "key": new_key})
        with ledger_lock:
            assert len(ledger.get(new_key, [])) == 1
            assert observed == {"receipt": ledger[new_key][0], "reused": False}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        if worker.is_alive():
            raise RuntimeError("service cleanup incomplete")


checked_main(verify)
