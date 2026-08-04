const won = new Intl.NumberFormat("ko-KR", { style: "currency", currency: "KRW", maximumFractionDigits: 0 });
const number = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 6 });
const rateNumber = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 7 });
const fxNumber = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const DASHBOARD_REFRESH_INTERVAL_MS = 60_000;

let portfolioRefreshPromise = null;

const state = {
  platforms: [],
  strategyCapabilities: { platforms: {} },
  strategies: [],
  editingStrategyId: null,
  summary: null,
  syncStatus: { latest: [], history: [], api_keys: [], alerts: [] },
  aliasTarget: null,
  assetFilters: {
    platform: "all",
    query: "",
    sort: "value_desc",
  },
};

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith("td_csrf="))
      ?.split("=", 2)[1];
    if (csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf);
  }
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const data = await response.json();
  if (response.status === 401) {
    window.location.assign("/login");
  }
  if (!response.ok) {
    const error = new Error(data.error || `API ${response.status}`);
    error.data = data;
    throw error;
  }
  return data;
}

function pnlClass(value) {
  return value >= 0 ? "positive" : "negative";
}

function platformLabel(platform) {
  return {
    upbit: "업비트",
    kis_pension: "한투(연금)",
    kis_isa: "한투(ISA)",
    toss: "토스",
  }[platform] ?? platform;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMetrics(total, exchangeRate) {
  const metrics = [
    ["평가금액", won.format(total.value)],
    ["매입금액", won.format(total.cost)],
    ["손익", `<span class="${pnlClass(total.pnl)}">${won.format(total.pnl)}</span>`],
    ["수익률", `<span class="${pnlClass(total.pnl)}">${total.pnl_pct.toFixed(2)}%</span>`],
  ];
  document.querySelector("#summaryCards").innerHTML =
    metrics
    .map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`)
    .join("") + fxCard(exchangeRate);
}

function table(headers, rows) {
  const labeledRows = rows.map((row) => {
    let cellIndex = 0;
    return row.replace(/<td(\s[^>]*)?>/g, (tag, attributes = "") => {
      const label = headers[cellIndex++] ?? "";
      return `<td${attributes} data-label="${escapeHtml(label)}">`;
    });
  });

  return `
    <table>
      <thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead>
      <tbody>${labeledRows.join("")}</tbody>
    </table>
  `;
}

function renderPlatforms() {
  document.querySelector("#platformStatus").innerHTML = state.platforms
    .map(
      (item) => `
        <div class="status-item">
          <div>
            <strong>${escapeHtml(item.name)}</strong>
            <div>${escapeHtml(item.category)} · 실거래 ${item.live_trading ? "허용" : "차단"}</div>
          </div>
          <span class="dot ${item.configured ? "ok" : ""}" title="${item.configured ? "키 설정됨" : "키 누락"}"></span>
        </div>
      `,
    )
    .join("");

  const options = state.platforms
    .map((p) => `<option value="${escapeHtml(p.code)}">${escapeHtml(p.name)}</option>`)
    .join("");
  document.querySelector('#strategyForm select[name="platform"]').innerHTML = `<option value="">전체</option>${options}`;
  updateStrategyFields();
  const assetFilter = document.querySelector("#assetPlatformFilter");
  const currentValue = state.platforms.some((p) => p.code === state.assetFilters.platform)
    ? state.assetFilters.platform
    : "all";
  state.assetFilters.platform = currentValue;
  assetFilter.innerHTML = [
    { code: "all", name: "전체" },
    ...state.platforms,
  ]
    .map(
      (platform) => `
        <button
          type="button"
          class="platform-filter-button${platform.code === currentValue ? " active" : ""}"
          data-platform="${escapeHtml(platform.code)}"
          aria-pressed="${platform.code === currentValue}"
        >${escapeHtml(platform.name)}</button>
      `,
    )
    .join("");
}

function renderPortfolio(summary) {
  renderMetrics(summary.total, summary.exchange_rate);
  renderCash(summary.cash);
  document.querySelector("#platformTable").innerHTML = table(
    ["플랫폼", "평가금액", "손익", "수익률"],
    summary.by_platform.map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.name)}</td>
          <td>${won.format(item.value)}</td>
          <td class="${pnlClass(item.pnl)}">${won.format(item.pnl)}</td>
          <td class="${pnlClass(item.pnl)}">${item.pnl_pct.toFixed(2)}%</td>
        </tr>
      `,
    ),
  );

  renderAssetTable();
  document.querySelector("#smallAssetsToggle").textContent = `펼치기 (${summary.small_symbols.length}개)`;

  document.querySelector("#smallSymbolTable").innerHTML = table(
    ["플랫폼", "종목", "상태", "수량", "현재가", "평가금액", "관리"],
    summary.small_symbols.length
      ? summary.small_symbols.map(
          (item) => `
        <tr>
          <td>${escapeHtml(platformLabel(item.platform))}</td>
          <td><strong>${escapeHtml(assetName(item))}</strong><br />${escapeHtml(item.symbol)}</td>
          <td>${valuationLabel(item.valuation_status)}</td>
          <td>${number.format(item.quantity)}</td>
          <td>${won.format(item.current_price)}</td>
          <td>${won.format(item.value)}</td>
          <td>${aliasButton(item)}</td>
        </tr>
      `,
        )
      : [`<tr><td colspan="7">평가금액 ${won.format(summary.dust_value_threshold)} 미만 자산이 없습니다.</td></tr>`],
  );
}

