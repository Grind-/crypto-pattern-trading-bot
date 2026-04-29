'use strict';

// ── Tab navigation ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.dataset.tab !== 'live') {
      const eyeBtn = document.getElementById('apikey-eye-btn');
      if (eyeBtn && eyeBtn.getAttribute('aria-pressed') === 'true') {
        toggleApiKeyReveal();
      }
      _revealedApiKey = null;
    }
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ── API Key reveal state ──────────────────────────────────────────────────────
let _revealedApiKey = null;

function _buildMaskedKey(hint) {
  if (hint && hint.includes('...')) return hint.replace('...', '••••••••••••••••');
  if (hint && hint.length >= 8) return hint.slice(0, 4) + '••••••••••••••••' + hint.slice(-4);
  return '••••••••••••••••••••••••';
}

async function toggleApiKeyReveal() {
  const displayEl = document.getElementById('apikey-display');
  const eyeBtn    = document.getElementById('apikey-eye-btn');
  if (!displayEl || !eyeBtn) return;
  const eyeOpen   = eyeBtn.querySelector('.eye-open');
  const eyeClosed = eyeBtn.querySelector('.eye-closed');
  const isRevealed = eyeBtn.getAttribute('aria-pressed') === 'true';
  if (isRevealed) {
    _revealedApiKey = null;
    displayEl.textContent = _buildMaskedKey(displayEl.dataset.hint || '');
    eyeBtn.setAttribute('aria-pressed', 'false');
    eyeBtn.setAttribute('aria-label', 'API Key anzeigen');
    if (eyeOpen)   eyeOpen.style.display  = '';
    if (eyeClosed) eyeClosed.style.display = 'none';
  } else {
    if (!_revealedApiKey) {
      const inputVal = document.getElementById('live-api-key')?.value.trim();
      if (inputVal) {
        _revealedApiKey = inputVal;
      } else {
        try {
          const data = await fetch('/api/live/credentials/reveal').then(r => r.json());
          _revealedApiKey = data.api_key || null;
        } catch { _revealedApiKey = null; }
      }
    }
    if (_revealedApiKey) {
      displayEl.textContent = _revealedApiKey;
      eyeBtn.setAttribute('aria-pressed', 'true');
      eyeBtn.setAttribute('aria-label', 'API Key verbergen');
      if (eyeOpen)   eyeOpen.style.display  = 'none';
      if (eyeClosed) eyeClosed.style.display = '';
    }
  }
}

// ── Chart instances ───────────────────────────────────────────────────────────
let priceChart = null;
let portfolioChart = null;
let perfChart = null;
let _perfMode = 'capital';
let _perfData = null;
let _perfLastFetch = 0;

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
let _currentUsername = null;

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
    min_confidence: parseInt(document.getElementById('live-min-confidence')?.value ?? '55', 10),
    sl_atr_mult: parseFloat(document.getElementById('live-sl-mult')?.value ?? '1.5'),
    tp_atr_mult: parseFloat(document.getElementById('live-tp-mult')?.value ?? '2.5'),
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

  livePolling = setInterval(pollLive, 5000);
}

async function stopLive() {
  await fetch('/api/live/stop', { method: 'POST' });
  clearInterval(livePolling);
  livePolling = null;
  document.getElementById('btn-live-start').disabled = false;
  document.getElementById('btn-live-stop').disabled = true;
  document.getElementById('live-countdown-box').style.display = 'none';
  // hide topup row on stop
  const tr = document.getElementById('topup-row');
  if (tr) tr.style.display = 'none';
  // reset performance chart
  if (perfChart) { perfChart.destroy(); perfChart = null; }
  _perfData = null; _perfLastFetch = 0;
  const pc = document.getElementById('live-perf-card');
  if (pc) pc.style.display = 'none';
}

function toggleTopup() {
  const row = document.getElementById('topup-row');
  const btn = document.getElementById('topup-toggle-btn');
  const open = row.style.display === 'none';
  row.style.display = open ? 'flex' : 'none';
  btn.classList.toggle('topup-toggle-btn--open', open);
  if (open) {
    document.getElementById('topup-amount').focus();
    document.getElementById('topup-result').textContent = '';
  }
}

