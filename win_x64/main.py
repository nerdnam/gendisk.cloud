"""gendisk-sync 진입점.

  python main.py            GUI 실행
  python main.py --startup  자동 시작(최소화)으로 GUI 실행 — 자동 로그인/드라이브 연결 수행
  python main.py --once     설정에 저장된 대로 한 번만 동기화 (자동화/테스트용)
"""
import sys


def main():
    if "--once" in sys.argv:
        from gendisk_sync.client import GenDiskClient
        from gendisk_sync.config import Config
        from gendisk_sync.engine import SyncEngine

        cfg = Config.load()
        if not cfg.is_ready():
            print("설정이 부족합니다 (서버/토큰/폴더). 먼저 GUI로 로그인·설정하세요.")
            sys.exit(1)
        client = GenDiskClient(cfg.server_url, cfg.token)
        engine = SyncEngine(client, cfg.space, cfg.local_folder, log=print)
        summary = engine.run_once()
        print("동기화 완료:", summary)
        return

    # 단일 인스턴스: 이미 실행 중이면 그 창을 띄우고 이 프로세스는 종료한다.
    # 단, 이 exe 가 안정 사본과 다른 새 빌드면 '업그레이드 인계' — 구 인스턴스에 종료를
    # 요청하고 자리를 넘겨받은 뒤 안정 사본(자동시작 대상)을 새 버전으로 갱신한다.
    # (예전에는 새 exe 를 실행해도 구버전 창만 뜨고 끝나서 업데이트가 적용되지 않았다.)
    from gendisk_sync import autostart, single_instance
    if not single_instance.is_primary():
        took_over = False
        if autostart.stale_stable_copy():
            single_instance.signal_existing(b"quit")
            took_over = single_instance.try_become_primary(timeout=15.0)
        if not took_over:
            single_instance.signal_existing()
            return
        autostart.refresh_stable_copy()

    from gendisk_sync.app import main as gui_main
    gui_main(startup="--startup" in sys.argv)


if __name__ == "__main__":
    main()
