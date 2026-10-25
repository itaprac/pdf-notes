# pdf-notes

Open a PDF in the browser, select a passage or draw a box around a figure,
and write what should change. Notes are saved as you go, as JSON and as
Markdown, so you can paste them into a chat with an AI agent or let the
agent read the files directly.

- Mark a **text passage**, a **rectangular area** (figures, tables), a
  **whole page**, or add a **general note**.
- Every note keeps the page, the section (from the PDF bookmarks), the
  quoted text with 80 characters of context on each side, and its status
  (`open`, `done`, `rejected`).
- **Copy as Markdown** / **Download .md** exports all notes in reading order.
- **Download PDF with notes** writes the notes into a copy of the PDF as
  standard annotations (highlights, rectangles, sticky notes), readable in
  Preview, Acrobat and other viewers. It exports the notes shown in the list.
- Upload PDFs by drag and drop, or open a PDF on disk. A PDF on disk is
  served live: when it is rebuilt (e.g. by LaTeX), the page offers a reload.
- Click PDF references to jump to their destination, or open web links.
- Sun/moon theme toggle with a saved preference; follows the system until chosen.
  Dark mode also darkens PDF pages in the preview. Exports keep the original colors.
- English and Polish interface.
- Python 3.9+ standard library only; pdf.js and pdf-lib are bundled.

## Run

From a checkout, without installing anything:

```sh
./pdf-notes                      # library: upload PDFs in the browser
./pdf-notes paper/build/main.pdf   # open a PDF on disk
```

Or install the `pdf-notes` command:

```sh
uv tool install .    # or: pipx install .
pdf-notes
```

The browser opens at <http://localhost:8765/>.

| Option | Meaning |
|---|---|
| `--port 8765` | port |
| `--host 127.0.0.1` | address to listen on; there is no login, so keep it local |
| `--data DIR` | data directory (default `$PDF_NOTES_HOME` or `~/.local/share/pdf-notes`) |
| `--notes FILE` | notes file for the single PDF given (default: in the data directory) |
| `--snapshots DIR` | where to keep copies of the PDF that notes refer to |
| `--name NAME` | display name for the single PDF given |
| `--no-browser` | do not open a browser |

To use it from another machine, keep it on `127.0.0.1` and put an
authenticated proxy or a private network in front of it (for example
`tailscale serve --bg --https=9458 http://127.0.0.1:8765`).

## Where the notes are

```
DATA/library.json                  documents
DATA/docs/<id>/document.pdf        uploaded PDF
DATA/docs/<id>/notes.json          notes (or the file given with --notes)
DATA/docs/<id>/notes.md            the same notes as Markdown
DATA/docs/<id>/snapshots/<v>.pdf   PDF version a note refers to (PDFs on disk only)
```

A note looks like this:

```json
{
  "id": "b0d4c922", "status": "open", "kind": "text",
  "page": 11, "top": 0.216, "section": "Related work",
  "quote": "Most earlier studies report only the final accuracy.",
  "prefix": "…compared on the same benchmark. ",
  "suffix": " Figure 1.2 shows the setup…",
  "rects": [{"p": 11, "x": 0.167, "y": 0.216, "w": 0.714, "h": 0.016}],
  "note": "Too long, split it.",
  "created": "2026-10-25T12:24:10", "pdfVersion": "9d1f8ad44d8f"
}
```

`page` is the 1-based PDF page; `rects` are fractions of the page size;
`pdfVersion` is the first 12 hex digits of the PDF's SHA-256.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/docs` | documents with note counts |
| POST | `/api/docs?name=file.pdf` | upload (request body: the PDF) |
| DELETE | `/api/docs/<id>` | forget a document (uploads are deleted, files on disk are kept) |
| GET | `/api/docs/<id>` | document info and current `version` |
| GET | `/api/docs/<id>/pdf` | the PDF |
| GET, POST | `/api/docs/<id>/notes` | list or add notes |
| PUT, DELETE | `/api/docs/<id>/notes/<note>` | change `note` or `status`, or delete |
| GET | `/api/docs/<id>/export.md` | notes as Markdown (`?download=1` to download) |

An agent that has applied a note can mark it done:

```sh
curl -X PUT localhost:8765/api/docs/<id>/notes/<note> -d '{"status": "done"}'
```

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Licence

MIT, see [LICENSE](LICENSE). Bundled libraries:

- pdf.js 4.10.38 (`src/pdf_notes/static/vendor/pdfjs`), © Mozilla and
  contributors, Apache License 2.0.
- pdf-lib 1.17.1 (`src/pdf_notes/static/vendor/pdf-lib`), © Andrew Dillon,
  MIT License.
