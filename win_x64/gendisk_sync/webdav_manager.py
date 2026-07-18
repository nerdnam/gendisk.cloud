"""메인 창 안에서 WebDAV 접속 프로파일을 만들고 관리하는 화면(패널).

팝업 창(Toplevel)이 아니라 App 메인 창에 끼워 넣는 CTkFrame 이다. 여러 개의
WebDAV 서버 프로파일(이름·URL·자격증명·드라이브)을 생성/편집/삭제하고, 각각을
Windows 드라이브로 연결/해제한다. 목록↔편집 폼도 이 패널 안에서 전환되어, 로그인처럼
별도 창이 뜨지 않는다. 자격증명은 DPAPI 로 암호화해 config.json 에 저장한다.
"""
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from . import secret
from .client import webdav_preflight_url
from .webdav_mount import connect_url, disconnect_drive, webclient_running

ACCENT = ("#007AFF", "#0A84FF")
ACCENT_HOVER = ("#0063CC", "#3D9BFF")
DANGER = ("#C7362F", "#FF453A")
MUTED = ("gray45", "gray60")

# genDISK Drive/시스템과 겹치지 않도록 C: 이전 문자는 제외.
_DRIVE_LETTERS = [f"{ch}:" for ch in "DEFGHIJKLMNOPQRSTUVWXYZ"]


def _label(m: dict) -> str:
    return m.get("name") or m.get("url") or "(이름 없음)"


