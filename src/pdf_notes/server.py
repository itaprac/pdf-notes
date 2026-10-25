"""Local web app for marking up PDFs and collecting notes.

The server keeps a small library of documents. A document is either an
uploaded PDF (copied into the data directory) or a PDF on disk that is
served live, so a document rebuilt by LaTeX shows up after a reload.
Notes are stored as JSON with a Markdown copy next to it, so that a person
or an AI agent can read them without the app.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 500 * 1024 * 1024
STATUSES = ("open", "done", "rejected")
NOTE_FIELDS = ("kind", "page", "top", "section", "quote", "prefix", "suffix", "rects", "note")
EDITABLE = ("note", "status")


def now():
    return datetime.now().isoformat(timespec="seconds")


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render_markdown(doc, notes):
    """Notes as Markdown, ordered as they appear in the document."""
    out = [f"# Notes: {doc['name']}", "",
           "Generated from the notes JSON file; do not edit by hand.", ""]
    key = lambda n: (n.get("page") or 10**9, n.get("top") or 0, n.get("created", ""))
    for i, n in enumerate(sorted(notes, key=key), 1):
        where = f"p. {n['page']}" if n.get("page") else "general"
        if n.get("section"):
            where += f" · {n['section']}"
        out += [f"## {i}. {where} · {n['status']} · `{n['id']}`", ""]
        if n.get("quote"):
            out += ["> " + n["quote"].replace("\n", " "), ""]
            if n.get("prefix") or n.get("suffix"):
                ctx = f"…{n.get('prefix', '')}**[{n['quote']}]**{n.get('suffix', '')}…"
                out += [f"Context: {ctx}", ""]
        elif n.get("kind") == "area":
            out += ["*(marked area of the page)*", ""]
        elif n.get("kind") == "page":
            out += ["*(whole page)*", ""]
        out += [n.get("note", "").strip() or "*(no text)*", ""]
        out += [f"*{n['created']} · PDF {n.get('pdfVersion', '?')}*", ""]
    return "\n".join(out)


class Library:
    """Documents and their notes, stored under one data directory."""

    def __init__(self, data_dir):
        self.data = Path(data_dir).expanduser().resolve()
        self.index = self.data / "library.json"
        self.lock = threading.RLock()
        self._versions = {}

    # ---- library ----
    def docs(self):
        if not self.index.exists():
            return []
        return json.loads(self.index.read_text(encoding="utf-8"))

    def _save_docs(self, docs):
        write_atomic(self.index, json.dumps(docs, ensure_ascii=False, indent=2) + "\n")

    def get(self, doc_id):
        return next((d for d in self.docs() if d["id"] == doc_id), None)

    def doc_dir(self, doc):
        return self.data / "docs" / doc["id"]

    def pdf_path(self, doc):
        if doc["kind"] == "path":
            return Path(doc["path"])
        return self.doc_dir(doc) / "document.pdf"

    def notes_path(self, doc):
        if doc.get("notes"):
            return Path(doc["notes"])
        return self.doc_dir(doc) / "notes.json"

    def snapshots_dir(self, doc):
        if doc.get("snapshots"):
            return Path(doc["snapshots"])
        return self.doc_dir(doc) / "snapshots"

    def version(self, doc):
        pdf = self.pdf_path(doc)
        st = pdf.stat()
        cached = self._versions.get(doc["id"])
        if cached and cached[0] == (st.st_mtime_ns, st.st_size):
            return cached[1]
        v = file_hash(pdf)
        self._versions[doc["id"]] = ((st.st_mtime_ns, st.st_size), v)
        return v

    def describe(self, doc):
        pdf = self.pdf_path(doc)
        info = {k: doc.get(k) for k in ("id", "name", "kind", "added")}
        info["exists"] = pdf.exists()
        if doc["kind"] == "path":
            info["path"] = doc["path"]
        if info["exists"]:
            info["version"] = self.version(doc)
            info["modified"] = datetime.fromtimestamp(pdf.stat().st_mtime).isoformat(timespec="minutes")
        notes = self.notes(doc)
        info["notes"] = len(notes)
        info["open"] = sum(1 for n in notes if n["status"] == "open")
        return info

    def add_upload(self, name, data):
        with self.lock:
            doc = {"id": uuid.uuid4().hex[:10], "name": name, "kind": "upload", "added": now()}
            d = self.doc_dir(doc)
            d.mkdir(parents=True, exist_ok=True)
            (d / "document.pdf").write_bytes(data)
            self._save_docs(self.docs() + [doc])
            return doc

    def add_path(self, pdf, notes=None, snapshots=None, name=None):
        """Register a PDF on disk. The id depends on the path, so notes survive restarts."""
        pdf = Path(pdf).expanduser().resolve()
        doc_id = "p" + hashlib.sha256(str(pdf).encode()).hexdigest()[:9]
        with self.lock:
            docs = [d for d in self.docs() if d["id"] != doc_id]
            old = self.get(doc_id) or {}
            doc = {"id": doc_id, "name": name or old.get("name") or pdf.name, "kind": "path",
                   "path": str(pdf), "added": old.get("added", now())}
            if notes:
                doc["notes"] = str(Path(notes).expanduser().resolve())
            if snapshots:
                doc["snapshots"] = str(Path(snapshots).expanduser().resolve())
            self._save_docs(docs + [doc])
            return doc

    def remove(self, doc_id):
        """Forget a document. Uploaded files and their notes are deleted;
        a PDF on disk and notes stored outside the data directory are kept."""
        with self.lock:
            doc = self.get(doc_id)
            if not doc:
                return False
            self._save_docs([d for d in self.docs() if d["id"] != doc_id])
            shutil.rmtree(self.doc_dir(doc), ignore_errors=True)
            return True

    # ---- notes ----
    def notes(self, doc):
        p = self.notes_path(doc)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_notes(self, doc, notes):
        p = self.notes_path(doc)
        write_atomic(p, json.dumps(notes, ensure_ascii=False, indent=2) + "\n")
        write_atomic(p.with_suffix(".md"), render_markdown(doc, notes))

    def add_note(self, doc, body):
        version = self.version(doc)
        note = {"id": uuid.uuid4().hex[:8], "created": now(), "pdfVersion": version, "status": "open"}
        note.update({k: body.get(k) for k in NOTE_FIELDS})
        note["kind"] = note["kind"] or "text"
        for k in ("section", "quote", "prefix", "suffix", "note"):
            note[k] = note[k] or ""
        note["rects"] = note["rects"] or []
        with self.lock:
            self._save_notes(doc, self.notes(doc) + [note])
            if doc["kind"] == "path":
                # Keep the exact PDF the note refers to; the live file will change.
                snap = self.snapshots_dir(doc) / f"{version}.pdf"
                if not snap.exists():
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.pdf_path(doc), snap)
        return note

    def update_note(self, doc, note_id, body):
        with self.lock:
            notes = self.notes(doc)
            for n in notes:
                if n["id"] == note_id:
                    n.update({k: body[k] for k in EDITABLE if k in body})
                    n["updated"] = now()
                    self._save_notes(doc, notes)
                    return n
        return None

    def delete_note(self, doc, note_id):
        with self.lock:
            notes = self.notes(doc)
            rest = [n for n in notes if n["id"] != note_id]
            if len(rest) == len(notes):
                return False
            self._save_notes(doc, rest)
            return True


def make_handler(lib):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pdf-notes/1"

        def log_message(self, fmt, *args):
            pass

        # ---- helpers ----
        def send_bytes(self, data, ctype, status=HTTPStatus.OK, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, obj, status=HTTPStatus.OK):
            self.send_bytes(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def fail(self, status, message):
            self.send_json({"error": message}, status)

        def body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def route(self):
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            return url, parts

        def doc_or_404(self, doc_id):
            doc = lib.get(doc_id)
            if not doc:
                self.fail(HTTPStatus.NOT_FOUND, "no such document")
            return doc

        # ---- GET ----
        def do_GET(self):
            url, parts = self.route()
            if not parts:
                return self.static("index.html")
            if parts[0] == "static":
                return self.static("/".join(parts[1:]))
            if parts[:2] == ["api", "docs"]:
                if len(parts) == 2:
                    with lib.lock:
                        return self.send_json([lib.describe(d) for d in lib.docs()])
                doc = self.doc_or_404(parts[2])
                if not doc:
                    return
                if len(parts) == 3:
                    return self.send_json(lib.describe(doc))
                if parts[3] == "pdf":
                    pdf = lib.pdf_path(doc)
                    if not pdf.exists():
                        return self.fail(HTTPStatus.NOT_FOUND, "PDF file is missing")
                    return self.send_bytes(pdf.read_bytes(), "application/pdf")
                if parts[3] == "notes" and len(parts) == 4:
                    return self.send_json(lib.notes(doc))
                if parts[3] == "export.md":
                    md = render_markdown(doc, lib.notes(doc)).encode()
                    name = Path(doc["name"]).stem + "-notes.md"
                    disp = "attachment; filename*=UTF-8''" + quote(name, safe="") if "download" in parse_qs(url.query) else "inline"
                    return self.send_bytes(md, "text/markdown; charset=utf-8", extra={"Content-Disposition": disp})
            self.fail(HTTPStatus.NOT_FOUND, "not found")

        def static(self, rel):
            target = (STATIC / rel).resolve()
            if STATIC != target and STATIC not in target.parents or not target.is_file():
                return self.fail(HTTPStatus.NOT_FOUND, "not found")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".mjs":
                ctype = "text/javascript"
            if target.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            self.send_bytes(target.read_bytes(), ctype)

        # ---- POST ----
        def do_POST(self):
            url, parts = self.route()
            if parts == ["api", "docs"]:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_UPLOAD:
                    return self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file too large")
                data = self.rfile.read(n)
                if not data.startswith(b"%PDF"):
                    return self.fail(HTTPStatus.BAD_REQUEST, "not a PDF file")
                name = (parse_qs(url.query).get("name") or ["document.pdf"])[0]
                name = Path(name).name or "document.pdf"
                doc = lib.add_upload(name, data)
                return self.send_json(lib.describe(doc), HTTPStatus.CREATED)
            if len(parts) == 4 and parts[:2] == ["api", "docs"] and parts[3] == "notes":
                doc = self.doc_or_404(parts[2])
                if not doc:
                    return
                body = self.body_json()
                if body is None:
                    return self.fail(HTTPStatus.BAD_REQUEST, "invalid JSON")
                return self.send_json(lib.add_note(doc, body), HTTPStatus.CREATED)
            self.fail(HTTPStatus.NOT_FOUND, "not found")

        # ---- PUT ----
        def do_PUT(self):
            _, parts = self.route()
            if len(parts) == 5 and parts[:2] == ["api", "docs"] and parts[3] == "notes":
                doc = self.doc_or_404(parts[2])
                if not doc:
                    return
                body = self.body_json()
                if body is None or body.get("status", "open") not in STATUSES:
                    return self.fail(HTTPStatus.BAD_REQUEST, f"status must be one of {', '.join(STATUSES)}")
                note = lib.update_note(doc, parts[4], body)
                return self.send_json(note) if note else self.fail(HTTPStatus.NOT_FOUND, "no such note")
            self.fail(HTTPStatus.NOT_FOUND, "not found")

        # ---- DELETE ----
        def do_DELETE(self):
            _, parts = self.route()
            if len(parts) == 3 and parts[:2] == ["api", "docs"]:
                return self.send_json({"ok": True}) if lib.remove(parts[2]) else self.fail(HTTPStatus.NOT_FOUND, "no such document")
            if len(parts) == 5 and parts[:2] == ["api", "docs"] and parts[3] == "notes":
                doc = self.doc_or_404(parts[2])
                if not doc:
                    return
                ok = lib.delete_note(doc, parts[4])
                return self.send_json({"ok": True}) if ok else self.fail(HTTPStatus.NOT_FOUND, "no such note")
            self.fail(HTTPStatus.NOT_FOUND, "not found")

    return Handler


def default_data_dir():
    if os.environ.get("PDF_NOTES_HOME"):
        return Path(os.environ["PDF_NOTES_HOME"])
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "pdf-notes"


def make_server(lib, host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), make_handler(lib))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pdf-notes",
        description="Open PDFs in the browser, mark passages and collect notes.")
    ap.add_argument("pdf", nargs="*", help="PDF files on disk to open; they are served live, not copied")
    ap.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    ap.add_argument("--data", type=Path, default=default_data_dir(),
                    help="data directory (default: $PDF_NOTES_HOME or ~/.local/share/pdf-notes)")
    ap.add_argument("--notes", help="notes JSON file for the single PDF given (default: inside the data directory)")
    ap.add_argument("--snapshots", help="directory for copies of the PDF that notes refer to (single PDF only)")
    ap.add_argument("--name", help="display name for the single PDF given")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = ap.parse_args(argv)

    if (args.notes or args.snapshots or args.name) and len(args.pdf) != 1:
        ap.error("--notes, --snapshots and --name need exactly one PDF")
    lib = Library(args.data)
    opened = []
    for pdf in args.pdf:
        if not Path(pdf).is_file():
            ap.error(f"no such file: {pdf}")
        opened.append(lib.add_path(pdf, args.notes, args.snapshots, args.name))

    try:
        srv = make_server(lib, args.host, args.port)
    except OSError as e:
        sys.exit(f"pdf-notes: cannot listen on {args.host}:{args.port}: {e.strerror}")
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"
    if len(opened) == 1:
        url += f"?doc={opened[0]['id']}"
    print(f"pdf-notes: {url}  (data: {lib.data})", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("pdf-notes: warning: there is no authentication; anyone who can reach this address can read and edit notes",
              file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
