"""Step-by-step wizard that sets up share links on the user's own free
Cloudflare account (see cloud_setup.py) - no Node.js, scripts or terminal."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Callable

from .. import cloud_setup as cs
from . import theme


class ShareWizard:
    def __init__(self, root: tk.Misc, on_done: Callable[[str, str], None], titlebar: Callable = lambda w: None):
        self.on_done = on_done
        self.q: queue.Queue = queue.Queue()
        self.cf: cs.Cloudflare | None = None
        self.accounts: list[cs.Account] = []
        self.account: cs.Account | None = None
        self.has_subdomain = False
        self.win = w = tk.Toplevel(root)
        w.title("Set up share links")
        w.configure(bg=theme.BG)
        w.transient(root)
        w.resizable(False, False)
        titlebar(w)
        outer = ttk.Frame(w, padding=(22, 18, 22, 16))
        outer.pack(fill="both", expand=True)
        self.step_lbl = ttk.Label(outer, text="", style="Muted.TLabel")
        self.step_lbl.pack(anchor="w")
        self.title = ttk.Label(outer, text="", style="Heading.TLabel", font=(theme.FONT, 13, "bold"))
        self.title.pack(anchor="w", pady=(2, 10))
        self.body = ttk.Frame(outer, width=520, height=270)
        self.body.pack(fill="both", expand=True)
        self.body.pack_propagate(False)
        nav = ttk.Frame(outer)
        nav.pack(fill="x", pady=(14, 0))
        self.back_btn = ttk.Button(nav, text="Back", command=self._back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", style="Accent.TButton", command=self._next)
        self.next_btn.pack(side="right")
        self.cancel_btn = ttk.Button(nav, text="Not now", command=w.destroy)
        self.cancel_btn.pack(side="right", padx=(0, 6))
        self.history: list[str] = []
        self.page = ""
        self._poll()
        self.show("intro")
        w.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - w.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - w.winfo_height()) // 3)
        w.geometry(f"+{x}+{y}")
        w.focus_force()

    # ------------------------------------------------------------------ plumbing
    def _poll(self) -> None:
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(100, self._poll)

    def _bg(self, work: Callable[[], object], done: Callable[[object], None], fail: Callable[[Exception], None]):
        def run():
            try:
                res = work()
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.q.put(lambda: fail(e))
                return
            self.q.put(lambda: done(res))

        threading.Thread(target=run, daemon=True).start()

    def _clear(self) -> None:
        for c in self.body.winfo_children():
            c.destroy()

    def _text(self, text: str, style: str = "TLabel", **pack) -> ttk.Label:
        lbl = ttk.Label(self.body, text=text, style=style, wraplength=510, justify="left")
        lbl.pack(anchor="w", **({"pady": (0, 8)} | pack))
        return lbl

    def _link_button(self, text: str, url: str) -> None:
        ttk.Button(self.body, text=text, command=lambda: webbrowser.open(url)).pack(anchor="w", pady=(0, 10))

    def _busy(self, on: bool, msg: str = "") -> None:
        for b in (self.next_btn, self.back_btn):
            b.state(["disabled"] if on else ["!disabled"])
        if on:
            self.status = self._text(msg, "Muted.TLabel", pady=(8, 0))

    def _error(self, e: Exception) -> None:
        self._busy(False)
        hint = getattr(e, "hint", "")
        self._text(f"⚠ {e}" + (f"\n{hint}" if hint else ""), "Error.TLabel", pady=(8, 0))
        if isinstance(e, cs.R2NotEnabled):
            self._link_button("Open R2 in Cloudflare", cs.R2_URL)

    # ------------------------------------------------------------------ navigation
    STEPS = {"intro": 1, "account": 2, "token": 3, "pick": 3, "name": 4, "run": 5, "done": 5}

    def show(self, page: str, push: bool = True) -> None:
        if push and self.page and self.page != page:
            self.history.append(self.page)
        self.page = page
        self._clear()
        self.step_lbl.configure(text=f"Step {self.STEPS[page]} of 5")
        self.back_btn.configure(state="normal" if self.history and page not in ("run", "done") else "disabled")
        self.next_btn.configure(text="Next", state="normal")
        self.cancel_btn.configure(text="Not now")
        getattr(self, f"_page_{page}")()

    def _back(self) -> None:
        if self.history:
            self.show(self.history.pop(), push=False)

    def _next(self) -> None:
        getattr(self, f"_next_{self.page}", lambda: None)()

    # ------------------------------------------------------------------ pages
    def _page_intro(self) -> None:
        self.title.configure(text="Share clips with a link")
        self._text("Share links let you send a clip as a short link. Discord and X play it right in the chat.")
        self._text("They're optional. Without them, LCClips still records and saves every clip on your PC.",
                   "Muted.TLabel")
        self._text("Links run on your own free Cloudflare account (10 GB free; viewers watching your clips "
                   "cost nothing). Setup takes about 5 minutes, and this wizard walks you through it.")

    def _next_intro(self) -> None:
        self.show("account")

    def _page_account(self) -> None:
        self.title.configure(text="Create your free Cloudflare account")
        self._text("1. Sign up (or log in) at Cloudflare.")
        self._link_button("Open Cloudflare sign-up", cs.SIGNUP_URL)
        self._text("2. Turn on R2 storage (it's where your shared clips live). Open R2 and click "
                   "'Purchase R2 Plan' / 'Enable R2' and pick the free plan. Cloudflare asks for a card, "
                   "but you aren't charged under 10 GB.")
        self._link_button("Open R2 in Cloudflare", cs.R2_URL)
        self.r2_done = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.body, text="I've signed in and turned on R2", variable=self.r2_done,
                        command=lambda: self.next_btn.state(["!disabled"] if self.r2_done.get() else ["disabled"])
                        ).pack(anchor="w", pady=(4, 0))
        self.next_btn.state(["disabled"])

    def _next_account(self) -> None:
        self.show("token")

    def _page_token(self) -> None:
        self.title.configure(text="Give LCClips a Cloudflare API token")
        self._text("This lets LCClips set everything up for you. Click the button, check the permissions "
                   "below are there, then 'Continue to summary' → 'Create Token', and copy the token.")
        self._link_button("Create the token in Cloudflare", cs.TOKEN_URL)
        self._text(cs.PERMISSIONS_TEXT, "Muted.TLabel")
        self._text("Paste the token here:")
        self.token_var = tk.StringVar()
        e = ttk.Entry(self.body, textvariable=self.token_var, show="•", width=60)
        e.pack(anchor="w", fill="x")
        e.focus_set()
        self._text("LCClips only uses the token during setup and doesn't keep it.", "Muted.TLabel", pady=(6, 0))

    def _next_token(self) -> None:
        token = self.token_var.get().strip()
        if len(token) < 20:
            self._error(cs.CFError("That doesn't look like a full token - copy it again from Cloudflare."))
            return
        self.cf = cs.Cloudflare(token)
        self._busy(True, "Checking the token…")

        def work():
            self.cf.verify()
            return self.cf.accounts()

        def done(accts):
            self.accounts = accts
            if len(accts) == 1:
                self._choose(accts[0])
            else:
                self._busy(False)
                self.show("pick")

        self._bg(work, done, self._error)

    def _page_pick(self) -> None:
        self.title.configure(text="Which Cloudflare account?")
        self._text("This token can use more than one account. Pick the one for your clips:")
        self.acct_var = tk.StringVar(value=self.accounts[0].name)
        ttk.Combobox(self.body, values=[a.name for a in self.accounts], textvariable=self.acct_var,
                     state="readonly", width=50).pack(anchor="w")

    def _next_pick(self) -> None:
        self._choose(next(a for a in self.accounts if a.name == self.acct_var.get()))

    def _choose(self, acct: cs.Account) -> None:
        self.account = acct
        self._busy(True, "Looking at your account…")

        def done(sub):
            self._busy(False)
            self.has_subdomain = bool(sub)
            self.show("run" if sub else "name")

        self._bg(lambda: self.cf.subdomain(acct.id), done, self._error)

    def _page_name(self) -> None:
        self.title.configure(text="Pick your link address")
        self._text("Your share links will look like this. Pick a name (letters, numbers and dashes):")
        self.sub_var = tk.StringVar(value=cs.suggest_subdomain(self.account.name if self.account else "clips"))
        ttk.Entry(self.body, textvariable=self.sub_var, width=40).pack(anchor="w")
        self.preview = ttk.Label(self.body, text="", style="Muted.TLabel")
        self.preview.pack(anchor="w", pady=(8, 0))
        upd = lambda *_: self.preview.configure(  # noqa: E731
            text=f"https://lcclips.{self.sub_var.get().strip().lower() or '…'}.workers.dev/c/k3J9xQmP")
        self.sub_var.trace_add("write", upd)
        upd()
        self._text("You can't change this later without setting links up again.", "Muted.TLabel", pady=(8, 0))

    def _next_name(self) -> None:
        import re

        name = self.sub_var.get().strip().lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?", name):
            self._error(cs.CFError("Use 3-63 letters, numbers or dashes (not at the start or end)."))
            return
        self.subdomain = name
        self.show("run")

    def _page_run(self) -> None:
        self.title.configure(text="Setting up your share server")
        self.next_btn.configure(text="Working…", state="disabled")
        self.cancel_btn.state(["disabled"])
        self.lines: list[ttk.Label] = []

        def progress(msg: str):
            def ui():
                if self.lines:
                    self.lines[-1].configure(text="✔  " + self.lines[-1].cget("text")[3:], style="Success.TLabel")
                self.lines.append(self._text("…  " + msg, "TLabel", pady=(0, 4)))
            self.q.put(ui)

        def done(res):
            url, key, online = res
            if self.lines:
                self.lines[-1].configure(text="✔  " + self.lines[-1].cget("text")[3:], style="Success.TLabel")
            self.result = (url, key, online)
            self.on_done(url, key)
            self.show("done")

        def fail(e):
            self.cancel_btn.state(["!disabled"])
            self.cancel_btn.configure(text="Close")
            self.next_btn.configure(text="Try again", state="normal")
            self.back_btn.state(["!disabled"])
            self._error(e)

        sub = getattr(self, "subdomain", None) if not self.has_subdomain else None
        self._bg(lambda: cs.setup(self.cf, self.account, sub, progress), done, fail)

    def _next_run(self) -> None:
        self.show("run", push=False)

    def _page_done(self) -> None:
        url, _key, online = self.result
        self.title.configure(text="Share links are ready" if online else "Almost ready")
        self._text("Your share server:")
        self._text(url, "Heading.TLabel")
        if online:
            self._text("It's online and LCClips is connected. Select a clip and click Share - the link is "
                       "copied to your clipboard.")
        else:
            self._text("It's set up, but brand-new addresses can take a few minutes to come online. "
                       "LCClips is already configured - try Share in a few minutes.", "Muted.TLabel")
        self._text("Shared clips are deleted from your server after 30 days (change it in Settings).",
                   "Muted.TLabel")
        self.next_btn.configure(text="Finish", command=self.win.destroy)
        self.cancel_btn.pack_forget()
        self.back_btn.state(["disabled"])
