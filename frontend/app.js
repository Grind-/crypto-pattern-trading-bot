'use strict';

// ── Tab navigation ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ── Chart instances ───────────────────────────────────────────────────────────
let priceChart = null;
let portfolioChart = null;
let liveChart = null;

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

function initLiveChart() {
  if (liveChart) liveChart.destroy();
  liveChart = mkChart('live-price-chart', 'line', {
    labels: [],
    datasets: [
      { label: 'Live Preis', data: [], borderColor: '#58a6ff', borderWidth: 1.5, pointRadius: 0, tension: 0.1 },
      { label: 'BUY', data: [], type: 'scatter', pointBackgroundColor: '#3fb950', pointRadius: 7, pointStyle: 'triangle' },
      { label: 'SELL', data: [], type: 'scatter', pointBackgroundColor: '#f85149', pointRadius: 7, pointStyle: 'triangle' },
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
    fee_tier: document.getElementById('fee-tier').value,
    compounding_mode: document.getElementById('compounding-mode').value,
    analysis_weight: parseInt(document.getElementById('sim-analysis-weight').value),
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

  badgeEl.textContent = '';

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
  if (result.trades && result.trades.length > 0) {
    const tbody = document.getElementById('trade-tbody');
    document.getElementById('no-trades').style.display = 'none';
    document.getElementById('trade-table-wrap').style.display = 'block';
    result.trades.forEach(t => {
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

  const modeLabel = result.compounding_mode_label || result.compounding_mode || '';
  if (modeLabel) {
    const existing = document.getElementById('sim-compounding-badge');
    const badge = existing || document.createElement('div');
    badge.id = 'sim-compounding-badge';
    badge.style.cssText = 'font-size:11px;color:var(--text-muted);margin-top:6px';
    badge.textContent = `Compounding: ${modeLabel}`;
    if (!existing) document.getElementById('metrics-row').after(badge);
  }

  if (result.analysis) {
    const box = document.getElementById('analysis-box');
    box.style.display = 'block';
    box.innerHTML = result.analysis;
  }

  showIterResult(result);
}

function showIterResult(result) {
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

  if (result.portfolio_history && result.portfolio_history.length > 0) {
    const hist = result.portfolio_history;
    const startCapital = hist[0].value;
    const startPrice = hist[0].close;
    const labels = hist.map(h => new Date(h.timestamp).toLocaleDateString('de-DE', { month: 'short', day: 'numeric' }));
    portfolioChart.data.labels = labels;
    portfolioChart.data.datasets[0].data = hist.map(h => h.value);
    portfolioChart.data.datasets[1].data = hist.map(h => startCapital * (h.close / startPrice));
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
let viewedSim = null;

async function validateBinanceKeys() {
  const btn = document.getElementById('btn-validate-keys');
  const result = document.getElementById('binance-validate-result');
  btn.disabled = true;
  result.textContent = 'Prüfe…';
  result.style.color = 'var(--text-muted)';
  const body = {
    api_key: document.getElementById('live-api-key').value.trim(),
    api_secret: document.getElementById('live-api-secret').value.trim(),
  };
  try {
    const r = await fetch('/api/live/validate-keys', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const data = await r.json();
    if (data.ok) {
      const bal = data.usdc_balance != null ? ` — USDC-Guthaben: ${data.usdc_balance.toFixed(2)}` : '';
      result.textContent = `✓ Keys gültig${bal}`;
      result.style.color = 'var(--green)';
      await loadSavedCredentials();
    } else {
      result.textContent = `✗ ${data.error || 'Ungültige Keys'}`;
      result.style.color = 'var(--red)';
    }
  } catch {
    result.textContent = '✗ Verbindungsfehler';
    result.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
  }
}

async function startLive() {
  const rawWeight = parseInt(document.getElementById('live-analysis-weight')?.value ?? '30', 10);
  const body = {
    api_key: document.getElementById('live-api-key').value.trim(),
    api_secret: document.getElementById('live-api-secret').value.trim(),
    interval: document.getElementById('live-interval').value,
    trade_amount_usdt: parseFloat(document.getElementById('live-amount').value),
    compounding_mode: document.getElementById('live-compounding-mode').value,
    analysis_weight: rawWeight,
  };
  const keyEl = document.getElementById('live-api-key');
  const secEl = document.getElementById('live-api-secret');
  const keyOk = body.api_key || keyEl.dataset.saved === '1';
  const secOk = body.api_secret || secEl.dataset.saved === '1';
  if (!keyOk || !secOk) { alert('Bitte API Key und Secret eingeben.'); return; }

  lastLiveLogLen = 0;
  document.getElementById('live-log-box').textContent = '';

  const r = await fetch('/api/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    alert('Fehler: ' + (err.detail || r.statusText));
    return;
  }

  await loadSavedCredentials();

  document.getElementById('btn-live-start').disabled = true;
  document.getElementById('btn-live-stop').disabled = false;
  document.getElementById('live-active-section').style.display = 'block';
  document.getElementById('live-chart-title').textContent = `Live Preischart — ${body.symbol}`;

  initLiveChart();
  livePolling = setInterval(pollLive, 5000);
}

async function stopLive() {
  await fetch('/api/live/stop', { method: 'POST' });
  clearInterval(livePolling);
  livePolling = null;
  document.getElementById('btn-live-start').disabled = false;
  document.getElementById('btn-live-stop').disabled = true;
  document.getElementById('live-countdown-box').style.display = 'none';
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
  const [state, chartData] = await Promise.all([
    fetch('/api/live/status').then(r => r.json()),
    fetch('/api/live/chart-data').then(r => r.json()).catch(() => null),
  ]);

  document.getElementById('live-status-text').textContent = state.status || 'idle';

  const symBadge = document.getElementById('live-symbol-badge');
  symBadge.textContent = state.symbol || '';

  const posBadge = document.getElementById('live-position-badge');
  posBadge.textContent = state.position || 'FLAT';
  posBadge.style.color = state.position === 'IN_POSITION' ? '#3fb950' : '#8b949e';

  const basisEl = document.getElementById('live-basis-name');
  if (basisEl) {
    const w = state.analysis_weight ?? 70;
    const kbPct = 100 - w;
    basisEl.innerHTML = `<span style="color:var(--blue);font-weight:600">Wissensbasis</span>`
      + `<span style="color:var(--text-muted);font-size:11px;margin-left:6px">${kbPct}% KB · ${w}% Markt</span>`;
  }

  const capRow = document.getElementById('live-capital-row');
  const capVal = document.getElementById('live-capital-value');
  if (capRow && capVal && state.running && state.current_capital > 0) {
    capRow.style.display = 'block';
    const initial = state.trade_amount || state.current_capital;
    const current = state.current_capital;
    const delta = current - initial;
    const deltaPct = initial > 0 ? (delta / initial * 100) : 0;
    const color = delta > 0 ? 'var(--green)' : delta < 0 ? 'var(--red)' : 'var(--text-muted)';
    const sign = delta >= 0 ? '+' : '';
    const modeLabels = {compound: 'Volles Compounding', fixed: 'Fixes Volumen', compound_wins: 'Nur Gewinne'};
    const modeStr = modeLabels[state.compounding_mode] || state.compounding_mode || '';
    capVal.innerHTML = `<strong>$${current.toFixed(2)}</strong> <span style="color:${color};font-size:11px">${sign}$${delta.toFixed(2)} (${sign}${deltaPct.toFixed(1)}%)</span> <span style="color:var(--text-muted);font-size:11px">· Start: $${initial.toFixed(2)}${modeStr ? ' · ' + modeStr : ''}</span>`;
  } else if (capRow && !state.running) {
    capRow.style.display = 'none';
  }

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

  if (chartData) {
    updateLiveChart(chartData);
    updateLiveTradeTable(chartData.trade_history || []);
    if (state.symbol) {
      document.getElementById('live-chart-title').textContent = `Live Preischart — ${state.symbol}`;
    }
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

function updateLiveChart(data) {
  if (!liveChart) return;
  const candles = data.candles || [];
  if (candles.length === 0) return;

  const labels = candles.map(c => new Date(c.timestamp).toLocaleDateString('de-DE', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
  const prices = candles.map(c => c.close);

  // Map trade markers to candle indices by closest timestamp
  const buys = [];
  const sells = [];
  (data.trade_history || []).forEach(t => {
    let bestIdx = 0;
    let bestDiff = Infinity;
    candles.forEach((c, i) => {
      const diff = Math.abs(c.timestamp - t.timestamp);
      if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
    });
    const pt = { x: bestIdx, y: t.price };
    if (t.type === 'BUY') buys.push(pt);
    else if (t.type === 'SELL') sells.push(pt);
  });

  liveChart.data.labels = labels;
  liveChart.data.datasets[0].data = prices;
  liveChart.data.datasets[1].data = buys;
  liveChart.data.datasets[2].data = sells;
  liveChart.update();
}

function updateLiveTradeTable(trades) {
  if (!trades || trades.length === 0) {
    document.getElementById('no-live-trades').style.display = 'block';
    document.getElementById('live-trade-table-wrap').style.display = 'none';
    return;
  }
  document.getElementById('no-live-trades').style.display = 'none';
  document.getElementById('live-trade-table-wrap').style.display = 'block';
  const tbody = document.getElementById('live-trade-tbody');
  tbody.innerHTML = '';
  trades.forEach((t, i) => {
    const tr = document.createElement('tr');
    const pnl = t.pnl_pct;
    const pnlCell = pnl != null
      ? `<td class="${pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%</td>`
      : '<td style="color:var(--text-muted)">—</td>';
    const time = new Date(t.timestamp).toLocaleString('de-DE', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${t.symbol || '—'}</td>
      <td style="color:${t.type === 'BUY' ? '#3fb950' : '#f85149'};font-weight:600">${t.type}</td>
      <td>$${t.price.toFixed(2)}</td>
      <td style="color:var(--text-muted);font-size:11px">${time}</td>
      ${pnlCell}
    `;
    tbody.appendChild(tr);
  });
}

// ── Market scanner ────────────────────────────────────────────────────────────

function getExtraSyms() {
  return [1, 2, 3]
    .map(i => (document.getElementById(`extra-sym-${i}`)?.value || '').trim().toUpperCase())
    .filter(s => s.length > 0);
}

function saveExtraSyms() {
  localStorage.setItem('scanExtraSyms', JSON.stringify(getExtraSyms()));
}

function loadExtraSyms() {
  try {
    const saved = JSON.parse(localStorage.getItem('scanExtraSyms') || '[]');
    saved.forEach((s, i) => {
      const el = document.getElementById(`extra-sym-${i + 1}`);
      if (el) el.value = s;
    });
  } catch (e) {}
}

async function runLiveScan() {
  const interval = document.getElementById('live-interval')?.value
    || document.getElementById('scan-interval').value;
  const extra_symbols = getExtraSyms();
  const btn = document.getElementById('btn-live-scan');
  btn.disabled = true;
  btn.textContent = '⏳ Scanne…';
  try {
    const r = await fetch('/api/scan/symbols', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({interval, extra_symbols}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const data = await r.json();
    // Show result in the scanner card (scroll up to it) and also log to live log
    const box = document.getElementById('live-log-box');
    const best = data.best_symbol || '?';
    const rec = data.recommendation || '';
    box.textContent += `\n🔍 Scanner: bestes Paar = ${best}\n${rec.slice(0, 200)}\n`;
    box.scrollTop = box.scrollHeight;
    // Also update full scanner result card
    renderScanResult(data, interval);
  } catch (e) {
    const box = document.getElementById('live-log-box');
    box.textContent += `\n⚠ Scanner-Fehler: ${e.message}\n`;
    box.scrollTop = box.scrollHeight;
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Jetzt scannen';
  }
}

async function runScanner() {
  const interval = document.getElementById('scan-interval').value;
  const extra_symbols = getExtraSyms();
  const btn = document.getElementById('btn-scan');
  const el = document.getElementById('scanner-result');
  btn.disabled = true;
  const total = 10 + extra_symbols.length;
  btn.textContent = '⏳ Scanne…';
  el.innerHTML = `<div class="empty-state">Claude analysiert ${total} USDC-Paare…</div>`;
  try {
    const r = await fetch('/api/scan/symbols', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({interval, extra_symbols}),
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
      <button class="${isBest ? 'btn-use-sim' : 'btn-tiny'}" onclick="useSym('${r.symbol}','${interval}')">
        ${isBest ? '★ Intervall übernehmen' : 'Intervall'}
      </button>
    </div>`;
  }).join('');
  el.innerHTML = html || '<div class="empty-state">Keine Ergebnisse.</div>';
}

async function logout() {
  await fetch('/auth/logout', { method: 'POST' });
  window.location.href = '/login';
}

function toggleMobileMenu() {
  const menu = document.getElementById('header-actions');
  const btn  = document.getElementById('burger-btn');
  if (!menu || !btn) return;
  const open = menu.classList.toggle('open');
  btn.classList.toggle('open', open);
  btn.setAttribute('aria-label', open ? 'Menü schließen' : 'Menü öffnen');
}

document.addEventListener('click', e => {
  const menu = document.getElementById('header-actions');
  const btn  = document.getElementById('burger-btn');
  if (!menu || !btn) return;
  if (!menu.contains(e.target) && !btn.contains(e.target)) {
    menu.classList.remove('open');
    btn.classList.remove('open');
    btn.setAttribute('aria-label', 'Menü öffnen');
  }
});

// ── Simulation history & picker ───────────────────────────────────────────────

let _simList = [];

async function loadSimHistory() {
  try {
    const data = await fetch('/api/simulations').then(r => r.json());
    _simList = data.simulations || [];
    renderSimHistory(_simList);
  } catch (e) {}
}

async function _loadSimDetail(id) {
  if (!id) { viewedSim = null; return null; }
  try {
    const data = await fetch(`/api/simulations/${id}`).then(r => r.json());
    viewedSim = data;
    return data;
  } catch (e) { return null; }
}

function renderSimDetail(sim) {
  document.getElementById('chart-title').textContent = `${sim.symbol} ${sim.interval}`;

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

  // Show trade table
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

function _applyWeightLabel(kbPct, labelId, hintId) {
  const w = 100 - kbPct;
  const lbl = document.getElementById(labelId);
  const hint = document.getElementById(hintId);
  if (lbl) lbl.textContent = `${kbPct}% Wissensbasis · ${w}% Markt`;
  if (hint) {
    if (kbPct >= 80) hint.textContent = 'Wissensbasis führt strikt. Marktanalyse vetoet nur bei extremen Risiken.';
    else if (kbPct >= 50) hint.textContent = 'Wissensbasis gibt Rahmen vor, Marktbedingungen können Signale anpassen.';
    else if (kbPct >= 20) hint.textContent = 'Marktanalyse dominiert. Wissensbasis dient nur zur Bestätigung.';
    else hint.textContent = 'Reine Marktanalyse — Wissensbasis nur als Hintergrundinformation.';
  }
}

function updateWeightLabel(val) {
  _applyWeightLabel(100 - parseInt(val, 10), 'weight-label', 'weight-hint');
}

function updateSimWeightLabel(val) {
  _applyWeightLabel(100 - parseInt(val, 10), 'sim-weight-label', 'sim-weight-hint');
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
          <span class="sim-hist-meta">${s.num_trades || 0} Trades · ${date}</span>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          <button class="btn-tiny" onclick="viewSimInCharts('${s.id}')">Charts</button>
          <button class="btn-use-sim" onclick="useSym('${s.symbol}','${s.interval}');switchTab('live')">→ Live</button>
        </div>
      </div>`;
  }).join('');
}

async function viewSimInCharts(id) {
  const sim = await _loadSimDetail(id);
  if (!sim) return;
  renderSimDetail(sim);
  switchTab('simulation');
}


function useSym(symbol, interval) {
  if (interval) document.getElementById('live-interval').value = interval;
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

async function loadSavedCredentials() {
  try {
    const creds = await fetch('/api/live/credentials').then(r => r.json());
    const keyEl = document.getElementById('live-api-key');
    const secEl = document.getElementById('live-api-secret');
    const hintEl = document.getElementById('live-key-hint');
    const statusEl = document.getElementById('binance-key-status');
    if (creds.has_key) {
      keyEl.placeholder = '••••••••••••••••';
      keyEl.dataset.saved = '1';
    }
    if (creds.has_secret) {
      secEl.placeholder = '••••••••••••••••';
      secEl.dataset.saved = '1';
    }
    if (hintEl) {
      hintEl.textContent = creds.has_key ? creds.key_hint : '';
    }
    if (statusEl) {
      if (creds.has_key && creds.has_secret) {
        statusEl.innerHTML = `<span style="background:rgba(63,185,80,0.15);border:1px solid var(--green,#3fb950);color:var(--green,#3fb950);padding:3px 10px;border-radius:12px">✓ Binance API Key gespeichert (${creds.key_hint})</span>`;
      } else {
        statusEl.innerHTML = `<span style="background:rgba(255,165,0,0.12);border:1px solid #f0a500;color:#f0a500;padding:3px 10px;border-radius:12px">⚠ Kein Binance API Key gespeichert</span>`;
      }
    }
  } catch {}
}

async function initPage() {
  loadSavedCredentials();
  try {
    const state = await fetch('/api/live/status').then(r => r.json());
    if (state.running) {
      switchTab('live');
      document.getElementById('live-active-section').style.display = 'block';
      document.getElementById('btn-live-start').disabled = true;
      document.getElementById('btn-live-stop').disabled = false;

      document.getElementById('live-status-text').textContent = state.status || 'active';

      const symBadge = document.getElementById('live-symbol-badge');
      symBadge.textContent = state.symbol || '';

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

      const basisEl = document.getElementById('live-basis-name');
      if (basisEl) {
        const w = state.analysis_weight ?? 70;
        const kbPct = 100 - w;
        const weightTag = `<span style="color:var(--text-muted);font-size:11px;margin-left:6px">${kbPct}% KB · ${w}% Markt</span>`;
        basisEl.innerHTML = `<span style="color:var(--blue);font-weight:600">Wissensbasis</span>${weightTag}`;
      }

      document.getElementById('live-chart-title').textContent = `Live Preischart — ${state.symbol || ''}`;
      initLiveChart();

      // Load initial chart data
      const chartData = await fetch('/api/live/chart-data').then(r => r.json()).catch(() => null);
      if (chartData) {
        updateLiveChart(chartData);
        updateLiveTradeTable(chartData.trade_history || []);
      }

      livePolling = setInterval(pollLive, 5000);
    }
  } catch (e) {}

  loadSimHistory();
  loadExtraSyms();
}

// Load user profile (show admin button, username in header)
fetch('/api/user/profile').then(r => r.json()).then(data => {
  const el = document.getElementById('header-user');
  if (el) el.textContent = data.username;
  if (data.role === 'admin') {
    const btn = document.getElementById('btn-admin');
    if (btn) btn.style.display = '';
    const docs = document.getElementById('btn-docs');
    if (docs) docs.style.display = '';
    const refreshBtn = document.getElementById('btn-news-refresh');
    if (refreshBtn) refreshBtn.style.display = '';
  }
}).catch(() => {});

// ── News Intelligence ────────────────────────────────────────────────────────
let _newsLoaded = false;

function renderNews(d) {
  const loading = document.getElementById('news-loading');
  const empty   = document.getElementById('news-empty');
  const content = document.getElementById('news-content');
  if (!d || !d.market_sentiment) {
    loading.style.display = 'none'; empty.style.display = 'block'; return;
  }

  const sentEl = document.getElementById('news-sentiment');
  const sentKey = (d.market_sentiment || 'neutral').replace(/\s+/g, '_');
  sentEl.textContent = (d.market_sentiment || '—').replace(/_/g, ' ');
  sentEl.className = 'news-sentiment sent-' + sentKey;

  const fgv = d.fear_greed_value ?? 50;
  document.getElementById('fng-label').textContent = d.fear_greed_label || '';
  document.getElementById('fng-value').textContent = fgv + '/100';
  const fill = document.getElementById('fng-fill');
  fill.style.width = fgv + '%';
  fill.style.background = fgv >= 60 ? 'var(--green)' : fgv <= 30 ? 'var(--red)' : 'var(--yellow)';

  if (d.timestamp) {
    const dt = new Date(d.timestamp);
    const age = Math.round((Date.now() - dt) / 60000);
    const ageStr = age < 60 ? `vor ${age} Min.` : `vor ${Math.round(age/60)} Std.`;
    const tsEl = document.getElementById('news-ts');
    if (tsEl) tsEl.textContent = `Zuletzt: ${dt.toLocaleTimeString('de-DE', {hour:'2-digit',minute:'2-digit'})} Uhr (${ageStr})`;
  }

  document.getElementById('news-analysis').textContent = d.analysis || '';

  const opps = d.top_opportunities || [];
  document.getElementById('news-opps').innerHTML = opps.map(o => {
    const dir = (o.direction || 'long').toLowerCase();
    const confBg = o.confidence >= 70 ? 'rgba(63,185,80,0.12)' : 'rgba(210,153,34,0.12)';
    const confColor = o.confidence >= 70 ? 'var(--green)' : 'var(--yellow)';
    const dirColor = dir === 'long' ? 'var(--green)' : 'var(--red)';
    return `<div class="opp-card direction-${dir}">
      <div class="opp-header">
        <span class="opp-symbol">${o.symbol}</span>
        <span class="opp-conf" style="background:${confBg};color:${confColor}">${o.confidence}%</span>
        <span class="opp-tf">${o.timeframe || ''}</span>
        <span style="margin-left:auto;font-size:10px;font-weight:700;text-transform:uppercase;color:${dirColor}">${dir}</span>
      </div>
      <div class="opp-catalyst">${o.catalyst || ''}</div>
      <div class="opp-source">${o.source || ''}</div>
    </div>`;
  }).join('');

  // Weighted news
  const weighted = d.weighted_news || [];
  const wnSection = document.getElementById('weighted-news-section');
  const wnList = document.getElementById('news-weighted-list');
  if (weighted.length > 0) {
    const sigIcon = { bullish: '🟢', bearish: '🔴', neutral: '⚪' };
    const impactColor = { bullish: 'var(--green)', bearish: 'var(--red)', neutral: 'var(--text-muted)' };
    wnList.innerHTML = weighted.map(n => {
      const wCls = 'ww-' + (n.weight || 'low');
      const wLabel = { high: 'HOCH', medium: 'MITTEL', low: 'GERING' }[n.weight] || n.weight;
      const sig = sigIcon[n.signal] || '⚪';
      const iColor = impactColor[n.signal] || 'var(--text-muted)';
      const syms = (n.affects_symbols || []).map(s => `<span class="wnews-sym">${s}</span>`).join('');
      const filteredBadge = n.flows_into_decision
        ? ''
        : `<span class="wnews-filtered">nicht in Entscheidung</span>`;
      return `<div class="wnews-item">
        <div class="wnews-left">
          <span class="wnews-weight ${wCls}">${wLabel}</span>
          <span class="wnews-signal">${sig}</span>
        </div>
        <div class="wnews-body">
          <div class="wnews-headline">${n.headline || ''}</div>
          <div class="wnews-impact" style="color:${iColor}">${n.decision_impact || ''}</div>
          <div class="wnews-reasoning">${n.reasoning || ''}</div>
          <div class="wnews-footer">
            <div class="wnews-symbols">${syms}</div>
            <span class="wnews-source">${n.source || ''}</span>
            ${filteredBadge}
          </div>
        </div>
      </div>`;
    }).join('');
    wnSection.style.display = 'block';
  } else {
    wnSection.style.display = 'none';
  }

  document.getElementById('news-warnings').innerHTML =
    (d.warnings || []).map(w => `<li>${w}</li>`).join('') || '<li style="list-style:none;color:var(--text-muted)">Keine Warnungen</li>';

  document.getElementById('news-trending').innerHTML =
    (d.trending_coins || []).map(c => `<span class="trend-pill">${c}</span>`).join('');

  document.getElementById('news-sources').textContent = (d.sources_used || []).join(', ') || '—';

  loading.style.display = 'none'; empty.style.display = 'none'; content.style.display = 'block';
  _newsLoaded = true;
}

async function ensureNewsLoaded() {
  if (_newsLoaded) return;
  try {
    const d = await fetch('/api/news/intelligence').then(r => r.json());
    renderNews(d);
  } catch {
    document.getElementById('news-loading').style.display = 'none';
    document.getElementById('news-empty').style.display = 'block';
  }
}

async function refreshNews() {
  const btn = document.getElementById('btn-news-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '↻ Läuft…'; }
  _newsLoaded = false;
  document.getElementById('news-content').style.display = 'none';
  document.getElementById('news-empty').style.display = 'none';
  document.getElementById('news-loading').style.display = 'block';
  try {
    const d = await fetch('/api/news/refresh', {method:'POST'}).then(r => r.json());
    renderNews(d);
  } catch {
    document.getElementById('news-loading').style.display = 'none';
    document.getElementById('news-empty').style.display = 'block';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Aktualisieren'; }
  }
}

// Load symbols on startup
fetch('/api/symbols').then(r => r.json()).then(data => {
  const sel = document.getElementById('symbol');
  const current = sel.value;
  sel.innerHTML = data.symbols.map(s => `<option${s === current ? ' selected' : ''}>${s}</option>`).join('');
}).catch(() => {});

initPage();
