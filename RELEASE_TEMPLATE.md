## ⚠️ 중요 안내 (백신 오진 가능성)

이 프로그램은 Windows 레지스트리(HKLM)의 IRQ Affinity 정책을 변경하므로, 아래 특성 때문에 일부 백신에서 오진(false positive)될 수 있습니다.

- PyInstaller 단일 실행 파일(onefile) 패키징
- 관리자 권한(UAC) 요청
- PowerShell 기반 하드웨어/장치 조회
- IRQ 정책 레지스트리 값 쓰기

본 프로젝트는 오픈소스이며, 실행 전에 저장소에서 동작을 직접 검토할 수 있습니다.

## 이번 릴리즈 포함 항목

- `IRQOptimizer.exe` (`build.spec` 기반 빌드)

## 안전 및 사용 주의사항

- Windows 10/11 전용
- 반드시 관리자 권한으로 실행
- Apply / Undo / Reset 후 시스템 재부팅 필수
- 사용에 따른 책임은 사용자에게 있습니다 (README, LICENSE 면책 조항 참고)
