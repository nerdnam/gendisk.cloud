"""ncloud-sync GUI: 로그인 · 폴더 동기화 설정 · 드라이브 연결 · 백그라운드 동기화 루프.

tkinter(표준)로 창을 만들고, 백그라운드 스레드에서 주기적으로 동기화한다.
pystray가 있으면 시스템 트레이 아이콘으로도 동작한다(없으면 창만).
"""
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .client import ApiError, AuthError, NCloudClient
from .config import Config
from .engine import SyncEngine
from .webdav_mount import connect_drive, disconnect_drive


class SyncWorker(threading.Thread):
    """백그라운드 동기화 루프. enabled일 때 interval마다 run_once()."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def sync_now(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            cfg = self.app.cfg
            if cfg.enabled and cfg.is_ready():
                try:
                    client = NCloudClient(cfg.server_url, cfg.token)
                    engine = SyncEngine(client, cfg.space, cfg.local_folder,
                                        log=self.app.log)
                    self.app.set_status("동기화 중...")
                    summary = engine.run_once()
                    self.app.log(f"동기화 완료: {summary}")
                    self.app.set_status("대기 중 (마지막 동기화 성공)")
                except AuthError:
                    self.app.set_status("세션 만료 — 다시 로그인하세요")
                    self.app.log("세션이 만료되었습니다.")
                except (ApiError, OSError) as e:
                    self.app.set_status("동기화 오류")
                    self.app.log(f"오류: {e}")
            interval = max(5, self.app.cfg.interval_sec)
            self._wake.wait(timeout=interval)
            self._wake.clear()


class App:
    def __init__(self):
        self.cfg = Config.load()
        self.root = tk.Tk()
        self.root.title("ncloud-sync")
        self.root.geometry("520x560")
        self._build_ui()
        self.worker = SyncWorker(self)
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="서버 주소 (예: https://cloud.example.com)").pack(anchor="w")
        self.e_url = ttk.Entry(frm)
        self.e_url.pack(fill="x")
        self.e_url.insert(0, self.cfg.server_url)

        row = ttk.Frame(frm); row.pack(fill="x", pady=(6, 0))
        left = ttk.Frame(row); left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="아이디").pack(anchor="w")
        self.e_user = ttk.Entry(left); self.e_user.pack(fill="x")
        self.e_user.insert(0, self.cfg.username)
        right = ttk.Frame(row); right.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Label(right, text="비밀번호").pack(anchor="w")
        self.e_pw = ttk.Entry(right, show="•"); self.e_pw.pack(fill="x")

        self.btn_login = ttk.Button(frm, text="로그인", command=self._login)
        self.btn_login.pack(fill="x", pady=(6, 2))
        self.lbl_login = ttk.Label(frm, text=self._login_state_text(), foreground="#555")
        self.lbl_login.pack(anchor="w")

        ttk.Separator(frm).pack(fill="x", pady=8)

        ttk.Label(frm, text="동기화할 저장소").pack(anchor="w")
        self.cmb_space = ttk.Combobox(frm, state="readonly", values=["home"])
        self.cmb_space.set(self.cfg.space)
        self.cmb_space.pack(fill="x")

        ttk.Label(frm, text="로컬 폴더").pack(anchor="w", pady=(6, 0))
        frow = ttk.Frame(frm); frow.pack(fill="x")
        self.e_folder = ttk.Entry(frow); self.e_folder.pack(side="left", fill="x", expand=True)
        self.e_folder.insert(0, self.cfg.local_folder)
        ttk.Button(frow, text="찾아보기", command=self._pick_folder).pack(side="left", padx=(6, 0))

        irow = ttk.Frame(frm); irow.pack(fill="x", pady=(6, 0))
        ttk.Label(irow, text="동기화 주기(초)").pack(side="left")
        self.e_interval = ttk.Entry(irow, width=8); self.e_interval.pack(side="left", padx=(6, 0))
        self.e_interval.insert(0, str(self.cfg.interval_sec))

        self.var_enabled = tk.BooleanVar(value=self.cfg.enabled)
        ttk.Checkbutton(frm, text="자동 동기화 켜기", variable=self.var_enabled,
                        command=self._toggle_enabled).pack(anchor="w", pady=(6, 0))

        brow = ttk.Frame(frm); brow.pack(fill="x", pady=(6, 2))
        ttk.Button(brow, text="설정 저장", command=self._save).pack(side="left")
        ttk.Button(brow, text="지금 동기화", command=self._sync_now).pack(side="left", padx=(6, 0))

        ttk.Separator(frm).pack(fill="x", pady=8)
        ttk.Label(frm, text="일반 디스크처럼 사용 (WebDAV 네트워크 드라이브)").pack(anchor="w")
        drow = ttk.Frame(frm); drow.pack(fill="x", pady=(2, 0))
        ttk.Label(drow, text="드라이브 문자").pack(side="left")
        self.cmb_drive = ttk.Combobox(drow, state="readonly", width=5,
                                      values=[f"{c}:" for c in "NPQRSVWXYZ"])
        self.cmb_drive.set("N:"); self.cmb_drive.pack(side="left", padx=(6, 0))
        ttk.Button(drow, text="드라이브 연결", command=self._connect_drive).pack(side="left", padx=(6, 0))
        ttk.Button(drow, text="연결 해제", command=self._disconnect_drive).pack(side="left", padx=(6, 0))

        self.lbl_status = ttk.Label(frm, text="대기 중", foreground="#0a7")
        self.lbl_status.pack(anchor="w", pady=(8, 0))
        self.txt_log = tk.Text(frm, height=8, state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True, pady=(4, 0))

    def _login_state_text(self):
        return f"로그인됨: {self.cfg.username}" if self.cfg.token else "로그인 필요"

    # ---------- 동작 ----------
    def _client(self):
        return NCloudClient(self.e_url.get().strip(), self.cfg.token)

    def _login(self):
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pw.get()
        if not url or not user or not pw:
            messagebox.showwarning("입력 필요", "서버 주소·아이디·비밀번호를 모두 입력하세요.")
            return
        try:
            c = NCloudClient(url)
            c.login(user, pw)
            self.cfg.server_url = url
            self.cfg.username = user
            self.cfg.token = c.token
            self.cfg.save()
            self.lbl_login.config(text=self._login_state_text())
            self._refresh_spaces(c)
            self.log("로그인 성공")
        except AuthError as e:
            messagebox.showerror("로그인 실패", str(e))
        except (ApiError, OSError) as e:
            messagebox.showerror("연결 오류", str(e))

    def _refresh_spaces(self, client):
        try:
            spaces = [s["id"] for s in client.spaces()]
            self.cmb_space["values"] = spaces
            if self.cfg.space not in spaces:
                self.cmb_space.set(spaces[0] if spaces else "home")
        except Exception:
            pass

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.e_folder.delete(0, "end")
            self.e_folder.insert(0, d)

    def _collect(self):
        self.cfg.server_url = self.e_url.get().strip()
        self.cfg.space = self.cmb_space.get() or "home"
        self.cfg.local_folder = self.e_folder.get().strip()
        try:
            self.cfg.interval_sec = max(5, int(self.e_interval.get()))
        except ValueError:
            self.cfg.interval_sec = 30
        self.cfg.enabled = self.var_enabled.get()

    def _save(self):
        self._collect()
        self.cfg.save()
        self.log("설정을 저장했습니다.")

    def _toggle_enabled(self):
        self._collect()
        self.cfg.save()
        self.worker.sync_now()

    def _sync_now(self):
        self._collect()
        self.cfg.save()
        if not self.cfg.is_ready():
            messagebox.showwarning("설정 필요", "로그인하고 로컬 폴더를 지정하세요.")
            return
        self.worker.sync_now()

    def _connect_drive(self):
        self._collect()
        if not self.cfg.server_url or not self.cfg.username:
            messagebox.showwarning("설정 필요", "서버 주소와 아이디가 필요합니다.")
            return
        pw = self.e_pw.get()
        if not pw:
            messagebox.showwarning("비밀번호 필요", "드라이브 연결에는 비밀번호를 입력하세요.")
            return
        try:
            connect_drive(self.cmb_drive.get(), self.cfg.server_url,
                          self.cfg.username, pw)
            self.log(f"{self.cmb_drive.get()} 드라이브로 연결했습니다.")
        except Exception as e:
            messagebox.showerror("드라이브 연결 실패", str(e))

    def _disconnect_drive(self):
        try:
            disconnect_drive(self.cmb_drive.get())
            self.log(f"{self.cmb_drive.get()} 연결을 해제했습니다.")
        except Exception as e:
            messagebox.showerror("연결 해제 실패", str(e))

    # ---------- 상태/로그 (스레드 안전) ----------
    def set_status(self, text):
        self.root.after(0, lambda: self.lbl_status.config(text=text))

    def log(self, text):
        def _append():
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", text + "\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self.root.after(0, _append)

    def _on_close(self):
        self._collect()
        self.cfg.save()
        self.worker.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    App().run()
