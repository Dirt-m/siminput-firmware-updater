"""A small popup list: autocomplete under a text field, and the dropdown of
a `Picker` with type-to-filter.

One per window (like the error tooltip), plain tk, colours resolved at
each show. It closes on Esc, on a pick, when its field loses focus, and on
any click outside it.

* `attach(entry, source, on_accept)` makes a text field suggest as you
  type: Up/Down move, Enter or Tab take the highlighted entry, Down on an
  empty field lists everything (free ones first). Focus stays in the field.
* `SuggestPopup.for_widget(w).show_list(...)` is the dropdown: focus moves
  to the list and typing filters it.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from .. import ui_theme as t

MAX_ROWS = 8
FOCUS_GRACE_MS = 150   # a click on the list must land before a focus-out closes it
_NAV_KEYS = {"Up", "Down", "Return", "KP_Enter", "Tab", "Escape", "ISO_Left_Tab",
             "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"}

Item = tuple[str, str]   # (value handed to on_pick, text shown)


class SuggestPopup:
    _instances: dict[str, "SuggestPopup"] = {}

    @classmethod
    def for_widget(cls, widget) -> "SuggestPopup":
        root = widget.winfo_toplevel()
        key = str(root)
        pop = cls._instances.get(key)
        if pop is None or not pop._win.winfo_exists():
            pop = cls._instances[key] = cls(root)
        return pop

    def __init__(self, root):
        self._root = root
        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._filter_label = tk.Label(self._win, anchor="w", padx=8)
        # The frame gives the list inner padding (a Listbox has none of its own).
        self._body = tk.Frame(self._win, bd=0)
        self._body.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self._list = tk.Listbox(self._body, activestyle="none", exportselection=False,
                                highlightthickness=0, bd=0, takefocus=0)
        self._list.pack(fill="both", expand=True, padx=t.px(root, 6), pady=t.px(root, 3))
        self._list.bind("<ButtonPress-1>", self._click)
        self._list.bind("<Motion>", self._hover)
        self._list.bind("<Key>", self._list_key)
        self._list.bind("<FocusOut>", lambda _e: self._close_soon())
        self.anchor: tk.Widget | None = None
        self._items: list[Item] = []
        self._all: list[Item] = []
        self._on_pick: Callable[[str], None] | None = None
        self._filter: str | None = None   # None: field mode; str: list mode
        self._close_after: str | None = None
        # Any click outside the popup or its anchor closes it.
        root.bind_all("<ButtonPress>", self._outside_click, add="+")

    @property
    def is_open(self) -> bool:
        return self._win.winfo_exists() and self._win.winfo_ismapped()

    # --------------------------------------------------------------- show

    def show(self, anchor: tk.Widget, items: list[Item], on_pick: Callable[[str], None]):
        """Field mode: suggestions under `anchor`; focus stays where it is."""
        self._filter = None
        self._filter_label.pack_forget()
        self._open(anchor, items, on_pick)

    def show_list(self, anchor: tk.Widget, items: list[Item], current: str,
                  on_pick: Callable[[str], None]):
        """List mode: a dropdown that takes focus; typing filters it."""
        self._filter = ""
        self._all = list(items)
        self._filter_label.pack(fill="x", before=self._body)
        self._open(anchor, items, on_pick)
        for i, (value, _) in enumerate(items):
            if value == current:
                self._select(i)
        self._list.focus_set()

    def _open(self, anchor, items: list[Item], on_pick):
        self._cancel_close()
        self.anchor, self._on_pick = anchor, on_pick
        font = t.tk_font(anchor, 12, mono=True)
        bg, fg = t.resolve(t.SURFACE_2), t.resolve(t.TEXT)
        self._win.configure(bg=t.resolve(t.BORDER_STRONG))
        self._body.configure(bg=bg)
        self._list.configure(font=font, bg=bg, fg=fg, selectbackground=t.resolve(t.ACCENT),
                             selectforeground="#FFFFFF")
        self._filter_label.configure(font=t.tk_font(anchor, 11), bg=bg, fg=t.resolve(t.TEXT_MUTED))
        self._fill(items)
        self._win.deiconify()
        self._win.lift()

    def _fill(self, items: list[Item]):
        self._items = list(items)
        self._list.delete(0, "end")
        for _, label in items:
            self._list.insert("end", label)
        width = max([len(label) for _, label in items] + [18]) + 1
        self._list.configure(width=width, height=max(1, min(MAX_ROWS, len(items))))
        if self._filter is not None:
            self._filter_label.configure(text=f"Filter: {self._filter}" if self._filter else "Type to filter")
        if items:
            self._select(0)
        a = self.anchor
        self._win.update_idletasks()
        x, y = a.winfo_rootx(), a.winfo_rooty() + a.winfo_height() + 2
        # Flip above the anchor when it would run off the bottom of the screen.
        if y + self._win.winfo_reqheight() > a.winfo_screenheight():
            y = a.winfo_rooty() - self._win.winfo_reqheight() - 2
        self._win.geometry(f"+{x}+{y}")

    def update(self, items: list[Item]):
        if not items:
            self.hide()
        elif self.is_open:
            self._fill(items)

    def hide(self):
        self._cancel_close()
        if self._win.winfo_exists():
            self._win.withdraw()
        self.anchor = None

    # ---------------------------------------------------------- selection

    def _select(self, i: int):
        self._list.selection_clear(0, "end")
        self._list.selection_set(i)
        self._list.see(i)

    def move(self, delta: int):
        cur = self._list.curselection()
        i = (cur[0] if cur else -1) + delta
        if self._items:
            self._select(max(0, min(len(self._items) - 1, i)))

    def pick(self, i: int | None = None) -> bool:
        if i is None:
            cur = self._list.curselection()
            if not cur:
                return False
            i = cur[0]
        if not 0 <= i < len(self._items):
            return False
        value, on_pick, anchor = self._items[i][0], self._on_pick, self.anchor
        self.hide()
        if self._filter is not None and anchor is not None and anchor.winfo_exists():
            anchor.focus_set()
        if on_pick:
            on_pick(value)
        return True

    # ------------------------------------------------------------- events

    def _click(self, event):
        self._cancel_close()
        self.pick(self._list.nearest(event.y))
        return "break"

    def _hover(self, event):
        self._select(self._list.nearest(event.y))

    def _list_key(self, event):
        """List mode: navigation and type-to-filter."""
        k = event.keysym
        if k in ("Up", "Down"):
            self.move(-1 if k == "Up" else 1)
        elif k in ("Return", "KP_Enter", "Tab"):
            self.pick()
        elif k == "Escape":
            anchor = self.anchor
            self.hide()
            if anchor is not None and anchor.winfo_exists():
                anchor.focus_set()
        elif k == "BackSpace":
            self._refilter(self._filter[:-1])
        elif event.char and event.char.isprintable():
            self._refilter(self._filter + event.char)
        return "break"

    def _refilter(self, text: str):
        self._filter = text
        low = text.lower()
        starts = [it for it in self._all if it[1].lower().startswith(low)]
        inside = [it for it in self._all if low in it[1].lower() and it not in starts]
        self._fill(starts + inside or [("", "No match")])

    def _outside_click(self, event):
        if not self.is_open:
            return
        w = event.widget
        try:
            inside = str(w).startswith(str(self._win)) or w is self.anchor or (
                self.anchor is not None and str(w).startswith(str(self.anchor)))
        except Exception:
            inside = False
        if not inside:
            self.hide()

    def _close_soon(self):
        self._cancel_close()
        if self.is_open:
            self._close_after = self._root.after(FOCUS_GRACE_MS, self._close_if_unfocused)

    def _close_if_unfocused(self):
        self._close_after = None
        focus = self._root.focus_get()
        if focus is None or (focus is not self._list and focus is not self.anchor
                             and not str(focus).startswith(str(self.anchor))):
            self.hide()

    def _cancel_close(self):
        if self._close_after is not None:
            self._root.after_cancel(self._close_after)
            self._close_after = None


def attach(entry, source: Callable[[str], list[Item]], on_accept: Callable[[str], None],
           token: bool = False):
    """Autocomplete for a text field (a CTkEntry or tk.Entry). `source(text)`
    returns the items for what is typed; `on_accept(value)` applies a pick.
    With `token`, only the text after the last comma is matched."""

    def typed() -> str:
        text = entry.get()
        return text.rsplit(",", 1)[-1].strip() if token else text.strip()

    def popup() -> SuggestPopup:
        return SuggestPopup.for_widget(entry)

    def mine() -> bool:
        p = popup()
        return p.is_open and p.anchor is entry

    def refresh(open_empty: bool = False):
        text = typed()
        if not text and not open_empty:
            if mine():
                popup().hide()
            return
        items = source(text)
        # Nothing to add when the only suggestion is what is already typed.
        if len(items) == 1 and items[0][0] == text:
            items = []
        if not items:
            if mine():
                popup().hide()
        elif mine():
            popup().update(items)
        else:
            popup().show(entry, items, on_accept)

    def key_release(event):
        if event.keysym not in _NAV_KEYS:
            refresh()

    def nav(event):
        k = event.keysym
        if not mine():
            if k == "Down":
                refresh(open_empty=True)
                return "break"
            return None
        if k in ("Up", "Down"):
            popup().move(-1 if k == "Up" else 1)
        elif k in ("Return", "KP_Enter", "Tab"):
            if not popup().pick():
                return None
        elif k == "Escape":
            popup().hide()
        return "break"

    entry.bind("<KeyRelease>", key_release, add="+")
    for seq in ("<Down>", "<Up>", "<Return>", "<KP_Enter>", "<Tab>", "<Escape>"):
        entry.bind(seq, nav, add="+")
    entry.bind("<FocusOut>", lambda _e: popup()._close_soon() if mine() else None, add="+")
