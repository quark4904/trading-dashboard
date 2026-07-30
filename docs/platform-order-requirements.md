# 플랫폼별 DCA 주문 요구사항

조사 기준일: 2026-07-30

이 문서는 전략 입력과 주문 요청 생성 규칙의 근거를 기록한다. 실제 주문 전송은 아직 비활성화되어 있으며, 실행 시점에는 주문 가능 금액·시장 운영 시간·종목별 주문 가능 정보를 다시 조회해야 한다.

## 토스증권

공식 자료:

- [토스증권 Open API](https://developers.tossinvest.com/)
- [Canonical OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)

주문 생성은 `POST /api/v1/orders`이며 `Authorization`과 `X-Tossinvest-Account` 헤더가 필요하다.

| 시장 | DCA 주문 | 주요 필드 | 제약 |
|---|---|---|---|
| 국내주식 | 수량 기반 시장가 매수 | `symbol`, `side=BUY`, `orderType=MARKET`, `quantity` | 매수 수량은 양의 정수 |
| 미국주식 | 금액 기반 시장가 매수 | `symbol`, `side=BUY`, `orderType=MARKET`, `orderAmount` | USD 금액, 정규장만 가능 |

`clientOrderId`는 최대 36자의 멱등성 키로 사용할 수 있다. 1억 원 이상 주문은 별도의 고액 주문 확인 플래그가 필요하므로 자동 DCA 범위에서는 거부하는 것이 안전하다.

Open API 안내 기준 국내주식 수수료는 KRX 0.015%, NXT 0.014%다. 미국주식은 0.1%이며
주문건별 총 체결금액이 10 USD 이하이면 수수료가 면제된다. 이 규칙은 외부 정책 파일에
저장하며 시장이나 금액 조건 변경 시 정책 파일을 갱신한다.

## 한국투자증권

공식 자료:

- [KIS Developers](https://apiportal.koreainvestment.com/)
- [공식 Open Trading API GitHub](https://github.com/koreainvestment/open-trading-api)
- [국내주식 현금주문 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py)
- [해외주식 주문 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/order/order.py)

현재 앱에 등록된 `kis_pension`, `kis_isa` 계좌는 국내주식 계좌로 취급한다.

국내 현금주문은 `POST /uapi/domestic-stock/v1/trading/order-cash`를 사용한다. 계좌번호와 상품코드는 계좌 설정에서 주입하며 전략 항목에는 다음 값이 필요하다.

| 필드 | DCA 값 |
|---|---|
| `PDNO` | 국내 종목 코드 |
| `ORD_DVSN` | `01` 시장가 |
| `ORD_QTY` | 양의 정수 수량 |
| `ORD_UNPR` | `0` |
| `EXCG_ID_DVSN_CD` | `KRX` |

실전 매수 TR ID는 공식 최신 예제 기준 `TTTC0012U`, 모의 매수는 `VTTC0012U`다. 해외주식은 거래소 코드와 수량·주문단가가 추가로 필요하지만 현재 앱에는 해외 KIS 계좌가 등록되어 있지 않으므로 capability에서 노출하지 않는다.

공식 BanKIS 온라인 KRX 기본 수수료는 국내주식 0.0140527%, ETF/ETN 0.0146527%를
정책 기본값으로 사용한다. 실제 연금·ISA 계좌의 우대 요율이 다르면 전략별 override를
설정해야 한다.

## 업비트

공식 자료:

- [주문 생성](https://docs.upbit.com/kr/reference/new-order)
- [페어별 주문 가능 정보](https://docs.upbit.com/kr/kr/reference/available-order-information)

DCA 시장가 매수는 `POST /v1/orders`에 다음 JSON을 전달한다.

| 필드 | 값 |
|---|---|
| `market` | `KRW-BTC` 형태의 페어 |
| `side` | `bid` |
| `ord_type` | `price` |
| `price` | 매수에 사용할 KRW 총액 |

실행 직전 `GET /v1/orders/chance?market={market}`로 지원 주문 유형, 수수료, 잔고, 최소·최대 주문 금액을 확인해야 한다. `identifier`를 고유하게 부여하면 중복 실행 방지에 활용할 수 있다.

현재 DRY_RUN은 이 API의 `bid_fee`를 읽어 정책 파일의 KRW 마켓 기본 수수료 0.05%보다
우선 적용한다. 조회가 실패하면 정책 기본값을 사용하고 실패 사실을 주문의 비용 프로필에
기록한다.

## 구현 원칙

- 프론트엔드는 `/api/strategy-capabilities`를 읽어 플랫폼과 시장에 필요한 입력만 표시한다.
- 서버 검증도 동일 capability 정의를 사용해 UI와 검증 규칙의 불일치를 막는다.
- `compile_dca_buy_request`는 저장된 DCA 항목을 플랫폼별 주문 요청 본문으로 변환한다.
- 수수료 숫자는 계산 코드에 두지 않고 `config/fee-policies.json`에서 관리한다.
- 정책 파일은 실행마다 다시 읽어 다음 DRY_RUN부터 변경 요율을 반영한다.
- 계좌번호, API 키, 토큰, TR ID 같은 실행 환경 값은 전략에 저장하지 않고 주문 어댑터가 주입한다.
- 현재는 DRY_RUN이며 실제 주문 API를 호출하지 않는다.
