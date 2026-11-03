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
import re
import shlex
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
EDITABLE = ("note", "status", "reply")
MAX_REPLY = 4000
# An origin the browser reports for itself, e.g. "https://host.example:8443".
ORIGIN = re.compile(r"https?://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:\d+)?/?")


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


def note_order(n):
    return (n.get("page") or 10**9, n.get("top") or 0, n.get("created", ""))


def render_markdown(doc, notes):
    """Notes as Markdown, ordered as they appear in the document."""
    out = [f"# Notes: {doc['name']}", "",
           "Generated from the notes JSON file; do not edit by hand.", ""]
    for i, n in enumerate(sorted(notes, key=note_order), 1):
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
        if n.get("reply"):
            out += ["Reply: " + " ".join(n["reply"].split()), ""]
        out += [f"*{n['created']} · PDF {n.get('pdfVersion', '?')}*", ""]
    return "\n".join(out)


def _span(a, b):
    return f"{a * 100:.0f}–{b * 100:.0f} %"


def _agent_note(n, no, ctx, with_status):
    """One note of the agent export: what it is about, where to find it, and the comment."""
    where = f"p. {n['page']}" if n.get("page") else "whole document"
    if n.get("section"):
        where += f" · {n['section']}"
    if with_status:
        where += f" · {n['status']}"
    out = [f"### {no}. {where} · `{n['id']}`", ""]

    version = n.get("pdfVersion")
    old = bool(version and ctx["version"] and version != ctx["version"])
    pdf = ctx["snapshots"].get(version) if old else ctx["pdf"]
    facts = []
    kind = n.get("kind")
    if kind == "area":
        for r in n.get("rects") or []:
            fact = (f"Marked area on p. {r['p']}, x {_span(r['x'], r['x'] + r['w'])}, y {_span(r['y'], r['y'] + r['h'])} "
                    "of the page (from the top-left): probably a figure, table or listing. Look at it before you act")
            if pdf:
                out_png = f"/tmp/note-{n['id']}"
                fact += (f": `pdftoppm -f {r['p']} -l {r['p']} -r 100 -singlefile -png {shlex.quote(pdf)} {out_png}` "
                         f"writes `{out_png}.png`")
            facts.append(fact + ".")
    elif kind == "page":
        facts.append(f"About the whole of p. {n.get('page')}.")
    elif kind == "general":
        facts.append("About the whole document, not a particular passage.")
    pages = sorted({r.get("p") for r in n.get("rects") or [] if r.get("p")})
    if n.get("quote") and len(pages) > 1:
        facts.append(f"The selection crosses a page break (pp. {pages[0]}–{pages[-1]}): the quote may contain a running "
                     "header, footer or page number; leave those out when you search the source.")
    if old:
        facts.append(f"Made on the older build `{version}`; page numbers and positions refer to "
                     + ("its snapshot (see Files)." if pdf else "that build, of which no snapshot was kept."))
    out += [f"- {f}" for f in facts] + ([""] if facts else [])

    q = " ".join(n.get("quote", "").split())
    if q:
        out += ["Quote:", "", "> " + q, ""]
        if (n.get("prefix") or n.get("suffix")) and len(q) < 160:
            out += [f"Context: …{n.get('prefix', '')}**[{q}]**{n.get('suffix', '')}…", ""]
    text = n.get("note", "").strip()
    out += ["Comment:", ""] + (_blockquote(text) if text else ["*(no text: the marking is the note)*"]) + [""]
    if n.get("reply"):
        out += ["Earlier reply:", ""] + _blockquote(n["reply"]) + [""]
    return out


def _blockquote(text):
    return ["> " + line if line.strip() else ">" for line in text.strip().splitlines()]