function fxCard(exchangeRate) {
  if (!exchangeRate) {
    return `<div class="metric fx-card unavailable"><span>USD/KRW 환율</span><strong>정보 없음</strong></div>`;
  }
  if (exchangeRate.status === "failed" || exchangeRate.rate == null) {
    return `
      <div class="metric fx-card failed">
        <div class="fx-card-heading"><span>USD/KRW 환율</span><span class="fx-badge">조회 실패</span></div>
        <strong>—</strong>
        <small>${escapeHtml(exchangeRate.error || "환율 정보를 가져오지 못했습니다.")}</small>
      </div>`;
  }

  const source = {
    configured: "설정값",
    tossinvest: "토스증권",
    "open.er-api.com": "ER-API",
    cached: "마지막 정상값",
  }[exchangeRate.source] || exchangeRate.source;
  const status = exchangeRate.status === "stale" ? "캐시 사용" : exchangeRate.source === "open.er-api.com" ? "대체 환율" : "정상";
  const details = exchangeRate.details || {};
  const detailRows = [
    details.mid_rate > 0 ? ["매매기준율", `₩${fxNumber.format(details.mid_rate)}`] : null,
    details.basis_point ? ["기준율 차이", `${number.format(details.basis_point)} bp`] : null,
    details.valid_until ? ["유효 시간", formatSyncTime(new Date(details.valid_until))] : null,
  ].filter(Boolean);
  const expandable = detailRows.length
    ? `<details class="fx-details"><summary>상세 정보</summary>${detailRows
        .map(([label, value]) => `<div><span>${label}</span><b>${escapeHtml(value)}</b></div>`)
        .join("")}<p>실제 주문 시 적용 환율과 다를 수 있습니다.</p></details>`
    : "";

  return `
    <div class="metric fx-card ${escapeHtml(exchangeRate.status)} source-${escapeHtml(exchangeRate.source)}">
      <div class="fx-card-heading">
        <span>USD/KRW 환율</span>
        <span class="fx-badge">${escapeHtml(status)}</span>
      </div>
      <strong>₩${fxNumber.format(exchangeRate.rate)}</strong>
      <div class="fx-meta"><b>${escapeHtml(source)}</b><span>${formatSyncTime(new Date(exchangeRate.fetched_at))}</span></div>
      ${expandable}
    </div>`;
}

function renderCash(cash) {
  const items = (cash?.by_platform ?? []).sort((a, b) => b.amount - a.amount);
  const platformCards = items
    .map(
      (item) => `
        <div class="cash-card">
          <span class="cash-card-label">${escapeHtml(item.name || platformLabel(item.platform))}</span>
          <strong>${won.format(item.amount)}</strong>
        </div>
      `,
    )
    .join("");
  document.querySelector("#cashCards").innerHTML = `
    <div class="cash-total-card">
      <div class="cash-total-heading">
        <span>전체 주문 가능 현금</span>
        <span class="cash-total-badge">합계</span>
      </div>
      <strong>${won.format(cash?.total ?? 0)}</strong>
      <small>모든 플랫폼의 주문 가능 현금</small>
    </div>
    <div class="cash-platforms">
      <div class="cash-platforms-label">플랫폼별 현금</div>
      <div class="cash-platform-grid">${platformCards || `<div class="cash-empty">표시할 플랫폼 현금이 없습니다.</div>`}</div>
    </div>`;
}

function renderAssetTable() {
  const items = filteredAssets();
  document.querySelector("#assetCount").textContent = `${items.length}개`;
  document.querySelector("#symbolTable").innerHTML = table(
    ["플랫폼", "종목", "수량", "평균가", "현재가", "평가금액", "손익", "수익률", "관리"],
    symbolRows(items),
  );
}

function filteredAssets() {
  if (!state.summary) return [];
  const query = state.assetFilters.query.trim().toLowerCase();
  const collator = new Intl.Collator("ko-KR");

  return state.summary.tradable_symbols
    .filter((item) => state.assetFilters.platform === "all" || item.platform === state.assetFilters.platform)
    .filter((item) => {
      if (!query) return true;
      return `${assetName(item)} ${item.name} ${item.symbol}`.toLowerCase().includes(query);
    })
    .sort((a, b) => {
      switch (state.assetFilters.sort) {
        case "pnl_asc":
          return a.pnl - b.pnl;
        case "pnl_desc":
          return b.pnl - a.pnl;
        case "pnl_pct_asc":
          return a.pnl_pct - b.pnl_pct;
        case "pnl_pct_desc":
          return b.pnl_pct - a.pnl_pct;
        case "name_asc":
          return collator.compare(assetName(a), assetName(b));
        case "value_desc":
        default:
          return b.value - a.value;
      }
    });
}

function symbolRows(items) {
  return items.length
    ? items.map(
        (item) => `
        <tr>
          <td>${escapeHtml(platformLabel(item.platform))}</td>
          <td><strong>${escapeHtml(assetName(item))}</strong><br />${escapeHtml(item.symbol)}</td>
          <td>${number.format(item.quantity)}</td>
          <td>${won.format(item.avg_price)}</td>
          <td>${won.format(item.current_price)}</td>
          <td>${won.format(item.value)}</td>
          <td class="${pnlClass(item.pnl)}">${won.format(item.pnl)}</td>
          <td class="${pnlClass(item.pnl)}">${item.pnl_pct.toFixed(2)}%</td>
          <td>${aliasButton(item)}</td>
        </tr>
      `,
      )
    : [`<tr><td colspan="9">표시할 일반 자산이 없습니다.</td></tr>`];
}

function assetName(item) {
  return item.display_name || item.alias || item.name || item.symbol;
}

function aliasButton(item) {
  return `
    <button
      class="table-action"
      type="button"
      data-alias-platform="${escapeHtml(item.platform)}"
      data-alias-symbol="${escapeHtml(item.symbol)}"
      data-alias-name="${escapeHtml(item.name)}"
      data-alias-value="${escapeHtml(item.alias || "")}"
    >${item.alias ? "별칭 수정" : "별칭 등록"}</button>
  `;
}

function valuationLabel(status) {
  return {
    cash: "현금",
    priced: "일반",
    dust: "100원 미만",
    unpriced: "가격 미확인",
  }[status] ?? status;
}

async function renderOrders() {
  const orders = await api("/api/orders");
  document.querySelector("#ordersTable").innerHTML = table(
    ["시간", "플랫폼", "종목", "신호", "수량", "주문 금액", "예상 비용", "예상 총 소요", "상태", "사유"],
    orders.length
      ? orders.map(
          (item) => `
        <tr>
          <td>${new Date(item.created_at).toLocaleString("ko-KR")}</td>
          <td>${escapeHtml(platformLabel(item.platform))}</td>
          <td>${escapeHtml(item.symbol)}</td>
          <td>${item.side === "buy" ? "매수" : "매도"}</td>
          <td>${item.quantity ?? "-"}</td>
          <td>${formatOrderAmount(item)}</td>
          <td>${formatOrderCosts(item)}</td>
          <td>${formatEstimatedTotal(item)}</td>
          <td>${escapeHtml(item.status)}</td>
          <td>${escapeHtml(item.reason)}</td>
        </tr>
      `,
        )
      : [`<tr><td colspan="10">아직 전략 실행 기록이 없습니다.</td></tr>`],
  );
}

