# PDF Annotator

A small GNOME app for marking up PDFs with a stylus, mouse or keyboard:
signing documents, scribbling notes, updating hit points on a character
sheet. The toolbar is a single header bar so the page gets the rest of
the window (F11 hides even that).

## Install

Needs only Fedora packages (all present on a default Workstation install):

    sudo dnf install python3-gobject gtk4 libadwaita poppler-glib python3-cairo python3-pikepdf

Then:

    ./install.sh              # installs to ~/.local, adds a launcher
    ./install.sh --uninstall

Or run straight from the checkout: `./pdf-annotator some.pdf`.

## Using it

| Tool | Key | What it does |
|---|---|---|
| Select | S | Drag any mark to move it; drag an image's corner handles to resize. Delete removes the selected mark. |
| Pen | P | Draw. Pressure-sensitive with a stylus. |
| Text | T | Click to type; Enter to finish (Shift+Enter for a new line), Esc to cancel. Click existing text to edit it, drag it to move it. |
| Eraser | E | Wipe over marks to remove them. The stylus eraser end works in any tool. |

The color dot next to the tools sets color, pen width and text size.

**Images:** the image button (or Ctrl+I) inserts a PNG/JPEG/etc. You can
also paste one with Ctrl+V or drag an image file onto the page. A
transparent PNG of your signature works well for signing.

**Pages drawer:** the sidebar button (or F9) shows thumbnails of every page,
including your marks. Click one to jump there.

Fingers scroll and pinch-zoom; the stylus and mouse draw. Middle-drag pans
with a mouse.

| Shortcut | |
|---|---|
| Ctrl+O / Ctrl+S / Ctrl+Shift+S | Open / Save / Save As |
| Ctrl+Z / Ctrl+Shift+Z (Ctrl+Y) | Undo / Redo |
| Ctrl+I / Ctrl+V | Insert image / paste image |
| F9 | Show/hide page thumbnails |
| Ctrl+scroll, Ctrl+plus/minus, Ctrl+0 | Zoom, fit width |
| F11 | Fullscreen |

## How saving works

Marks are saved as standard PDF annotations (ink, free text and image
stamps) with
proper appearance streams, so they show up in Evince/Papers, Okular,
Firefox, Chrome, Acrobat and friends. Each one also stores its editable
data, so when you reopen a file here your old marks can still be
erased, moved and edited. That's handy for hit points that change every
session.

**Save Flattened Copy…** (main menu) writes a copy with the marks burned
into the page content instead. Use that for signed documents you send to
someone, so the signature can't be stripped or edited as an annotation.

Saves are *incremental*: the file you opened is kept byte-for-byte and
your changes are appended after it. So if someone sent you a PDF that is
already digitally signed (DocuSign, Adobe Sign, …), their signature stays
valid after you add yours; viewers will note that annotations were added
after signing. Unchanged marks are reused, so saving repeatedly doesn't grow
the file. Encrypted or damaged PDFs fall back to a full rewrite.

Saves are atomic (written to a temp file, then renamed), so a crash
mid-save won't corrupt your PDF.

## Development

    python3 -m unittest discover tests

- `pdf_annotator/document.py`: loading, marks, undo/redo, PDF writing
- `pdf_annotator/render.py`: cairo drawing shared by screen and PDF output
- `pdf_annotator/canvas.py`: scrolling page view, threaded rendering, input
- `pdf_annotator/thumbnails.py`: page thumbnail sidebar
- `pdf_annotator/window.py`, `app.py`: GTK/libadwaita UI