class WebDavPanel(ctk.CTkFrame):
    """메인 창에 표시되는 WebDAV 프로파일 관리 화면 (목록/편집을 안에서 전환)."""

    def __init__(self, master, cfg, log, on_close):
        super().__init__(master, fg_color="transparent")
        self.cfg = cfg
        self.log = log
        self.on_close = on_close
        self.font_title = ctk.CTkFont(family="Segoe UI", size=20, weight="bold")
        self.font_h = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.font_s = ctk.CTkFont(family="Segoe UI", size=12)
        self._build_shell()
        self.show_list()

    def _build_shell(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 4))
        ctk.CTkLabel(header, text="WebDAV 서버", font=self.font_title).pack(side="left")
        ctk.CTkButton(header, text="← 뒤로", width=88, command=self.on_close,
                      fg_color="transparent", border_width=1, text_color=MUTED,
                      hover_color=("gray90", "gray25")).pack(side="right")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    # ---------- 목록 화면 ----------
    def show_list(self):
        self._clear_body()
        bar = ctk.CTkFrame(self.body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            bar,
            text="여러 WebDAV 서버(NAS·Nextcloud 등) 프로파일을 만들고 관리합니다.\n"
                 "연결하면 탐색기에서 일반 폴더처럼 파일을 다룰 수 있어요.",
            font=self.font_s, text_color=MUTED, justify="left").pack(side="left")
        ctk.CTkButton(bar, text="+ 새 프로파일", width=124,
                      command=lambda: self.show_form(None),
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="right")
        lst = ctk.CTkScrollableFrame(self.body, fg_color=("gray92", "gray14"))
        lst.pack(fill="both", expand=True)
        mounts = self.cfg.webdav_mounts
        if not mounts:
            ctk.CTkLabel(lst, text="저장된 프로파일이 없습니다.\n오른쪽 위 '+ 새 프로파일'로 추가하세요.",
                         text_color=MUTED, justify="left").pack(anchor="w", padx=10, pady=16)
            return
        for i, m in enumerate(mounts):
            self._card(lst, i, m)

    def _card(self, parent, idx: int, m: dict):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.pack(fill="x", pady=6, padx=6)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(top, text=_label(m), font=self.font_h).pack(side="left")
        tag = m.get("drive", "")
        if m.get("auto"):
            tag += "  · 자동"
        ctk.CTkLabel(top, text=tag, text_color=MUTED, font=self.font_s).pack(side="right")
        ctk.CTkLabel(card, text=m.get("url", ""), text_color=MUTED, font=self.font_s,
                     anchor="w").pack(fill="x", padx=12)
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(6, 10))
        ctk.CTkButton(btns, text="연결", width=60, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda mm=m: self._connect(mm)).pack(side="left")
        ctk.CTkButton(btns, text="해제", width=60,
                      command=lambda mm=m: self._disconnect(mm)).pack(side="left", padx=(6, 0))
        ctk.CTkButton(btns, text="편집", width=56, fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25"),
                      command=lambda i=idx, mm=m: self.show_form((i, mm))).pack(side="left", padx=(6, 0))
        ctk.CTkButton(btns, text="삭제", width=56, fg_color="transparent", border_width=1,
                      text_color=DANGER, hover_color=("gray90", "gray25"),
                      command=lambda i=idx: self._delete(i)).pack(side="left", padx=(6, 0))

    # ---------- 편집/추가 폼 화면 (같은 패널 안에서 전환) ----------
    def show_form(self, existing):
        self._clear_body()
        idx = existing[0] if existing else None
        m = existing[1] if existing else {}
        ctk.CTkLabel(self.body, text=("프로파일 편집" if existing else "새 프로파일"),
                     font=self.font_h, anchor="w").pack(fill="x", pady=(0, 6))
        form = ctk.CTkFrame(self.body, corner_radius=12)
        form.pack(fill="x")
        pad = ctk.CTkFrame(form, fg_color="transparent")
        pad.pack(fill="x", padx=16, pady=14)

        def field(label, init="", show=None):
            ctk.CTkLabel(pad, text=label, font=self.font_s, text_color=MUTED,
                         anchor="w").pack(fill="x", pady=(6, 0))
            e = ctk.CTkEntry(pad, show=show)
            e.pack(fill="x")
            if init:
                e.insert(0, init)
            return e

        e_name = field("이름 (예: 회사 NAS)", m.get("name", ""))
        e_url = field("WebDAV 주소 (예: https://nas.example.com:5006/dav)", m.get("url", ""))
        e_user = field("아이디", m.get("username", ""))
        e_pw = field("비밀번호", secret.decrypt(m.get("password_enc", "")) or "", show="•")
        ctk.CTkLabel(pad, text="드라이브 문자", font=self.font_s, text_color=MUTED,
                     anchor="w").pack(fill="x", pady=(6, 0))
        cmb = ctk.CTkOptionMenu(pad, values=_DRIVE_LETTERS, width=100)
        cmb.set(m.get("drive", "W:") if m.get("drive", "W:") in _DRIVE_LETTERS else "W:")
        cmb.pack(anchor="w")
        var_auto = tk.BooleanVar(value=bool(m.get("auto")))
        ctk.CTkSwitch(pad, text="프로그램 시작 시 자동 연결", variable=var_auto).pack(
            anchor="w", pady=(12, 0))

        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(row, text="취소", width=90, command=self.show_list,
                      fg_color="transparent", border_width=1, text_color=MUTED,
                      hover_color=("gray90", "gray25")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row, text="저장", width=100, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._save_form(
                          idx, e_name, e_url, e_user, e_pw, cmb, var_auto)).pack(side="right")

    def _save_form(self, idx, e_name, e_url, e_user, e_pw, cmb, var_auto):
        url = e_url.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "WebDAV 주소를 입력하세요.", parent=self.winfo_toplevel())
            return
        if "://" not in url:
            url = "https://" + url            # 스킴 없으면 https (포트는 보존)
        if url.lower().startswith("http://") and not messagebox.askyesno(
                "보안 경고",
                "http(암호화 안 됨) 주소입니다. 비밀번호가 평문으로 전송될 수 있고\n"
                "Windows도 기본적으로 http WebDAV의 Basic 인증을 막습니다.\n\n계속 저장할까요?",
                parent=self.winfo_toplevel()):
            return
        entry = {
            "name": e_name.get().strip(),
            "url": url.rstrip("/"),
            "username": e_user.get().strip(),
            "password_enc": secret.encrypt(e_pw.get()) or "",
            "drive": cmb.get(),
            "auto": bool(var_auto.get()),
        }
        if idx is None:
            self.cfg.webdav_mounts.append(entry)
        else:
            self.cfg.webdav_mounts[idx] = entry
        self.cfg.save()
        self.show_list()

    def _delete(self, idx: int):
        m = self.cfg.webdav_mounts[idx]
        if not messagebox.askyesno(
                "삭제", f"'{_label(m)}' 프로파일을 삭제할까요?\n"
                        "(이미 연결된 드라이브는 자동 해제되지 않습니다)",
                parent=self.winfo_toplevel()):
            return
        del self.cfg.webdav_mounts[idx]
        self.cfg.save()
        self.show_list()

    # ---------- 연결/해제 (블로킹 호출은 워커 스레드에서) ----------
    def _connect(self, m: dict):
        url = m.get("url", "")
        user = m.get("username", "")
        drive = m.get("drive", "")
        pw = secret.decrypt(m.get("password_enc", "")) or ""
        if not url or not drive:
            messagebox.showwarning("정보 필요", "주소와 드라이브 문자가 필요합니다.",
                                   parent=self.winfo_toplevel())
            return

        def work():
            try:
                connect_url(drive, url, user, pw)
                self.log(f"[WebDAV] {drive} 에 '{_label(m)}' 연결")
                self._info("연결됨", f"{drive} 드라이브로 연결했습니다.\n탐색기에서 확인하세요.")
            except Exception as e:  # noqa: BLE001
                err = str(e)
                diag = ""
                try:
                    webdav_preflight_url(url, user, pw)
                except RuntimeError as pe:
                    diag = "\n\n▶ 서버 확인: " + str(pe)
                except Exception:
                    pass
                if not diag and not webclient_running():
                    diag = ("\n\n▶ Windows 'WebClient' 서비스가 꺼져 있을 수 있습니다.\n"
                            "   메인 화면의 'Windows WebClient 서비스 켜기'로 켠 뒤 다시 시도하세요.")
                self._error("연결 실패", err + diag)

        threading.Thread(target=work, daemon=True).start()

    def _disconnect(self, m: dict):
        drive = m.get("drive", "")

        def work():
            try:
                disconnect_drive(drive)
                self.log(f"[WebDAV] {drive} 연결 해제")
                self._info("해제", f"{drive} 연결을 해제했습니다.")
            except Exception as e:  # noqa: BLE001
                self._error("해제 실패", str(e))

        threading.Thread(target=work, daemon=True).start()

    # 결과 알림을 메인 스레드에서 안전하게 (패널이 살아있는 창일 때만).
    def _info(self, title, msg):
        self.after(0, lambda: self._box(messagebox.showinfo, title, msg))

    def _error(self, title, msg):
        self.after(0, lambda: self._box(messagebox.showerror, title, msg))

    def _box(self, fn, title, msg):
        try:
            fn(title, msg, parent=self.winfo_toplevel())
        except Exception:
            pass