async function renderExecutions() {
  const executions = await api("/api/executions");
  document.querySelector("#executionsTable").innerHTML = table(
    ["체결 시간", "플랫폼", "종목", "구분", "수량", "평균 체결가", "체결 금액", "수수료", "세금", "상태"],
    executions.length
      ? executions.map(
          (item) => `
        <tr>
          <td>${new Date(item.executed_at || item.ordered_at).toLocaleString("ko-KR")}</td>
          <td>${escapeHtml(platformLabel(item.platform))}</td>
          <td>${escapeHtml(item.display_name || item.symbol)}<br /><span class="muted">${escapeHtml(item.symbol)}</span></td>
          <td>${item.side === "buy" ? "매수" : "매도"}</td>
          <td>${number.format(item.quantity)}</td>
          <td>${formatCurrencyAmount(item.average_price, item.currency)}</td>
          <td>${formatCurrencyAmount(item.amount, item.currency)}</td>
          <td>${formatExecutionCost(item.fee, item.currency, item.fee_status, item.cost_profile?.fee_source?.label)}</td>
          <td>${formatExecutionCost(item.tax, item.currency, item.tax_status, item.cost_profile?.tax_source?.label)}</td>
          <td>${escapeHtml(item.status)}</td>
        </tr>
      `,
        )
      : [`<tr><td colspan="10">아직 동기화된 실제 체결 이력이 없습니다.</td></tr>`],
  );
}

function formatExecutionCost(value, currency, status, source) {
  if (value == null) return "확인 불가";
  const label = status === "actual" ? "실제" : "추정";
  return `
    <div class="order-cost-breakdown">
      <strong>${formatCurrencyAmount(value, currency)}</strong>
      <span>${escapeHtml(label)}${source ? ` · ${escapeHtml(source)}` : ""}</span>
    </div>`;
}

function formatOrderAmount(item) {
  if (item.amount == null) return "-";
  return formatCurrencyAmount(item.amount, item.currency);
}

function formatCurrencyAmount(value, currency) {
  if (value == null) return "-";
  return currency === "USD" ? `$${number.format(value)}` : `₩${number.format(value)}`;
}

function formatOrderCosts(item) {
  if (item.estimated_notional == null) return "기준가 없음";
  const profile = item.cost_profile || {};
  const feeSource = profile.fee_source?.label ? ` · ${escapeHtml(profile.fee_source.label)}` : "";
  const liveFallback = profile.live_fee_lookup?.status === "fallback" ? " · 실시간 조회 실패" : "";
  return `
    <div class="order-cost-breakdown">
      <strong>${formatCurrencyAmount(
        (item.estimated_fee || 0) + (item.estimated_tax || 0) + (item.estimated_slippage || 0),
        item.currency,
      )}</strong>
      <span>수수료 ${formatCurrencyAmount(item.estimated_fee, item.currency)} (${rateNumber.format(profile.fee_pct ?? 0)}%${feeSource}${liveFallback})</span>
      <span>세금 ${formatCurrencyAmount(item.estimated_tax, item.currency)} (${rateNumber.format(profile.tax_pct ?? 0)}%)</span>
      <span>슬리피지 ${formatCurrencyAmount(item.estimated_slippage, item.currency)} (${rateNumber.format(profile.slippage_pct ?? 0)}%)</span>
    </div>`;
}

function formatEstimatedTotal(item) {
  if (item.estimated_total == null) return "기준가 없음";
  const reference = item.reference_price == null
    ? ""
    : `<span>기준가 ${formatCurrencyAmount(item.reference_price, item.currency)}</span>`;
  return `
    <div class="order-cost-breakdown">
      <strong>${formatCurrencyAmount(item.estimated_total, item.currency)}</strong>
      <span>주문 원금 ${formatCurrencyAmount(item.estimated_notional, item.currency)}</span>
      ${reference}
    </div>`;
}

async function renderStrategyRuns() {
  const runs = await api("/api/strategy-runs");
  document.querySelector("#strategyRunsTable").innerHTML = table(
    ["시작", "전략", "실행 방식", "상태", "주문", "오류"],
    runs.length
      ? runs.map((run) => `
        <tr>
          <td>${new Date(run.started_at).toLocaleString("ko-KR")}</td>
          <td>${escapeHtml(run.strategy_name)}</td>
          <td>${run.trigger === "scheduled" ? "일정" : "수동 테스트"}</td>
          <td>${escapeHtml(run.status)}</td>
          <td>${run.order_count}건</td>
          <td>${escapeHtml(run.error || "-")}</td>
        </tr>
      `)
      : [`<tr><td colspan="6">아직 DCA 실행 이력이 없습니다.</td></tr>`],
  );
}

async function renderStrategies() {
  const strategies = await api("/api/strategies");
  state.strategies = strategies;
  const activeCount = strategies.filter((item) => item.enabled).length;
  document.querySelector("#strategyStats").innerHTML = `
    <span><strong>${activeCount}</strong> 활성</span>
    <span><strong>${strategies.length - activeCount}</strong> 중지</span>
    <span><strong>${strategies.length}</strong> 전체</span>
  `;
  document.querySelector("#strategiesList").innerHTML = strategies.length
    ? strategies.map(strategyCard).join("")
    : `<div class="strategy-empty"><strong>아직 등록된 전략이 없습니다.</strong><span>새 전략을 만들어 투자 계획을 시작하세요.</span></div>`;
}

