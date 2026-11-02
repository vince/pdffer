"""Main window: minimal header bar plus the page canvas."""

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

from . import errors, settings  # noqa: E402
from .canvas import PageCanvas  # noqa: E402
from .document import Document, ImageError, LoadError, write_atomic  # noqa: E402
from .style_button import StyleButton  # noqa: E402
from .style_button import css as style_button_css  # noqa: E402
from .thumbnails import ThumbnailPanel  # noqa: E402

APP_NAME = "PDF Annotator"

TOOLS = [
    ("select", "pdfannot-select-symbolic", "Select, move and resize (S)"),
    ("pen", "pdfannot-pen-symbolic", "Pen (P)"),
    ("text", "pdfannot-text-symbolic", "Text (T)"),
    ("eraser", "pdfannot-eraser-symbolic", "Eraser (E)"),
]


def install_css():
    css = ".page-osd { padding: 4px 12px; border-radius: 999px; }\n" + style_button_css()
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME,
                         default_width=1000, default_height=1100)
        self.doc = None
        self._saving = False
        self._force_close = False
        self._osd_timeout = 0
        self.prefs = settings.load()
        self._add_actions()

        # ---- content
        self.overlay = Gtk.Overlay()
        self.canvas = PageCanvas(self.overlay)
        self.scrolled = Gtk.ScrolledWindow(child=self.canvas)
        self.overlay.set_child(self.scrolled)

        self.page_label = Gtk.Label()
        self.page_label.add_css_class("osd")
        self.page_label.add_css_class("page-osd")
        self.page_osd = Gtk.Revealer(
            child=self.page_label, transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=18,
            can_target=False)
        self.overlay.add_overlay(self.page_osd)

        self.unfullscreen_btn = Gtk.Button(
            icon_name="view-restore-symbolic", tooltip_text="Leave Fullscreen (F11)",
            halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=12, margin_end=12,
            action_name="win.fullscreen", visible=False)
        self.unfullscreen_btn.add_css_class("osd")
        self.unfullscreen_btn.add_css_class("circular")
        self.overlay.add_overlay(self.unfullscreen_btn)

        open_btn = Gtk.Button(label="Open PDF…", action_name="win.open",
                              halign=Gtk.Align.CENTER)
        open_btn.add_css_class("pill")
        open_btn.add_css_class("suggested-action")
        empty = Adw.StatusPage(
            icon_name="io.github.vince.PdfAnnotator", title="Open a PDF",
            description="Drop a PDF here or press Ctrl+O", child=open_btn)

        self.stack = Gtk.Stack()
        self.stack.add_named(empty, "empty")
        self.stack.add_named(self.overlay, "doc")
        self.thumbnails = ThumbnailPanel(self.canvas)
        self.split = Adw.OverlaySplitView(
            sidebar=self.thumbnails, content=self.stack,
            min_sidebar_width=150, max_sidebar_width=190, sidebar_width_fraction=0.16)
        self.toasts = Adw.ToastOverlay(child=self.split)

        # ---- header bar
        header = Adw.HeaderBar()
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                            tooltip_text="Pages (F9)",
                                            active=bool(self.prefs.get("sidebar")))
        self.sidebar_btn.connect("toggled", lambda *_: self._sync_sidebar(save=True))
        header.pack_start(self.sidebar_btn)
        for icon, action, tip in (
                ("document-open-symbolic", "win.open", "Open (Ctrl+O)"),
                ("edit-undo-symbolic", "win.undo", "Undo (Ctrl+Z)"),
                ("edit-redo-symbolic", "win.redo", "Redo (Ctrl+Shift+Z)")):
            header.pack_start(Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip))

        self.tools = Adw.ToggleGroup()
        for name, icon, tip in TOOLS:
            self.tools.add(Adw.Toggle(name=name, icon_name=icon, tooltip=tip))
        self.tools.connect("notify::active-name", self._on_tool_toggled)

        self.style_btn = StyleButton(self.canvas.style)
        self.style_btn.connect("changed", self._on_style_changed)
        title = Gtk.Box(spacing=6)
        title.append(self.tools)
        title.append(self.style_btn)
        title.append(Gtk.Button(icon_name="insert-image-symbolic",
                                action_name="win.insert-image",
                                tooltip_text="Insert Image (Ctrl+I, or paste with Ctrl+V)"))
        header.set_title_widget(title)

        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Save As…", "win.save-as")
        sec.append("Save Flattened Copy…", "win.save-flattened")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Insert Image…", "win.insert-image")
        sec.append("Paste Image", "win.paste")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Zoom In", "win.zoom-in")
        sec.append("Zoom Out", "win.zoom-out")
        sec.append("Fit Width", "win.zoom-fit")
        sec.append("Fullscreen", "win.fullscreen")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("About PDF Annotator", "app.about")
        menu.append_section(None, sec)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Main Menu"))
        self.save_btn = Gtk.Button(icon_name="document-save-symbolic",
                                   action_name="win.save", tooltip_text="Save (Ctrl+S)")
        header.pack_end(self.save_btn)

        self.toolbar_view = Adw.ToolbarView(content=self.toasts)
        self.toolbar_view.add_top_bar(header)
        self.set_content(self.toolbar_view)

        # ---- keys, drop
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        img_drop = Gtk.DropTarget.new(Gdk.Texture, Gdk.DragAction.COPY)
        img_drop.connect("drop", self._on_texture_drop)
        self.canvas.add_controller(img_drop)
        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        self.canvas.connect("view-changed", self._on_view_changed)
        self.canvas.connect("editor-changed", lambda *_: self._update_state())
        self.canvas.connect("selection-changed", lambda *_: self._update_state())

        # ---- restore preferences
        st = self.canvas.style
        st.color = tuple(self.prefs["color"])
        st.width = float(self.prefs["width"])
        st.text_size = float(self.prefs["text_size"])
        self.style_btn.sync()
        self.tools.set_active_name(self.prefs.get("tool", "pen"))
        # The group starts with its first toggle active, so restoring that
        # tool emits no notify; sync the canvas explicitly.
        self.canvas.set_tool(self.tools.get_active_name() or "pen")
        self._update_state()

    # ------------------------------------------------------------ actions

    def _add_actions(self):
        def add(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            return a

        add("open", self.action_open)
        add("save", self.action_save)
        add("save-as", self.action_save_as)
        add("save-flattened", self.action_save_flattened)
        add("undo", self.action_undo)
        add("redo", self.action_redo)
        add("zoom-in", lambda: self.canvas.zoom_by(1.2))
        add("zoom-out", lambda: self.canvas.zoom_by(1 / 1.2))
        add("zoom-fit", lambda: self.canvas.zoom_fit_width())
        add("fullscreen", self.toggle_fullscreen)
        add("insert-image", self.action_insert_image)
        add("paste", self.action_paste)
        add("toggle-sidebar", lambda: self.sidebar_btn.set_active(
            not self.sidebar_btn.get_active()))
        add("delete", lambda: self.canvas.delete_selected())

    def _enable(self, name, on):
        self.lookup_action(name).set_enabled(on)

    def _update_state(self):
        d = self.doc
        has = d is not None
        for name in ("save-as", "save-flattened", "zoom-in", "zoom-out", "zoom-fit",
                     "insert-image", "paste", "toggle-sidebar"):
            self._enable(name, has and not (self._saving and name.startswith("save")))
        self._enable("save", has and not self._saving)
        editing = self.canvas.editor is not None
        self._enable("undo", has and (d.can_undo or editing))
        self._enable("redo", has and (d.can_redo or editing))
        self._enable("delete", has and self.canvas.selected is not None)
        self.sidebar_btn.set_sensitive(has)
        if has:
            name = os.path.basename(d.path)
            self.set_title(f"{'• ' if d.dirty else ''}{name} — {APP_NAME}")
            if d.dirty:
                self.save_btn.add_css_class("suggested-action")
            else:
                self.save_btn.remove_css_class("suggested-action")
        else:
            self.set_title(APP_NAME)
            self.save_btn.remove_css_class("suggested-action")
        self.stack.set_visible_child_name("doc" if has else "empty")
        self._sync_sidebar()

    def _sync_sidebar(self, save=False):
        want = self.sidebar_btn.get_active()
        self.split.set_show_sidebar(want and self.doc is not None)
        if save:
            self.prefs["sidebar"] = want
            settings.save(self.prefs)

    def _on_doc_changed(self):
        self.canvas.refresh()
        self.thumbnails.refresh()
        self._update_state()

    def action_undo(self):
        if self.canvas.editor:
            self.canvas.editor.undo()
        elif self.doc:
            self.doc.undo()

    def action_redo(self):
        if self.canvas.editor:
            self.canvas.editor.redo()
        elif self.doc:
            self.doc.redo()

    # ------------------------------------------------------------ open

    def action_open(self):
        self.canvas.commit_editor()
        dialog = Gtk.FileDialog(title="Open PDF", modal=True)
        dialog.set_default_filter(self._pdf_filter())
        if self.doc:
            dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.doc.path)))

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.open_path(f.get_path())
        dialog.open(self, None, done)

    @staticmethod
    def _pdf_filter():
        flt = Gtk.FileFilter(name="PDF documents")
        flt.add_mime_type("application/pdf")
        flt.add_suffix("pdf")
        return flt

    def open_path(self, path):
        """Open a PDF, asking to save unsaved changes first."""
        self._confirm_unsaved(lambda: self._load(path))

    def _load(self, path):
        try:
            doc = Document.load(path)
        except LoadError as e:
            self._error("Couldn't Open PDF", e)
            return
        except Exception as e:  # a bug; still tell the user something useful
            errors.log.error("unexpected error opening %s", path, exc_info=e)
            self._error("Couldn't Open PDF", errors.UserError(
                f"Something went wrong while opening {errors.name(path)}.",
                errors.technical(e)))
            return
        doc.on_changed = self._on_doc_changed
        self.doc = doc
        self.canvas.set_document(doc)
        self.thumbnails.set_document(doc)
        self._update_state()
        self._show_page_osd()

    def has_document(self):
        return self.doc is not None

    def _on_drop(self, _target, value, x, y):
        if not isinstance(value, Gio.File) or not value.get_path():
            return False
        path = value.get_path()
        ctype, _uncertain = Gio.content_type_guess(path, None)
        if self.doc and ctype and ctype.startswith("image/"):
            ok, pt = self.compute_point(self.canvas, Graphene.Point().init(x, y))
            try:
                data = value.load_contents(None)[1]
            except GLib.Error as e:
                self._error("Couldn't Insert Image", errors.read_error(e, path))
                return True
            self._insert_image_bytes(data, (pt.x, pt.y) if ok else None)
        else:
            self.open_path(path)
        return True

    def _on_texture_drop(self, _target, texture, x, y):
        self._insert_image_bytes(texture.save_to_png_bytes().get_data(), (x, y))
        return True

    # ------------------------------------------------------------ images

    def action_insert_image(self):
        self.canvas.commit_editor()
        dialog = Gtk.FileDialog(title="Insert Image", modal=True)
        flt = Gtk.FileFilter(name="Images")
        flt.add_pixbuf_formats()
        dialog.set_default_filter(flt)

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return  # dismissed
            try:
                data = f.load_contents(None)[1]
            except GLib.Error as e:
                path = f.get_path() or f.get_basename()
                self._error("Couldn't Insert Image", errors.read_error(e, path))
                return
            self._insert_image_bytes(data)
        dialog.open(self, None, done)

    def action_paste(self):
        if self.canvas.editor:
            self.canvas.editor.tv.emit("paste-clipboard")
            return
        clipboard = self.get_clipboard()

        def done(cb, res):
            try:
                texture = cb.read_texture_finish(res)
            except GLib.Error:
                texture = None
            if texture is None:
                self.toasts.add_toast(Adw.Toast(title="No image on the clipboard", timeout=2))
                return
            self._insert_image_bytes(texture.save_to_png_bytes().get_data())
        clipboard.read_texture_async(None, done)

    def _insert_image_bytes(self, data, at=None):
        if not self.doc:
            return
        self.canvas.commit_editor()
        self.tools.set_active_name("select")
        try:
            self.canvas.insert_image(bytes(data), at)
        except ImageError as e:
            self._error("Couldn't Insert Image", e)
        except Exception as e:  # a bug; still tell the user something useful
            errors.log.error("unexpected error inserting image", exc_info=e)
            self._error("Couldn't Insert Image", errors.UserError(
                "Something went wrong while inserting the image.", errors.technical(e)))

    # ------------------------------------------------------------ save

    def action_save(self):
        if self.doc:
            self.save(self.doc.path)

    def action_save_as(self):
        self._ask_save_path("Save As", os.path.basename(self.doc.path),
                            lambda p: self.save(p))

    def action_save_flattened(self):
        base, _ext = os.path.splitext(os.path.basename(self.doc.path))
        self._ask_save_path("Save Flattened Copy", f"{base} (flattened).pdf",
                            lambda p: self.save(p, flatten=True))

    def _ask_save_path(self, title, name, cb):
        self.canvas.commit_editor()
        dialog = Gtk.FileDialog(title=title, modal=True, initial_name=name)
        dialog.set_default_filter(self._pdf_filter())
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.doc.path)))

        def done(dlg, res):
            try:
                f = dlg.save_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                path = f.get_path()
                if not path.lower().endswith(".pdf"):
                    path += ".pdf"
                cb(path)
        dialog.save(self, None, done)

    def save(self, path, flatten=False, then=None):
        """Save in a worker thread. Flattened saves are copies: they don't
        change the open document's path or saved state."""
        if not self.doc or self._saving:
            return
        self.canvas.commit_editor()
        doc = self.doc
        marker, job = doc.snapshot()
        self._saving = True
        self._update_state()

        def work():
            try:
                write_atomic(path, job(flatten))
                err = None
            except Exception as e:  # report anything to the user
                err = e
            GLib.idle_add(finished, err)

        def finished(err):
            self._saving = False
            if err is not None:
                self._error(f"Couldn't Save {errors.name(path)}", errors.save_error(err, path))
            elif not flatten and doc is self.doc:
                doc.path = path
                doc.mark_saved(marker)
                self.toasts.add_toast(Adw.Toast(title="Saved", timeout=2))
            elif flatten:
                self.toasts.add_toast(Adw.Toast(
                    title=f"Saved flattened copy “{os.path.basename(path)}”", timeout=3))
            self._update_state()
            if then:
                then(err is None)
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ unsaved

    def _confirm_unsaved(self, proceed):
        self.canvas.commit_editor()
        if not self.doc or not self.doc.dirty:
            proceed()
            return
        dlg = Adw.AlertDialog(
            heading="Save Changes?",
            body=f"“{os.path.basename(self.doc.path)}” has unsaved changes. "
                 "Changes you don't save will be lost.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("discard", "Discard")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")

        def on_response(_d, resp):
            if resp == "discard":
                proceed()
            elif resp == "save":
                self.save(self.doc.path, then=lambda ok: ok and proceed())
        dlg.connect("response", on_response)
        dlg.present(self)

    def _on_close_request(self, _win):
        self.canvas.commit_editor()
        if self._force_close or not self.doc or not self.doc.dirty:
            self.canvas.set_document(None)
            self.thumbnails.set_document(None)
            return False

        def really_close():
            self._force_close = True
            self.close()
        self._confirm_unsaved(really_close)
        return True

    def _error(self, heading, error):
        """Show a UserError: its plain message, with the technical cause
        tucked away under "Technical Details"."""
        errors.log.warning("%s: %s%s", heading, error.message,
                           f" ({error.details})" if error.details else "")
        dlg = Adw.AlertDialog(heading=heading, body=error.message)
        dlg.add_response("ok", "OK")
        if error.details:
            label = Gtk.Label(label=error.details, selectable=True, wrap=True,
                              xalign=0, css_classes=["monospace", "caption"])
            dlg.set_extra_child(Gtk.Expander(label="Technical Details", child=label))
        dlg.present(self)

    # ------------------------------------------------------------ tools & style

    def _on_tool_toggled(self, group, _pspec):
        tool = group.get_active_name() or "pen"
        self.canvas.set_tool(tool)
        self.prefs["tool"] = tool
        settings.save(self.prefs)

    def _on_style_changed(self, _button, what):
        if what in ("color", "text_size"):
            self.canvas.style_changed(what)  # restyles open text
        st = self.canvas.style
        self.prefs.update(color=list(st.color), width=st.width, text_size=st.text_size)
        settings.save(self.prefs)

    # ------------------------------------------------------------ view

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_):
        fs = self.is_fullscreen()
        self.toolbar_view.set_reveal_top_bars(not fs)
        self.unfullscreen_btn.set_visible(fs)

    def _on_view_changed(self, _canvas):
        self._show_page_osd()
        self._update_state()
        if self.doc:
            self.thumbnails.sync_current(self.canvas.current_page())

    def _show_page_osd(self):
        if not self.doc:
            return
        self.page_label.set_text(
            f"{self.canvas.current_page() + 1} / {len(self.doc.pages)}  ·  "
            f"{round(self.canvas.zoom * 100)}%")
        self.page_osd.set_reveal_child(True)
        if self._osd_timeout:
            GLib.source_remove(self._osd_timeout)

        def hide():
            self._osd_timeout = 0
            self.page_osd.set_reveal_child(False)
            return False
        self._osd_timeout = GLib.timeout_add(1200, hide)

    def _on_key(self, _ctl, keyval, _code, state):
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                        | Gdk.ModifierType.SUPER_MASK)
        if mods or self.canvas.editor is not None:
            return False
        tool = {Gdk.KEY_p: "pen", Gdk.KEY_t: "text", Gdk.KEY_e: "eraser",
                Gdk.KEY_s: "select"}.get(keyval)
        if tool:
            self.tools.set_active_name(tool)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.canvas.selected:
            self.canvas.delete_selected()
            return True
        if keyval == Gdk.KEY_Escape and self.canvas.selected:
            self.canvas.select(None)
            return True
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
            return True
        adj = self.scrolled.get_vadjustment()
        page = adj.get_page_size()
        delta = {
            Gdk.KEY_Page_Down: page * 0.9, Gdk.KEY_Page_Up: -page * 0.9,
            Gdk.KEY_space: page * 0.9,
            Gdk.KEY_Down: 50, Gdk.KEY_Up: -50,
        }.get(keyval)
        if delta is not None:
            adj.set_value(adj.get_value() + delta)
            return True
        if keyval == Gdk.KEY_Home:
            adj.set_value(0)
            return True
        if keyval == Gdk.KEY_End:
            adj.set_value(adj.get_upper())
            return True
        return False
