import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pdf_notes.server import Library, make_server, render_agent_markdown, render_markdown

PDF = b"%PDF-1.4\n% minimal test file\n%%EOF\n"


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = Library(self.root / "data")
        self.srv = make_server(self.lib, port=0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, raw=None, headers=False):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return (r.status, r.read(), r.headers) if headers else (r.status, r.read())
        except urllib.error.HTTPError as e:
            with e:
                return (e.code, e.read(), e.headers) if headers else (e.code, e.read())

    def jcall(self, *a, **kw):
        status, body = self.call(*a, **kw)
        return status, json.loads(body)

    def test_upload_and_notes_roundtrip(self):
        status, doc = self.jcall("POST", "/api/docs?name=paper%20draft.pdf", raw=PDF)
        self.assertEqual(status, 201)
        self.assertEqual(doc["name"], "paper draft.pdf")
        self.assertEqual(doc["kind"], "upload")

        status, pdf = self.call("GET", f"/api/docs/{doc['id']}/pdf")
        self.assertEqual((status, pdf), (200, PDF))

        status, note = self.jcall("POST", f"/api/docs/{doc['id']}/notes", {
            "kind": "text", "page": 2, "top": 0.3, "quote": "a sentence", "note": "too long",
            "rects": [{"p": 2, "x": 0.1, "y": 0.3, "w": 0.5, "h": 0.02}]})
        self.assertEqual(status, 201)
        self.assertEqual(note["status"], "open")
        self.assertEqual(note["pdfVersion"], doc["version"])

        status, note = self.jcall("PUT", f"/api/docs/{doc['id']}/notes/{note['id']}", {"status": "done", "page": 99})
        self.assertEqual((status, note["status"], note["page"]), (200, "done", 2))
        status, _ = self.jcall("PUT", f"/api/docs/{doc['id']}/notes/{note['id']}", {"status": "nonsense"})
        self.assertEqual(status, 400)

        status, md = self.call("GET", f"/api/docs/{doc['id']}/export.md")
        self.assertIn("> a sentence", md.decode())
        self.assertIn("too long", md.decode())

        notes_file = self.root / "data" / "docs" / doc["id"] / "notes.json"
        self.assertTrue(notes_file.exists())
        self.assertTrue(notes_file.with_suffix(".md").exists())

        status, _ = self.jcall("DELETE", f"/api/docs/{doc['id']}/notes/{note['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(notes_file.read_text()), [])

        status, docs = self.jcall("GET", "/api/docs")
        self.assertEqual(docs[0]["notes"], 0)
        status, _ = self.jcall("DELETE", f"/api/docs/{doc['id']}")
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "data" / "docs" / doc["id"]).exists())

    def test_rejects_non_pdf(self):
        status, _ = self.jcall("POST", "/api/docs?name=x.pdf", raw=b"hello")
        self.assertEqual(status, 400)

    def test_path_document_uses_external_notes_and_snapshots(self):
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(PDF)
        notes = self.root / "review" / "notes.json"
        snaps = self.root / "snapshots"
        doc = self.lib.add_path(pdf, notes=notes, snapshots=snaps)
        # Same path gives the same id, so notes survive a restart.
        self.assertEqual(self.lib.add_path(pdf, notes=notes, snapshots=snaps)["id"], doc["id"])
        self.assertEqual(len(self.lib.docs()), 1)

        status, note = self.jcall("POST", f"/api/docs/{doc['id']}/notes", {"kind": "general", "note": "x"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(notes.read_text())[0]["id"], note["id"])
        self.assertTrue((snaps / f"{note['pdfVersion']}.pdf").exists())

        # The live file changes; the version follows it.
        pdf.write_bytes(PDF + b"% rebuilt\n")
        status, info = self.jcall("GET", f"/api/docs/{doc['id']}")
        self.assertNotEqual(info["version"], note["pdfVersion"])

        # Forgetting a path document keeps the PDF and external notes.
        self.jcall("DELETE", f"/api/docs/{doc['id']}")
        self.assertTrue(pdf.exists() and notes.exists())

    def test_reply(self):
        _, doc = self.jcall("POST", "/api/docs?name=a.pdf", raw=PDF)
        url = f"/api/docs/{doc['id']}/notes"
        _, note = self.jcall("POST", url, {"kind": "general", "note": "is this needed?"})
        status, note = self.jcall("PUT", f"{url}/{note['id']}", {"status": "rejected", "reply": "  Kept: it is the only result.  "})
        self.assertEqual((status, note["status"], note["reply"]), (200, "rejected", "Kept: it is the only result."))
        # A reply alone keeps the status.
        status, note = self.jcall("PUT", f"{url}/{note['id']}", {"reply": "Kept, see section 3."})
        self.assertEqual((note["status"], note["reply"]), ("rejected", "Kept, see section 3."))
        self.assertEqual(self.jcall("PUT", f"{url}/{note['id']}", {"reply": 5})[0], 400)
        self.assertEqual(self.jcall("PUT", f"{url}/{note['id']}", {"reply": "x" * 5000})[0], 400)
        self.assertEqual(self.jcall("PUT", f"{url}/{note['id']}", [1])[0], 400)
        _, md = self.call("GET", f"/api/docs/{doc['id']}/export.md")
        self.assertIn("Reply: Kept, see section 3.", md.decode())

    def test_notes_download(self):
        _, doc = self.jcall("POST", "/api/docs?name=paper.pdf", raw=PDF)
        self.jcall("POST", f"/api/docs/{doc['id']}/notes", {"kind": "general", "note": "x"})
        status, body, headers = self.call("GET", f"/api/docs/{doc['id']}/notes?download=1", headers=True)
        self.assertEqual(status, 200)
        self.assertIn("paper-notes.json", headers["Content-Disposition"])
        self.assertEqual(json.loads(body)[0]["note"], "x")

    def test_agent_export(self):
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(PDF)
        notes = self.root / "review" / "notes.json"
        snaps = self.root / "snapshots"
        doc = self.lib.add_path(pdf, notes=notes, snapshots=snaps, name="Thesis")
        url = f"/api/docs/{doc['id']}"
        _, old_area = self.jcall("POST", f"{url}/notes", {
            "kind": "area", "page": 12, "top": 0.12, "note": "Move this figure up.",
            "rects": [{"p": 12, "x": 0.11, "y": 0.124, "w": 0.76, "h": 0.29}]})
        pdf.write_bytes(PDF + b"% rebuilt\n")
        _, across = self.jcall("POST", f"{url}/notes", {
            "kind": "text", "page": 7, "top": 0.9, "quote": "odsetkiem 7 8 Header rozwiązanych zadań", "note": "Shorten.",
            "rects": [{"p": 7, "x": 0.1, "y": 0.9, "w": 0.7, "h": 0.02}, {"p": 8, "x": 0.1, "y": 0.13, "w": 0.2, "h": 0.02}]})
        _, done = self.jcall("POST", f"{url}/notes", {"kind": "general", "note": "Already handled."})
        self.jcall("PUT", f"{url}/notes/{done['id']}", {"status": "done", "reply": "Fixed."})
        current = self.jcall("GET", url)[1]["version"]

        status, body, headers = self.call("GET", f"{url}/export-agent.md", headers=True)
        md = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], "inline")
        self.assertIn("# Review notes for an AI agent: Thesis", md)
        self.assertIn("3 notes: 2 open, 1 done, 0 rejected", md)
        self.assertIn(f"`{pdf.resolve()}`, current build `{current}`", md)
        self.assertIn(f"`{notes.resolve()}`", md)
        self.assertIn(f"`{(snaps / (old_area['pdfVersion'] + '.pdf')).resolve()}`", md)
        self.assertIn(f"curl -X PUT {self.base}/api/docs/{doc['id']}/notes/NOTE_ID", md)
        self.assertNotIn("Already handled.", md)
        # Reading order, numbered as in the app.
        self.assertIn(f"### 1. p. 7 · `{across['id']}`", md)
        self.assertIn(f"### 2. p. 12 · `{old_area['id']}`", md)
        self.assertIn("crosses a page break (pp. 7–8)", md)
        self.assertIn("Marked area on p. 12, x 11–87 %, y 12–41 %", md)
        self.assertIn(f"pdftoppm -f 12 -l 12 -r 100 -singlefile -png {(snaps / (old_area['pdfVersion'] + '.pdf')).resolve()}", md)
        self.assertIn(f"Made on the older build `{old_area['pdfVersion']}`", md)

        _, body = self.call("GET", f"{url}/export-agent.md?status=all")
        self.assertIn("Already handled.", body.decode())
        self.assertIn("Fixed.", body.decode())
        self.assertEqual(self.call("GET", f"{url}/export-agent.md?status=bogus")[0], 400)

        _, body, headers = self.call("GET", f"{url}/export-agent.md?download=1&origin=https://box.example.net:8443", headers=True)
        self.assertIn("Thesis-agent.md", headers["Content-Disposition"])
        self.assertIn("opened in the browser at <https://box.example.net:8443/>", body.decode())
        _, body = self.call("GET", f"{url}/export-agent.md?origin=javascript:alert(1)")
        self.assertNotIn("alert", body.decode())

    def test_static_paths_cannot_escape(self):
        status, _ = self.call("GET", "/static/../server.py")
        self.assertEqual(status, 404)
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"pdf-notes", body)


