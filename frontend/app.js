'use strict';

// ── Tab navigation ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'live' && !livePolling) {
      runScanner();
    }
  });
});

// ── Chart instances ───────────────────────────────────────────────────────────
let priceChart = null;
let portfolioChart = null;

function mkChart(id, type, data, options) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, { type, data, options });
}

const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  plugins: { legend: { labels: { color: '#8b949e', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#21262d' } },
    y: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } },
  },
};

function initCharts() {
  if (priceChart) priceChart.destroy();
  if (portfolioChart) portfolioChart.destroy();

  priceChart = mkChart('price-chart', 'line', {
    labels: [],
    datasets: [
      { label: 'Preis (USDT)', data: [], borderColor: '#58a6ff', borderWidth: 1.5, pointRadius: 0, tension: 0.1 },
      { label: 'BUY', data: [], type: 'scatter', pointBackgroundColor: '#3fb950', pointRadius: 6, pointStyle: 'triangle' },
      { label: 'SELL', data: [], type: 'scatter', pointBackgroundColor: '#f85149', pointRadius: 6, pointStyle: 'triangle' },
    ],
  }, { ...chartDefaults });

  portfolioChart = mkChart('portfolio-chart', 'line', {
    labels: [],
    datasets: [
      { label: 'Portfolio (USDT)', data: [], borderColor: '#bc8cff', borderWidth: 1.5, pointRadius: 0, fill: true, backgroundColor: 'rgba(188,140,255,0.07)', tension: 0.1 },
      { label: 'Buy & Hold', data: [], borderColor: '#d29922', borderWidth: 1, borderDash: [4, 3], pointRadius: 0, tension: 0.1 },
    ],
  }, { ...chartDefaults });
}

initCharts();

// ── State ─────────────────────────────────────────────────────────────────────
let polling = null;
let lastLogLen = 0;
let lastResultCount = 0;
let bestResult = null;
let chartPrices = [];
let chartTimestamps = [];

