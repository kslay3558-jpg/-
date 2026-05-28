# IRQ Optimizer (Ultimate Safe Edition)

Windows 환경에서 특정 장치(GPU/오디오/스토리지/NIC)의 IRQ Affinity를 수동으로 지정해  
게임 중 입력 지연(Input Lag)과 스터터링(특히 1% Low) 완화를 목표로 하는 도구입니다.

## 서론(문제의식) 초안 평가

제시하신 서론은 **게이밍 관점의 문제 정의가 명확**하고, 아래 강점이 있습니다.

- Windows 기본 스케줄러의 한계를 “공평 분배” 관점에서 잘 짚음
- AMD 칩렛/Infinity Fabric, Intel P/E 코어 구조의 지연 리스크를 실사용 시나리오로 설명
- “평균 FPS보다 1% Low 개선”이라는 실제 체감 포인트를 명확히 제시

다만 문서 신뢰도를 높이려면 아래를 함께 명시하는 것을 권장합니다.

- 시스템/드라이버/게임별 편차가 큼 (항상 동일 개선 보장 불가)
- 잘못된 코어 고정 시 오히려 성능 저하 가능
- 변경 전 백업/복구 절차가 필수

---

## GitHub README에서 CSS 적용 가능 여부

요약: **README.md 자체에는 커스텀 CSS를 직접 적용하기 어렵습니다.**  
GitHub는 보안상 `<style>` 태그/외부 CSS 링크를 제한합니다.

대신 아래 방식이 현실적입니다.

1. README는 GitHub 친화 마크다운(표, 배지, 접기 섹션)으로 구성  
2. 별도 HTML 가이드(`docs/guide.html`) + CSS(`docs/style.css`)를 제공  
3. 필요 시 GitHub Pages로 배포해 “스타일 적용된 문서”로 안내

이 저장소에는 위 방식을 바로 쓸 수 있도록 `docs/guide.html`, `docs/style.css`를 포함했습니다.

---

## 요구 사항

- Windows 10/11
- 관리자 권한 실행
- Python 3.9+ 권장

> `winreg`를 사용하므로 Windows 이외 환경에서는 동작하지 않습니다.

## 빠른 시작

1. 관리자 권한 PowerShell 또는 CMD 실행
2. 저장소 경로로 이동
3. 아래 명령 실행

```bash
python irq_optimizer_ultimate_safe.py
```

앱은 관리자 권한이 아닐 경우 UAC로 자동 재실행을 시도합니다.

## 상세 사용 가이드

### 1) 장치 스캔

- `Refresh Devices` 클릭
- 장치 목록이 로드되면 역할 태그(`[GPU]`, `[GPU-ROOT]`, `[AUDIO]`, `[STORAGE]`, `[NIC]`)를 확인

### 2) 대상 장치 자동 선택(권장)

- `Select Target Devices` 클릭
- IRQ 튜닝 우선 대상(GPU → GPU Root Port → Audio → Storage → NIC)이 자동 선택됨

### 3) 코어 선택

- 장치 하나를 클릭하면 현재 마스크가 있으면 반영되어 표시됨
- `Use Recommended` 클릭 시 CPU 아키텍처 기반 권장 코어 자동 체크
- 필요 시 체크박스로 수동 조정

### 4) 적용

- `Apply` 클릭
- 앱이 현재 상태를 백업한 뒤 `DevicePolicy=4`, `AssignmentSetOverride`를 기록
- 즉시 검증 후 성공/실패 메시지 표시

### 5) 복구

- **개별 장치 마지막 변경 되돌리기:** `Undo Last`
- **해당 장치 커스텀 IRQ 값 제거:** `Factory Reset`

백업 파일 위치:

```text
C:\ProgramData\IRQOptimizer\irq_backup.json
```

## 권장 운영 팁

- 한 번에 모든 장치를 건드리지 말고, 핵심 장치부터 순차 적용
- 적용 후 게임 10~20분 플레이로 체감/프레임타임 비교
- 문제 발생 시 `Undo Last` 또는 `Factory Reset`로 즉시 복구

## 안전 주의 사항

- 레지스트리 변경 작업이므로 반드시 관리자 권한으로 실행
- 64개(Processor Group 0) 초과 환경은 현재 범위 밖
- 오버클럭/언더볼팅/백그라운드 앱 상태에 따라 결과 편차 발생 가능

## 스타일 적용 문서

- 로컬 미리보기: [`docs/guide.html`](docs/guide.html)
- 스타일 시트: [`docs/style.css`](docs/style.css)

로컬에서 스타일 문서를 열면(브라우저로 `docs/guide.html` 오픈)  
README보다 깔끔한 카드형 안내 레이아웃을 볼 수 있습니다.