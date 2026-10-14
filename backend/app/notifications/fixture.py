"""Local-only SMTP capture for repeatable development gates; never prints messages."""

import argparse
import os
import socketserver
from pathlib import Path
from threading import Lock
from uuid import uuid4


class CaptureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Fixture socket failures must not print raw message context.
        pass

    def __init__(self, port=0, *, output_dir=None, reject_recipient=None):
        self.messages = []
        self.capture_lock = Lock()
        self.output_dir = Path(output_dir) if output_dir else None
        self.reject_recipient = reject_recipient
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(("127.0.0.1", port), CaptureHandler)


class CaptureHandler(socketserver.StreamRequestHandler):
    server: CaptureServer

    def reply(self, value):
        self.wfile.write(value + b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.request.settimeout(20)
        self.reply(b"220 loopback SMTP fixture")
        recipient = ""
        while line := self.rfile.readline(65537):
            if len(line) > 65536:
                return
            command = line.decode("ascii", "replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in {"EHLO", "HELO"}:
                self.reply(b"250 loopback fixture")
            elif verb == "MAIL":
                self.reply(b"250 sender accepted")
            elif verb == "RCPT":
                recipient = command.partition(":")[2].strip().strip("<>")
                self.reply(
                    b"550 fixture recipient rejected"
                    if recipient == self.server.reject_recipient
                    else b"250 recipient accepted"
                )
            elif verb == "DATA":
                self.reply(b"354 send message")
                content = bytearray()
                while data := self.rfile.readline(65537):
                    if data == b".\r\n":
                        break
                    content.extend(data[1:] if data.startswith(b"..") else data)
                    if len(content) > 10 * 1024 * 1024:
                        self.reply(b"552 fixture size limit")
                        return
                raw = bytes(content)
                with self.server.capture_lock:
                    self.server.messages.append((recipient, raw))
                    if self.server.output_dir:
                        destination = self.server.output_dir / f"{uuid4()}.eml"
                        descriptor = os.open(
                            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(raw)
                self.reply(b"250 message captured")
            elif verb == "QUIT":
                self.reply(b"221 goodbye")
                return
            elif verb in {"RSET", "NOOP"}:
                self.reply(b"250 OK")
            else:
                self.reply(b"502 unsupported fixture command")


def main():
    parser = argparse.ArgumentParser(description="Loopback-only SMTP development fixture")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reject-recipient")
    args = parser.parse_args()
    with CaptureServer(
        args.port, output_dir=args.output_dir, reject_recipient=args.reject_recipient
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
