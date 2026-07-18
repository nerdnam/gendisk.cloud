"""일반(범용) WebDAV 서버를 Windows 드라이브로 연결·관리하는 대화상자.

genDISK 서버 전용 마운트와 별개로, 임의의 WebDAV 서버(NAS/Nextcloud/기타)를
전체 URL·자격증명·드라이브 문자로 저장해 탐색기 드라이브로 마운트한다.
Windows는 네이티브 WebDAV 리다이렉터가 있으므로, 드라이브로 연결하면 탐색기가
그대로 완전한 WebDAV 탐색기가 된다(복사·붙여넣기·열기·삭제·이름변경 모두 지원).

자격증명은 DPAPI(현재 Windows 사용자에 묶임)로 암호화해 config.json 에 저장한다.
"""
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from . import secret
from .client import webdav_preflight_url
from .icon import icon_path
from .webdav_mount import connect_url, disconnect_drive, webclient_running

ACCENT = ("#007AFF", "#0A84FF")
ACCENT_HOVER = ("#0063CC", "#3D9BFF")
DANGER = ("#C7362F", "#FF453A")
MUTED = ("gray45", "gray60")

# genDISK Drive/시스템과 겹치지 않도록 C: 이전 문자는 제외.
_DRIVE_LETTERS = [f"{ch}:" for ch in "DEFGHIJKLMNOPQRSTUVWXYZ"]


def _label(m: dict) -> str:
    return m.get("name") or m.get("url") or "(이름 없음)"