async function submitTopup() {
  const input = document.getElementById('topup-amount');
  const result = document.getElementById('topup-result');
  const amount = parseFloat(input.value);
  if (!amount || amount < 1) {
    result.textContent = 'Mindestbetrag $1';
    result.style.color = 'var(--red)';
    return;
  }
  result.textContent = '…';
  result.style.color = 'var(--text-muted)';
  try {
    const res = await fetch('/api/live/topup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount }),
    });
    if (res.ok) {
      const data = await res.json();
      result.textContent = `✓ Neues Kapital: $${data.new_capital.toFixed(2)}`;
      result.style.color = 'var(--green)';
      input.value = '';
      setTimeout(() => toggleTopup(), 2000);
    } else {
      const err = await res.json().catch(() => ({}));
      result.textContent = err.detail || 'Fehler';
      result.style.color = 'var(--red)';
    }
  } catch (e) {
    result.textContent = 'Netzwerkfehler';
    result.style.color = 'var(--red)';
  }
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

  // Running state card glow
  const statusCard = document.getElementById('live-status-card');
  if (statusCard) statusCard.classList.toggle('card--running', !!state.running);

  const symBadge = document.getElementById('live-symbol-badge');
  if (state.symbol) {
    symBadge.textContent = state.symbol;
    symBadge.style.display = 'inline-block';
  } else {
    symBadge.style.display = 'none';
  }

  const posBadge = document.getElementById('live-position-badge');
  const isInPos = state.position === 'IN_POSITION';
  posBadge.textContent = state.position || 'FLAT';
  posBadge.style.cssText = isInPos
    ? 'color:#3fb950;border-color:#3fb95044;background:rgba(63,185,80,0.1)'
    : 'color:#8b949e';

  const basisEl = document.getElementById('live-basis-name');
  if (basisEl) {
    const w = state.analysis_weight ?? 70;
    const kbPct = 100 - w;
    basisEl.innerHTML = `<span style="color:var(--blue);font-weight:600">Wissensbasis</span>`
      + `<span style="color:var(--text-muted);font-size:11px;margin-left:6px">${kbPct}% KB · ${w}% Markt</span>`;
  }

  const capRow = document.getElementById('live-capital-row');
  const capVal = document.getElementById('live-capital-value');
  const capMeta = document.getElementById('live-capital-meta');
  if (capRow && capVal && state.running) {
    capRow.style.display = 'block';
    const initial = state.trade_amount || state.current_capital;
    const current = state.current_capital;
    const delta = current - initial;
    const deltaPct = initial > 0 ? (delta / initial * 100) : 0;
    const color = delta > 0 ? 'var(--green)' : delta < 0 ? 'var(--red)' : 'var(--text-muted)';
    const sign = delta >= 0 ? '+' : '';
    const modeLabels = {compound: 'Volles Compounding', fixed: 'Fixes Volumen', compound_wins: 'Nur Gewinne'};
    const modeStr = modeLabels[state.compounding_mode] || state.compounding_mode || '';
    capVal.innerHTML = `$${current.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} <span style="color:${color};font-size:13px;font-weight:600">${sign}${deltaPct.toFixed(1)}%</span>`;
    if (capMeta) capMeta.innerHTML = `<span style="color:${color}">${sign}$${delta.toFixed(2)}</span> seit Start ($${initial.toFixed(2)})${modeStr ? ' · ' + modeStr : ''}`;
  } else if (capRow && !state.running) {
    capRow.style.display = 'none';
  }

  if (state.next_check_ts) {
    startCountdown(state.next_check_ts);
    document.getElementById('live-next-str').textContent = state.next_check_str || '';
  }

  // Regime badge
  const regime = state.last_regime;
  const rb = document.getElementById('live-regime-badge');
  if (rb) {
    if (regime?.regime) {
      const colors = {BULL_TREND:'#3fb950',BEAR_TREND:'#f85149',RANGING:'#d29922',HIGH_VOLATILITY:'#e3b341'};
      const c = colors[regime.regime] || '#8b949e';
      rb.textContent = regime.regime.replace('_',' ');
      rb.style.cssText = `display:inline-block;color:${c};border-color:${c};background:${c}22`;
    } else { rb.style.display = 'none'; }
  }

  // News score badge
  const ns = state.last_news_score;
  const nb = document.getElementById('live-news-score-badge');
  if (nb) {
    if (ns?.sentiment_score != null) {
      const c = ns.sentiment_score >= 60 ? '#3fb950' : ns.sentiment_score <= 30 ? '#f85149' : '#d29922';
      nb.textContent = `News ${ns.sentiment_score}/100${ns.veto ? ' 🚫' : ''}`;
      nb.style.cssText = `display:inline-block;color:${c};border-color:${c};background:${c}22;font-size:11px;padding:2px 6px;border-radius:4px;border:1px solid`;
    } else { nb.style.display = 'none'; }
  }

  // Agent panel
  const rk = state.last_risk;
  const panel = document.getElementById('live-agent-panel');
  if (panel) {
    if (regime || ns || rk) {
      panel.style.display = 'block';
      document.getElementById('live-regime-text').textContent =
        regime ? `Regime: ${regime.regime} (${regime.strength}/100)` : '';
      document.getElementById('live-news-text').textContent =
        ns ? `News: ${ns.sentiment_score}/100` : '';
      document.getElementById('live-risk-text').textContent =
        rk ? `Risk: ${rk.position_size_pct}% pos, SL ${rk.stop_loss_pct?.toFixed(2)}%, TP ${rk.take_profit_pct?.toFixed(2)}%` : '';
    }
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

  renderLastDecision(state.last_decision);
  loadPerformance();

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

function renderLastDecision(d) {
  const card = document.getElementById('live-decision-card');
  if (!card) return;
  if (!d) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  // Meta line
  const ts = d.ts ? new Date(d.ts).toLocaleString('de-DE', {hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}) : '';
  document.getElementById('dec-meta').textContent =
    `Kerze #${d.candle_num} · ${d.symbol} · $${(d.price||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})} · ${ts}`;

  // Final action badge
  const finalBadge = document.getElementById('dec-final-badge');
  const actionColors = {BUY:'#3fb950', SELL:'#f85149', HOLD:'#d29922'};
  const actionIcons = {BUY:'▲ KAUF', SELL:'▼ VERKAUF', HOLD:'◆ HALTEN'};
  const fc = actionColors[d.final_action] || '#8b949e';
  finalBadge.textContent = actionIcons[d.final_action] || d.final_action;
  finalBadge.style.cssText = `font-size:14px;padding:5px 12px;font-weight:700;color:${fc};border-color:${fc};background:${fc}22;border:1px solid;border-radius:6px`;

  // Override note
  const rawOverride = document.getElementById('dec-raw-override');
  if (d.raw_action && d.raw_action !== d.final_action) {
    rawOverride.textContent = `Signal war ${d.raw_action} → überstimmt`;
    rawOverride.style.color = 'var(--text-muted)';
  } else {
    rawOverride.textContent = '';
  }

  // Signal agent row
  const sigBadge = document.getElementById('dec-signal-badge');
  const sc = actionColors[d.raw_action] || '#8b949e';
  sigBadge.textContent = d.raw_action || '—';
  sigBadge.style.cssText = `color:${sc};border-color:${sc};background:${sc}22;font-size:11px;padding:2px 7px;border-radius:4px;border:1px solid`;
  document.getElementById('dec-confidence').textContent = d.confidence != null ? `${d.confidence}% Konfidenz` : '';
  const reasonEl = document.getElementById('dec-reason');
  reasonEl.textContent = d.reason || '';
  reasonEl.title = d.reason || '';

  // Regime agent row
  const regBadge = document.getElementById('dec-regime-badge');
  const regColors = {BULL_TREND:'#3fb950',BEAR_TREND:'#f85149',RANGING:'#d29922',HIGH_VOLATILITY:'#e3b341'};
  const rc = regColors[d.regime?.type] || '#8b949e';
  regBadge.textContent = (d.regime?.type||'').replace('_',' ');
  regBadge.style.cssText = `color:${rc};border-color:${rc};background:${rc}22;font-size:11px;padding:2px 7px;border-radius:4px;border:1px solid`;
  document.getElementById('dec-regime-detail').textContent =
    d.regime ? `${d.regime.strength}/100 · ${(d.regime.strategy||'').replace(/_/g,' ')}` : '';

  // News agent row
  const newsBadge = document.getElementById('dec-news-badge');
  const ns = d.news?.score ?? 50;
  const nc = ns >= 60 ? '#3fb950' : ns <= 30 ? '#f85149' : '#d29922';
  newsBadge.textContent = `${ns}/100${d.news?.veto ? ' 🚫' : ''}`;
  newsBadge.style.cssText = `color:${nc};border-color:${nc};background:${nc}22;font-size:11px;padding:2px 7px;border-radius:4px;border:1px solid`;
  document.getElementById('dec-news-detail').textContent = d.news?.veto ? 'Veto aktiv — kein Kauf möglich' : 'kein Veto';

  // Voting matrix bars
  const voteSection = document.getElementById('dec-voting-section');
  const voteRows = document.getElementById('dec-vote-rows');
  if (d.voting && !d.force_sell) {
    voteSection.style.display = 'block';
    const { vote, news_mod, regime_boost, total_score } = d.voting;
    const maxAbs = 2.0;
    const pct = v => Math.round(Math.abs(v) / maxAbs * 100);
    const sign = v => v >= 0 ? '+' : '';
    const barColor = v => v > 0 ? '#3fb950' : v < 0 ? '#f85149' : '#8b949e';
    const rows = [
      { label: 'Signal-Vote', val: vote },
      { label: 'News-Einfluss', val: news_mod },
      { label: 'Regime-Boost', val: regime_boost },
    ];
    const threshBuy = 1.3;
    const totalPct = pct(total_score);
    const totalColor = total_score >= threshBuy ? '#3fb950' : total_score <= -0.8 ? '#f85149' : '#d29922';
    voteRows.innerHTML = rows.map(r => `
      <div class="dec-vote-row">
        <span class="dec-vote-label">${r.label}</span>
        <span class="dec-vote-val" style="color:${barColor(r.val)}">${sign(r.val)}${r.val.toFixed(2)}</span>
        <div class="dec-vote-bar-wrap">
          <div class="dec-vote-bar" style="width:${pct(r.val)}%;background:${barColor(r.val)}"></div>
        </div>
      </div>`).join('') + `
      <div class="dec-vote-row dec-vote-total">
        <span class="dec-vote-label" style="font-weight:600">Gesamt-Score</span>
        <span class="dec-vote-val" style="color:${totalColor};font-weight:700">${sign(total_score)}${total_score.toFixed(2)}</span>
        <div class="dec-vote-bar-wrap">
          <div class="dec-vote-bar" style="width:${totalPct}%;background:${totalColor}"></div>
          <div class="dec-vote-threshold" style="left:${Math.round(threshBuy/maxAbs*100)}%" title="Kauf-Schwellenwert 1.3"></div>
        </div>
      </div>`;
  } else {
    voteSection.style.display = 'none';
  }

  // Overrides
  const overSec = document.getElementById('dec-overrides-section');
  const overList = document.getElementById('dec-overrides-list');
  if (d.overrides?.length) {
    overSec.style.display = 'block';
    overList.innerHTML = d.overrides.map(o => `<li>${o}</li>`).join('');
  } else {
    overSec.style.display = 'none';
  }

  // Risk agent
  const riskSec = document.getElementById('dec-risk-section');
  const riskDet = document.getElementById('dec-risk-detail');
  if (d.risk) {
    riskSec.style.display = 'block';
    const greenDots = Array.from({length:4}, (_,i) => i < (d.risk.green_signals||0) ? '🟢' : '⚪').join(' ');
    const blockedHtml = d.risk.blocked ? '<span style="color:#f85149;font-weight:600"> — blockiert</span>' : '';
    riskDet.innerHTML = `<span style="color:var(--text-muted)">Positionsgröße:</span> <strong>${d.risk.position_size_pct}%</strong> &nbsp;
      <span style="color:var(--text-muted)">SL:</span> <strong style="color:#f85149">${d.risk.stop_loss_pct?.toFixed(2)}%</strong> &nbsp;
      <span style="color:var(--text-muted)">TP:</span> <strong style="color:#3fb950">${d.risk.take_profit_pct?.toFixed(2)}%</strong> &nbsp;
      <span style="font-size:11px">${greenDots} ${d.risk.green_signals}/4 Signale grün</span>${blockedHtml}`;
  } else {
    riskSec.style.display = 'none';
  }
}

// ── Performance chart ─────────────────────────────────────────────────────────

async function loadPerformance(force = false) {
  const now = Date.now();
  if (!force && now - _perfLastFetch < 30_000) return;
  _perfLastFetch = now;
  try {
    const data = await fetch('/api/live/performance').then(r => r.json());
    _perfData = data;
    renderPerfChart(data, _perfMode);
  } catch {}
}

function setPerfMode(mode) {
  _perfMode = mode;
  document.querySelectorAll('.perf-mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));
  if (_perfData) renderPerfChart(_perfData, mode);
}

function renderPerfChart(data, mode) {
  const card = document.getElementById('live-perf-card');
  if (!card) return;
  card.style.display = 'block';

  const kpiRow    = document.getElementById('perf-kpi-row');
  const legendRow = document.getElementById('perf-legend-row');
  const emptyEl   = document.getElementById('perf-empty');
  const wrapEl    = document.getElementById('perf-chart-wrap');

  // ── KPI cards ──────────────────────────────────────────────────────────
  if (data.summary) {
    const s = data.summary;
    const sign = v => v >= 0 ? '+' : '';
    const col  = v => v >= 0 ? 'var(--green)' : 'var(--red)';
    const kpi  = (label, val, cls = '') =>
      `<div class="perf-kpi"><span class="perf-kpi-label">${label}</span>` +
      `<span class="perf-kpi-value ${cls}">${val}</span></div>`;
    const delta = s.current_capital - s.start_capital;
    const deltaStr = `${delta >= 0 ? '+' : ''}$${Math.abs(delta).toFixed(2)}`;
    kpiRow.innerHTML =
      kpi('Start', `$${s.start_capital.toFixed(2)}`) +
      kpi('Aktuell', `$${s.current_capital.toFixed(2)}`) +
      kpi('P&L', `<span style="color:${col(delta)}">${deltaStr}</span>`) +
      kpi('Return', `<span style="color:${col(s.bot_pct)}">${sign(s.bot_pct)}${s.bot_pct.toFixed(2)}%</span>`) +
      (s.btc_pct != null
        ? kpi('BTC', `<span style="color:${col(s.btc_pct)}">${sign(s.btc_pct)}${s.btc_pct.toFixed(2)}%</span>`)
        : '') +
      kpi('Trades', String(s.num_sells), 'sm');
    kpiRow.style.display = 'grid';
  }

  if (perfChart) { perfChart.destroy(); perfChart = null; }
  legendRow.innerHTML = ''; legendRow.style.display = 'none';

  const fmtTs  = ts => new Date(ts).toLocaleDateString('de-DE', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
  const fmtDay = ts => new Date(ts).toLocaleDateString('de-DE', {month:'short', day:'numeric'});
  const noLegend = { plugins: { legend: { display: false } } };

  const showEmpty = () => { emptyEl.style.display = 'block'; wrapEl.style.display = 'none'; };
  const showChart = () => { emptyEl.style.display = 'none';  wrapEl.style.display = ''; };

  // ── Mode: Kapitalwert ─────────────────────────────────────────────────
  if (mode === 'capital') {
    const series = data.capital_series || [];
    if (series.length < 2) { showEmpty(); return; }
    showChart();
    const first = series[0].usdc, last = series[series.length - 1].usdc;
    const up = last >= first;
    const lineCol = up ? '#3fb950' : '#f85149';
    const fillCol = up ? 'rgba(63,185,80,0.08)' : 'rgba(248,81,73,0.08)';
    perfChart = mkChart('perf-chart', 'line', {
      labels: series.map(p => fmtTs(p.ts)),
      datasets: [{
        label: 'Kapital', data: series.map(p => p.usdc),
        borderColor: lineCol, borderWidth: 2.5,
        pointRadius: series.length <= 8 ? 4 : 0,
        pointHoverRadius: 5,
        fill: true, backgroundColor: fillCol,
        tension: 0, stepped: 'after',
      }],
    }, { ...chartDefaults, ...noLegend,
      scales: { ...chartDefaults.scales,
        y: { ticks: { color:'#8b949e', font:{size:10},
              callback: v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) },
             grid: { color:'#21262d' } },
      },
      plugins: { ...noLegend.plugins,
        tooltip: { callbacks: {
          label: ctx => `$${ctx.parsed.y.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`,
        }},
      },
    });

  // ── Mode: % vs. BTC ───────────────────────────────────────────────────
  } else if (mode === 'pct') {
    const bot = data.bot_pct_series || [];
    const btc = data.btc_pct_series || [];
    if (bot.length < 2 && !btc.length) { showEmpty(); return; }
    showChart();
    // custom legend
    legendRow.innerHTML =
      `<div class="perf-legend-item"><div class="perf-legend-line" style="background:#58a6ff"></div>Bot</div>` +
      (btc.length ? `<div class="perf-legend-item"><div class="perf-legend-line" style="border-top:2px dashed #f0a500;background:none"></div>BTC</div>` : '');
    legendRow.style.display = 'flex';
    perfChart = mkChart('perf-chart', 'line', { datasets: [
      { label: 'Bot',
        data: bot.map(p => ({x: p.ts, y: p.pct})), parsing: false,
        borderColor: '#58a6ff', borderWidth: 2.5,
        pointRadius: bot.length <= 8 ? 4 : 0, pointHoverRadius: 5,
        fill: false, tension: 0, stepped: 'after' },
      ...(btc.length ? [{
        label: 'BTC',
        data: btc.map(p => ({x: p.ts, y: p.pct})), parsing: false,
        borderColor: '#f0a500', borderWidth: 1.5, borderDash: [5,4],
        pointRadius: 0, pointHoverRadius: 4,
        fill: false, tension: 0.1 }] : []),
    ]}, { ...chartDefaults, ...noLegend,
      scales: {
        x: { type:'linear', ticks:{ color:'#8b949e', font:{size:10}, maxTicksLimit:6,
              callback: v => fmtDay(v) }, grid:{color:'#21262d'} },
        y: { ticks:{ color:'#8b949e', font:{size:10},
              callback: v => (v>=0?'+':'') + v.toFixed(1) + '%' },
             grid:{ color: ctx => ctx.tick.value === 0 ? 'rgba(139,148,158,0.4)' : '#21262d' } },
      },
      plugins: { ...noLegend.plugins,
        tooltip: { callbacks: {
          label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}%`,
        }},
      },
    });

  // ── Mode: Trade P&L ───────────────────────────────────────────────────
  } else if (mode === 'trades') {
    const pnl = data.trade_pnl || [];
    if (!pnl.length) { showEmpty(); return; }
    showChart();
    perfChart = mkChart('perf-chart', 'bar', {
      labels: pnl.map((t, i) => `#${i + 1}`),
      datasets: [{
        label: 'P&L',
        data: pnl.map(t => t.pct),
        backgroundColor: pnl.map(t => t.pct >= 0 ? 'rgba(63,185,80,0.75)' : 'rgba(248,81,73,0.75)'),
        borderColor:     pnl.map(t => t.pct >= 0 ? '#3fb950' : '#f85149'),
        borderWidth: 1, borderRadius: 4,
      }],
    }, { ...chartDefaults, ...noLegend,
      scales: { ...chartDefaults.scales,
        y: { ticks:{ color:'#8b949e', font:{size:10},
              callback: v => (v>=0?'+':'') + v.toFixed(1) + '%' },
             grid:{ color: ctx => ctx.tick.value === 0 ? 'rgba(139,148,158,0.4)' : '#21262d' } },
      },
      plugins: { ...noLegend.plugins,
        tooltip: { callbacks: {
          label: ctx => {
            const t = pnl[ctx.dataIndex];
            return `${t.symbol}: ${t.pct >= 0 ? '+' : ''}${t.pct.toFixed(2)}%`;
          },
        }},
      },
    });
  }
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

function _showApiKeyFields(focus = true) {
  const fields = document.getElementById('api-key-fields');
  if (fields) {
    fields.style.display = 'block';
    if (focus) document.getElementById('live-api-key')?.focus();
  }
}

function _hideApiKeyFields() {
  const fields = document.getElementById('api-key-fields');
  if (fields) fields.style.display = 'none';
}

async function loadSavedCredentials() {
  try {
    const creds = await fetch('/api/live/credentials').then(r => r.json());
    const keyEl = document.getElementById('live-api-key');
    const secEl = document.getElementById('live-api-secret');
    const statusEl = document.getElementById('binance-key-status');

    if (creds.has_key) { keyEl.placeholder = '••••••••••••••••'; keyEl.dataset.saved = '1'; }
    if (creds.has_secret) { secEl.placeholder = '••••••••••••••••'; secEl.dataset.saved = '1'; }

    if (statusEl) {
      if (creds.has_key && creds.has_secret) {
        _hideApiKeyFields();
        const masked = _buildMaskedKey(creds.key_hint || '');
        const hint   = (creds.key_hint || '').replace(/"/g, '');
        statusEl.innerHTML = `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <div class="apikey-status-pill">
            <span class="apikey-ok-mark">✓</span>
            <span class="apikey-masked-text" id="apikey-display" data-hint="${hint}">${masked}</span>
            <button class="apikey-eye-btn" id="apikey-eye-btn" aria-label="API Key anzeigen"
                    aria-pressed="false" onclick="toggleApiKeyReveal()" type="button">
              <svg class="eye-icon eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/>
              </svg>
              <svg class="eye-icon eye-closed" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="display:none">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <line x1="1" y1="1" x2="23" y2="23" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              </svg>
            </button>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="_showApiKeyFields(true)" type="button"
                  style="font-size:11px;padding:4px 10px">✎ Keys ändern</button>
        </div>`;
      } else {
        _showApiKeyFields(false);
        statusEl.innerHTML = `<span style="background:rgba(255,165,0,0.12);border:1px solid #f0a500;color:#f0a500;padding:3px 10px;border-radius:12px;font-size:12px">⚠ Kein Binance API Key gespeichert</span>`;
      }
    }
  } catch {}
}

// ── User settings persist ────────────────────────────────────────────────────
let _saveSettingsTimer = null;

function saveUserSettings() {
  clearTimeout(_saveSettingsTimer);
  _saveSettingsTimer = setTimeout(async () => {
    const s = {
      live_interval:         document.getElementById('live-interval')?.value,
      live_amount:           parseFloat(document.getElementById('live-amount')?.value) || 50,
      live_compounding_mode: document.getElementById('live-compounding-mode')?.value,
      live_analysis_weight:  parseInt(document.getElementById('live-analysis-weight')?.value) || 30,
      live_min_confidence:   parseInt(document.getElementById('live-min-confidence')?.value) || 55,
      live_sl_mult:          parseFloat(document.getElementById('live-sl-mult')?.value) || 1.5,
      live_tp_mult:          parseFloat(document.getElementById('live-tp-mult')?.value) || 2.5,
      sim_symbol:            document.getElementById('symbol')?.value,
      sim_interval:          document.getElementById('interval')?.value,
      sim_days:              parseInt(document.getElementById('days')?.value) || 30,
      sim_capital:           parseFloat(document.getElementById('capital')?.value) || 1000,
      sim_fee_tier:          document.getElementById('fee-tier')?.value,
      sim_compounding_mode:  document.getElementById('compounding-mode')?.value,
      sim_analysis_weight:   parseInt(document.getElementById('sim-analysis-weight')?.value) || 30,
    };
    try {
      await fetch('/api/user/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(s),
      });
    } catch {}
  }, 500);
}

async function loadUserSettings() {
  try {
    const s = await fetch('/api/user/settings').then(r => r.json());
    const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
    set('live-interval',          s.live_interval);
    set('live-amount',            s.live_amount);
    set('live-compounding-mode',  s.live_compounding_mode);
    set('live-analysis-weight',   s.live_analysis_weight);
    if (s.live_analysis_weight != null) updateWeightLabel(s.live_analysis_weight);
    set('live-min-confidence',    s.live_min_confidence);
    set('live-sl-mult',           s.live_sl_mult);
    set('live-tp-mult',           s.live_tp_mult);
    set('symbol',                 s.sim_symbol);
    set('interval',               s.sim_interval);
    set('days',                   s.sim_days);
    set('capital',                s.sim_capital);
    set('fee-tier',               s.sim_fee_tier);
    set('compounding-mode',       s.sim_compounding_mode);
    set('sim-analysis-weight',    s.sim_analysis_weight);
    if (s.sim_analysis_weight != null) updateSimWeightLabel(s.sim_analysis_weight);
  } catch {}
}

async function initPage() {
  loadSavedCredentials();
  loadUserSettings();
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

      livePolling = setInterval(pollLive, 5000);
      pollLive();
      loadPerformance(true);
    }
  } catch (e) {}

  loadSimHistory();
  loadExtraSyms();
}

// Load user profile (show admin button, username in header)
fetch('/api/user/profile').then(r => r.json()).then(data => {
  _currentUsername = data.username;
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
