"""gendisk-sync GUI: 로그인 · 폴더 동기화 · 드라이브 연결 · 시작 옵션.

customtkinter로 macOS 스타일(둥근 카드·토글 스위치·플랫 강조 버튼·시스템 다크/라이트)
창을 만든다. macOS 앱과 동일하게 **로그인 화면이 첫 화면**이고, 로그인하면 **설정 화면**으로
전환된다. 백그라운드 스레드에서 주기적으로 동기화하며, 설정에 따라 시작 시 자동 로그인 →
드라이브 자동 연결 → 자동 동기화까지 수행한다.
"""
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from urllib.parse import urlsplit

import customtkinter as ctk

from . import autostart, secret, single_instance
from .client import (
    ApiError, AuthError, GenDiskClient, webdav_preflight, webdav_preflight_url)
from .config import Config
from .drive import DriveController, TransferTracker
from .engine import SyncEngine
from .icon import icon_path, render_icon
from .webdav_manager import WebDavPanel
from .webdav_mount import (
    cleanup_stale_webdav, connect_drive, connect_url, disconnect_drive,
    start_webclient_elevated, webclient_running)

def _crash_log_path() -> str:
    import os
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "genDISK", "crash.log")


def _report_tk_exception(exc_type, exc, tb):
    """Tk 콜백 예외 기록 (창은 계속 살린다)."""
    import os
    import time
    import traceback
    try:
        path = _crash_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} [tk] =====\n")
            traceback.print_exception(exc_type, exc, tb, file=f)
    except OSError:
        pass


# macOS 시스템 강조색 (라이트/다크). accent 버튼에 사용.
ACCENT = ("#007AFF", "#0A84FF")
ACCENT_HOVER = ("#0063CC", "#3D9BFF")
SUCCESS = ("#1C8A3B", "#30D158")
DANGER = ("#C7362F", "#FF453A")
MUTED = ("gray45", "gray60")

# 화면 테마 선택(설정) ↔ customtkinter 모드 매핑
_THEME_LABELS = {"light": "라이트", "dark": "다크", "system": "자동"}
_THEME_MODES = {v: k for k, v in _THEME_LABELS.items()}

# 앱 시작 시 한 번만: 기본은 시스템 테마, macOS풍 파란 강조 테마 사용.
# (실제 적용 모드는 App.__init__ 에서 저장된 설정값으로 덮어쓴다.)
ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")


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
                    client = GenDiskClient(cfg.server_url, cfg.token)
                    engine = SyncEngine(client, cfg.space, cfg.local_folder, log=self.app.log)
                    self.app.set_status("동기화 중…", SUCCESS)
                    summary = engine.run_once()
                    self.app.log(f"동기화 완료: {summary}")
                    self.app.set_status("대기 중 (마지막 동기화 성공)", SUCCESS)
                except AuthError:
                    self.app.set_status("세션 만료 — 자동 재로그인 시도", DANGER)
                    self.app.try_relogin()
                except (ApiError, OSError) as e:
                    self.app.set_status("동기화 오류", DANGER)
                    self.app.log(f"오류: {e}")
            self._wake.wait(timeout=max(5, self.app.cfg.interval_sec))
            self._wake.clear()


