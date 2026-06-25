const won = new Intl.NumberFormat("ko-KR", { style: "currency", currency: "KRW", maximumFractionDigits: 0 });
const number = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 6 });

const state = {
  platforms: [],
  summary: null,
  assetFilters: {
    platform: "all",
    query: "",
    sort: "value_desc",
  },
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json();
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

function renderMetrics(total) {
  document.querySelector("#summaryCards").innerHTML = [
    ["평가금액", won.format(total.value)],
    ["매입금액", won.format(total.cost)],
    ["손익", `<span class="${pnlClass(total.pnl)}">${won.format(total.pnl)}</span>`],
    ["수익률", `<span class="${pnlClass(total.pnl)}">${total.pnl_pct.toFixed(2)}%</span>`],
  ]
    .map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
}

function table(headers, rows) {
  return `
    <table>
      <thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table>
  `;
}

function renderPlatforms() {
  document.querySelector("#platformStatus").innerHTML = state.platforms
    .map(
      (item) => `
        <div class="status-item">
          <div>
            <strong>${item.name}</strong>
            <div>${item.category} · 실거래 ${item.live_trading ? "허용" : "차단"}</div>
          </div>
          <span class="dot ${item.configured ? "ok" : ""}" title="${item.configured ? "키 설정됨" : "키 누락"}"></span>
        </div>
      `,
    )
    .join("");

  const options = state.platforms.map((p) => `<option value="${p.code}">${p.name}</option>`).join("");
  document.querySelector('#strategyForm select[name="platform"]').innerHTML = `<option value="">전체</option>${options}`;
  const assetFilter = document.querySelector("#assetPlatformFilter");
  const currentValue = assetFilter.value || "all";
  assetFilter.innerHTML = `<option value="all">전체 플랫폼</option>${options}`;
  assetFilter.value = state.platforms.some((p) => p.code === currentValue) ? currentValue : "all";
}

function renderPortfolio(summary) {
  renderMetrics(summary.total);
  renderCash(summary.cash);
  document.querySelector("#platformTable").innerHTML = table(
    ["플랫폼", "평가금액", "손익", "수익률"],
    summary.by_platform.map(
      (item) => `
        <tr>
          <td>${item.name}</td>
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
    ["플랫폼", "종목", "상태", "수량", "현재가", "평가금액"],
    summary.small_symbols.length
      ? summary.small_symbols.map(
          (item) => `
        <tr>
          <td>${platformLabel(item.platform)}</td>
          <td><strong>${item.name}</strong><br />${item.symbol}</td>
          <td>${valuationLabel(item.valuation_status)}</td>
          <td>${number.format(item.quantity)}</td>
          <td>${won.format(item.current_price)}</td>
          <td>${won.format(item.value)}</td>
        </tr>
      `,
        )
      : [`<tr><td colspan="6">평가금액 ${won.format(summary.dust_value_threshold)} 미만 자산이 없습니다.</td></tr>`],
  );
}

function renderCash(cash) {
  const items = cash?.by_platform ?? [];
  const cards = [
    { platform: "total", name: "전체", amount: cash?.total ?? 0 },
    ...items.sort((a, b) => b.amount - a.amount),
  ];
  document.querySelector("#cashCards").innerHTML = cards
    .map(
      (item) => `
        <div class="cash-card">
          <span>${item.name || platformLabel(item.platform)}</span>
          <strong>${won.format(item.amount)}</strong>
        </div>
      `,
    )
    .join("");
}

function renderAssetTable() {
  const items = filteredAssets();
  document.querySelector("#assetCount").textContent = `${items.length}개`;
  document.querySelector("#symbolTable").innerHTML = table(
    ["플랫폼", "종목", "수량", "평균가", "현재가", "평가금액", "손익", "수익률"],
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
      return `${item.name} ${item.symbol}`.toLowerCase().includes(query);
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
          return collator.compare(a.name, b.name);
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
          <td>${platformLabel(item.platform)}</td>
          <td><strong>${item.name}</strong><br />${item.symbol}</td>
          <td>${number.format(item.quantity)}</td>
          <td>${won.format(item.avg_price)}</td>
          <td>${won.format(item.current_price)}</td>
          <td>${won.format(item.value)}</td>
          <td class="${pnlClass(item.pnl)}">${won.format(item.pnl)}</td>
          <td class="${pnlClass(item.pnl)}">${item.pnl_pct.toFixed(2)}%</td>
        </tr>
      `,
      )
    : [`<tr><td colspan="8">표시할 일반 자산이 없습니다.</td></tr>`];
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
    ["시간", "플랫폼", "종목", "신호", "수량", "금액", "상태", "사유"],
    orders.length
      ? orders.map(
          (item) => `
        <tr>
          <td>${new Date(item.created_at).toLocaleString("ko-KR")}</td>
          <td>${platformLabel(item.platform)}</td>
          <td>${item.symbol}</td>
          <td>${item.side === "buy" ? "매수" : "매도"}</td>
          <td>${item.quantity ?? "-"}</td>
          <td>${item.amount ? won.format(item.amount) : "-"}</td>
          <td>${item.status}</td>
          <td>${item.reason}</td>
        </tr>
      `,
        )
      : [`<tr><td colspan="8">아직 전략 실행 기록이 없습니다.</td></tr>`],
  );
}

async function renderStrategies() {
  const strategies = await api("/api/strategies");
  document.querySelector("#strategiesTable").innerHTML = table(
    ["상태", "전략", "유형", "대상", "예산", "익절", "손절", "작업"],
    strategies.map(
      (item) => `
        <tr>
          <td>${item.enabled ? "활성" : "중지"}</td>
          <td>${item.name}</td>
          <td>${item.strategy_type}</td>
          <td>${item.platform ? platformLabel(item.platform) : "전체"} ${item.symbol || ""}</td>
          <td>${won.format(item.budget || 0)}</td>
          <td>${item.take_profit_pct ?? "-"}%</td>
          <td>${item.stop_loss_pct ?? "-"}%</td>
          <td><button class="secondary" data-strategy="${item.id}" data-enabled="${!item.enabled}">${item.enabled ? "중지" : "활성"}</button></td>
        </tr>
      `,
    ),
  );
}

async function refresh() {
  const [platforms, summary] = await Promise.all([api("/api/platforms"), api("/api/portfolio/summary")]);
  state.platforms = platforms;
  state.summary = summary;
  renderPlatforms();
  renderPortfolio(summary);
  await Promise.all([renderOrders(), renderStrategies()]);
}

function formObject(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ["budget", "take_profit_pct", "stop_loss_pct"]) {
    if (data[key] === "") delete data[key];
    else if (data[key] !== undefined) data[key] = Number(data[key]);
  }
  data.enabled = form.querySelector('[name="enabled"]')?.checked ?? false;
  return data;
}