// ── Simulation ────────────────────────────────────────────────────────────────
async function startSim() {
  const body = {
    symbol: document.getElementById('symbol').value,
    interval: document.getElementById('interval').value,
    days: parseInt(document.getElementById('days').value),
    initial_capital: parseFloat(document.getElementById('capital').value),
    max_iterations: parseInt(document.getElementById('iterations').value),
    fee_tier: document.getElementById('fee-tier').value,
  };

  lastLogLen = 0;
  lastResultCount = 0;
  bestResult = null;
  chartPrices = [];
  chartTimestamps = [];
  initCharts();
  document.getElementById('log-box').textContent = '';
  document.getElementById('trade-tbody').innerHTML = '';
  document.getElementById('trade-table-wrap').style.display = 'none';
  document.getElementById('no-trades').style.display = 'block';
  document.getElementById('iter-cards').innerHTML = '';
  document.getElementById('iterations-list').style.display = 'none';
  document.getElementById('metrics-row').style.display = 'none';
  document.getElementById('analysis-box').style.display = 'none';
  document.getElementById('status-card').style.display = 'block';

  const r = await fetch('/api/simulate/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) { alert('Fehler: ' + (await r.text())); return; }

  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled = false;
  document.getElementById('chart-title').textContent = body.symbol + ' Preischart';

  polling = setInterval(pollStatus, 2000);
}

async function stopSim() {
  await fetch('/api/simulate/stop', { method: 'POST' });
  clearInterval(polling);
  polling = null;
  document.getElementById('btn-start').disabled = false;
  document.getElementById('btn-stop').disabled = true;
}

async function pollStatus() {
  const [statusRes, chartRes] = await Promise.all([
    fetch('/api/simulate/status').then(r => r.json()),
    fetch('/api/simulate/chart-data').then(r => r.json()),
  ]);

  updateStatusUI(statusRes);
  updateCharts(chartRes);

  if (!statusRes.running && polling) {
    clearInterval(polling);
    polling = null;
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-stop').disabled = true;
    loadSimHistory();
  }
}

function updateStatusUI(state) {
  const statusEl = document.getElementById('status-text');
  const pulseEl = document.getElementById('pulse');
  const badgeEl = document.getElementById('iteration-badge');

  const statusMap = {
    idle: 'Bereit',
    starting: 'Starte…',
    fetching: 'Lade Kursdaten…',
    computing_indicators: 'Berechne Indikatoren…',
    profitable: '✅ Profitable Strategie gefunden!',
    completed: 'Abgeschlossen',
    stopped: 'Gestoppt',
    error: '❌ Fehler',
  };

  let label = statusMap[state.status] || state.status.replace(/_/g, ' ');
  statusEl.textContent = label;
  pulseEl.style.background = state.status === 'profitable' ? '#3fb950' : state.running ? '#58a6ff' : '#8b949e';

  if (state.iteration > 0) {
    badgeEl.textContent = `Iteration ${state.iteration} / ${state.max_iterations}`;
  }

  // Append new log lines
  const log = state.log || [];
  if (log.length > lastLogLen) {
    const box = document.getElementById('log-box');
    const newLines = log.slice(lastLogLen);
    newLines.forEach(line => {
      const span = document.createElement('span');
      if (line.startsWith('✅') || line.includes('PROFITABLE')) span.className = 'log-ok';
      else if (line.startsWith('❌') || line.startsWith('ERROR')) span.className = 'log-err';
      else if (line.startsWith('\n────') || line.startsWith('──')) span.className = 'log-head';
      span.textContent = line + '\n';
      box.appendChild(span);
    });
    box.scrollTop = box.scrollHeight;
    lastLogLen = log.length;
  }
}

function updateCharts(data) {
  if (data.prices && data.prices.length > chartPrices.length) {
    chartPrices = data.prices;
    chartTimestamps = data.timestamps;
    const labels = chartTimestamps.map(ts => {
      const d = new Date(ts);
      return d.toLocaleDateString('de-DE', { month: 'short', day: 'numeric' });
    });
    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = chartPrices;
    priceChart.update();
  }

  const results = data.results || [];
  if (results.length > lastResultCount) {
    const newResults = results.slice(lastResultCount);
    newResults.forEach(result => addIterationResult(result));
    lastResultCount = results.length;
  }

  if (data.best_result && data.best_result !== bestResult) {
    bestResult = data.best_result;
    showBestResult(bestResult);
  }
}

function addIterationResult(result) {
  const profitable = result.total_return_pct > 0;
  const ret = result.total_return_pct;

  // Iteration card
  const iterList = document.getElementById('iterations-list');
  const iterCards = document.getElementById('iter-cards');
  iterList.style.display = 'block';

  const card = document.createElement('div');
  card.className = 'iter-card' + (profitable ? ' profitable' : '');
  card.innerHTML = `
    <span class="iter-num">Iter. ${result.iteration}</span>
    <span class="iter-name">${result.strategy_name}</span>
    <span class="iter-ret" style="color:${profitable ? '#3fb950' : '#f85149'}">${ret > 0 ? '+' : ''}${ret.toFixed(2)}%</span>
  `;
  card.addEventListener('click', () => showIterResult(result));
  iterCards.appendChild(card);

  // Add trades to table
  if (result.trades && result.trades.length > 0) {
    const tbody = document.getElementById('trade-tbody');
    document.getElementById('no-trades').style.display = 'none';
    document.getElementById('trade-table-wrap').style.display = 'block';
    result.trades.forEach((t, i) => {
      const tr = document.createElement('tr');
      const priceMov = t.price_move_pct != null ? t.price_move_pct : t.pnl_pct;
      tr.innerHTML = `
        <td>${tbody.children.length + 1}</td>
        <td>${t.buy_index}</td>
        <td>${t.sell_index}</td>
        <td>$${t.buy_price.toFixed(2)}</td>
        <td>$${t.sell_price.toFixed(2)}</td>
        <td class="${priceMov >= 0 ? 'pnl-pos' : 'pnl-neg'}">${priceMov >= 0 ? '+' : ''}${priceMov.toFixed(2)}%</td>
        <td class="${t.pnl_pct >= 0 ? 'pnl-pos' : 'pnl-neg'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(3)}%</td>
        <td style="color:#d29922">$${(t.fees_total || 0).toFixed(3)}</td>
      `;
      tbody.appendChild(tr);
    });
  }
}

function showBestResult(result) {
  document.getElementById('metrics-row').style.display = 'grid';
  const ret = result.total_return_pct;
  document.getElementById('m-return').textContent = (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%';
  document.getElementById('m-return').style.color = ret >= 0 ? '#3fb950' : '#f85149';
  document.getElementById('m-winrate').textContent = result.win_rate.toFixed(1) + '%';
  document.getElementById('m-trades').textContent = result.num_trades;
  document.getElementById('m-drawdown').textContent = result.max_drawdown.toFixed(1) + '%';
  document.getElementById('m-fees').textContent = '$' + (result.total_fees_usdt || 0).toFixed(2);
  document.getElementById('m-feedrag').textContent = '-' + (result.fee_drag_pct || 0).toFixed(2) + '%';

  if (result.analysis) {
    const box = document.getElementById('analysis-box');
    box.style.display = 'block';
    box.innerHTML = `<strong>${result.strategy_name}</strong><br/>${result.analysis}`;
  }

  showIterResult(result);
}

function showIterResult(result) {
  // Update price chart with buy/sell markers
  if (chartPrices.length > 0 && result.signals) {
    const buyPoints = [];
    const sellPoints = [];

    result.signals.forEach(s => {
      const idx = s.candle_index;
      if (idx >= 0 && idx < chartPrices.length) {
        const point = { x: idx, y: chartPrices[idx] };
        if (s.action === 'BUY') buyPoints.push(point);
        else if (s.action === 'SELL') sellPoints.push(point);
      }
    });

    priceChart.data.datasets[1].data = buyPoints;
    priceChart.data.datasets[2].data = sellPoints;
    priceChart.update();
  }

  // Update portfolio chart
  if (result.portfolio_history && result.portfolio_history.length > 0) {
    const hist = result.portfolio_history;
    const startCapital = hist[0].value;
    const startPrice = hist[0].close;

    const labels = hist.map((h, i) => {
      const d = new Date(h.timestamp);
      return d.toLocaleDateString('de-DE', { month: 'short', day: 'numeric' });
    });
    const portfolioVals = hist.map(h => h.value);
    const buyHoldVals = hist.map(h => startCapital * (h.close / startPrice));

    portfolioChart.data.labels = labels;
    portfolioChart.data.datasets[0].data = portfolioVals;
    portfolioChart.data.datasets[1].data = buyHoldVals;
    portfolioChart.update();
  }
}

function clearLog() {
  document.getElementById('log-box').textContent = '';
  lastLogLen = 0;
}

// ── Live Trading ──────────────────────────────────────────────────────────────
let livePolling = null;
let lastLiveLogLen = 0;
let selectedSimForLive = null; // strategy context for live trading
let viewedSim = null;          // currently loaded sim detail

async function startLive() {
  const body = {
    api_key: document.getElementById('live-api-key').value.trim(),
    api_secret: document.getElementById('live-api-secret').value.trim(),
    symbol: document.getElementById('live-symbol').value,
    interval: document.getElementById('live-interval').value,
    trade_amount_usdt: parseFloat(document.getElementById('live-amount').value),
    strategy_name: selectedSimForLive?.strategy_name || '',
    strategy_analysis: selectedSimForLive?.strategy_analysis || '',
    strategy_patterns: selectedSimForLive?.strategy_patterns || [],
  };
  if (!body.api_key || !body.api_secret) { alert('Bitte API Key und Secret eingeben.'); return; }

  lastLiveLogLen = 0;
  document.getElementById('live-log-box').textContent = '';

  const r = await fetch('/api/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    alert('Fehler: ' + (err.detail || r.statusText));
    return;
  }

  document.getElementById('btn-live-start').disabled = true;
  document.getElementById('btn-live-stop').disabled = false;
  document.getElementById('live-status-card').style.display = 'block';

  livePolling = setInterval(pollLive, 5000);
}

async function stopLive() {
  await fetch('/api/live/stop', { method: 'POST' });
  clearInterval(livePolling);
  livePolling = null;
  document.getElementById('btn-live-start').disabled = false;
  document.getElementById('btn-live-stop').disabled = true;
}

let liveNextCheckTs = null;
let countdownTick = null;

function startCountdown(ts) {
  liveNextCheckTs = ts;
  if (countdownTick) clearInterval(countdownTick);
  const box = document.getElementById('live-countdown-box');
  const el = document.getElementById('live-countdown');
  box.style.display = 'flex';

  countdownTick = setInterval(() => {
    if (!liveNextCheckTs) { clearInterval(countdownTick); return; }
    const rem = Math.max(0, liveNextCheckTs - Date.now() / 1000);
    const h = Math.floor(rem / 3600);
    const m = Math.floor((rem % 3600) / 60);
    const s = Math.floor(rem % 60);
    el.textContent = h > 0
      ? `${h}h ${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`
      : `${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;
    if (rem === 0) el.textContent = 'Analysiere…';
  }, 1000);
}

async function pollLive() {
  const state = await fetch('/api/live/status').then(r => r.json());

  document.getElementById('live-status-text').textContent = state.status || 'idle';
  const posBadge = document.getElementById('live-position-badge');
  posBadge.textContent = state.position || 'FLAT';
  posBadge.style.color = state.position === 'IN_POSITION' ? '#3fb950' : '#8b949e';

  if (state.next_check_ts) {
    startCountdown(state.next_check_ts);
    document.getElementById('live-next-str').textContent = state.next_check_str || '';
  }

  const log = state.log || [];
  if (log.length > lastLiveLogLen) {
    const box = document.getElementById('live-log-box');
    const newLines = log.slice(lastLiveLogLen);
    newLines.forEach(line => {
      box.textContent += line + '\n';
    });
    box.scrollTop = box.scrollHeight;
    lastLiveLogLen = log.length;
  }

  if (!state.running && livePolling) {
    clearInterval(livePolling);
    if (countdownTick) clearInterval(countdownTick);
    livePolling = null;
    liveNextCheckTs = null;
    document.getElementById('live-countdown-box').style.display = 'none';
    document.getElementById('btn-live-start').disabled = false;
    document.getElementById('btn-live-stop').disabled = true;
  }
}

// ── Market scanner ────────────────────────────────────────────────────────────

async function runScanner() {
  const interval = document.getElementById('scan-interval').value;
  const btn = document.getElementById('btn-scan');
  const el = document.getElementById('scanner-result');
  btn.disabled = true;
  btn.textContent = '⏳ Scanne…';
  el.innerHTML = '<div class="empty-state">Claude analysiert 10 USDC-Paare…</div>';
  try {
    const r = await fetch('/api/scan/symbols', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({interval}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    renderScanResult(await r.json(), interval);
  } catch (e) {
    el.innerHTML = `<div class="empty-state" style="color:var(--red)">Fehler: ${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Markt scannen';
  }
}

function renderScanResult(data, interval) {
  const el = document.getElementById('scanner-result');
  const ranking = data.ranking || [];
  const best = data.best_symbol || '';
  let html = '';
  if (data.recommendation) {
    html += `<div class="analysis-box" style="margin-bottom:12px">${data.recommendation}</div>`;
  }
  html += ranking.map((r, i) => {
    const isBest = r.symbol === best;
    const scoreColor = r.score >= 70 ? '#3fb950' : r.score >= 50 ? '#d29922' : '#8b949e';
    return `<div class="scanner-row${isBest ? ' scanner-best' : ''}">
      <span class="scanner-rank">${i + 1}.</span>
      <span class="scanner-sym">${r.symbol}</span>
      <span class="scanner-score" style="color:${scoreColor}">${r.score}/100</span>
      <span class="scanner-reason">${r.reason}</span>
      <button class="${isBest ? 'btn-use-sim' : 'btn-tiny'}" onclick="useSim('${r.symbol}','${interval}')">
        ${isBest ? '★ Verwenden' : 'Verwenden'}
      </button>
    </div>`;
  }).join('');
  el.innerHTML = html || '<div class="empty-state">Keine Ergebnisse.</div>';
}

async function logout() {
  await fetch('/auth/logout', { method: 'POST' });
  window.location.href = '/login';
}

// ── Simulation history & picker ───────────────────────────────────────────────

let _simList = [];

async function loadSimHistory() {
  try {
    const data = await fetch('/api/simulations').then(r => r.json());
    _simList = data.simulations || [];
    renderSimHistory(_simList);
    populateSimPickers(_simList);
  } catch (e) {}
}

function populateSimPickers(sims) {
  const opts = ['<option value="">— Aktuelle Simulation —</option>',
    ...sims.map(s => {
      const ret = (s.total_return_pct || 0);
      const sign = ret >= 0 ? '+' : '';
      return `<option value="${s.id}">${s.symbol} ${s.interval} · ${sign}${ret.toFixed(2)}% · ${s.strategy_name || '?'}</option>`;
    })
  ].join('');
  document.getElementById('charts-sim-select').innerHTML = opts;
  document.getElementById('trades-sim-select').innerHTML = opts;
}

function syncSimPicker(source) {
  const val = document.getElementById(source + '-sim-select').value;
  const other = source === 'charts' ? 'trades' : 'charts';
  document.getElementById(other + '-sim-select').value = val;
}

async function _loadSimDetail(id) {
  if (!id) { viewedSim = null; return null; }
  try {
    const data = await fetch(`/api/simulations/${id}`).then(r => r.json());
    viewedSim = data;
    return data;
  } catch (e) { return null; }
}

async function loadSimIntoCharts() {
  const id = document.getElementById('charts-sim-select').value;
  syncSimPicker('charts');
  if (!id) return;
  const sim = await _loadSimDetail(id);
  if (!sim) return;

  document.getElementById('chart-title').textContent = `${sim.symbol} ${sim.interval} — ${sim.strategy_name || 'Simulation'}`;

  if (sim.candle_prices && sim.candle_prices.length > 0) {
    chartPrices = sim.candle_prices;
    chartTimestamps = sim.candle_timestamps || [];
    const labels = chartTimestamps.map(ts => new Date(ts).toLocaleDateString('de-DE', { month: 'short', day: 'numeric' }));
    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = chartPrices;
    priceChart.data.datasets[0].label = `Preis (${sim.symbol})`;

    const buys = [], sells = [];
    (sim.signals || []).forEach(s => {
      const idx = s.candle_index;
      if (idx >= 0 && idx < chartPrices.length) {
        const pt = { x: idx, y: chartPrices[idx] };
        if (s.action === 'BUY') buys.push(pt);
        else if (s.action === 'SELL') sells.push(pt);
      }
    });
    priceChart.data.datasets[1].data = buys;
    priceChart.data.datasets[2].data = sells;
    priceChart.update();
  }

  if (sim.portfolio_history && sim.portfolio_history.length > 0) {
    const hist = sim.portfolio_history;
    const startCapital = hist[0].value;
    const startPrice = hist[0].close;
    portfolioChart.data.labels = hist.map(h => new Date(h.timestamp).toLocaleDateString('de-DE', { month: 'short', day: 'numeric' }));
    portfolioChart.data.datasets[0].data = hist.map(h => h.value);
    portfolioChart.data.datasets[1].data = hist.map(h => startCapital * (h.close / startPrice));
    portfolioChart.update();
  }

  // Also show iteration cards if available
  if (sim.iteration) {
    const iterList = document.getElementById('iterations-list');
    const iterCards = document.getElementById('iter-cards');
    iterCards.innerHTML = '';
    iterList.style.display = 'block';
    const card = document.createElement('div');
    card.className = 'iter-card' + (sim.profitable ? ' profitable' : '');
    const ret = sim.total_return_pct || 0;
    card.innerHTML = `
      <span class="iter-num">Iter. ${sim.iteration}</span>
      <span class="iter-name">${sim.strategy_name || '?'}</span>
      <span class="iter-ret" style="color:${ret >= 0 ? '#3fb950' : '#f85149'}">${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%</span>
    `;
    iterCards.appendChild(card);
  }
}

async function loadSimIntoTrades() {
  const id = document.getElementById('trades-sim-select').value;
  syncSimPicker('trades');
  if (!id) return;
  const sim = await _loadSimDetail(id);
  if (!sim) return;

  const tbody = document.getElementById('trade-tbody');
  tbody.innerHTML = '';
  const trades = sim.trades || [];
  if (trades.length === 0) {
    document.getElementById('no-trades').style.display = 'block';
    document.getElementById('trade-table-wrap').style.display = 'none';
  } else {
    document.getElementById('no-trades').style.display = 'none';
    document.getElementById('trade-table-wrap').style.display = 'block';
    trades.forEach((t, i) => {
      const priceMov = t.price_move_pct != null ? t.price_move_pct : t.pnl_pct;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td>${t.buy_index}</td>
        <td>${t.sell_index}</td>
        <td>$${t.buy_price.toFixed(2)}</td>
        <td>$${t.sell_price.toFixed(2)}</td>
        <td class="${priceMov >= 0 ? 'pnl-pos' : 'pnl-neg'}">${priceMov >= 0 ? '+' : ''}${priceMov.toFixed(2)}%</td>
        <td class="${t.pnl_pct >= 0 ? 'pnl-pos' : 'pnl-neg'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(3)}%</td>
        <td style="color:#d29922">$${(t.fees_total || 0).toFixed(3)}</td>
      `;
      tbody.appendChild(tr);
    });
  }
}

function setSimAsLiveStrategy() {
  const id = document.getElementById('charts-sim-select').value || document.getElementById('trades-sim-select').value;
  const sim = viewedSim || _simList.find(s => s.id === id);
  if (!id || !sim) { alert('Bitte zuerst eine Simulation auswählen und laden.'); return; }
  selectedSimForLive = {
    id: sim.id,
    symbol: sim.symbol,
    interval: sim.interval,
    strategy_name: sim.strategy_name || '',
    strategy_analysis: sim.strategy_analysis || sim.analysis || '',
    strategy_patterns: sim.strategy_patterns || sim.patterns_found || [],
  };
  switchTab('live');
  updateLiveStrategyBar();
  useSim(sim.symbol, sim.interval);
}

function clearLiveStrategy() {
  selectedSimForLive = null;
  document.getElementById('live-strategy-bar').style.display = 'none';
}

function updateLiveStrategyBar() {
  if (!selectedSimForLive) {
    document.getElementById('live-strategy-bar').style.display = 'none';
    return;
  }
  document.getElementById('live-strategy-bar').style.display = 'block';
  document.getElementById('live-strategy-name').textContent = selectedSimForLive.strategy_name || 'Strategie';
  const patterns = (selectedSimForLive.strategy_patterns || []).join(', ');
  document.getElementById('live-strategy-detail').textContent =
    `${selectedSimForLive.symbol} ${selectedSimForLive.interval}${patterns ? ' · ' + patterns : ''}`;
}

function renderSimHistory(sims) {
  const list = document.getElementById('sim-history-list');
  if (!sims || sims.length === 0) {
    list.innerHTML = '<div class="empty-state">Noch keine Simulationen gespeichert.</div>';
    return;
  }
  list.innerHTML = sims.map(s => {
    const ret = s.total_return_pct || 0;
    const color = ret >= 0 ? '#3fb950' : '#f85149';
    const date = new Date(s.created_at).toLocaleString('de-DE', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
    });
    return `
      <div class="sim-hist-entry" data-id="${s.id}">
        <div class="sim-hist-main">
          <span class="sim-hist-sym">${s.symbol} ${s.interval}</span>
          <span class="sim-hist-ret" style="color:${color}">${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%</span>
          <span class="sim-hist-strat">${s.strategy_name || '—'}</span>
          <span class="sim-hist-meta">${s.num_trades || 0} Trades · ${date}</span>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          <button class="btn-tiny" onclick="viewSimInCharts('${s.id}')">Charts</button>
          <button class="btn-use-sim" onclick="useSimForLive('${s.id}')">★ Für Live</button>
        </div>
      </div>`;
  }).join('');
}

async function viewSimInCharts(id) {
  document.getElementById('charts-sim-select').value = id;
  document.getElementById('trades-sim-select').value = id;
  await loadSimIntoCharts();
  await loadSimIntoTrades();
  switchTab('charts');
}

async function useSimForLive(id) {
  const sim = _simList.find(s => s.id === id);
  if (!sim) return;
  // Load full detail to get analysis
  const detail = await _loadSimDetail(id);
  selectedSimForLive = {
    id: sim.id,
    symbol: sim.symbol,
    interval: sim.interval,
    strategy_name: sim.strategy_name || '',
    strategy_analysis: (detail || sim).strategy_analysis || (detail || sim).analysis || '',
    strategy_patterns: sim.strategy_patterns || (detail || sim).patterns_found || [],
  };
  useSim(sim.symbol, sim.interval);
  updateLiveStrategyBar();
  // Flash the strategy bar
  const bar = document.getElementById('live-strategy-bar');
  bar.style.outline = '2px solid #3fb950';
  setTimeout(() => { bar.style.outline = ''; }, 1500);
}

function useSim(symbol, interval) {
  const sel = document.getElementById('live-symbol');
  if (![...sel.options].some(o => o.value === symbol)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${symbol}">${symbol}</option>`);
  }
  sel.value = symbol;
  document.getElementById('live-interval').value = interval;
  const card = document.querySelector('#tab-live .card:not(.warning-card):not(#sim-history-card):not(#live-strategy-bar)');
  if (card) {
    card.style.outline = '2px solid #58a6ff';
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => { card.style.outline = ''; }, 1500);
  }
}

// ── Page init ─────────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn) btn.classList.add('active');
  const sec = document.getElementById(`tab-${name}`);
  if (sec) sec.classList.add('active');
}

async function initPage() {
  try {
    const state = await fetch('/api/live/status').then(r => r.json());
    if (state.running) {
      switchTab('live');
      document.getElementById('live-status-card').style.display = 'block';
      document.getElementById('btn-live-start').disabled = true;
      document.getElementById('btn-live-stop').disabled = false;

      document.getElementById('live-status-text').textContent = state.status || 'active';
      const posBadge = document.getElementById('live-position-badge');
      posBadge.textContent = state.position || 'FLAT';
      posBadge.style.color = state.position === 'IN_POSITION' ? '#3fb950' : '#8b949e';

      if (state.log && state.log.length > 0) {
        const box = document.getElementById('live-log-box');
        state.log.forEach(line => { box.textContent += line + '\n'; });
        box.scrollTop = box.scrollHeight;
        lastLiveLogLen = state.log.length;
      }

      if (state.next_check_ts) {
        startCountdown(state.next_check_ts);
        document.getElementById('live-next-str').textContent = state.next_check_str || '';
      }

      // Restore strategy bar if a strategy was active
      if (state.strategy_name) {
        selectedSimForLive = {
          strategy_name: state.strategy_name,
          strategy_analysis: state.strategy_analysis || '',
          strategy_patterns: state.strategy_patterns || [],
          symbol: state.symbol,
          interval: state.interval,
        };
        updateLiveStrategyBar();
      }

      livePolling = setInterval(pollLive, 5000);
    }
  } catch (e) {}

  loadSimHistory();
}

// Load symbols on startup
fetch('/api/symbols').then(r => r.json()).then(data => {
  const sel = document.getElementById('symbol');
  const current = sel.value;
  sel.innerHTML = data.symbols.map(s => `<option${s === current ? ' selected' : ''}>${s}</option>`).join('');
}).catch(() => {});

initPage();