function strategyCard(item) {
  const target = item.strategy_type === "dca"
    ? dcaItems(item, item.platform).map((entry) => `${entry.symbol} ${dcaItemValueLabel(entry)}`).join(" · ")
    : item.symbol || "전체 자산";
  const schedule = item.strategy_type === "dca"
    ? `${scheduleLabel(item.params)} · ${item.budget > 0 ? `일일 ${won.format(item.budget)}` : "예산 제한 없음"} · 최대 ${(item.params?.risk_limits?.max_orders_per_day || 20)}건`
    : `${won.format(item.budget || 0)} 예산`;
  return `
    <article class="strategy-card">
      <div class="strategy-card-main">
        <div class="strategy-card-title">
          <span class="strategy-status ${item.enabled ? "active" : ""}">${item.enabled ? "활성" : "중지"}</span>
          <h3>${escapeHtml(item.name)}</h3>
        </div>
        <div class="strategy-meta">
          <span>${item.strategy_type === "dca" ? "DCA" : escapeHtml(item.strategy_type)}</span>
          <span>${escapeHtml(item.platform ? platformLabel(item.platform) : "전체")}</span>
          <span>${escapeHtml(schedule)}</span>
        </div>
        <p>${escapeHtml(target)}</p>
      </div>
      <div class="strategy-card-actions">
        <button class="secondary" data-strategy="${escapeHtml(item.id)}" data-action="toggle" data-enabled="${!item.enabled}">${item.enabled ? "중지" : "활성"}</button>
        ${item.strategy_type === "dca" ? `<button class="table-action" data-strategy="${escapeHtml(item.id)}" data-action="dry-run">DRY_RUN 테스트</button>` : ""}
        <button class="table-action" data-strategy="${escapeHtml(item.id)}" data-action="edit">수정</button>
        <button class="danger-button" data-strategy="${escapeHtml(item.id)}" data-action="delete" data-name="${escapeHtml(item.name)}">삭제</button>
      </div>
    </article>
  `;
}

function dcaItems(strategy, platform) {
  if (Array.isArray(strategy.params?.items)) return strategy.params.items;
  return String(strategy.symbol || "")
    .split(",")
    .filter(Boolean)
    .map((symbol) => platform === "kis_pension" || platform === "kis_isa"
      ? { symbol, order_type: "quantity", quantity: strategy.params?.quantity || 1 }
      : { symbol, order_type: "amount", amount: strategy.params?.amount_usd || 1, currency: platform === "upbit" ? "KRW" : "USD" });
}

function dcaItemValueLabel(item) {
  if (item.order_type === "quantity") return `${number.format(item.quantity)}주`;
  const currency = item.currency || "USD";
  return currency === "USD" ? `$${number.format(item.amount ?? item.amount_usd)}` : `${won.format(item.amount)}`;
}

function dcaOrderSpec(platform, market = "overseas") {
  const platformCapability = state.strategyCapabilities.platforms?.[platform];
  const selectedMarket = market || platformCapability?.default_market;
  const capability = platformCapability?.markets?.[selectedMarket];
  if (!capability) {
    return { orderType: "amount", currency: "USD", label: "주문 값", title: "자산별 주문 설정", step: "0.01", min: "0.01" };
  }
  return {
    orderType: capability.order_mode,
    currency: capability.currency,
    label: capability.value_label,
    title: Object.keys(platformCapability.markets).length > 1 ? "자산별 시장 및 주문 설정" : `자산별 ${capability.value_label}`,
    step: String(capability.value_step),
    min: String(capability.value_min),
    placeholder: capability.symbol_placeholder,
  };
}

function intervalLabel(interval) {
  return { daily: "매일", weekly: "매주", monthly: "매월" }[interval] ?? interval;
}

function scheduleLabel(params = {}) {
  const suffix = params.interval === "weekly"
    ? ` ${weekdayLabel(params.execution_day || "monday")}`
    : params.interval === "monthly"
      ? ` ${params.execution_day || 1}일`
      : "";
  return `${intervalLabel(params.interval)}${suffix} ${params.execution_time || "23:30"} KST`;
}

function weekdayLabel(value) {
  return { monday: "월요일", tuesday: "화요일", wednesday: "수요일", thursday: "목요일", friday: "금요일", saturday: "토요일", sunday: "일요일" }[value] ?? value;
}

async function refreshPortfolio({ force = false } = {}) {
  if (portfolioRefreshPromise) {
    if (!force) return portfolioRefreshPromise;
    try {
      await portfolioRefreshPromise;
    } catch {
      // A forced refresh should retry after an earlier automatic refresh failed.
    }
  }

  const request = (async () => {
    const [platforms, summary, syncStatus] = await Promise.all([
      api("/api/platforms"),
      api("/api/portfolio/summary"),
      api("/api/sync/status"),
    ]);
    state.platforms = platforms;
    state.summary = summary;
    state.syncStatus = syncStatus;
    renderSyncStatus(syncStatus);
    renderPlatforms();
    renderPortfolio(summary);
  })();
  portfolioRefreshPromise = request;

  try {
    return await request;
  } finally {
    if (portfolioRefreshPromise === request) portfolioRefreshPromise = null;
  }
}

async function refresh({ forcePortfolio = false } = {}) {
  const [, strategyCapabilities] = await Promise.all([
    refreshPortfolio({ force: forcePortfolio }),
    api("/api/strategy-capabilities"),
  ]);
  state.strategyCapabilities = strategyCapabilities;
  await Promise.all([renderExecutions(), renderOrders(), renderStrategyRuns(), renderStrategies()]);
}

function refreshVisiblePortfolio() {
  if (document.visibilityState !== "visible") return;
  refreshPortfolio().catch((error) => {
    console.error("화면 자동 갱신 실패", error);
  });
}

function formObject(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ["budget", "take_profit_pct", "stop_loss_pct"]) {
    if (data[key] === "") delete data[key];
    else if (data[key] !== undefined) data[key] = Number(data[key]);
  }
  if (data.strategy_type === "dca") {
    const items = [...form.querySelectorAll(".dca-item-row")].map((row) => ({
      symbol: row.querySelector('[data-dca-input="symbol"]').value,
      market: row.querySelector('[data-dca-input="market"]').value,
      value: Number(row.querySelector('[data-dca-input="value"]').value),
    }));
    data.symbol = items.map((item) => item.symbol).join(",");
    data.budget = data.budget === undefined || data.budget === "" ? 0 : Number(data.budget);
    delete data.take_profit_pct;
    delete data.stop_loss_pct;
    data.params = {
      items,
      interval: data.interval,
      execution_time: data.execution_time,
      risk_limits: {
        daily_budget_krw: data.budget,
        max_orders_per_day: Number(data.max_orders_per_day || 20),
      },
      cost_overrides: {
        fee_pct: data.fee_pct === "" ? null : Number(data.fee_pct),
        tax_pct: data.tax_pct === "" ? null : Number(data.tax_pct),
        slippage_pct: Number(data.slippage_pct || 0),
      },
    };
    if (data.execution_day) data.params.execution_day = data.execution_day;
  }
  delete data.interval;
  delete data.execution_day;
  delete data.execution_time;
  delete data.fee_pct;
  delete data.tax_pct;
  delete data.slippage_pct;
  delete data.max_orders_per_day;
  data.enabled = false;
  return data;
}

