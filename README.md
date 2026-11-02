# PDFfer

<img src="pdffer/icons/hicolor/scalable/apps/io.github.vince.PDFfer.svg" width="96"
     alt="PDFfer's mascot, a cartoon heifer, in front of a signed page" align="right">

A small GNOME app for marking up PDFs with a stylus, mouse or keyboard:
signing documents, scribbling notes, updating hit points on a character
sheet. The toolbar is a single header bar so the page gets the rest of
the window (F11 hides even that).

## Install

Needs only Fedora packages (all present on a default Workstation install):

    sudo dnf install python3-gobject gtk4 libadwaita poppler-glib python3-cairo python3-pikepdf python3-cryptography

Then:

    ./install.sh              # installs to ~/.local, adds a launcher
    ./install.sh --uninstall

Or run straight from the checkout: `./bin/pdffer some.pdf`.

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
the file.

Encrypted PDFs (e.g. ones that open without a password but restrict
editing or copying) stay encrypted with the same restrictions, including
in flattened copies. Damaged PDFs that can't take an incremental update
are rewritten in full instead.

Saves are atomic (written to a temp file, then renamed), so a crash
mid-save won't corrupt your PDF.

## Troubleshooting

Error messages explain the problem in plain language; the dialog's
**Technical Details** section has the underlying cause. Running
`pdffer` from a terminal also logs warnings there, and
`PDFFER_DEBUG=1 pdffer file.pdf` logs more detail (for example
about stylus input).

## Development

    python3 -m unittest discover tests

On a desktop this includes GUI tests that drive the real window (windows
flash briefly; they use a separate app instance and a throwaway settings
file, so a copy of the app you have open is unaffected). Without a
display the GUI tests are skipped; to run them headless:

    dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 GSK_RENDERER=cairo \
        python3 -m unittest discover tests

Lint with [ruff](https://docs.astral.sh/ruff/) (configured in
`pyproject.toml`; CI pins the version):

    pipx run ruff==0.17.3 check .

GitHub Actions runs the linter and all tests on each push in a Fedora container
(`.github/workflows/tests.yml`). `python3 tests/benchmark.py` prints
redraw, hover and memory numbers to compare before and after a change.

- `document.py`: loading, marks, undo/redo, PDF writing (incremental, encrypted)
- `render.py`: cairo drawing shared by screen and PDF output
- `canvas.py`: the scrolling page view; routes input to the active tool
- `tools.py`: pen, text, eraser and select tools
- `layout.py`: page layout geometry (pure Python, unit-tested)
- `page_renderer.py`: background Poppler rendering thread
- `text_editor.py`: the in-place text box
- `stylus.py`: stylus pressure, eraser tip and coalesced samples
- `thumbnails.py`: page thumbnail drawer
- `style_button.py`: color, pen width and text size popover
- `errors.py`: plain-language error messages
- `window.py`, `app.py`, `settings.py`: the window, application and preferences

## License

MIT; see [LICENSE](LICENSE).
