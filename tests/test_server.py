import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pdf_notes.server import Library, make_server, render_markdown

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

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

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


if __name__ == "__main__":
    unittest.main()
