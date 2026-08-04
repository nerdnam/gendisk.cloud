# gendisk-sync (Windows 클라이언트)

genDISK 서버에 연결하는 Windows용 프로그램.

**genDISK 접속은 높은 호환성을 위해 FTP 프로토콜을 사용합니다.** 로그인 화면의 서버 주소에
서버의 FTP 주소를 입력하세요 (예: `ftp.example.com:2121` — 포트 생략 시 2121 → 21 순서로
자동 시도, `ftp://` 스킴 생략 가능). 서버에서 FTP를 켜는 방법은
[메인 README의 "FTP 접속"](../README.md#ftp-접속)을 참고하세요.

1. **genDISK Drive (온디맨드)** — 탐색기 사이드바에 iCloud처럼 나타납니다. 목록만 먼저
   보이고 파일을 열 때 FTP로 내려받으며, 드롭한 파일은 자동 업로드됩니다.
2. **일반 WebDAV 클라이언트** — genDISK 외 임의의 WebDAV 서버(NAS·Nextcloud 등)를
   네트워크 드라이브(예: `N:`)로 연결·관리합니다 (여러 프로파일 저장/전환).

> 폴더 동기화·WebDAV 드라이브 연결 등 HTTPS API 기반 기능은 FTP 접속에서는 쉽니다.

## 받기 / 실행

- **바로 받기**: genDISK 웹 UI의 "⬇ Windows 앱" 버튼, 또는 [GitHub Releases](https://github.com/nerdnam/gendisk.cloud/releases)의 `gendisk-sync-<버전>.exe` (파이썬 불필요, 더블클릭 실행).
- **소스로 실행** (파이썬 설치 시):

```
python main.py            # GUI
python main.py --startup  # 자동 시작(최소화) — 자동 로그인/드라이브 연결 수행
python main.py --once     # 저장된 설정으로 한 번만 동기화 (자동화용)
```

## 시작 옵션 (자동화)

- **로그인 정보 저장** — 비밀번호를 Windows DPAPI로 암호화해 저장합니다 (현재 Windows 사용자만 복호화 가능, 평문 저장 안 함). FTP 접속은 이 저장된 정보로 인증합니다.
- **Windows 시작 시 자동 실행** — 로그인 시 자동으로 프로그램을 띄웁니다 (레지스트리 Run 키, 최소화 상태).
- **프로그램 시작 시 자동 로그인** — 저장된 정보로 자동 로그인합니다 (FTP).

이 옵션들을 켜고 "설정 저장"하면, 이후 PC를 켤 때마다 **자동 실행 → 자동 로그인(FTP) → genDISK Drive 연결**까지 설정한 대로 동작합니다.

## 동기화 동작

- **양방향** — 로컬/원격 어느 쪽에서 만들고·수정·삭제해도 반영됩니다.
- **충돌 안전** — 같은 파일을 양쪽에서 서로 다르게 고치면, 로컬본을 `이름 (conflict 시각).확장자` 로 보존하고 원격본을 받아옵니다. 데이터를 잃지 않습니다.
- **상태 추적** — 로컬 폴더의 `.gendisk\state.json` 에 마지막 동기화 상태를 저장해 신규/수정/삭제를 구분합니다.
- 서버의 저장소 접근 권한·용량 제한을 그대로 따릅니다.

## .exe 빌드

```
build.bat
```

PyInstaller로 `dist\gendisk-sync.exe` (약 19MB, 단독 실행) 를 만듭니다.

## 구조

```
win_x64/
  main.py                    진입점 (GUI / --startup / --once)
  gendisk_sync/
    client.py                서버 HTTP 클라이언트 (표준 라이브러리만)
    engine.py                양방향 동기화 엔진
    config.py                설정 (%APPDATA%\gendisk-sync\config.json)
    secret.py                비밀번호 DPAPI 암호화 저장
    autostart.py             Windows 시작 시 자동 실행 등록 (레지스트리)
    app.py                   customtkinter GUI(macOS 스타일) + 백그라운드 동기화 루프
    webdav_mount.py          WNetAddConnection2W 로 WebDAV 드라이브 연결/해제
  gendisk-sync.spec          PyInstaller 스펙
  build.bat                  빌드 스크립트
```

## 창 닫기 = 트레이

창의 닫기(X) 버튼을 누르면 종료되지 않고 **시스템 트레이로 숨습니다** (백그라운드 동기화는 계속). 트레이 아이콘을 누르면 다시 열리고, 우클릭 메뉴의 **종료**로 완전히 끕니다.

## 참고

- UI는 `customtkinter`(macOS 스타일, 시스템 다크/라이트 따라감), 트레이 아이콘은 `pystray`·`pillow`를 씁니다 (소스로 실행 시 `pip install -r requirements.txt`; **.exe에는 모두 포함**되어 별도 설치 불필요). 서버 통신은 표준 라이브러리(urllib)만 사용합니다.
- WebDAV 드라이브 연결은 **HTTPS 서버**를 권장합니다. 평문 HTTP면 Windows WebClient가 기본적으로 Basic 인증을 막습니다.
- 드라이브 연결 시 **Windows 'WebClient' 서비스**가 실행 중이어야 합니다 (프로그램이 자동 시작을 시도하지만, 서비스가 사용 안 함으로 설정돼 있으면 서비스 관리자에서 수동/자동으로 바꿔야 합니다).