class MarkdownTest(unittest.TestCase):
    def test_orders_by_page_and_position(self):
        notes = [
            {"id": "b", "page": 3, "top": 0.1, "status": "open", "note": "second", "created": "2"},
            {"id": "c", "page": None, "status": "open", "note": "general", "created": "3", "kind": "general"},
            {"id": "a", "page": 1, "top": 0.9, "status": "done", "note": "first", "created": "1"},
        ]
        md = render_markdown({"name": "doc.pdf"}, notes)
        self.assertLess(md.index("first"), md.index("second"))
        self.assertLess(md.index("second"), md.index("general"))

    def test_agent_export_of_an_upload(self):
        notes = [
            {"id": "a", "page": 1, "top": 0.5, "status": "done", "note": "first", "created": "1", "pdfVersion": "v1"},
            {"id": "b", "page": 2, "top": 0.1, "status": "open", "note": "line one\n\nline two", "created": "2",
             "pdfVersion": "v1", "kind": "page", "reply": "Proposal: drop it?"},
        ]
        ctx = {"pdf": "/data/docs/x/document.pdf", "version": "v1", "notes": "/data/docs/x/notes.json",
               "notes_md": "/data/docs/x/notes.md", "snapshots": {}, "api": "http://127.0.0.1:8765"}
        md = render_agent_markdown({"id": "x", "name": "doc.pdf", "kind": "upload"}, notes, ctx)
        self.assertIn("an uploaded copy", md)
        self.assertIn("All these notes were made on the current build.", md)
        self.assertNotIn("first", md)
        self.assertIn("### 2. p. 2 · `b`", md)
        self.assertIn("About the whole of p. 2.", md)
        self.assertIn("> line one\n>\n> line two", md)
        self.assertIn("Earlier reply:\n\n> Proposal: drop it?", md)
        self.assertNotIn("opened in the browser", md)
        self.assertIn("*(no open notes)*", render_agent_markdown({"id": "x", "name": "d", "kind": "upload"}, notes[:1], ctx))


if __name__ == "__main__":
    unittest.main()