class WebDavManager(ctk.CTkToplevel):
    """저장된 일반 WebDAV 연결 목록 + 추가/편집/삭제/연결/해제."""

    def __init__(self, master, cfg, log):
        super().__init__(master)
        self._root = master          # 항상 살아있는 상위 창(App.root) — 결과 콜백 마셜링용
        self.cfg = cfg
        self.log = log
        self.title("일반 WebDAV 서버")
        self.geometry("580x540")
        self.minsize(520, 440)
        self.transient(master)
        self._apply_icon()
        self._build()
        self._reload()
        # customtkinter Toplevel 이 부모 뒤로 가는 경우가 있어 전면화.
        self.after(120, self._raise)

    def _apply_icon(self):
        def _set():
            try:
                self.iconbitmap(icon_path())
            except Exception:
                pass
        self.after(250, _set)

    def _raise(self):
        try:
            self.lift(); self.focus_force()
        except Exception:
            pass

    def _post(self, fn):
        """워커 스레드의 결과 콜백을 항상 살아있는 루트 이벤트루프로 넘긴다.
        (관리자 창이 그 사이 닫혀도 destroy 된 Toplevel 에 after 를 걸어 TclError 나지 않도록)"""
        try:
            self._root.after(0, fn)
        except Exception:
            pass

    def _parent(self):
        """messagebox 부모: 관리자 창이 아직 살아있으면 그 위, 닫혔으면 루트."""
        try:
            return self if self.winfo_exists() else self._root
        except Exception:
            return self._root

    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(16, 2))
        ctk.CTkLabel(head, text="일반 WebDAV 서버",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text="+ 새 연결", width=92, command=self._add,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="right")
        ctk.CTkLabel(
            self,
            text="NAS·Nextcloud 등 임의의 WebDAV 서버를 탐색기 드라이브로 연결합니다.\n"
                 "연결하면 탐색기에서 일반 폴더처럼 파일을 복사·열기·삭제할 수 있어요.",
            font=ctk.CTkFont(size=12), text_color=MUTED, justify="left").pack(
            fill="x", padx=16, pady=(4, 0))
        self.listbox = ctk.CTkScrollableFrame(self, fg_color=("gray92", "gray14"))
        self.listbox.pack(fill="both", expand=True, padx=16, pady=(10, 16))

    def _reload(self):
        for w in self.listbox.winfo_children():
            w.destroy()
        mounts = self.cfg.webdav_mounts
        if not mounts:
            ctk.CTkLabel(self.listbox,
                         text="저장된 연결이 없습니다.\n오른쪽 위 '+ 새 연결'로 추가하세요.",
                         text_color=MUTED, justify="left").pack(anchor="w", padx=8, pady=14)
            return
        for i, m in enumerate(mounts):
            self._row(i, m)

    def _row(self, idx: int, m: dict):
        card = ctk.CTkFrame(self.listbox, corner_radius=10)
        card.pack(fill="x", pady=6, padx=4)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(top, text=_label(m),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        tag = m.get("drive", "")
        if m.get("auto"):
            tag += "  · 자동"
        ctk.CTkLabel(top, text=tag, text_color=MUTED).pack(side="right")
        ctk.CTkLabel(card, text=m.get("url", ""), text_color=MUTED, anchor="w",
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=12)
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(6, 10))
        ctk.CTkButton(btns, text="연결", width=64, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER,
                      command=lambda mm=m: self._connect(mm)).pack(side="left")
        ctk.CTkButton(btns, text="해제", width=64,
                      command=lambda mm=m: self._disconnect(mm)).pack(side="left", padx=(6, 0))
        ctk.CTkButton(btns, text="편집", width=56, fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25"),
                      command=lambda i=idx, mm=m: self._edit(i, mm)).pack(side="left", padx=(6, 0))
        ctk.CTkButton(btns, text="삭제", width=56, fg_color="transparent", border_width=1,
                      text_color=DANGER, hover_color=("gray90", "gray25"),
                      command=lambda i=idx: self._delete(i)).pack(side="left", padx=(6, 0))

    # ---------- 추가/편집/삭제 ----------
    def _add(self):
        _WebDavEditDialog(self, None, self._on_saved)

    def _edit(self, idx: int, m: dict):
        _WebDavEditDialog(self, (idx, m), self._on_saved)

    def _on_saved(self, idx, entry: dict):
        if idx is None:
            self.cfg.webdav_mounts.append(entry)
        else:
            self.cfg.webdav_mounts[idx] = entry
        self.cfg.save()
        self._reload()

    def _delete(self, idx: int):
        m = self.cfg.webdav_mounts[idx]
        if not messagebox.askyesno(
                "삭제", f"'{_label(m)}' 연결을 목록에서 삭제할까요?\n"
                        "(이미 연결된 드라이브는 자동 해제되지 않습니다 — 필요하면 먼저 '해제')",
                parent=self):
            return
        del self.cfg.webdav_mounts[idx]
        self.cfg.save()
        self._reload()

    # ---------- 연결/해제 ----------
    def _connect(self, m: dict):
        url = m.get("url", "")
        user = m.get("username", "")
        drive = m.get("drive", "")
        pw = secret.decrypt(m.get("password_enc", "")) or ""
        if not url or not drive:
            messagebox.showwarning("정보 필요", "주소와 드라이브 문자가 필요합니다.", parent=self)
            return

        def work():
            try:
                connect_url(drive, url, user, pw)
                self.log(f"[WebDAV] {drive} 에 '{_label(m)}' 연결")
                self._post(lambda: messagebox.showinfo(
                    "연결됨", f"{drive} 드라이브로 연결했습니다.\n탐색기에서 확인하세요.",
                    parent=self._parent()))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                # 실패 진단: 서버 측(주소/인증/차단) 문제인지 로컬(WebClient) 문제인지 구분
                diag = ""
                try:
                    webdav_preflight_url(url, user, pw)
                except RuntimeError as pe:
                    diag = "\n\n▶ 서버 확인: " + str(pe)
                except Exception:
                    pass
                if not diag and not webclient_running():
                    diag = ("\n\n▶ Windows 'WebClient' 서비스가 꺼져 있을 수 있습니다.\n"
                            "   메인 창의 'Windows WebClient 서비스 켜기'로 켠 뒤 다시 시도하세요.")
                self._post(lambda: messagebox.showerror(
                    "연결 실패", err + diag, parent=self._parent()))

        threading.Thread(target=work, daemon=True).start()

    def _disconnect(self, m: dict):
        # WNetCancelConnection2W 는 죽은/응답없는 서버에서 오래 블록될 수 있으므로
        # 메인 스레드(UI)를 얼리지 않도록 워커에서 실행하고 결과만 마셜링한다.
        drive = m.get("drive", "")

        def work():
            try:
                disconnect_drive(drive)
                self.log(f"[WebDAV] {drive} 연결 해제")
                self._post(lambda: messagebox.showinfo(
                    "해제", f"{drive} 연결을 해제했습니다.", parent=self._parent()))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                self._post(lambda: messagebox.showerror(
                    "해제 실패", err, parent=self._parent()))

        threading.Thread(target=work, daemon=True).start()