document.querySelector("#refreshButton").addEventListener("click", refresh);

document.querySelector("#assetPlatformFilter").addEventListener("change", (event) => {
  state.assetFilters.platform = event.target.value;
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
  message.textContent = `${label} 잔고를 동기화하는 중입니다.`;
  try {
    const result = await api(path, { method: "POST" });
    message.textContent = syncMessage(label, result);
    await refresh();
  } catch (error) {
    message.textContent = `${label} 동기화 실패: ${error.message}`;
  }
}

function syncMessage(label, result) {
  if (result.synced_count !== undefined) return `${label} ${result.synced_count}개 자산을 동기화했습니다.`;
  if (Array.isArray(result.results)) {
    const failed = result.results.filter((item) => item.ok === false).length;
    return failed ? `${label} 완료: 성공 ${result.results.length - failed}개, 실패 ${failed}개` : `${label} 동기화 완료`;
  }
  return `${label} 동기화 완료`;
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

document.querySelector("#strategyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/api/strategies", { method: "POST", body: JSON.stringify(formObject(form)) });
  form.reset();
  await renderStrategies();
});

document.querySelector("#strategiesTable").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-strategy]");
  if (!button) return;
  await api(`/api/strategies/${button.dataset.strategy}/enabled?value=${button.dataset.enabled}`, { method: "PATCH" });
  await renderStrategies();
});

refresh().catch((error) => {
  document.body.innerHTML = `<main><section class="section"><h2>초기화 실패</h2><p>${error.message}</p></section></main>`;
});
