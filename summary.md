
# ⚙️ Render 완전 호환 버전 (Python 3.11.9 + Upbit 자동매매 + Telegram 승인 + 뉴스 기능)

이 버전은 Render 서버에 바로 업로드할 수 있도록 구성된 **실행 가능한 완성형 패키지**입니다.

## 📦 주요 특징
- **Python 3.11.9 런타임 명시** (runtime.txt 포함)
- **Flask + Telegram + GridTrader + News Collector 통합**
- **업비트 호가단위/최소단위 검증 로직 포함**
- **시뮬레이션 / 실거래 전환 가능**
- **Render 호환 환경 (.env.example, Procfile, runtime.txt 포함)**

## 🚀 Render 설정 요약
| 항목 | 값 |
|------|----|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python app.py` |
| Python Version | 3.11.9 |
| Port | 8080 |
| Environment Vars | `.env.example` 참고 |

## 📡 주요 기능
- 텔레그램에서 실시간 매수/매도 승인
- 완전 자동모드(/auto), 수동 승인 모드(/manual)
- 뉴스 수집 및 전략 자동 추천 (CoinDesk / CoinTelegraph)
- 호가단위 자동 반올림 및 최소금액 검사 (Upbit 규칙)
- QuotaGuard Static IP 프록시 연동 가능 (실거래 지원)

## 🔐 보안 권장사항
- Render에는 **조회(Read)** 및 **주문(Trade)** 권한까지만 있는 키 사용
- **출금(Withdraw)** 권한은 절대 활성화하지 말 것
- `.env` 파일은 GitHub에 커밋 금지

## 🧠 실행 명령
```bash
pip install -r requirements.txt
python app.py
```

Render에 업로드 시, `.env.example` 내용을 Environment 탭에 복사해 변수로 등록하세요.
