"""Juristi-käyttöliittymän paikallinen HTTP-palvelin (vain Pythonin standardikirjasto).

Käynnistys: python3 server.py [--port 8765] [--no-browser]
"""
import argparse
import base64
import binascii
import json
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import juristi
import storage

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 120 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Juristi/1.0"

    def log_message(self, fmt, *args):
        pass  # ei kirjata kyselyjen sisältöä tai polkuja konsoliin

    # --- apurit ---

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, status, message):
        self._json(status, {"virhe": message})

    def _host_ok(self):
        # Estää DNS rebinding -hyökkäykset: vain paikalliset Host-otsakkeet kelpaavat.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            self._error(403, "Kielletty")
            return False
        return True

    # --- reitit ---

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        path = url.path

        if path in ("/", "/index.html"):
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")

        if path == "/api/queries":
            params = parse_qs(url.query)
            return self._json(200, storage.list_queries(
                params.get("q", [""])[0], params.get("sort", ["desc"])[0]))

        m = re.fullmatch(r"/api/queries/([\w-]+)", path)
        if m:
            item = storage.get_query(m.group(1))
            return self._json(200, item) if item else self._error(404, "Kyselyä ei löydy")

        m = re.fullmatch(r"/api/queries/([\w-]+)/files/(.+)", path)
        if m:
            name = unquote(m.group(2))
            fp = storage.file_path(m.group(1), name)
            if not fp or not fp.is_file():
                return self._error(404, "Tiedostoa ei löydy")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            return self._send(200, fp.read_bytes(), ctype, {
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
                "Content-Security-Policy": "sandbox",
            })

        self._error(404, "Ei löydy")

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return
        if urlparse(self.path).path != "/api/queries":
            return self._error(404, "Ei löydy")
        # Vaaditaan JSON: selain ei lähetä sitä toiselta sivustolta ilman CORS-esitarkistusta.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Pyynnön on oltava JSON-muotoinen")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._error(413, "Pyyntö on liian suuri")
        try:
            payload = json.loads(self.rfile.read(length))
            query = str(payload.get("query", "")).strip()
            files = []
            for f in payload.get("files", []):
                name = storage.safe_filename(str(f["name"]))
                if os.path.splitext(name)[1].lower() not in juristi.SUPPORTED_EXTENSIONS:
                    return self._error(400, f"Tiedostotyyppiä ei tueta: {name}")
                data = base64.b64decode(f["data"], validate=True)
                if len(data) > MAX_FILE_BYTES:
                    return self._error(413, f"Tiedosto on liian suuri: {name}")
                files.append((name, data))
        except (ValueError, KeyError, TypeError, binascii.Error):
            return self._error(400, "Virheellinen pyyntö")
        if not query:
            return self._error(400, "Kirjoita kysymys ennen neuvon pyytämistä")

        query_id = storage.create_query(query, files)
        juristi.start(query_id)
        self._json(201, {"id": query_id})

    def do_DELETE(self):
        if not self._host_ok():
            return
        m = re.fullmatch(r"/api/queries/([\w-]+)", urlparse(self.path).path)
        if not m:
            return self._error(404, "Ei löydy")
        item = storage.get_query(m.group(1))
        if not item:
            return self._error(404, "Kyselyä ei löydy")
        if item["status"] == "running":
            return self._error(409, "Käsittelyssä olevaa kyselyä ei voi poistaa")
        storage.delete_query(item["id"])
        self._json(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(description="Juristi – paikallinen käyttöliittymä")
    parser.add_argument("--port", type=int, default=int(os.environ.get("JURISTI_PORT", 8765)))
    parser.add_argument("--no-browser", action="store_true", help="älä avaa selainta automaattisesti")
    args = parser.parse_args()

    storage.init()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Juristi käynnissä: {url}  (tallennus: {storage.DATA_DIR})  – lopetus Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