def render_agent_markdown(doc, notes, ctx, status="open"):
    """Notes as a hand-off to an AI coding agent.

    Unlike `render_markdown`, which is meant for reading, this states where the
    files are, which PDF build each note refers to, what the agent should do,
    and how to report back through the API. `ctx` comes from
    `Library.agent_context` plus `api` (base URL of this server) and
    `browser` (the URL as opened in the browser, if different).
    """
    ordered = sorted(notes, key=note_order)
    number = {n["id"]: i for i, n in enumerate(ordered, 1)}
    shown = [n for n in ordered if status == "all" or n["status"] == status]
    counts = ", ".join(f"{sum(1 for n in notes if n['status'] == s)} {s}" for s in STATUSES)
    which = "all of them" if status == "all" else f"the {len(shown)} {status} ones"
    api = ctx["api"].rstrip("/")
    doc_api = f"{api}/api/docs/{quote(doc['id'], safe='')}"

    out = [f"# Review notes for an AI agent: {doc['name']}", "",
           "The author read the PDF in pdf-notes, marked passages and wrote what should change.",
           f"{len(notes)} notes: {counts}. Below are {which}, in reading order; "
           "the number is the one shown in the app.", "",
           "## Files", ""]
    if doc["kind"] == "path":
        out.append(f"- PDF: `{ctx['pdf']}`, current build `{ctx['version'] or '?'}`. "
                   "It is built from the source and changes when the source is rebuilt.")
    else:
        out.append(f"- PDF: `{ctx['pdf']}` (an uploaded copy; it does not change), version `{ctx['version'] or '?'}`.")
    copy = f" (Markdown copy: `{ctx['notes_md']}`)" if Path(ctx["notes_md"]).exists() else ""
    out.append(f"- Notes: `{ctx['notes']}`{copy}. The server owns these files; do not edit them.")
    versions = {}
    for n in shown:
        versions[n.get("pdfVersion")] = versions.get(n.get("pdfVersion"), 0) + 1
    if set(versions) - {ctx["version"]}:
        out.append("- PDF builds these notes were made on (`pdfVersion`, the first 12 hex digits of the PDF's SHA-256):")
        for v, k in sorted(versions.items(), key=lambda x: (x[0] != ctx["version"], str(x[0]))):
            label = "the current build" if v == ctx["version"] else "older"
            snap = ctx["snapshots"].get(v)
            where = f"snapshot `{snap}`" if snap else ("same as the PDF above" if v == ctx["version"] else "no snapshot kept")
            out.append(f"  - `{v or '?'}`, {label}: {k} {'note' if k == 1 else 'notes'}; {where}")
    elif shown:
        out.append("- All these notes were made on the current build.")
    out.append("")

    out += ["## What to do", "",
            "1. Make each change in the source the PDF is built from (e.g. the LaTeX files), not in the PDF.",
            "2. Find the passage by its quote; search for a distinctive part of it. Page numbers belong to the "
            "build the note was made on and may have moved since.",
            "3. If a note is a question, is unclear or is a matter of taste, do not guess: leave it open and "
            "put your proposal (or your question) in `reply`.",
            "4. When you have applied a note, mark it `done`; when you decide against it, mark it `rejected`. "
            "Either way give a one-line `reply` saying what you changed or why not.",
            "5. Report through the API below. Do not edit the notes files by hand and do not add or delete notes.", "",
            "## Reporting back", "",
            "```sh",
            f"curl -X PUT {doc_api}/notes/NOTE_ID \\",
            "  -H 'Content-Type: application/json' \\",
            "  -d '{\"status\": \"done\", \"reply\": \"Shortened the paragraph to two sentences.\"}'",
            "```", "",
            "`status` is `open`, `done` or `rejected`; `reply` is shown to the author next to the note. "
            "Sending only `reply` keeps the status. "
            f"`GET {doc_api}/notes` returns the current notes as JSON.", "",
            f"The server runs at <{api}/> on the machine that runs pdf-notes"
            + (f" and was opened in the browser at <{ctx['browser'].rstrip('/')}/>." if ctx.get("browser") else ".")
            + " If you cannot reach it, list the note ids with your status and reply in your answer instead.", "",
            "## Notes", ""]
    if not shown:
        out += [f"*(no {status} notes)*" if status != "all" else "*(no notes)*", ""]
    for n in shown:
        out += _agent_note(n, number[n["id"]], ctx, status == "all")
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

    def agent_context(self, doc, notes):
        """Paths and versions the agent export needs."""
        pdf = self.pdf_path(doc)
        notes_file = self.notes_path(doc)
        ctx = {"pdf": str(pdf), "version": self.version(doc) if pdf.exists() else None,
               "notes": str(notes_file), "notes_md": str(notes_file.with_suffix(".md")), "snapshots": {}}
        if doc["kind"] == "path":
            for v in {n.get("pdfVersion") for n in notes if n.get("pdfVersion")}:
                snap = self.snapshots_dir(doc) / f"{v}.pdf"
                if snap.exists():
                    ctx["snapshots"][v] = str(snap)
        return ctx

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
                    if "reply" in body:
                        n["reply"] = (body["reply"] or "").strip()
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

        def disposition(self, doc, suffix, query):
            if "download" not in query:
                return {"Content-Disposition": "inline"}
            name = Path(doc["name"]).stem + suffix
            return {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")}

        def local_url(self):
            """This server as seen from its own machine."""
            host, port = self.server.server_address[:2]
            host = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(host, host)
            return f"http://{'[' + host + ']' if ':' in host else host}:{port}"

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
                query = parse_qs(url.query)
                if parts[3] == "notes" and len(parts) == 4:
                    if "download" in query:
                        data = (json.dumps(lib.notes(doc), ensure_ascii=False, indent=2) + "\n").encode()
                        return self.send_bytes(data, "application/json; charset=utf-8",
                                               extra=self.disposition(doc, "-notes.json", query))
                    return self.send_json(lib.notes(doc))
                if parts[3] == "export.md":
                    md = render_markdown(doc, lib.notes(doc)).encode()
                    return self.send_bytes(md, "text/markdown; charset=utf-8", extra=self.disposition(doc, "-notes.md", query))
                if parts[3] == "export-agent.md":
                    status = (query.get("status") or ["open"])[0]
                    if status not in STATUSES + ("all",):
                        return self.fail(HTTPStatus.BAD_REQUEST, f"status must be one of {', '.join(STATUSES)}, all")
                    with lib.lock:
                        notes = lib.notes(doc)
                        ctx = lib.agent_context(doc, notes)
                    ctx["api"] = self.local_url()
                    scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
                    browser = (query.get("origin") or [""])[0] or f"{scheme}://{self.headers.get('Host', '')}"
                    if ORIGIN.fullmatch(browser) and urlparse(browser).hostname not in ("localhost", "127.0.0.1", "::1"):
                        ctx["browser"] = browser
                    md = render_agent_markdown(doc, notes, ctx, status).encode()
                    return self.send_bytes(md, "text/markdown; charset=utf-8", extra=self.disposition(doc, "-agent.md", query))
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
                if not isinstance(body, dict):
                    return self.fail(HTTPStatus.BAD_REQUEST, "expected a JSON object")
                if body.get("status", "open") not in STATUSES:
                    return self.fail(HTTPStatus.BAD_REQUEST, f"status must be one of {', '.join(STATUSES)}")
                if not isinstance(body.get("reply", ""), (str, type(None))) or len(body.get("reply") or "") > MAX_REPLY:
                    return self.fail(HTTPStatus.BAD_REQUEST, f"reply must be text of at most {MAX_REPLY} characters")
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