function updateStrategyFields() {
  const form = document.querySelector("#strategyForm");
  const isDca = form.elements.strategy_type.value === "dca";
  const budgetLabel = document.querySelector("#strategyBudgetField");
  budgetLabel.firstChild.textContent = isDca ? "일일 예산 한도 (KRW)" : "예산 (KRW)";
  form.querySelectorAll("[data-dca-field]").forEach((element) => element.classList.toggle("hidden", !isDca));
  form.querySelectorAll("[data-standard-field]").forEach((element) => element.classList.toggle("hidden", isDca));
  updateExecutionDayField();
  updateDcaOrderFields();
}

function updateExecutionDayField() {
  const form = document.querySelector("#strategyForm");
  const field = document.querySelector("#executionDayField");
  const select = form.elements.execution_day;
  const interval = form.elements.interval.value;
  const isDca = form.elements.strategy_type.value === "dca";
  field.classList.toggle("hidden", !isDca || interval === "daily");
  if (interval === "weekly") {
    document.querySelector("#executionDayLabel").textContent = "실행 요일";
    select.innerHTML = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
      .map((day) => `<option value="${day}">${weekdayLabel(day)}</option>`)
      .join("");
  } else if (interval === "monthly") {
    document.querySelector("#executionDayLabel").textContent = "실행일";
    select.innerHTML = Array.from({ length: 28 }, (_, index) => `<option value="${index + 1}">${index + 1}일</option>`).join("");
  }
}

function updateDcaOrderFields() {
  const form = document.querySelector("#strategyForm");
  const platform = form.elements.platform.value;
  const platformCapability = state.strategyCapabilities.platforms?.[platform];
  document.querySelector("#dcaItemsTitle").textContent = dcaOrderSpec(platform, platformCapability?.default_market).title;
  for (const row of form.querySelectorAll(".dca-item-row")) {
    const marketLabel = row.querySelector("[data-dca-market-label]");
    const marketInput = row.querySelector('[data-dca-input="market"]');
    const markets = platformCapability?.markets || {};
    const currentMarket = markets[marketInput.value] ? marketInput.value : platformCapability?.default_market;
    marketInput.innerHTML = Object.entries(markets)
      .map(([value, capability]) => `<option value="${escapeHtml(value)}">${escapeHtml(capability.label)}</option>`)
      .join("");
    if (currentMarket) marketInput.value = currentMarket;
    marketLabel.classList.toggle("hidden", Object.keys(markets).length <= 1);
    const spec = dcaOrderSpec(platform, marketInput.value);
    const label = row.querySelector("[data-dca-value-label]");
    const input = row.querySelector('[data-dca-input="value"]');
    row.querySelector('[data-dca-input="symbol"]').placeholder = spec.placeholder || "종목 코드";
    label.childNodes[0].textContent = spec.label;
    input.step = spec.step;
    input.min = spec.min;
    if (spec.orderType === "quantity" && !Number.isInteger(Number(input.value))) input.value = "1";
  }
}

function addDcaItemRow(symbol = "", value = 1, market = "") {
  const row = document.createElement("div");
  row.className = "dca-item-row";
  row.innerHTML = `
    <label>종목 코드<input data-dca-input="symbol" value="${escapeHtml(symbol)}" placeholder="예: SCHD" required /></label>
    <label data-dca-market-label>시장<select data-dca-input="market"><option value="overseas">해외주식</option><option value="domestic">국내주식</option></select></label>
    <label data-dca-value-label>주문 값<input data-dca-input="value" type="number" value="${escapeHtml(value)}" required /></label>
    <button class="danger-button" data-remove-dca-item type="button">삭제</button>
  `;
  document.querySelector("#dcaItemRows").append(row);
  updateDcaOrderFields();
  if (market) {
    row.querySelector('[data-dca-input="market"]').value = market;
    updateDcaOrderFields();
  }
}

function resetDcaItems() {
  document.querySelector("#dcaItemRows").replaceChildren();
  addDcaItemRow();
}

document.querySelector("#refreshButton").addEventListener("click", refresh);

document.querySelector("#assetPlatformFilter").addEventListener("click", (event) => {
  const button = event.target.closest("[data-platform]");
  if (!button) return;

  state.assetFilters.platform = button.dataset.platform;
  document.querySelectorAll("#assetPlatformFilter [data-platform]").forEach((item) => {
    const isActive = item === button;
    item.classList.toggle("active", isActive);
    item.setAttribute("aria-pressed", String(isActive));
  });
  renderAssetTable();
});

document.querySelector("#assetSearchInput").addEventListener("input", (event) => {
  state.assetFilters.query = event.target.value;
  renderAssetTable();
});

document.querySelector("#assetSortSelect").addEventListener("change", (event) => {
  state.assetFilters.sort = event.target.value;
  renderAssetTable();
});

document.querySelector("#smallAssetsToggle").addEventListener("click", () => {
  const tableElement = document.querySelector("#smallSymbolTable");
  const toggle = document.querySelector("#smallAssetsToggle");
  const isHidden = tableElement.classList.toggle("hidden");
  const count = state.summary?.small_symbols.length ?? 0;
  toggle.textContent = isHidden ? `펼치기 (${count}개)` : `접기 (${count}개)`;
});

async function syncHoldings(path, label) {
  const message = document.querySelector("#syncMessage");
  message.textContent = `${label} 잔고와 체결 이력을 동기화하는 중입니다.`;
  document.querySelector("#syncDialogSummary").textContent = `${label} 잔고와 체결 이력을 동기화하는 중입니다.`;
  setSyncButtonsDisabled(true);
  try {
    const result = await api(path, { method: "POST" });
    renderSyncResult(result);
    await refresh({ forcePortfolio: true });
  } catch (error) {
    const detail = error.data?.error || error.data?.results?.find((item) => item.error)?.error || error.message;
    message.textContent = `${label} 동기화 실패: ${detail}`;
    document.querySelector("#syncDialogSummary").textContent = `${label} 동기화 실패: ${detail}`;
  } finally {
    setSyncButtonsDisabled(false);
  }
}