class App:
    def __init__(self, startup: bool = False):
        self.cfg = Config.load()
        # 자동시작이 켜져 있으면 Run 키를 현재 exe(안정 사본) 경로로 자가 치유한다.
        # (바탕화면 exe 를 지우거나 이름을 바꿔도 자동시작이 계속 동작하도록)
        if self.cfg.auto_start:
            try:
                autostart.sync(True)
            except Exception:
                pass
        # 저장된 화면 테마(light/dark/system) 적용
        ctk.set_appearance_mode(self.cfg.appearance if self.cfg.appearance in _THEME_MODES.values() else "system")
        # 이번 세션의 비밀번호(WebDAV 드라이브 연결용). 저장 여부와 무관하게 메모리에 보관.
        self._pw = self.cfg.get_password()
        self.root = ctk.CTk()
        # Tk 콜백에서 난 예외도 crash.log 로 (기본은 콘솔로 새어 사라진다)
        try:
            self.root.report_callback_exception = _report_tk_exception
        except Exception:  # noqa: BLE001
            pass
        self.root.title("genDISK")
        self.root.geometry("1120x1020")  # 2열 배치 — 드라이브 옵션·상태 로그까지 스크롤 없이 보이는 높이
        self.root.minsize(980, 760)
        self._apply_window_icon()
        self.transfers = TransferTracker()      # 진행 중 전송(업로드) 추적 → 상태 패널
        self._build_ui()
        self.worker = SyncWorker(self)
        self.worker.start()
        self.drive = DriveController(self.cfg, on_reauth=self._drive_reauth,
                                     log=self.log, notify=self._drive_notify,
                                     progress=self._on_transfer)
        self.tray = None
        self._tray_notified = False
        self._build_tray()
        self._refresh_transfers()               # 전송 현황 주기 갱신 시작
        # 두 번째 실행이 신호를 보내면 이 창을 전면화한다(단일 인스턴스).
        # quit 신호(새 버전의 업그레이드 인계)를 받으면 조용히 종료해 자리를 내준다.
        try:
            single_instance.start_show_listener(
                self._bring_to_front,
                on_quit=lambda: self.root.after(0, self._real_quit))
        except Exception:
            pass
        # 온디맨드(Cloud Files) 방식은 제거됨 — FTP 드라이브(rclone)로 대체.
        # 구버전 설정에 남은 vfs_enabled 는 무시하고 끈다.
        self.cfg.vfs_enabled = False
        # FTP 세션 + 드라이브 켬이면 시작 시 자동 마운트 (자동 로그인과 무관하게)
        if (self.cfg.is_ftp_session() and self.cfg.ftp_drive_enabled
                and self.cfg.get_password() and not self.cfg.auto_login):
            self._mount_ftp_drive_async()
        # 저장된 일반 WebDAV 연결 중 '자동 연결' 항목을 시작 시 마운트 (genDISK 로그인과 무관)
        if any(m.get("auto") for m in self.cfg.webdav_mounts):
            threading.Thread(target=self._auto_connect_webdav_mounts, daemon=True).start()
        # GitHub 최신 릴리스 확인 → 새 버전이면 물어보고 자체 업데이트
        threading.Thread(target=self._check_update_async, daemon=True).start()
        # 닫기(X)는 트레이가 있으면 트레이로 숨기고, 없으면 그냥 종료
        self.root.protocol("WM_DELETE_WINDOW",
                           self._hide_to_tray if self.tray else self._real_quit)
        if startup:
            # 자동 시작이면 트레이만 남기고 창은 숨김 (트레이 없으면 최소화)
            (self._hide_to_tray if self.tray else self.root.iconify)()
        # 시작 시 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거
        if self.cfg.auto_login and self.cfg.username and self.cfg.get_password():
            threading.Thread(target=self._auto_sequence, daemon=True).start()

    # ---------- 창/트레이 아이콘 ----------
    def _apply_window_icon(self):
        """창(제목줄·작업표시줄) 아이콘을 genDISK 마크로. customtkinter 가 200ms 뒤
        기본 아이콘으로 덮으므로 그 이후에도 한 번 더 적용한다."""
        def _set():
            try:
                self.root.iconbitmap(icon_path())
            except Exception:
                pass
        _set()
        self.root.after(300, _set)

    # ---------- 시스템 트레이 ----------
    def _build_tray(self):
        try:
            import pystray
        except Exception:
            return  # pystray 없으면 트레이 비활성 (닫기 = 종료)
        img = render_icon(64)   # 안드로이드 앱과 동일한 마크
        menu = pystray.Menu(
            pystray.MenuItem("열기", self._tray_show, default=True),
            pystray.MenuItem("지금 동기화", lambda i, it: self.root.after(0, self._sync_now)),
            pystray.MenuItem("종료", self._tray_quit),
        )
        try:
            self.tray = pystray.Icon("gendisk-sync", img, "genDISK Sync", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def _hide_to_tray(self):
        self._collect(); self.cfg.save()
        self.root.withdraw()
        if not self._tray_notified and self.tray is not None:
            self._tray_notified = True
            try:
                self.tray.notify("트레이에서 계속 실행됩니다. 아이콘을 눌러 다시 열 수 있어요.",
                                 "genDISK Sync")
            except Exception:
                pass

    def _tray_show(self, icon=None, item=None):
        self._bring_to_front()

    def _bring_to_front(self):
        """창을 복원·전면화한다 (트레이 클릭 / 두 번째 실행 신호). 스레드 안전."""
        def _show():
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
                self.root.focus_force()
            except Exception:
                pass
        self.root.after(0, _show)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._real_quit)

    def _real_quit(self):
        self._collect(); self.cfg.save()
        try:
            single_instance.cleanup()
        except Exception:
            pass
        try:
            self.drive.stop()   # provider 연결만 해제(노드/싱크루트는 유지 → 다음 실행 시 재연결)
        except Exception:
            pass
        # 종료 시 탐색기 썸네일 설정을 원래대로 — 앱이 없으면 되돌릴 UI 도 없어서
        # 시스템 전역 설정이 꺼진 채로 남아버린다. (마운트는 유지: 드라이브는 계속 쓸 수 있게)
        try:
            from . import explorer_preview
            explorer_preview.restore()
        except Exception:
            pass
        self.worker.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------- UI 공통 ----------
    def _card(self, parent, title):
        """제목이 붙은 둥근 카드. 내용을 담을 안쪽 프레임을 돌려준다."""
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 14))
        if title:
            ctk.CTkLabel(card, text=title, font=self.font_h, anchor="w").pack(
                fill="x", padx=16, pady=(12, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        return inner

    def _field_label(self, parent, text, **kw):
        return ctk.CTkLabel(parent, text=text, font=self.font_s, text_color=MUTED,
                            anchor="w", **kw)

    def _logo(self, parent):
        """헤더 로고: genDISK G 마크 아이콘 + 'genDISK' 텍스트 (안드로이드 앱과 동일)."""
        img = render_icon(64)  # 고해상도로 렌더 → CTkImage 가 표시 크기로 축소(HiDPI 대응)
        logo = ctk.CTkImage(light_image=img, dark_image=img, size=(28, 28))
        return ctk.CTkLabel(parent, image=logo, text="  genDISK",
                            font=self.font_title, compound="left")

    def _build_ui(self):
        self.font_title = ctk.CTkFont(family="Segoe UI", size=22, weight="bold")
        self.font_h = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self.font_s = ctk.CTkFont(family="Segoe UI", size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        self.container = ctk.CTkFrame(self.root, fg_color="transparent")
        self.container.pack(fill="both", expand=True)

        self.login_frame = self._build_login(self.container)
        self.settings_frame = self._build_settings(self.container)
        # WebDAV 프로파일 관리 화면 — 팝업이 아니라 메인 창 안에서 전환된다.
        self.webdav_frame = WebDavPanel(
            self.container, self.cfg, self.log, on_close=self._close_webdav)

        # macOS 앱과 동일: 로그인돼 있으면 설정 화면, 아니면 로그인 화면부터.
        if self.cfg.token or self.cfg.is_ftp_session():
            self._show_settings()
            self._refresh_spaces_async()   # 저장된 토큰으로 시작 시 저장소 목록 채우기
        else:
            self._show_login()

    def _show_login(self):
        self.settings_frame.pack_forget()
        self.webdav_frame.pack_forget()
        if getattr(self, "_login_mode", "genDISK") == "WebDAV":
            self._refresh_login_profiles()   # 다시 돌아왔을 때 최신 프로파일 반영
        self.login_frame.pack(fill="both", expand=True)

    def _show_settings(self):
        self.login_frame.pack_forget()
        self.webdav_frame.pack_forget()
        self._refresh_account_labels()
        self.settings_frame.pack(fill="both", expand=True)

    def _show_webdav(self):
        """WebDAV 프로파일 관리 화면으로 전환 (메인 창 안에서)."""
        self.login_frame.pack_forget()
        self.settings_frame.pack_forget()
        self.webdav_frame.show_list()      # 최신 목록으로 갱신
        self.webdav_frame.pack(fill="both", expand=True)

    def _close_webdav(self):
        """WebDAV 화면에서 '뒤로' — 로그인돼 있으면 설정, 아니면 로그인 화면으로."""
        self.webdav_frame.pack_forget()
        if self.cfg.token or self.cfg.is_ftp_session():
            self._show_settings()
        else:
            self._show_login()

    # ---------- 로그인 화면 ----------
    def _build_login(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        # 가운데 정렬 행: [로그인 카드] + (WebDAV 모드에서만) [프로파일 선택 패널]
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.place(relx=0.5, rely=0.5, anchor="center")
        card = ctk.CTkFrame(row, corner_radius=16)
        card.pack(side="left")
        pad = ctk.CTkFrame(card, fg_color="transparent")
        pad.pack(padx=32, pady=28)

        self._logo(pad).pack()
        self.lbl_login_subtitle = ctk.CTkLabel(
            pad, text="로그인하고 파일을 동기화·연결하세요",
            font=self.font_s, text_color=MUTED)
        self.lbl_login_subtitle.pack(pady=(2, 14))

        # 접속 모드 토글 (안드로이드와 동일): genDISK 계정 로그인 / 일반 WebDAV 드라이브 연결
        self._login_mode = "genDISK"
        self.seg_login_mode = ctk.CTkSegmentedButton(
            pad, width=320, values=["genDISK", "WebDAV"], command=self._on_login_mode)
        self.seg_login_mode.set("genDISK")
        self.seg_login_mode.pack(pady=(0, 14))

        self.e_url = ctk.CTkEntry(pad, width=320,
                                  placeholder_text="서버 주소 (예: ftp.example.com:2121)")
        self.e_url.pack(pady=4)
        if self.cfg.server_url:  # 빈 값 insert는 placeholder를 없애므로 값이 있을 때만
            self.e_url.insert(0, self.cfg.server_url)
        self.e_user = ctk.CTkEntry(pad, width=320, placeholder_text="아이디")
        self.e_user.pack(pady=4)
        if self.cfg.username:
            self.e_user.insert(0, self.cfg.username)
        self.e_pw = ctk.CTkEntry(pad, width=320, show="•", placeholder_text="비밀번호")
        self.e_pw.pack(pady=4)
        if self.cfg.get_password():
            self.e_pw.insert(0, self.cfg.get_password())
        self.e_pw.bind("<Return>", lambda e: self._login_submit())

        # WebDAV 모드에서만 보이는 드라이브 문자 선택 (기본 숨김)
        self.frm_login_drive = ctk.CTkFrame(pad, fg_color="transparent")
        self._field_label(self.frm_login_drive, "드라이브 문자").pack(side="left")
        self.cmb_login_drive = ctk.CTkOptionMenu(
            self.frm_login_drive, width=90,
            values=[f"{ch}:" for ch in "DEFGHIJKLMNOPQRSTUVWXYZ"])
        self.cmb_login_drive.set("W:")
        self.cmb_login_drive.pack(side="left", padx=(10, 0))

        self.var_savecred = tk.BooleanVar(value=self.cfg.save_credentials)
        self.sw_savecred = ctk.CTkSwitch(pad, text="로그인 정보 저장 (암호화)",
                                         variable=self.var_savecred)
        self.sw_savecred.pack(anchor="w", pady=(10, 0))

        self.lbl_login_error = ctk.CTkLabel(pad, text="", font=self.font_s,
                                            text_color=DANGER, wraplength=320)
        self.lbl_login_error.pack(pady=(8, 0))

        self.btn_login = ctk.CTkButton(pad, text="로그인", width=320,
                                       command=self._login_submit,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.btn_login.pack(pady=(8, 0))

        # 오른쪽 프로파일 선택 패널 (WebDAV 모드에서만 표시 — _on_login_mode 에서 pack).
        self.frm_login_profiles = ctk.CTkFrame(row, corner_radius=16)
        return frame

    def _on_login_mode(self, mode):
        """로그인 화면 모드 전환(genDISK ↔ WebDAV)."""
        self._login_mode = mode
        self.lbl_login_error.configure(text="")
        cur = self.e_url.get().strip()
        if mode == "WebDAV":
            # genDISK 주소가 그대로 프리필돼 있으면 비워서 WebDAV placeholder 안내가 보이게 한다.
            if cur == (self.cfg.server_url or ""):
                self.e_url.delete(0, "end")
            self.lbl_login_subtitle.configure(text="WebDAV 서버를 드라이브로 연결합니다")
            self.e_url.configure(placeholder_text="WebDAV 주소 (예: https://호스트:포트/dav)")
            self.frm_login_drive.pack(fill="x", pady=(8, 0), before=self.sw_savecred)
            self.btn_login.configure(text="연결")
            self._refresh_login_profiles()
            self.frm_login_profiles.pack(side="left", padx=(16, 0), fill="y")
        else:
            # WebDAV 로 비웠던 경우 genDISK 주소를 되살린다.
            if not cur and self.cfg.server_url:
                self.e_url.insert(0, self.cfg.server_url)
            self.lbl_login_subtitle.configure(text="로그인하고 파일을 동기화·연결하세요")
            self.e_url.configure(placeholder_text="서버 주소 (예: ftp.example.com:2121)")
            self.frm_login_drive.pack_forget()
            self.frm_login_profiles.pack_forget()
            self.btn_login.configure(text="로그인")

    def _refresh_login_profiles(self):
        """오른쪽 패널: 저장된 WebDAV 프로파일 목록 + 관리(추가·편집) 진입. 로그인 불필요."""
        for w in self.frm_login_profiles.winfo_children():
            w.destroy()
        pad = ctk.CTkFrame(self.frm_login_profiles, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=18, pady=22)
        ctk.CTkLabel(pad, text="저장된 프로파일", font=self.font_h).pack(anchor="w")
        ctk.CTkLabel(pad, text="클릭하면 왼쪽 폼에 채워집니다.", font=self.font_s,
                     text_color=MUTED).pack(anchor="w", pady=(0, 8))
        # 로그인하지 않아도 프로파일 추가·편집·삭제 화면으로 바로 진입한다.
        ctk.CTkButton(pad, text="＋ 프로파일 추가·편집", command=self._show_webdav,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(fill="x", pady=(0, 10))
        mounts = self.cfg.webdav_mounts
        if not mounts:
            ctk.CTkLabel(pad, text="저장된 프로파일이 없습니다.\n위 '추가·편집'에서 만들 수 있어요.",
                         font=self.font_s, text_color=MUTED, justify="left").pack(anchor="w")
            return
        lst = ctk.CTkScrollableFrame(pad, fg_color="transparent", width=260, height=340)
        lst.pack(fill="both", expand=True)
        for m in mounts:
            name = m.get("name") or m.get("url") or "(이름 없음)"
            sub = f"{m.get('drive', '')}   {m.get('url', '')}"
            ctk.CTkButton(
                lst, text=f"{name}\n{sub}", anchor="w", height=50, font=self.font_s,
                fg_color=("gray92", "gray20"), text_color=("gray10", "gray90"),
                hover_color=("gray85", "gray28"),
                command=lambda mm=m: self._fill_login_from_profile(mm)).pack(fill="x", pady=4)

    def _fill_login_from_profile(self, m):
        """선택한 프로파일 값으로 로그인 폼을 채운다 (검토 후 '연결')."""
        self.e_url.delete(0, "end"); self.e_url.insert(0, m.get("url", ""))
        self.e_user.delete(0, "end"); self.e_user.insert(0, m.get("username", ""))
        pw = secret.decrypt(m.get("password_enc", "")) or ""
        self.e_pw.delete(0, "end")
        if pw:
            self.e_pw.insert(0, pw)
        drive = m.get("drive", "")
        if drive:
            self.cmb_login_drive.set(drive)
        self.lbl_login_error.configure(text="")

    def _login_submit(self):
        """기본 버튼/Enter 처리 — 로그인 화면 모드에 따라 분기."""
        if self._login_mode == "WebDAV":
            self._webdav_login()
        else:
            self._login()

    # ---------- 설정 화면 (로그인 후) ----------
    def _build_settings(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 6))
        self._logo(header).pack(side="left")
        ctk.CTkButton(header, text="로그아웃", width=88, command=self._logout,
                      fg_color="transparent", border_width=1,
                      text_color=DANGER, hover_color=("gray90", "gray25")).pack(side="right")

        # 스크롤 없는 일반 프레임 — 창 높이가 콘텐츠에 맞춰 스크롤바가 안 나온다.
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body.grid_columnconfigure(0, weight=1, uniform="col")
        body.grid_columnconfigure(1, weight=1, uniform="col")
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="new", padx=(0, 8))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="new", padx=(8, 0))

        # ══════ 왼쪽 열 ══════
        # ── 계정 ──
        c = self._card(left, "계정")
        self.lbl_acc_server = self._field_label(c, "")
        self.lbl_acc_server.pack(fill="x")
        self.lbl_acc_user = self._field_label(c, "")
        self.lbl_acc_user.pack(fill="x", pady=(2, 0))

        # ── 동기화 ──
        c = self._card(left, "동기화")
        self._field_label(c, "동기화할 저장소").pack(fill="x")
        self.cmb_space = ctk.CTkOptionMenu(c, values=["home"])
        self.cmb_space.set(self.cfg.space); self.cmb_space.pack(fill="x", pady=(2, 10))

        self._field_label(c, "로컬 폴더").pack(fill="x")
        frow = ctk.CTkFrame(c, fg_color="transparent"); frow.pack(fill="x", pady=(2, 10))
        self.e_folder = ctk.CTkEntry(frow, placeholder_text="C:\\Users\\…")
        self.e_folder.pack(side="left", fill="x", expand=True)
        if self.cfg.local_folder:
            self.e_folder.insert(0, self.cfg.local_folder)
        ctk.CTkButton(frow, text="찾아보기", width=90, command=self._pick_folder).pack(
            side="left", padx=(8, 0))

        irow = ctk.CTkFrame(c, fg_color="transparent"); irow.pack(fill="x")
        self._field_label(irow, "동기화 주기(초)").pack(side="left")
        self.e_interval = ctk.CTkEntry(irow, width=90)
        self.e_interval.pack(side="left", padx=(10, 0))
        self.e_interval.insert(0, str(self.cfg.interval_sec))

        self.var_enabled = tk.BooleanVar(value=self.cfg.enabled)
        ctk.CTkSwitch(c, text="자동 동기화 켜기", variable=self.var_enabled,
                      command=self._toggle_enabled).pack(anchor="w", pady=(12, 8))

        brow = ctk.CTkFrame(c, fg_color="transparent"); brow.pack(fill="x")
        ctk.CTkButton(brow, text="설정 저장", width=110, command=self._save).pack(side="left")
        ctk.CTkButton(brow, text="지금 동기화", width=110, command=self._sync_now,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=(8, 0))

        # ── 드라이브 연결 (WebDAV) ──
        c = self._card(left, "드라이브 연결 (WebDAV)")
        self._field_label(c, "일반 디스크처럼 사용 — 파일 탐색기에 드라이브로 연결합니다.").pack(fill="x")
        drow = ctk.CTkFrame(c, fg_color="transparent"); drow.pack(fill="x", pady=(8, 8))
        self._field_label(drow, "드라이브 문자").pack(side="left")
        self.cmb_drive = ctk.CTkOptionMenu(drow, width=80,
                                           values=[f"{ch}:" for ch in "NPQRSTVWXYZ"])
        self.cmb_drive.set(self.cfg.drive_letter); self.cmb_drive.pack(side="left", padx=(10, 0))
        ctk.CTkButton(drow, text="연결", width=70, command=self._connect_drive,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=(8, 0))
        ctk.CTkButton(drow, text="해제", width=70, command=self._disconnect_drive).pack(
            side="left", padx=(8, 0))
        ctk.CTkButton(c, text="Windows WebClient 서비스 켜기", command=self._start_webclient,
                      fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25")).pack(fill="x")
        ctk.CTkButton(c, text="🧹 끊긴 WebDAV 연결 정리", command=self._cleanup_webdav,
                      fg_color="transparent", border_width=1,
                      text_color=MUTED, hover_color=("gray90", "gray25")).pack(fill="x", pady=(6, 0))
        ctk.CTkButton(c, text="🌐 일반 WebDAV 서버 관리…", command=self._show_webdav,
                      fg_color="transparent", border_width=1,
                      text_color=ACCENT, hover_color=("gray90", "gray25")).pack(fill="x", pady=(6, 0))

        # ══════ 오른쪽 열 ══════
        # ── 시작 옵션 ──
        c = self._card(right, "시작 옵션")
        self.var_autostart = tk.BooleanVar(value=self.cfg.auto_start)
        self.var_autologin = tk.BooleanVar(value=self.cfg.auto_login)
        self.var_autodrive = tk.BooleanVar(value=self.cfg.auto_connect_drive)
        ctk.CTkSwitch(c, text="Windows 시작 시 자동 실행", variable=self.var_autostart,
                      command=self._apply_autostart).pack(anchor="w", pady=6)
        ctk.CTkSwitch(c, text="프로그램 시작 시 자동 로그인", variable=self.var_autologin).pack(
            anchor="w", pady=6)
        ctk.CTkSwitch(c, text="자동 로그인 후 드라이브 자동 연결", variable=self.var_autodrive).pack(
            anchor="w", pady=6)

        # ── 화면 테마 ──
        c = self._card(right, "화면 테마")
        self.seg_theme = ctk.CTkSegmentedButton(
            c, values=list(_THEME_LABELS.values()), command=self._on_theme_change)
        self.seg_theme.set(_THEME_LABELS.get(self.cfg.appearance, "자동"))
        self.seg_theme.pack(fill="x")

        # ── FTP 드라이브 (rclone + WinFsp) — 로컬 저장 없음 ──
        c = self._card(right, "genDISK 드라이브 (FTP 직결)")
        self._field_label(
            c, "탐색기 사이드바의 genDISK Drive 로 서버에 바로 연결합니다.\n"
               "드라이브 문자를 만들지 않고, 목록·파일도 컴퓨터에 저장하지 않습니다\n"
               "(WinFsp·rclone 필요).").pack(fill="x")
        self.var_ftp_drive = tk.BooleanVar(value=getattr(self.cfg, "ftp_drive_enabled", True))
        ctk.CTkSwitch(c, text="genDISK Drive 연결 (사이드바)", variable=self.var_ftp_drive,
                      command=self._toggle_ftp_drive).pack(anchor="w", pady=(8, 0))
        self.var_no_preview = tk.BooleanVar(value=getattr(self.cfg, "ftp_no_preview", True))
        ctk.CTkSwitch(c, text="탐색기 미리 보기·썸네일 끄기 (서버 부하 감소)",
                      variable=self.var_no_preview,
                      command=self._toggle_no_preview).pack(anchor="w", pady=(8, 0))
        self._field_label(
            c, "탐색기는 썸네일을 만들려고 폴더 안 파일을 통째로 내려받습니다.\n"
               "끄면 그 트래픽이 사라집니다. (탐색기 전역 설정이며 해제 시 복원됩니다)").pack(
            fill="x", pady=(2, 0))
        frow = ctk.CTkFrame(c, fg_color="transparent")
        frow.pack(fill="x", pady=(4, 0))
        self._field_label(frow, "FTP 호스트").pack(side="left")
        self.e_ftp_host = ctk.CTkEntry(frow, width=180,
                                       placeholder_text="예: ftp.example.com")
        if getattr(self.cfg, "ftp_host", ""):
            self.e_ftp_host.insert(0, self.cfg.ftp_host)
        self.e_ftp_host.pack(side="left", padx=(8, 0))
        self._field_label(frow, "포트").pack(side="left", padx=(10, 0))
        self.e_ftp_port = ctk.CTkEntry(frow, width=64)
        self.e_ftp_port.insert(0, str(getattr(self.cfg, "ftp_port", 2121)))
        self.e_ftp_port.pack(side="left", padx=(8, 0))

        # ── 상태 & 로그 ──
        c = self._card(right, "상태")
        self.lbl_status = ctk.CTkLabel(c, text="대기 중", font=self.font_s,
                                       text_color=MUTED, anchor="w")
        self.lbl_status.pack(fill="x", pady=(0, 6))
        # 전송 현황 (FTP식 파일별 진행률·속도)
        ctk.CTkLabel(c, text="전송 현황", font=self.font_s, text_color=MUTED,
                     anchor="w").pack(fill="x")
        self.txt_transfers = ctk.CTkTextbox(c, height=76, wrap="none", font=self.font_mono)
        self.txt_transfers.pack(fill="x", pady=(2, 8))
        self.txt_transfers.configure(state="disabled")
        self.txt_log = ctk.CTkTextbox(c, height=150, wrap="word", font=self.font_mono)
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.configure(state="disabled")
        return frame

    def _refresh_account_labels(self):
        self.lbl_acc_server.configure(text=f"서버   {self.cfg.server_url or '-'}")
        self.lbl_acc_user.configure(text=f"아이디   {self.cfg.username or '-'}")

    # ---------- 동작 ----------
    def _login(self):
        """genDISK 접속 — 높은 호환성을 위해 **FTP 프로토콜만** 사용한다.
        주소는 ftp:// 스킴·포트를 생략해도 되고(기본 2121, 실패 시 21),
        https:// 를 넣어도 호스트만 뽑아 FTP 로 접속한다."""
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pw.get()
        if not url or not user or not pw:
            self.lbl_login_error.configure(text="서버 주소·아이디·비밀번호를 모두 입력하세요.")
            return
        raw = url.split("://", 1)[-1].split("/", 1)[0]   # 스킴·경로 제거 → host[:port]
        host, _, port_s = raw.partition(":")
        if not host:
            self.lbl_login_error.configure(text="서버 주소를 확인하세요.")
            return
        try:
            port = int(port_s) if port_s else None       # None = 기본 포트(2121→21) 시도
        except ValueError:
            self.lbl_login_error.configure(text="포트 번호가 올바르지 않습니다.")
            return
        self._ftp_login(host, port, user, pw)

    def _ftp_login(self, host: str, port: int | None, user: str, pw: str):
        """FTP 로그인. 성공하면 FTP 세션으로 전환 — 드라이브(탐색기 사이드바)는
        FTP 전송으로 동작하고, 동기화·WebDAV 등 API 전용 기능은 쉰다."""
        if getattr(self, "_login_busy", False):
            return                     # Enter 연타 등으로 로그인이 겹치면 마운트가 경쟁한다
        self._login_busy = True
        self.lbl_login_error.configure(text="")
        self.btn_login.configure(state="disabled", text="FTP 연결 중…")

        def unlock():
            """어떤 경로로 끝나든 버튼을 되살린다 — 안 그러면 재시작 전까지 로그인 불가."""
            self._login_busy = False
            try:
                self.btn_login.configure(state="normal", text="로그인")
            except Exception:  # noqa: BLE001 (창이 닫히는 중이면 무시)
                pass

        def work():
            nonlocal port
            spaces = None
            last_err = None
            try:
                from .ftp_client import FtpDriveClient
                ports = [port] if port else [2121, 21]
                for p in ports:
                    try:
                        c = FtpDriveClient(host, p, user, pw)
                        spaces = c.spaces()
                        c.close()
                        port = p
                        break
                    except AuthError as e:
                        last_err = e                # 연결은 됐고 비밀번호가 틀림 — 즉시 중단
                        break
                    except Exception as e:  # noqa: BLE001
                        last_err = e
            except BaseException as e:              # noqa: BLE001 (import 실패 등)
                last_err = e
            if spaces is None:
                self.root.after(0, lambda: (
                    unlock(),
                    self.lbl_login_error.configure(text=str(last_err))))
                return

            def done():
                unlock()
                cfg = self.cfg
                # 순서 주의: 드라이브 스레드가 설정을 실시간으로 읽으므로, 전송 방식을
                # 먼저 FTP 로 바꾼 뒤 토큰을 비운다 (그 사이 API 경로로 새는 창 차단).
                cfg.vfs_transport = "ftp"
                cfg.ftp_host = host
                cfg.ftp_port = port
                cfg.username = user
                cfg.server_url = f"ftp://{host}:{port}"
                cfg.token = ""                     # API 세션 없음 (FTP 전용)
                self._pw = pw
                cfg.save_credentials = self.var_savecred.get()
                if cfg.save_credentials:
                    cfg.set_password(pw)
                else:
                    cfg.clear_password()
                cfg.save()
                ids = [s["id"] for s in spaces]
                self.cmb_space.configure(values=ids or ["home"])
                if cfg.space not in ids:
                    self.cmb_space.set(ids[0] if ids else "home")
                self.e_ftp_host.delete(0, "end"); self.e_ftp_host.insert(0, host)
                self.e_ftp_port.delete(0, "end"); self.e_ftp_port.insert(0, str(port))
                self._show_settings()
                self.log(f"FTP 로그인 성공 ({host}:{port})")
                if cfg.ftp_drive_enabled:
                    self._mount_ftp_drive_async()
            self.root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _webdav_login(self):
        """로그인 화면 WebDAV 모드: 임의 WebDAV 서버를 드라이브로 연결한다.
        성공하면 연결을 목록에 저장하고 관리 창을 연다 (탐색기가 곧 파일 브라우저)."""
        url = self.e_url.get().strip()
        user = self.e_user.get().strip()
        pw = self.e_pw.get()
        drive = self.cmb_login_drive.get()
        if not url or not user or not pw:
            self.lbl_login_error.configure(text="주소·아이디·비밀번호를 모두 입력하세요.")
            return
        if "://" not in url:
            url = "https://" + url
        url = url.rstrip("/")
        if url.lower().startswith("http://") and not messagebox.askyesno(
                "보안 경고",
                "http(암호화 안 됨) 주소입니다. 비밀번호가 평문으로 전송될 수 있고\n"
                "Windows도 기본적으로 http WebDAV의 Basic 인증을 막습니다.\n\n계속할까요?"):
            return
        self.lbl_login_error.configure(text="")
        self.cfg.save_credentials = self.var_savecred.get()
        self.btn_login.configure(state="disabled", text="연결 중…")

        def work():
            try:
                connect_url(drive, url, user, pw)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                diag = ""
                try:
                    webdav_preflight_url(url, user, pw)
                except RuntimeError as pe:
                    diag = "\n" + str(pe)
                except Exception:
                    pass
                if not diag and not webclient_running():
                    diag = "\nWindows 'WebClient' 서비스가 꺼져 있을 수 있습니다."
                self.root.after(0, lambda: self._webdav_login_done(
                    False, err + diag, url, user, pw, drive))
                return
            self.root.after(0, lambda: self._webdav_login_done(True, "", url, user, pw, drive))

        threading.Thread(target=work, daemon=True).start()

    def _webdav_login_done(self, ok, err, url, user, pw, drive):
        self.btn_login.configure(state="normal", text="연결")
        if not ok:
            self.lbl_login_error.configure(text=err)
            return
        self._save_webdav_mount(url, user, pw, drive)
        self.log(f"[WebDAV] {drive} 드라이브로 연결했습니다 (탐색기에서 확인): {url}")
        # 팝업 없이 메인 창을 WebDAV 관리 화면으로 전환 — 전환 자체가 성공 신호다.
        self._show_webdav()

    def _save_webdav_mount(self, url, user, pw, drive):
        """방금 연결한 WebDAV 를 저장 목록에 추가/갱신(같은 url+drive 는 갱신)."""
        from . import secret
        enc = secret.encrypt(pw) or "" if self.var_savecred.get() else ""
        name = urlsplit(url).hostname or url
        for m in self.cfg.webdav_mounts:
            if m.get("url") == url and m.get("drive") == drive:
                m["username"] = user
                m["password_enc"] = enc
                if not m.get("name"):
                    m["name"] = name
                self.cfg.save()
                return
        self.cfg.webdav_mounts.append({
            "name": name, "url": url, "username": user,
            "password_enc": enc, "drive": drive, "auto": False,
        })
        self.cfg.save()

    def _logout(self):
        """세션을 지우고 로그인 화면으로. 명시적 로그아웃이므로 자동 로그인/저장 비번도 끈다.
        (서버 주소·아이디는 재로그인 편의를 위해 유지)"""
        self._collect()
        self.cfg.token = ""
        self._pw = ""
        # 명시적 로그아웃 → 다음 실행에서 자동 로그인하지 않고, 저장된 비번도 제거
        self.cfg.auto_login = False
        self.var_autologin.set(False)
        self.cfg.clear_password()
        # FTP 세션 표식(server_url=ftp://…)까지 지워야 실제로 로그아웃된다.
        # 남겨두면 드라이브가 마운트된 채로 있고, 다음 실행에서 자격증명도 없이
        # '로그인된 화면'이 떠 버린다.
        if self.cfg.is_ftp_session():
            threading.Thread(target=self._unmount_ftp_drive, daemon=True).start()
        self.cfg.server_url = ""
        self.cfg.save()
        self.e_pw.delete(0, "end")   # 저장 비번을 지웠으니 프리필도 비움
        self.lbl_login_error.configure(text="")
        self._show_login()

    def _auto_sequence(self):
        """시작 시: 자동 로그인 → (설정 시) 드라이브 연결 → 동기화 트리거."""
        cfg = self.cfg
        pw = cfg.get_password()
        if cfg.is_ftp_session():
            # FTP 세션: FTP 로 자격 검증만 하고 드라이브를 연결한다 (API 기능 없음)
            from .ftp_client import FtpDriveClient
            try:
                c = FtpDriveClient(cfg.ftp_host, cfg.ftp_port, cfg.username, pw)
                c.spaces()
                c.close()
                self._pw = pw
                self.root.after(0, self._show_settings)
                self.set_status("자동 로그인 성공 (FTP)", SUCCESS)
                self.log("자동 로그인 성공 (FTP)")
            except Exception as e:  # noqa: BLE001
                self.set_status("자동 로그인 실패", DANGER)
                self.log(f"자동 로그인 실패 (FTP): {e}")
                return
            if cfg.ftp_drive_enabled:
                self._mount_ftp_drive_async()
            return
        try:
            c = GenDiskClient(cfg.server_url)
            c.login(cfg.username, pw)
            cfg.token = c.token
            self._pw = pw
            cfg.save()
            self.root.after(0, self._show_settings)
            self.root.after(0, self._refresh_spaces_async)
            self.set_status("자동 로그인 성공", SUCCESS)
            self.log("자동 로그인 성공")
        except Exception as e:
            self.set_status("자동 로그인 실패", DANGER)
            self.log(f"자동 로그인 실패: {e}")
            return
        if cfg.auto_connect_drive:
            try:
                connect_drive(cfg.drive_letter, cfg.server_url, cfg.username, pw)
                self.log(f"{cfg.drive_letter} 드라이브 자동 연결")
            except Exception as e:
                self.log(f"드라이브 자동 연결 실패: {e}")
        if cfg.vfs_enabled:
            self._start_drive_async()
        if cfg.enabled:
            self.worker.sync_now()

    def try_relogin(self):
        """세션 만료 시 저장된 정보로 조용히 재로그인."""
        if self.cfg.is_ftp_session():
            return                      # FTP 는 비밀번호 직접 인증 — 갱신할 세션이 없다
        pw = self.cfg.get_password() or self._pw
        if not (self.cfg.username and pw):
            return
        try:
            c = GenDiskClient(self.cfg.server_url)
            c.login(self.cfg.username, pw)
            self.cfg.token = c.token
            self.cfg.save()
            self.log("세션 재로그인 성공")
        except Exception as e:
            self.log(f"재로그인 실패: {e}")

    # ---------- genDISK Drive (온디맨드) ----------
    @staticmethod
    def _fmt_size(n):
        n = float(max(0, int(n or 0)))
        u = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while n >= 1024 and i < 4:
            n /= 1024
            i += 1
        return f"{n:.1f} {u[i]}"

    def _fmt_rate(self, bps):
        return self._fmt_size(bps) + "/s"

    def _on_transfer(self, key, name, direction, done, total):
        """DriveController 전송 진행 콜백 → 트래커 갱신. done=None 이면 종료."""
        if done is None:
            self.transfers.finish(key)
        else:
            self.transfers.update(key, name, direction, done, total)

    def _refresh_transfers(self):
        """전송 현황 패널을 주기적으로 다시 그린다(FTP식 파일별 진행률·속도)."""
        try:
            items = self.transfers.snapshot()
            if items:
                lines = []
                for it in items:
                    total = it["total"]
                    pct = int(it["done"] * 100 / total) if total else 0
                    arrow = "⬆" if it["dir"] == "up" else "⬇"
                    lines.append(
                        f"{arrow} {it['name'][:26]:<26} {pct:3d}%  "
                        f"{self._fmt_rate(it['rate']):>11}  "
                        f"({self._fmt_size(it['done'])}/{self._fmt_size(total)})")
                text = "\n".join(lines)
            else:
                text = "(전송 없음)"
            self.txt_transfers.configure(state="normal")
            self.txt_transfers.delete("1.0", "end")
            self.txt_transfers.insert("1.0", text)
            self.txt_transfers.configure(state="disabled")
        except Exception:
            pass
        try:
            self.root.after(700, self._refresh_transfers)
        except Exception:
            pass

    def _drive_notify(self, msg: str):
        """드라이브 업로드 등 진행 상황을 트레이 토스트로 알린다(트레이 없으면 로그만).
        업로드는 Windows 가 자체 진행창을 안 띄워서(다운로드/하이드레이션만 띄움) 이걸로 표시."""
        try:
            if self.tray is not None:
                self.tray.notify(str(msg), "genDISK Drive")
            else:
                self.log(str(msg))
        except Exception:
            pass

    def _drive_reauth(self) -> bool:
        """드라이브 콜백에서 세션 만료 시 저장된 정보로 재로그인 (성공 시 cfg.token 갱신)."""
        pw = self.cfg.get_password() or self._pw
        if not (self.cfg.server_url and self.cfg.username and pw):
            return False
        if self.cfg.is_ftp_session():
            # FTP 는 비밀번호 직접 인증 — 자격 증명이 유효한지 한 번 확인만 한다
            from .ftp_client import FtpDriveClient
            try:
                c = FtpDriveClient(self.cfg.ftp_host, self.cfg.ftp_port,
                                   self.cfg.username, pw)
                c.spaces()
                c.close()
                return True
            except Exception:
                return False
        try:
            c = GenDiskClient(self.cfg.server_url)
            c.login(self.cfg.username, pw)
            self.cfg.token = c.token
            self.cfg.save()
            return True
        except Exception:
            return False

    def _start_drive_async(self):
        try:
            space = self.cmb_space.get() or self.cfg.space   # 위젯 접근은 메인 스레드에서
        except Exception:
            space = self.cfg.space
        def work():
            try:
                self.cfg.space = space
                self.drive.start()
                self.cfg.vfs_enabled = True
                self.cfg.save()
                self.set_status("genDISK Drive 연결됨", SUCCESS)
                self.log("genDISK Drive 를 연결했습니다 (탐색기 사이드바 확인).")
            except Exception as e:  # noqa: BLE001
                self.log(f"genDISK Drive 연결 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.root.after(0, lambda: self.var_vfs.set(False))
        threading.Thread(target=work, daemon=True).start()

    def _set_drive_buttons(self, busy: bool, refresh_text="지금 새로고침",
                           reconnect_text="다시 연결"):
        """드라이브 카드의 두 버튼을 함께 잠금/해제한다(동시 실행으로 상태가 엉키지 않게)."""
        state = "disabled" if busy else "normal"
        def apply():
            try:
                self.btn_drive_refresh.configure(state=state, text=refresh_text)
                self.btn_drive_reconnect.configure(state=state, text=reconnect_text)
            except Exception:  # noqa: BLE001 (창 종료 중이면 무시)
                pass
        self.root.after(0, apply)

    def _drive_refresh_now(self):
        """서버와 전체 재대조해 드라이브 목록을 지금 최신화한다(깊은 폴더 포함)."""
        if not self.drive.running:
            messagebox.showwarning("드라이브 필요", "genDISK Drive 를 먼저 연결하세요.")
            return
        self._set_drive_buttons(True, refresh_text="새로고침 중…")

        def work():
            try:
                self.set_status("드라이브 새로고침 중…", MUTED)
                n = self.drive.refresh_now()
                if n is None:                      # 그 사이 드라이브가 내려감(재연결 중 등)
                    self.set_status("드라이브가 연결돼 있지 않습니다", DANGER)
                    self.log("드라이브 새로고침: 드라이브가 연결돼 있지 않습니다.")
                else:
                    self.set_status("genDISK Drive 연결됨", SUCCESS)
                    self.log(f"드라이브 새로고침 완료 (신규 {n}개 반영)")
            except Exception as e:  # noqa: BLE001
                self.log(f"드라이브 새로고침 실패: {e}")
                self.set_status("드라이브 새로고침 실패", DANGER)
            finally:
                self._set_drive_buttons(False)
        threading.Thread(target=work, daemon=True).start()

    def _toggle_drive_sync(self):
        """백그라운드 자동 반영 켬/끔 — 저장하고, 드라이브가 켜져 있으면 재연결로 적용한다.
        (SSE 구독 스레드를 시작/중지해야 해서 재연결이 가장 깔끔하다)"""
        self.cfg.vfs_sync = self.var_drive_sync.get()
        self.cfg.save()
        mode = "자동 반영 켬" if self.cfg.vfs_sync else "SMB식 (자동 반영 끔)"
        if not self.drive.running:
            self.log(f"드라이브 동기화 설정 저장: {mode} (다음 연결부터 적용)")
            return
        self._set_drive_buttons(True, reconnect_text="적용 중…")

        def work():
            try:
                self.drive.restart()
                self.log(f"드라이브 동기화 설정 적용: {mode}")
            except Exception as e:  # noqa: BLE001
                self.log(f"동기화 설정 적용 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.root.after(0, lambda: self.var_vfs.set(False))
            finally:
                self._set_drive_buttons(False)
        threading.Thread(target=work, daemon=True).start()

    def _toggle_free_space(self):
        """무저장(디스크 공간 자동 확보) 켬/끔 — 저장하고, 드라이브가 켜져 있으면 재연결로 적용."""
        self.cfg.vfs_free_space = self.var_free_space.get()
        self.cfg.save()
        mode = "무저장 켬 (컴퓨터에 안 남김)" if self.cfg.vfs_free_space else "무저장 끔 (로컬 캐시 유지)"
        if not self.drive.running:
            self.log(f"드라이브 공간 설정 저장: {mode} (다음 연결부터 적용)")
            return
        self._set_drive_buttons(True, reconnect_text="적용 중…")

        def work():
            try:
                self.drive.restart()
                self.log(f"드라이브 공간 설정 적용: {mode}")
            except Exception as e:  # noqa: BLE001
                self.log(f"공간 설정 적용 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.root.after(0, lambda: self.var_vfs.set(False))
            finally:
                self._set_drive_buttons(False)
        threading.Thread(target=work, daemon=True).start()

    # ---------- FTP 드라이브 (rclone + WinFsp) ----------
    def _mount_ftp_drive_async(self):
        """서버 FTP 를 드라이브 문자로 마운트 — 탐색기가 서버를 직접 본다.
        로컬에 목록·파일을 저장하지 않으므로 온디맨드 방식의 문제가 없다."""
        cfg = self.cfg
        pw = self._pw or cfg.get_password()
        # 게이트도 실사용과 같은 해석값을 봐야 한다. 원본 ftp_host 만 보면, 두 줄 뒤
        # configure() 는 server_url 에서 뽑아 성공할 상황인데도 스스로 막아버렸다.
        if not (cfg.ftp_host_resolved() and cfg.username and pw):
            self.log("FTP 드라이브: 로그인 정보가 없어 건너뜁니다.")
            return

        def work():
            from . import ftp_drive
            point = cfg.ftp_mount_path()
            try:
                msg = ftp_drive.requirements_message()
                if msg:
                    # 안내만 하지 않고 '지금 설치'를 제안한다 (winget, UAC 1회).
                    self.log("genDISK Drive 구성 요소 없음 —\n" + msg)
                    done = threading.Event()
                    choice = {"install": False}

                    def ask():
                        choice["install"] = messagebox.askyesno(
                            "구성 요소 설치",
                            msg + "\n\n지금 설치할까요?\n"
                                  "(관리자 권한 승인이 한 번 필요합니다)")
                        done.set()
                    self.root.after(0, ask)
                    done.wait(300)
                    if not choice["install"]:
                        self.log("구성 요소 설치를 건너뛰었습니다.")
                        return
                    self.set_status("구성 요소 설치 중…", MUTED)
                    err = ftp_drive.install_requirements(log=self.log)
                    if err:
                        self.set_status("구성 요소 설치 실패", DANGER)
                        self.log(err)
                        self.root.after(0, lambda: messagebox.showerror("설치 실패", err))
                        return
                ftp_drive.configure(cfg.ftp_host_resolved(), cfg.ftp_port, cfg.username, pw,
                                    tls=bool(getattr(cfg, "ftp_tls", False)))
                ftp_drive.mount(point)
                # 드라이브 문자를 만들지 않고, 탐색기 사이드바의 'genDISK Drive'
                # 브랜디드 노드가 이 마운트 지점을 가리키게 한다.
                # desktop.ini 는 서버로 올라가므로 쓰지 않는다(folder_icon=False).
                try:
                    from . import navdrive
                    from .drive import stable_icon_path
                    navdrive.register_drive(point, stable_icon_path(), folder_icon=False)
                except Exception as e:  # noqa: BLE001 (노드 없이도 마운트는 동작)
                    self.log(f"사이드바 노드 등록 실패(무시): {e}")
                # 탐색기가 썸네일을 만들려고 파일을 통째로 내려받는 것을 막는다
                # (서버 부하의 대부분) — 사용자가 끈 경우엔 건드리지 않는다.
                if getattr(cfg, "ftp_no_preview", True):
                    try:
                        from . import explorer_preview
                        if explorer_preview.disable():
                            self.log("탐색기 미리 보기·썸네일을 껐습니다 "
                                     "(서버에서 파일을 미리 받지 않도록).")
                    except Exception as e:  # noqa: BLE001
                        self.log(f"미리 보기 끄기 실패(무시): {e}")
                self.set_status("genDISK Drive 연결됨 (FTP)", SUCCESS)
                self.log("genDISK Drive 를 연결했습니다 — 탐색기 사이드바에서 확인하세요.")
                self._start_ftp_watchdog(point)
            except Exception as e:  # noqa: BLE001
                self.set_status("FTP 드라이브 연결 실패", DANGER)
                self.log(f"FTP 드라이브 연결 실패: {e}")
        threading.Thread(target=work, daemon=True).start()

    SICK_LIMIT = 2            # 연속 무응답 N회면 강제 재마운트

    def _start_ftp_watchdog(self, point: str):
        """마운트 생존 감시 — rclone 이 예고 없이 죽어도 상태는 '연결됨'으로 남아
        사이드바 노드만 사라지는 문제가 있었다. 30초마다 확인해 끊겼으면 자동
        재마운트하고, 반복 실패하면 상태를 바꿔 사용자가 알게 한다."""
        self._ftp_watch_gen = getattr(self, "_ftp_watch_gen", 0) + 1
        gen = self._ftp_watch_gen

        def watch():
            from . import ftp_drive
            fails = 0
            wait = 30
            sick = 0       # 연속 무응답 횟수
            while True:
                time.sleep(wait)
                if gen != self._ftp_watch_gen or not self.cfg.ftp_drive_enabled:
                    return                     # 해제됐거나 새 감시로 교체됨
                # 볼륨 존재 여부만 보면 '프로세스는 살아 있는데 FTP 연결만 죽은' 상태를
                # 놓친다(실측: 7분 34초 동안 모든 열람이 실패하는데 워치독은 정상 판정).
                # 그래서 실제로 목록을 읽어 보고, 연속으로 실패할 때만 재마운트한다
                # (일시적 지연 한 번으로 멀쩡한 마운트를 내리지 않도록).
                if ftp_drive.is_mounted(point):
                    if ftp_drive.probe(point):
                        if fails:
                            self.set_status("genDISK Drive 연결됨 (FTP)", SUCCESS)
                        fails, wait, sick = 0, 30, 0
                        continue
                    # 응답이 없다고 다 죽은 건 아니다. 썸네일 생성처럼 전송이 몰리면
                    # 목록 응답이 늦어질 뿐인데, 그때 재마운트하면 받던 것을 끊어
                    # 오히려 악화된다. 진짜 사망이면 로그에 연결 오류가 쏟아진다.
                    errs = ftp_drive.recent_errors(120)
                    if errs == 0:
                        self.log("genDISK Drive 응답이 느립니다 (전송 중인 것으로 보임) "
                                 "— 재연결하지 않고 기다립니다.")
                        sick, wait = 0, 30
                        continue
                    sick += 1
                    self.log(f"genDISK Drive 응답 없음 ({sick}/{self.SICK_LIMIT}, "
                             f"최근 연결오류 {errs}건) — 확인 중입니다.")
                    if sick < self.SICK_LIMIT:
                        wait = 15              # 의심 구간에서는 더 자주 확인
                        continue
                    self.log("genDISK Drive 가 응답하지 않습니다 — 강제로 다시 연결합니다.")
                    try:
                        ftp_drive.unmount(point)   # 죽은 rclone 을 확실히 회수
                    except Exception as e:  # noqa: BLE001
                        self.log(f"기존 마운트 정리 실패(무시): {e}")
                    sick = 0
                else:
                    sick = 0
                    self.log("genDISK Drive 마운트가 끊겼습니다 — 자동으로 다시 연결합니다.")
                try:
                    cfg = self.cfg
                    pw = self._pw or cfg.get_password()
                    if not pw:
                        raise RuntimeError("저장된 로그인 정보가 없습니다")
                    # 네트워크가 바뀌었을 수 있으니 접속 정보도 다시 반영한다
                    ftp_drive.configure(cfg.ftp_host_resolved(), cfg.ftp_port, cfg.username, pw,
                                        tls=bool(getattr(cfg, "ftp_tls", False)))
                    ftp_drive.mount(point)
                    self.set_status("genDISK Drive 연결됨 (FTP)", SUCCESS)
                    self.log("genDISK Drive 를 다시 연결했습니다.")
                    fails, wait = 0, 30
                except Exception as e:  # noqa: BLE001 (감시는 절대 죽지 않아야 한다)
                    fails += 1
                    # 예전에는 3회 실패하면 영구히 포기해, 잠깐 끊겼다 돌아온 뒤에도
                    # 사람이 '다시 연결'을 눌러야 했다. 이제는 간격만 늘리며 계속 살핀다.
                    wait = min(wait * 2, 300)
                    self.set_status(f"genDISK Drive 재연결 대기 중 ({wait}초)", DANGER)
                    self.log(f"자동 재연결 실패({fails}회, {wait}초 후 재시도): {e}")
        threading.Thread(target=watch, daemon=True).start()

    # ---------- 자동 업데이트 (GitHub Releases) ----------
    def _check_update_async(self):
        """시작 시 백그라운드로 최신 릴리스를 확인한다. 새 버전이면 사용자에게
        묻고, 동의하면 내려받아 exe 를 교체한 뒤 앱을 재시작한다."""
        from . import __version__, updater
        updater.cleanup_old()            # 이전 업데이트 잔재(<exe>.old) 정리
        try:
            upd = updater.check()
        except Exception:  # noqa: BLE001 (오프라인·레이트리밋 등 — 조용히 넘어간다)
            return
        if not upd:
            return
        ver, url, size = upd
        self.log(f"새 버전 v{ver} 이(가) 있습니다 (현재 v{__version__}).")

        def do_update():
            self.set_status("업데이트 다운로드 중…", MUTED)
            try:
                updater.apply(url, size, log=self.log)
            except Exception as e:  # noqa: BLE001
                self.set_status("업데이트 실패", DANGER)
                self.log(f"업데이트 실패: {e}")
                return
            self.log("업데이트를 적용했습니다 — 앱을 다시 시작합니다.")
            self.root.after(0, self._real_quit)

        def ask():
            if messagebox.askyesno(
                    "업데이트",
                    f"새 버전 v{ver}이(가) 나왔습니다.\n"
                    "지금 업데이트할까요? (다운로드 후 자동으로 다시 시작됩니다)"):
                threading.Thread(target=do_update, daemon=True).start()
            else:
                self.log("업데이트를 건너뛰었습니다 — 다음 실행 때 다시 확인합니다.")
        self.root.after(0, ask)

    def _unmount_ftp_drive(self):
        from . import explorer_preview, ftp_drive, navdrive
        # 감시 중단 — 사용자가 해제한 마운트를 워치독이 도로 붙이면 안 된다
        self._ftp_watch_gen = getattr(self, "_ftp_watch_gen", 0) + 1
        point = self.cfg.ftp_mount_path()
        try:
            navdrive.unregister_drive()
        except Exception:  # noqa: BLE001
            pass
        try:
            explorer_preview.restore()   # 껐던 미리 보기를 원래대로
        except Exception:  # noqa: BLE001
            pass
        try:
            if ftp_drive.unmount(point):
                self.log("genDISK Drive 연결을 해제했습니다.")
        except Exception as e:  # noqa: BLE001
            self.log(f"FTP 드라이브 해제 실패: {e}")

    def _toggle_no_preview(self):
        """탐색기 미리 보기·썸네일 끄기 켬/끔 — 즉시 적용(해제 시 원래 설정 복원)."""
        from . import explorer_preview
        self.cfg.ftp_no_preview = self.var_no_preview.get()
        self.cfg.save()
        try:
            if self.cfg.ftp_no_preview:
                explorer_preview.disable()
                self.log("탐색기 미리 보기·썸네일을 껐습니다.")
            else:
                explorer_preview.restore()
                self.log("탐색기 미리 보기·썸네일을 원래대로 되돌렸습니다.")
            self.log("  (이미 열려 있는 탐색기 창은 다시 열어야 반영됩니다)")
        except Exception as e:  # noqa: BLE001
            self.log(f"미리 보기 설정 변경 실패: {e}")

    def _toggle_ftp_drive(self):
        self.cfg.ftp_drive_enabled = self.var_ftp_drive.get()
        self.cfg.save()
        if self.cfg.ftp_drive_enabled:
            self._mount_ftp_drive_async()
        else:
            threading.Thread(target=self._unmount_ftp_drive, daemon=True).start()

    def _toggle_ftp_transport(self):
        """드라이브 파일 전송을 API(HTTPS) ↔ FTP 로 전환 — 저장하고, 켜져 있으면 재연결."""
        self.cfg.vfs_transport = "ftp" if self.var_ftp.get() else "api"
        _h = self.e_ftp_host.get().strip()
        if _h:                      # 비어 있으면 기존 값을 지우지 않는다
            self.cfg.ftp_host = _h
        try:
            self.cfg.ftp_port = int(self.e_ftp_port.get().strip() or 2121)
        except ValueError:
            self.cfg.ftp_port = 2121
        if self.cfg.vfs_transport == "ftp" and not (self._pw or self.cfg.get_password()):
            self.var_ftp.set(False)
            self.cfg.vfs_transport = "api"
            messagebox.showwarning(
                "로그인 정보 필요",
                "FTP 전송은 저장된 로그인 정보(비밀번호)로 접속합니다.\n"
                "로그인 화면에서 '로그인 정보 저장'을 켜고 다시 로그인하세요.")
            return
        self.cfg.save()
        mode = "FTP" if self.cfg.vfs_transport == "ftp" else "API(HTTPS)"
        if not self.drive.running:
            self.log(f"드라이브 전송 설정 저장: {mode} (다음 연결부터 적용)")
            return
        self._set_drive_buttons(True, reconnect_text="적용 중…")

        def work():
            try:
                self.drive.restart()
                self.log(f"드라이브 전송 방식 적용: {mode}")
            except Exception as e:  # noqa: BLE001
                self.log(f"전송 설정 적용 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.root.after(0, lambda: self.var_vfs.set(False))
            finally:
                self._set_drive_buttons(False)
        threading.Thread(target=work, daemon=True).start()

    def _drive_reconnect(self):
        """genDISK Drive 를 완전히 내렸다 다시 연결한다(연결·목록 문제 수동 복구).
        탐색기 노드는 유지되고 provider 연결·폴링·실시간 이벤트만 새로 만든다."""
        if not (self.cfg.server_url
                and (self.cfg.token or self.cfg.is_ftp_session())):
            messagebox.showwarning("로그인 필요", "먼저 로그인하세요.")
            return
        self._set_drive_buttons(True, reconnect_text="연결 중…")

        def work():
            try:
                self.set_status("genDISK Drive 다시 연결 중…", MUTED)
                self.drive.restart()
                self.cfg.vfs_enabled = True
                self.cfg.save()
                self.root.after(0, lambda: self.var_vfs.set(True))
                self.set_status("genDISK Drive 연결됨", SUCCESS)
                self.log("genDISK Drive 를 다시 연결했습니다.")
            except Exception as e:  # noqa: BLE001
                # stop 은 이미 됐는데 start 가 실패한 상태 — 토글/설정을 실제 상태(꺼짐)로 되돌린다.
                self.log(f"genDISK Drive 다시 연결 실패: {e}")
                self.set_status("genDISK Drive 연결 실패", DANGER)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.root.after(0, lambda: self.var_vfs.set(False))
            finally:
                self._set_drive_buttons(False)
        threading.Thread(target=work, daemon=True).start()

    def _toggle_vfs(self):
        if self.var_vfs.get():
            if not (self.cfg.server_url
                    and (self.cfg.token or self.cfg.is_ftp_session())):
                messagebox.showwarning("로그인 필요", "먼저 로그인하세요.")
                self.var_vfs.set(False)
                return
            self._start_drive_async()
        else:
            def work():
                self.drive.stop(remove_node=True)
                self.cfg.vfs_enabled = False
                self.cfg.save()
                self.set_status("genDISK Drive 해제됨", MUTED)
            threading.Thread(target=work, daemon=True).start()

    def _refresh_spaces(self, client):
        try:
            spaces = [s["id"] for s in client.spaces()]
            self.cmb_space.configure(values=spaces or ["home"])
            if self.cfg.space not in spaces:
                self.cmb_space.set(spaces[0] if spaces else "home")
        except Exception:
            pass

    def _refresh_spaces_async(self):
        """저장된 토큰으로 시작했을 때 저장소 목록을 백그라운드에서 채운다(네트워크는 스레드에서)."""
        if not self.cfg.server_url:
            return
        if not (self.cfg.token or (self.cfg.is_ftp_session() and self.cfg.get_password())):
            return
        def work():
            try:
                if self.cfg.is_ftp_session():
                    from .ftp_client import FtpDriveClient
                    c = FtpDriveClient(self.cfg.ftp_host, self.cfg.ftp_port,
                                       self.cfg.username, self.cfg.get_password())
                    spaces = [s["id"] for s in c.spaces()]
                    c.close()
                else:
                    spaces = [s["id"] for s in
                              GenDiskClient(self.cfg.server_url, self.cfg.token).spaces()]
            except Exception:
                return
            def apply():
                self.cmb_space.configure(values=spaces or ["home"])
                if self.cfg.space not in spaces:
                    self.cmb_space.set(spaces[0] if spaces else "home")
            self.root.after(0, apply)   # 위젯 갱신은 메인 스레드에서
        threading.Thread(target=work, daemon=True).start()

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.e_folder.delete(0, "end"); self.e_folder.insert(0, d)

    def _apply_autostart(self):
        try:
            autostart.sync(self.var_autostart.get())
        except OSError as e:
            messagebox.showerror("자동 실행 등록 실패", str(e))

    def _on_theme_change(self, label):
        """라이트/다크/자동 선택 → 즉시 적용 + 저장."""
        mode = _THEME_MODES.get(label, "system")
        self.cfg.appearance = mode
        ctk.set_appearance_mode(mode)
        self.cfg.save()

    def _collect(self):
        """설정 화면의 값들을 cfg에 모은다. (서버/아이디/토큰은 로그인에서만 설정)"""
        self.cfg.space = self.cmb_space.get() or "home"
        self.cfg.local_folder = self.e_folder.get().strip()
        self.cfg.drive_letter = self.cmb_drive.get()
        try:
            self.cfg.interval_sec = max(5, int(self.e_interval.get()))
        except ValueError:
            self.cfg.interval_sec = 30
        self.cfg.enabled = self.var_enabled.get()
        _h = self.e_ftp_host.get().strip()
        if _h:                      # 비어 있으면 기존 값을 지우지 않는다
            self.cfg.ftp_host = _h
        try:
            self.cfg.ftp_port = int(self.e_ftp_port.get().strip() or 2121)
        except ValueError:
            self.cfg.ftp_port = 2121
        self.cfg.ftp_drive_enabled = self.var_ftp_drive.get()
        self.cfg.ftp_no_preview = self.var_no_preview.get()
        self.cfg.save_credentials = self.var_savecred.get()
        self.cfg.auto_start = self.var_autostart.get()
        self.cfg.auto_login = self.var_autologin.get()
        self.cfg.auto_connect_drive = self.var_autodrive.get()
        if not self.cfg.save_credentials:
            self.cfg.clear_password()
        elif self._pw:
            self.cfg.set_password(self._pw)

    def _save(self):
        self._collect()
        self.cfg.save()
        self._apply_autostart()
        self.log("설정을 저장했습니다.")

    # 폴더 동기화는 HTTPS API(/api/sync) 기능이라 FTP 접속에서는 동작하지 않는다.
    # 예전에는 스위치를 켜도 조용히 아무 일도 없었고, '지금 동기화'는 로그인 상태인데도
    # "로그인하세요" 경고를 띄웠다 — 이제 이유를 분명히 알린다.
    _SYNC_UNAVAILABLE = ("FTP 접속에서는 폴더 동기화를 지원하지 않습니다.\n"
                         "탐색기 사이드바의 genDISK Drive 를 사용하세요.")

    def _toggle_enabled(self):
        if self.cfg.is_ftp_session() and self.var_enabled.get():
            self.var_enabled.set(False)
            messagebox.showinfo("지원하지 않음", self._SYNC_UNAVAILABLE)
            return
        self._collect(); self.cfg.save(); self.worker.sync_now()

    def _sync_now(self):
        if self.cfg.is_ftp_session():
            messagebox.showinfo("지원하지 않음", self._SYNC_UNAVAILABLE)
            return
        self._collect(); self.cfg.save()
        if not self.cfg.is_ready():
            messagebox.showwarning("설정 필요", "로그인하고 로컬 폴더를 지정하세요.")
            return
        self.worker.sync_now()

    def _connect_drive(self):
        self._collect()
        pw = self._pw or self.cfg.get_password()
        if not self.cfg.server_url or not self.cfg.username or not pw:
            messagebox.showwarning("정보 필요", "로그인 후 다시 시도하세요 (비밀번호가 필요합니다).")
            return
        if self.cfg.is_ftp_session():
            messagebox.showinfo(
                "FTP 접속", "FTP 접속에서는 WebDAV 드라이브 연결을 지원하지 않습니다.\n"
                            "탐색기 사이드바의 genDISK Drive 를 사용하세요.")
            return
        # 먼저 서버의 /dav 를 직접 확인 → 서버 문제와 로컬(WebClient) 문제를 구분
        try:
            webdav_preflight(self.cfg.server_url, self.cfg.username, pw)
        except RuntimeError as e:
            messagebox.showerror("서버 확인 실패 (서버 측 문제)", str(e))
            return
        # 서버 WebDAV는 정상 → 로컬에서 드라이브로 마운트
        try:
            connect_drive(self.cmb_drive.get(), self.cfg.server_url, self.cfg.username, pw)
            self.log(f"{self.cmb_drive.get()} 드라이브로 연결했습니다.")
        except Exception as e:
            extra = ""
            if not webclient_running():
                extra = ("\n\n▶ 원인: Windows 'WebClient' 서비스가 꺼져 있습니다.\n"
                         "   아래 'WebClient 서비스 켜기' 버튼을 눌러(관리자 승인) 켠 뒤 다시 연결하세요.")
            messagebox.showerror(
                "드라이브 연결 실패 (로컬 측)",
                "서버의 WebDAV는 정상 확인됐습니다. Windows 쪽 문제입니다.\n\n" + str(e) + extra)

    def _start_webclient(self):
        if webclient_running():
            messagebox.showinfo("WebClient", "WebClient 서비스가 이미 실행 중입니다.")
            return
        try:
            start_webclient_elevated()  # UAC 프롬프트
        except Exception as e:
            messagebox.showerror("WebClient 시작 실패", str(e))
            return
        import time
        time.sleep(1.5)
        if webclient_running():
            self.log("WebClient 서비스를 켰습니다. 이제 드라이브 연결을 다시 시도하세요.")
            messagebox.showinfo("WebClient", "WebClient 서비스를 켰습니다.\n'연결'을 다시 눌러주세요.")
        else:
            messagebox.showwarning(
                "WebClient",
                "서비스를 켜지 못했습니다 (관리자 승인 거부 또는 서비스 없음).\n"
                "관리자 PowerShell에서: Set-Service WebClient -StartupType Automatic; Start-Service WebClient")

    def _disconnect_drive(self):
        try:
            disconnect_drive(self.cmb_drive.get())
            self.log(f"{self.cmb_drive.get()} 연결을 해제했습니다.")
        except Exception as e:
            messagebox.showerror("연결 해제 실패", str(e))

    def _auto_connect_webdav_mounts(self):
        """저장된 일반 WebDAV 연결 중 'auto' 항목을 시작 시 마운트한다 (백그라운드)."""
        for m in list(self.cfg.webdav_mounts):
            if not m.get("auto"):
                continue
            drive = m.get("drive", "")
            url = m.get("url", "")
            if not drive or not url:
                continue
            try:
                from . import secret
                pw = secret.decrypt(m.get("password_enc", "")) or ""
                connect_url(drive, url, m.get("username", ""), pw)
                self.log(f"[WebDAV] {drive} 자동 연결: {m.get('name') or url}")
            except Exception as e:  # noqa: BLE001
                self.log(f"[WebDAV] 자동 연결 실패({m.get('name') or url}): {e}")

    def _cleanup_webdav(self):
        """끊긴 WebDAV 드라이브/네트워크 위치 잔여를 제거하고, 자동 연결을 끈다."""
        if not messagebox.askyesno(
                "끊긴 WebDAV 연결 정리",
                "탐색기에 남은 끊긴 WebDAV 네트워크 드라이브(빨간 X)와 잔여 항목을 제거합니다.\n"
                "'자동 로그인 후 드라이브 자동 연결'도 함께 끕니다.\n"
                "(genDISK Drive 온디맨드 기능에는 영향 없음)\n\n계속할까요?"):
            return
        try:
            removed = cleanup_stale_webdav(self.cmb_drive.get() or self.cfg.drive_letter,
                                           self.cfg.server_url)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("정리 실패", str(e))
            return
        # 재발 방지: 자동 드라이브 연결 끄기
        self.cfg.auto_connect_drive = False
        self.var_autodrive.set(False)
        self.cfg.save()
        if removed:
            self.log("WebDAV 정리: " + ", ".join(removed))
            messagebox.showinfo(
                "정리 완료",
                "정리했습니다:\n\n· " + "\n· ".join(removed) +
                "\n\n'자동 드라이브 연결'도 껐습니다.\n탐색기를 새로 열면 사라집니다.")
        else:
            messagebox.showinfo("정리", "제거할 끊긴 WebDAV 항목이 없습니다.\n"
                                        "('자동 드라이브 연결'은 껐습니다.)")

    # ---------- 상태/로그 (스레드 안전) ----------
    def set_status(self, text, color=MUTED):
        self.root.after(0, lambda: self.lbl_status.configure(text=text, text_color=color))

    # 화면 로그는 최근 것만 남긴다. 드라이브 콜백이 초당 수십 줄을 뿜을 수 있어
    # 무한히 쌓으면 Tk 위젯 메모리와 after() 큐가 함께 불어나 앱이 죽는다
    # (탐색기에는 '클라우드 파일 공급자가 예기치 않게 종료' 로 보인다).
    LOG_MAX_LINES = 400

    def log(self, text):
        def _append():
            try:
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", text + "\n")
                # 넘치면 앞부분을 잘라낸다
                n = int(self.txt_log.index("end-1c").split(".")[0])
                if n > self.LOG_MAX_LINES:
                    self.txt_log.delete("1.0", f"{n - self.LOG_MAX_LINES}.0")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
            except Exception:      # noqa: BLE001 (창 종료 중이면 무시)
                pass
        self.root.after(0, _append)

    def run(self):
        self.root.mainloop()


def _install_crash_logger():
    """처리 안 된 예외를 %LOCALAPPDATA%\\genDISK\\crash.log 에 남긴다.
    창이 있는 exe(콘솔 없음)라 예외가 조용히 사라지면 원인 추적이 불가능하다."""
    import os
    import sys
    import threading as _th
    import time
    import traceback

    path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "genDISK", "crash.log")

    def write(where, exc_type, exc, tb):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} [{where}] =====\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except OSError:
            pass

    def hook(exc_type, exc, tb):
        write("main", exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    _th.excepthook = lambda a: write(f"thread:{a.thread and a.thread.name}",
                                     a.exc_type, a.exc_value, a.exc_traceback)


def main(startup: bool = False):
    _install_crash_logger()
    App(startup=startup).run()