class _WebDavEditDialog(ctk.CTkToplevel):
    """WebDAV 연결 한 건을 추가/편집하는 모달 폼."""

    def __init__(self, master, existing, on_save):
        super().__init__(master)
        self.on_save = on_save
        self.idx = existing[0] if existing else None
        m = existing[1] if existing else {}
        self.title("WebDAV 연결 편집" if existing else "새 WebDAV 연결")
        self.geometry("460x480")
        self.resizable(False, False)
        self.transient(master)

        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=22, pady=20)

        def field(label, init="", show=None):
            ctk.CTkLabel(pad, text=label, text_color=MUTED, anchor="w").pack(fill="x", pady=(8, 0))
            e = ctk.CTkEntry(pad, width=400, show=show)
            e.pack(fill="x")
            if init:
                e.insert(0, init)
            return e

        self.e_name = field("이름 (예: 회사 NAS)", m.get("name", ""))
        self.e_url = field("WebDAV 주소 (예: https://nas.example.com:5006/dav)", m.get("url", ""))
        self.e_user = field("아이디", m.get("username", ""))
        self.e_pw = field("비밀번호", secret.decrypt(m.get("password_enc", "")) or "", show="•")

        ctk.CTkLabel(pad, text="드라이브 문자", text_color=MUTED, anchor="w").pack(fill="x", pady=(8, 0))
        self.cmb_drive = ctk.CTkOptionMenu(pad, values=_DRIVE_LETTERS, width=100)
        self.cmb_drive.set(m.get("drive", "W:") if m.get("drive", "W:") in _DRIVE_LETTERS else "W:")
        self.cmb_drive.pack(anchor="w")

        self.var_auto = tk.BooleanVar(value=bool(m.get("auto")))
        ctk.CTkSwitch(pad, text="프로그램 시작 시 자동 연결", variable=self.var_auto).pack(
            anchor="w", pady=(12, 0))

        row = ctk.CTkFrame(pad, fg_color="transparent")
        row.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(row, text="저장", width=90, command=self._save,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="right")
        ctk.CTkButton(row, text="취소", width=80, command=self.destroy,
                      fg_color="transparent", border_width=1, text_color=MUTED,
                      hover_color=("gray90", "gray25")).pack(side="right", padx=(0, 8))

        self.after(120, self._raise)
        # 창이 뜬 뒤 모달로 잡는다 (grab_set 은 뷰가 보인 뒤 호출해야 안정적).
        self.after(200, lambda: self._safe_grab())

    def _raise(self):
        try:
            self.lift(); self.e_url.focus_set()
        except Exception:
            pass

    def _safe_grab(self):
        try:
            self.grab_set()
        except Exception:
            pass

    def _save(self):
        url = self.e_url.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "WebDAV 주소를 입력하세요.", parent=self)
            return
        if "://" not in url:
            url = "https://" + url            # 스킴 없으면 https (포트는 그대로 보존)
        # http(비암호화)면 비밀번호가 평문으로 전송될 수 있음을 경고하고 확인받는다.
        if url.lower().startswith("http://"):
            if not messagebox.askyesno(
                    "보안 경고",
                    "http(암호화 안 됨) 주소입니다. 비밀번호가 네트워크에 평문으로 전송될 수 있고,\n"
                    "Windows도 기본적으로 http WebDAV의 Basic 인증을 막습니다.\n\n"
                    "가능하면 https 를 쓰세요. 그래도 이대로 저장할까요?",
                    parent=self):
                return
        drive = self.cmb_drive.get()
        entry = {
            "name": self.e_name.get().strip(),
            "url": url.rstrip("/"),
            "username": self.e_user.get().strip(),
            "password_enc": secret.encrypt(self.e_pw.get()) or "",
            "drive": drive,
            "auto": bool(self.var_auto.get()),
        }
        self.on_save(self.idx, entry)
        self.destroy()