function setSyncButtonsDisabled(disabled) {
  for (const id of ["syncAllButton", "syncUpbitButton", "syncKisButton", "syncTossButton"]) {
    document.querySelector(`#${id}`).disabled = disabled;
  }
}

function renderSyncResult(result) {
  const message = document.querySelector("#syncMessage");
  if (result.status === "partial") {
    const failures = collectSyncResults(result).filter((item) => item.ok === false);
    message.textContent = `일부 동기화 실패: ${failures.map((item) => platformLabel(item.platform)).join(", ")}`;
    return;
  }

  const completed = collectSyncResults(result)
    .map((item) => item.completed_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  if (completed) message.textContent = `Last synced: ${formatSyncTime(new Date(completed))}`;
}

function collectSyncResults(result) {
  if (!Array.isArray(result.results)) return [result];
  return result.results.flatMap((item) => collectSyncResults(item));
}

function syncHealth(latest) {
  if (!latest?.length) return { status: "unknown", label: "동기화 미확인" };

  const expected = new Set(state.platforms.filter((item) => item.configured).map((item) => item.code));
  const completedPlatforms = new Set(latest.map((item) => item.platform));
  const hasMissing = [...expected].some((platform) => !completedPlatforms.has(platform));
  const running = latest.filter((item) => item.status === "running").length;
  const failed = latest.filter((item) => item.status === "failed").length;
  const succeeded = latest.filter((item) => item.status === "success").length;

  if (running) return { status: "partial", label: "동기화 중" };
  if (failed && !succeeded) return { status: "failed", label: "동기화 실패" };
  if (failed || hasMissing) return { status: "partial", label: "동기화 확인 필요" };
  return { status: "success", label: "동기화 정상" };
}

function renderSyncStatus(syncStatus) {
  const latest = syncStatus?.latest ?? [];
  const health = syncHealth(latest);
  const badge = document.querySelector("#syncStatusButton");
  badge.className = `sync-status-badge ${health.status}`;
  badge.querySelector("span:last-child").textContent = health.label;
  renderSyncDialog(syncStatus);

  if (!latest.length) return;
  const message = document.querySelector("#syncMessage");
  const failures = latest.filter((item) => item.status === "failed");
  if (failures.length) {
    message.textContent = `동기화 확인 필요: ${failures.map((item) => platformLabel(item.platform)).join(", ")}`;
    return;
  }

  const completed = latest
    .filter((item) => item.status === "success" && item.completed_at)
    .map((item) => item.completed_at)
    .sort()
    .at(-1);
  if (completed) message.textContent = `Last synced: ${formatSyncTime(new Date(completed))}`;
}

function renderSyncDialog(syncStatus) {
  const latest = syncStatus?.latest ?? [];
  const history = syncStatus?.history ?? [];
  const apiKeys = syncStatus?.api_keys ?? [];
  const alerts = syncStatus?.alerts ?? [];
  const health = syncHealth(latest);
  const completed = latest
    .map((item) => item.completed_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  document.querySelector("#syncDialogSummary").textContent = completed
    ? `${health.label} · Last synced: ${formatSyncTime(new Date(completed))}`
    : health.label;

  document.querySelector("#apiKeyCards").innerHTML = apiKeys.length
    ? apiKeys
        .map(
          (item) => `
            <article class="api-key-card">
              <div class="api-key-card-header">
                <strong>${escapeHtml(item.name)}</strong>
                <span class="api-key-days ${escapeHtml(item.status)}">${escapeHtml(apiKeyStatusText(item))}</span>
              </div>
              <p>${item.expires_at ? escapeHtml(item.expires_at) : "만료일 미설정"}</p>
            </article>
          `,
        )
        .join("")
    : `<div class="sync-empty">API 키 만료일 설정이 없습니다.</div>`;

  document.querySelector("#syncAlerts").innerHTML = alerts.length
    ? alerts
        .map(
          (item) => `
            <article class="sync-alert-card ${escapeHtml(item.severity)}">
              <div class="sync-alert-card-header">
                <strong>${escapeHtml(alertSeverityLabel(item.severity))} · ${escapeHtml(item.platform || "전체")}</strong>
                <button class="secondary compact-button" type="button" data-alert-id="${escapeHtml(item.id)}">확인 처리</button>
              </div>
              <p>${escapeHtml(item.message)}</p>
              <small>${escapeHtml(formatSyncTime(new Date(item.updated_at)))} · ${number.format(item.occurrences || 1)}회</small>
            </article>
          `,
        )
        .join("")
    : `<div class="sync-empty">확인하지 않은 장애 알림이 없습니다.</div>`;

  document.querySelector("#syncPlatformCards").innerHTML = latest.length
    ? latest
        .map(
          (item) => `
            <article class="sync-platform-card">
              <div class="sync-platform-card-header">
                <strong>${escapeHtml(platformLabel(item.platform))}</strong>
                <span class="sync-result ${escapeHtml(item.status)}">${escapeHtml(syncStatusLabel(item.status))}</span>
              </div>
              <p>${syncCountLabel(item)}</p>
              <p>${escapeHtml(syncRunTime(item))}</p>
              ${item.error ? `<p>${escapeHtml(item.error)}</p>` : ""}
            </article>
          `,
        )
        .join("")
    : `<div class="sync-empty">아직 동기화 기록이 없습니다.</div>`;

  document.querySelector("#syncHistory").innerHTML = history.length
    ? history
        .map(
          (item) => `
            <article class="sync-history-item">
              <div class="sync-history-item-header">
                <strong>${escapeHtml(platformLabel(item.platform))}</strong>
                <span class="sync-result ${escapeHtml(item.status)}">${escapeHtml(syncStatusLabel(item.status))}</span>
              </div>
              <p>${escapeHtml(syncRunTime(item))}${item.synced_count == null ? "" : ` · ${syncCountLabel(item)}`}</p>
              ${item.error ? `<p>${escapeHtml(item.error)}</p>` : ""}
            </article>
          `,
        )
        .join("")
    : `<div class="sync-empty">최근 실행 이력이 없습니다.</div>`;
}

function syncCountLabel(item) {
  if (item.synced_count == null) return "동기화 수량 없음";
  const executions = item.execution_count == null ? "" : ` · 체결 ${number.format(item.execution_count)}건`;
  return `자산 ${number.format(item.synced_count)}개${executions}`;
}

function apiKeyStatusText(item) {
  if (item.status === "unknown") return "미설정";
  if (item.status === "invalid") return "형식 오류";
  if (item.status === "expired") return `${Math.abs(item.days_remaining)}일 경과`;
  return `${item.days_remaining}일 남음`;
}

function syncStatusLabel(status) {
  return {
    success: "성공",
    failed: "실패",
    running: "진행 중",
  }[status] ?? status;
}

function alertSeverityLabel(severity) {
  return {
    error: "오류",
    warning: "주의",
    info: "안내",
  }[severity] ?? severity;
}

function syncRunTime(item) {
  const timestamp = item.completed_at || item.started_at;
  return timestamp ? formatSyncTime(new Date(timestamp)) : "시간 정보 없음";
}

function formatSyncTime(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}. ${pad(date.getMonth() + 1)}. ${pad(date.getDate())}. ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

document.querySelector("#syncAllButton").addEventListener("click", () => {
  syncHoldings("/api/sync/all", "전체");
});

document.querySelector("#syncUpbitButton").addEventListener("click", () => {
  syncHoldings("/api/sync/upbit", "업비트");
});

document.querySelector("#syncKisButton").addEventListener("click", () => {
  syncHoldings("/api/sync/kis", "한국투자");
});

document.querySelector("#syncTossButton").addEventListener("click", () => {
  syncHoldings("/api/sync/toss", "토스");
});

document.querySelector("#syncStatusButton").addEventListener("click", () => {
  document.querySelector("#syncStatusDialog").showModal();
});

document.querySelector("#syncDialogClose").addEventListener("click", () => {
  document.querySelector("#syncStatusDialog").close();
});

document.querySelector("#syncStatusDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

document.querySelector("#logoutButton").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    window.location.assign("/login");
  }
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-alias-symbol]");
  if (!button) return;

  state.aliasTarget = {
    platform: button.dataset.aliasPlatform,
    symbol: button.dataset.aliasSymbol,
    name: button.dataset.aliasName,
    alias: button.dataset.aliasValue,
  };
  document.querySelector("#aliasAssetInfo").textContent =
    `${platformLabel(state.aliasTarget.platform)} · ${state.aliasTarget.name} (${state.aliasTarget.symbol})`;
  const input = document.querySelector("#aliasInput");
  input.value = state.aliasTarget.alias;
  input.setCustomValidity("");
  document.querySelector("#aliasDeleteButton").hidden = !state.aliasTarget.alias;
  document.querySelector("#aliasDialog").showModal();
  input.focus();
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-alert-id]");
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/alerts/${encodeURIComponent(button.dataset.alertId)}?acknowledged=true`, { method: "PATCH" });
    await refreshPortfolio({ force: true });
  } catch (error) {
    button.disabled = false;
    document.querySelector("#syncMessage").textContent = `알림 확인 처리 실패: ${error.message}`;
  }
});

document.querySelector("#aliasForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.aliasTarget) return;

  const input = document.querySelector("#aliasInput");
  const alias = input.value.trim();
  if (!alias) {
    input.setCustomValidity("별칭을 입력해 주세요.");
    input.reportValidity();
    return;
  }

  input.setCustomValidity("");
  const path = aliasApiPath(state.aliasTarget);
  try {
    await api(path, { method: "PUT", body: JSON.stringify({ alias }) });
    closeAliasDialog();
    await refresh();
  } catch (error) {
    input.setCustomValidity(error.message);
    input.reportValidity();
  }
});

document.querySelector("#aliasDeleteButton").addEventListener("click", async () => {
  if (!state.aliasTarget) return;
  await api(aliasApiPath(state.aliasTarget), { method: "DELETE" });
  closeAliasDialog();
  await refresh();
});

function aliasApiPath(target) {
  return `/api/asset-aliases/${encodeURIComponent(target.platform)}/${encodeURIComponent(target.symbol)}`;
}

function closeAliasDialog() {
  document.querySelector("#aliasDialog").close();
  state.aliasTarget = null;
}

document.querySelector("#aliasDialogClose").addEventListener("click", closeAliasDialog);
document.querySelector("#aliasCancelButton").addEventListener("click", closeAliasDialog);
document.querySelector("#aliasDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeAliasDialog();
});

document.querySelector("#strategyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const path = state.editingStrategyId ? `/api/strategies/${state.editingStrategyId}` : "/api/strategies";
  await api(path, { method: state.editingStrategyId ? "PUT" : "POST", body: JSON.stringify(formObject(form)) });
  closeStrategyDialog();
  await renderStrategies();
});

function openStrategyDialog(strategy = null) {
  const form = document.querySelector("#strategyForm");
  form.reset();
  state.editingStrategyId = strategy?.id ?? null;
  document.querySelector("#strategyDialogTitle").textContent = strategy ? "전략 수정" : "새 전략 만들기";
  document.querySelector("#strategySubmitButton").textContent = strategy ? "변경사항 저장" : "전략 저장";
  resetDcaItems();
  if (strategy) {
    form.elements.name.value = strategy.name;
    form.elements.strategy_type.value = strategy.strategy_type;
    form.elements.platform.value = strategy.platform || "";
    form.elements.symbol.value = strategy.symbol || "";
    form.elements.budget.value = strategy.budget || 0;
    form.elements.max_orders_per_day.value = strategy.params?.risk_limits?.max_orders_per_day || strategy.params?.max_orders_per_day || 20;
    form.elements.take_profit_pct.value = strategy.take_profit_pct ?? "";
    form.elements.stop_loss_pct.value = strategy.stop_loss_pct ?? "";
    form.elements.interval.value = strategy.params?.interval || "daily";
    updateExecutionDayField();
    if (strategy.params?.execution_day) form.elements.execution_day.value = strategy.params.execution_day;
    form.elements.execution_time.value = strategy.params?.execution_time || "23:30";
    const costOverrides = strategyCostOverrides(strategy.params);
    form.elements.fee_pct.value = costOverrides.fee_pct ?? "";
    form.elements.tax_pct.value = costOverrides.tax_pct ?? "";
    form.elements.slippage_pct.value = costOverrides.slippage_pct ?? 0;
    if (strategy.strategy_type === "dca") {
      document.querySelector("#dcaItemRows").replaceChildren();
      for (const item of dcaItems(strategy, strategy.platform)) {
        addDcaItemRow(item.symbol, item.order_type === "quantity" ? item.quantity : (item.amount ?? item.amount_usd), item.market);
      }
    }
  }
  updateStrategyFields();
  updateStrategyPreview();
  document.querySelector("#strategyDialog").showModal();
  form.elements.name.focus();
}

function closeStrategyDialog() {
  document.querySelector("#strategyDialog").close();
  state.editingStrategyId = null;
}

function updateStrategyPreview() {
  const form = document.querySelector("#strategyForm");
  const name = form.elements.name.value.trim() || "이 전략";
  const platform = platformLabel(form.elements.platform.value || "플랫폼 미선택");
  let description;
  if (form.elements.strategy_type.value === "dca") {
    const assets = [...form.querySelectorAll(".dca-item-row")]
      .map((row) => {
        const symbol = row.querySelector('[data-dca-input="symbol"]').value.trim();
        const value = row.querySelector('[data-dca-input="value"]').value;
        const spec = dcaOrderSpec(form.elements.platform.value, row.querySelector('[data-dca-input="market"]').value);
        if (!symbol) return "";
        return `${symbol} ${spec.orderType === "quantity" ? `${value}주` : spec.currency === "USD" ? `$${value}` : `${number.format(value)}원`}`;
      })
      .filter(Boolean)
      .join(", ") || "선택한 자산";
    const costs = [
      form.elements.fee_pct.value === "" ? "수수료 공식 자동" : `수수료 직접 설정 ${number.format(Number(form.elements.fee_pct.value))}%`,
      form.elements.tax_pct.value === "" ? "매수 세금 자동" : `세금 직접 설정 ${number.format(Number(form.elements.tax_pct.value))}%`,
      `슬리피지 ${number.format(Number(form.elements.slippage_pct.value || 0))}%`,
    ].join(", ");
    const riskLimits = [
      Number(form.elements.budget.value || 0) > 0 ? `일일 예산 ${won.format(Number(form.elements.budget.value))}` : "일일 예산 제한 없음",
      `일일 최대 ${Number(form.elements.max_orders_per_day.value || 20)}건`,
    ].join(", ");
    description = `${platform}에서 ${scheduleLabel({ interval: form.elements.interval.value, execution_time: form.elements.execution_time.value, execution_day: form.elements.execution_day.value })}에 다음 자산을 매수합니다: ${assets}. 비용 가정: ${costs}. 리스크 제한: ${riskLimits}.`;
  } else {
    description = `${platform}에서 ${won.format(Number(form.elements.budget.value || 0))} 예산으로 ${form.elements.strategy_type.value} 전략을 운용합니다.`;
  }
  document.querySelector("#strategyPreview").innerHTML = `<span>저장 전 확인</span><strong>${escapeHtml(name)}</strong><p>${escapeHtml(description)}</p>`;
}

function strategyCostOverrides(params = {}) {
  if (params.cost_overrides) return params.cost_overrides;
  const legacy = params.cost_assumptions;
  if (!legacy || Object.values(legacy).every((value) => Number(value || 0) === 0)) {
    return { fee_pct: null, tax_pct: null, slippage_pct: 0 };
  }
  return legacy;
}

document.querySelector("#newStrategyButton").addEventListener("click", () => openStrategyDialog());
document.querySelector("#strategyDialogClose").addEventListener("click", closeStrategyDialog);
document.querySelector("#strategyCancelButton").addEventListener("click", closeStrategyDialog);
document.querySelector("#strategyDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeStrategyDialog();
});
document.querySelector("#strategyForm").addEventListener("input", updateStrategyPreview);
document.querySelector("#strategyForm").addEventListener("change", updateStrategyPreview);
document.querySelector('#strategyForm select[name="strategy_type"]').addEventListener("change", updateStrategyFields);
document.querySelector('#strategyForm select[name="platform"]').addEventListener("change", updateDcaOrderFields);
document.querySelector('#strategyForm select[name="interval"]').addEventListener("change", () => {
  updateExecutionDayField();
  updateStrategyPreview();
});
document.querySelector("#addDcaItemButton").addEventListener("click", () => {
  addDcaItemRow();
  updateStrategyPreview();
});
document.querySelector("#dcaItemRows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-dca-item]");
  if (!button) return;
  if (document.querySelectorAll(".dca-item-row").length === 1) return;
  button.closest(".dca-item-row").remove();
  updateStrategyPreview();
});
document.querySelector("#dcaItemRows").addEventListener("change", (event) => {
  if (event.target.matches('[data-dca-input="market"]')) updateDcaOrderFields();
});

resetDcaItems();

document.querySelector("#strategiesList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-strategy]");
  if (!button) return;
  if (button.dataset.action === "edit") {
    const strategy = state.strategies.find((item) => String(item.id) === button.dataset.strategy);
    if (strategy) openStrategyDialog(strategy);
    return;
  }
  if (button.dataset.action === "delete") {
    if (!window.confirm(`"${button.dataset.name}" 전략을 삭제할까요?`)) return;
    await api(`/api/strategies/${button.dataset.strategy}`, { method: "DELETE" });
    await renderStrategies();
    return;
  }
  if (button.dataset.action === "dry-run") {
    try {
      await api(`/api/strategies/${button.dataset.strategy}/dry-run`, { method: "POST" });
    } catch (error) {
      window.alert(error.data?.error || error.message);
    }
    await Promise.all([renderOrders(), renderStrategyRuns()]);
    return;
  }
  await api(`/api/strategies/${button.dataset.strategy}/enabled?value=${button.dataset.enabled}`, { method: "PATCH" });
  await renderStrategies();
});

refresh().catch((error) => {
  document.body.innerHTML = `<main><section class="section"><h2>초기화 실패</h2><p>${escapeHtml(error.message)}</p></section></main>`;
});

setInterval(refreshVisiblePortfolio, DASHBOARD_REFRESH_INTERVAL_MS);
document.addEventListener("visibilitychange", refreshVisiblePortfolio);
window.addEventListener("focus", refreshVisiblePortfolio);
