/* The direct-trading page, wired to the engine over pywebview's JS bridge.
 *
 * The ticket's rules are the design's, which are the book's: the side falls out of where
 * the stop sits (below price/entry = buy, above = sell), rr and target sync with
 * whichever was touched last keeping its meaning, and there is no lots field — every
 * slave sizes from its own risk budget, so the ticket shows what each would risk instead.
 * The book still rules on every order; this page's checks only keep the button honest.
 */
'use strict';

var $ = function (id) { return document.getElementById(id); };

// --- state ---------------------------------------------------------------------------------
var S = {
  symbol: '', type: 'market', tf: 'M5',
  lastTouched: 'rr',
  live: null,          // last state() answer from the engine
  candles: [],         // last rates() answer, oldest first
  candlesKey: '',      // symbol|tf the candles belong to
  digits: 2,           // decimals seen in the data, for formatting dragged prices
  dragging: null,      // 'sl' | 'tp' | 'entry' while a line is held, whichever mode owns it
  notice: '', noticeTone: '',
  mode: 'new',         // 'new' places orders; 'modify' takes the panel AND the chart for one trade
  modRow: null,        // the trade being modified, as its book row read at entry
  placing: false,
  placedUntil: 0,      // while now < this, the place button says "✓ Placed" and stays dead
  haltBusy: false,
  guardSeen: null,     // guard-event keys already toasted; null until the first poll primes it
  page: 'trading'      // which page owns #main: 'trading' or 'terminals'
};

var defaultRr = '';
try {
  S.symbol = localStorage.getItem('dt-symbol') || '';
  S.tf = localStorage.getItem('dt-tf') || 'M5';
  defaultRr = localStorage.getItem('dt-rr') || '';
  var savedPage = localStorage.getItem('dt-page');
  if (savedPage === 'terminals' || savedPage === 'guards' || savedPage === 'news') S.page = savedPage;
} catch (e) { /* storage may be unavailable; defaults are fine */ }

function rememberRr(text) {
  // The last rr that meant something becomes the default, restarts included.
  var v = parseFloat(text);
  if (!isFinite(v) || v <= 0) return;
  defaultRr = text;
  try { localStorage.setItem('dt-rr', text); } catch (e) { /* fine */ }
}

function num(text) { var v = parseFloat(text); return isFinite(v) ? v : NaN; }

function fmt(price) {
  return (Math.round(price * Math.pow(10, S.digits)) / Math.pow(10, S.digits)).toFixed(S.digits);
}

function decimalsOf(x) {
  var s = String(x);
  var i = s.indexOf('.');
  return i < 0 ? 0 : Math.min(8, s.length - i - 1);
}

// --- the chart -----------------------------------------------------------------------------
var chart = null, series = null, lineSl = null, lineTp = null, lineEntry = null;

function activeFieldIds() {
  // The chart always drags the fields of whichever panel owns it: the ticket's in new
  // mode, the modify form's in modify mode. One chart, one job at a time.
  if (S.mode === 'modify') {
    var ids = { sl: 'm-sl', tp: 'm-tp' };
    if (S.modRow && !S.modRow.open) ids.entry = 'm-entry';
    return ids;
  }
  var ids2 = { sl: 'f-stop', tp: 'f-tp' };
  if (S.type !== 'market') ids2.entry = 'f-entry';
  return ids2;
}

function makeChart() {
  var el = $('chart');
  chart = LightweightCharts.createChart(el, {
    layout: {
      background: { type: 'solid', color: '#221d16' },
      textColor: '#a39a87',
      fontFamily: "'JetBrains Mono', Consolas, monospace",
      fontSize: 12
    },
    grid: {
      vertLines: { color: '#2c2720' },
      horzLines: { color: '#2c2720' }
    },
    rightPriceScale: { borderColor: '#332d23' },
    timeScale: { borderColor: '#332d23', timeVisible: true, secondsVisible: false },
    crosshair: {
      horzLine: { color: '#a39a87', labelBackgroundColor: '#4a4235' },
      vertLine: { color: '#a39a87', labelBackgroundColor: '#4a4235' }
    }
  });
  series = chart.addCandlestickSeries({
    upColor: '#7fbf6e', downColor: '#e5645a',
    wickUpColor: '#7fbf6e', wickDownColor: '#e5645a',
    borderVisible: false
  });
  new ResizeObserver(function () {
    var r = el.getBoundingClientRect();
    chart.resize(Math.floor(r.width), Math.floor(r.height));
  }).observe(el);

  // Dragging the levels: grab the nearest line within reach, write the price it is let
  // go at straight into the ticket's field — the field is what actually moves.
  function nearestLine(y) {
    var ids = activeFieldIds();
    var best = null, bestAway = 9;
    Object.keys(ids).forEach(function (which) {
      var price = num($(ids[which]).value);
      if (!isFinite(price) || price <= 0) return;
      var at = series.priceToCoordinate(price);
      if (at === null) return;
      var away = Math.abs(at - y);
      if (away < bestAway) { bestAway = away; best = which; }
    });
    return best;
  }
  el.addEventListener('mousedown', function (ev) {
    if (!S.candles.length || ev.button !== 0) return;
    var y = ev.clientY - el.getBoundingClientRect().top;
    var best = nearestLine(y);
    if (best === null) {
      // A click with no stop yet starts the ticket: the stop lands where the click is,
      // and the side falls out of it as always. Only the ticket seeds this way.
      var stopBox = $('f-stop');
      if (S.mode === 'new' && !stopBox.value.trim()) {
        var price = series.coordinateToPrice(y);
        if (price !== null) { stopBox.value = fmt(price); onLevelTyped('sl'); }
      }
      return;
    }
    S.dragging = best;
    el.classList.add('dragging');
    chart.applyOptions({ handleScroll: false, handleScale: false });
    ev.preventDefault();
  });
  // A line within grab reach shows its cursor, so it reads as draggable before it is.
  el.addEventListener('mousemove', function (ev) {
    if (S.dragging || !S.candles.length) return;
    var y = ev.clientY - el.getBoundingClientRect().top;
    el.classList.toggle('line-hover', nearestLine(y) !== null);
  });
  el.addEventListener('mouseleave', function () { el.classList.remove('line-hover'); });
  window.addEventListener('mousemove', function (ev) {
    if (!S.dragging) return;
    var y = ev.clientY - el.getBoundingClientRect().top;
    var price = series.coordinateToPrice(y);
    if (price === null) return;
    $(activeFieldIds()[S.dragging]).value = fmt(price);
    if (S.mode === 'modify') renderModify();
    else onLevelTyped(S.dragging);
  });
  window.addEventListener('mouseup', function () {
    if (!S.dragging) return;
    S.dragging = null;
    el.classList.remove('dragging');
    chart.applyOptions({ handleScroll: true, handleScale: true });
  });
}

function setLine(which, price, opts) {
  var have = which === 'sl' ? lineSl : which === 'tp' ? lineTp : lineEntry;
  if (have) { series.removePriceLine(have); have = null; }
  if (price && isFinite(price)) {
    have = series.createPriceLine(Object.assign({ price: price, lineWidth: 2, axisLabelVisible: true }, opts));
  }
  if (which === 'sl') lineSl = have; else if (which === 'tp') lineTp = have; else lineEntry = have;
}

function showCandles(data) {
  var key = S.symbol + '|' + S.tf;
  var moved = key !== S.candlesKey;
  var fresh = moved || data.length !== S.candles.length ||
    (data.length && S.candles.length && data[data.length - 1].time !== S.candles[S.candles.length - 1].time);
  S.candles = data;
  if (!data.length) {
    // The key stays unset, so the first real bars count as a move and reset the scales.
    S.candlesKey = '';
    series.setData([]);
    return;
  }
  S.candlesKey = key;
  var d = 0;
  for (var i = Math.max(0, data.length - 20); i < data.length; i++) d = Math.max(d, decimalsOf(data[i].close));
  S.digits = Math.max(d, 1);
  if (fresh) series.setData(data);
  else series.update(data[data.length - 1]);
  if (moved) {
    // A new symbol or timeframe is a new chart, and whatever zoom or axis drag was done
    // on the old one must not carry over. Dragging the price axis turns autoscale off
    // FOR GOOD in lightweight-charts -- that is how ETH's bars ended up invisible under
    // BTC's left-over 61k-69k range -- so it is switched back on here explicitly.
    chart.priceScale('right').applyOptions({ autoScale: true });
    chart.timeScale().fitContent();
  }
}

// --- modify mode: the panel and the chart belong to one trade ------------------------------
function enterModify(row) {
  S.mode = 'modify';
  S.modRow = row;
  if (row.symbol !== S.symbol) {
    // The chart follows the trade: modifying is meaningless over another symbol's bars.
    S.symbol = row.symbol;
    $('f-symbol').value = row.symbol;
    symbolChanged(); // the ticket's levels were the old symbol's; they come back blank
    $('chart-title').firstChild.textContent = row.symbol + ' ';
    pollRates();
  }
  $('m-entry').value = row.open ? '' : String(row.entry);
  $('m-sl').value = row.sl ? String(row.sl) : '';
  $('m-tp').value = row.tp ? String(row.tp) : '';
  $('mod-notice').textContent = '';
  $('mod-notice').className = 'notice';
  $('ticket').hidden = true;
  $('modify-panel').hidden = false;
  $('chart-hint').textContent = 'drag the lines — nothing changes until Apply';
  renderModify();
  $('m-sl').focus();
}

function exitModify() {
  S.mode = 'new';
  S.modRow = null;
  $('modify-panel').hidden = true;
  $('ticket').hidden = false;
  $('chart-hint').textContent = 'drag the SL / TP lines · click with no stop to set one';
  renderTicket();
}

function renderModify() {
  var row = S.modRow;
  if (!row) return;
  var q = S.live && S.live.quote;
  $('mod-title').textContent = 'Modify #' + row.ticket + ' · ' + row.symbol;
  $('mod-quote').textContent = q ? 'bid ' + q.bid + ' · ask ' + q.ask : '';

  var facts = $('mod-facts');
  facts.textContent = '';
  var side = document.createElement('span');
  side.className = row.side === 'buy' ? 'side-buy' : 'side-sell';
  side.textContent = row.side + (row.kind === 'market' ? '' : ' ' + row.kind);
  facts.appendChild(side);
  var lots = document.createElement('span');
  lots.textContent = row.lots + ' lots';
  facts.appendChild(lots);
  var at = document.createElement('span');
  at.textContent = (row.open ? 'in at ' : 'resting at ') + row.entry;
  facts.appendChild(at);
  if (row.open && row.copies && row.copies.length) {
    var cp = copiesSummary(row);
    var cps = document.createElement('span');
    cps.className = cp.cls;
    cps.textContent = 'copies: ' + cp.text;
    cps.title = cp.title;
    facts.appendChild(cps);
  }

  $('mod-entry-field').hidden = row.open;

  var way = row.side === 'buy' ? 1 : -1;
  var entry = row.open ? row.entry : num($('m-entry').value);
  var sl = num($('m-sl').value), tp = num($('m-tp').value);
  var entryOk = isFinite(entry) && entry > 0;
  var slOk = isFinite(sl) && sl > 0;
  var tpIn = isFinite(tp) && tp > 0;

  var slDelta = $('m-sl-delta');
  if (slOk && entryOk) {
    var risk = way * (entry - sl);
    if (risk > 0) {
      slDelta.textContent = '−' + fmt(risk) + ' from the entry';
      slDelta.className = 'field-note red';
    } else if (risk === 0) {
      slDelta.textContent = 'at the entry — breakeven';
      slDelta.className = 'field-note blue';
    } else {
      slDelta.textContent = '+' + fmt(-risk) + ' past the entry — profit locked';
      slDelta.className = 'field-note blue';
    }
  } else {
    slDelta.textContent = '';
    slDelta.className = 'field-note';
  }
  $('m-tp-delta').textContent = tpIn && entryOk
    ? (way * (tp - entry) >= 0 ? '+' : '−') + fmt(Math.abs(tp - entry))
    : '';
  if (!row.open && entryOk && q) {
    var away = entry - (row.side === 'buy' ? q.ask : q.bid);
    $('m-entry-delta').textContent = (away >= 0 ? '+' : '−') + fmt(Math.abs(away)) + ' from price';
  } else {
    $('m-entry-delta').textContent = '';
  }

  var apply = $('mod-apply');
  apply.textContent = 'Apply to #' + row.ticket;
  apply.disabled = !slOk || (!row.open && !entryOk);

  // The chart carries only this trade, as a draft: dashed until Apply makes it real.
  setLine('entry', entryOk ? entry : NaN, row.open
    ? { color: '#6f6757', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'entry' }
    : { color: '#7ea6e0', lineStyle: LightweightCharts.LineStyle.Dashed, title: 'entry' });
  setLine('sl', slOk ? sl : NaN, {
    color: '#e5645a', lineStyle: LightweightCharts.LineStyle.Dashed, title: '#' + row.ticket + ' SL'
  });
  setLine('tp', tpIn ? tp : NaN, {
    color: '#7fbf6e', lineStyle: LightweightCharts.LineStyle.Dashed, title: '#' + row.ticket + ' TP'
  });
}

function applyModify() {
  var row = S.modRow;
  if (!row) return;
  function bad(msg) {
    var n = $('mod-notice');
    n.textContent = msg;
    n.className = 'notice bad';
  }
  var sl = num($('m-sl').value), tp = num($('m-tp').value);
  if (!isFinite(sl) || sl <= 0) { bad('The stop loss is required.'); return; }
  var call;
  if (row.open) {
    call = api().modify_position(row.ticket, sl, isFinite(tp) && tp > 0 ? tp : 0);
  } else {
    var entry = num($('m-entry').value);
    if (!isFinite(entry) || entry <= 0) { bad('The entry price is required.'); return; }
    call = api().modify_order(row.ticket, entry, sl, isFinite(tp) && tp > 0 ? tp : 0);
  }
  call.then(function (r) {
    if (r.ok) { toast(r.message, 'ok'); exitModify(); poll(); }
    else bad(r.message);
  });
}

function nudgeLastBar(bid) {
  // Between bar refreshes the last candle moves live off the quote, like the TUI's chart.
  // Only once the bars on the chart are this symbol's own: right after a symbol change
  // the old symbol's candles are still up while the feed warms, and nudging those with
  // the new quote smears one symbol's price into another's bar and wrecks the scale.
  if (S.candlesKey !== S.symbol + '|' + S.tf) return;
  if (!S.candles.length || !isFinite(bid)) return;
  var last = S.candles[S.candles.length - 1];
  if (bid === last.close) return;
  last = {
    time: last.time, open: last.open,
    high: Math.max(last.high, bid), low: Math.min(last.low, bid), close: bid
  };
  S.candles[S.candles.length - 1] = last;
  series.update(last);
}

// --- the ticket's arithmetic ---------------------------------------------------------------
function compute() {
  var q = S.live && S.live.quote;
  var isPending = S.type !== 'market';
  var stop = num($('f-stop').value);
  var stopIn = isFinite(stop) && stop > 0;
  var entryIn = num($('f-entry').value);

  var side = null, entry = NaN;
  if (stopIn) {
    if (isPending) {
      if (isFinite(entryIn) && entryIn > 0) {
        if (stop < entryIn) side = 'buy';
        else if (stop > entryIn) side = 'sell';
        entry = entryIn;
      }
    } else if (q) {
      if (stop < q.bid) { side = 'buy'; entry = q.ask; }
      else if (stop > q.ask) { side = 'sell'; entry = q.bid; }
    }
  }
  var entryOk = isFinite(entry) && entry > 0 && side !== null;
  var dist = entryOk ? Math.abs(entry - stop) : 0;

  var rrIn = num($('f-rr').value), tpIn = num($('f-tp').value);
  var rr = NaN, tp = NaN;
  if (S.lastTouched === 'tp' && isFinite(tpIn) && tpIn > 0) {
    tp = tpIn;
    if (dist > 0) rr = Math.abs(tp - entry) / dist;
  } else if (isFinite(rrIn) && rrIn > 0) {
    rr = rrIn;
    if (entryOk) tp = side === 'buy' ? entry + dist * rr : entry - dist * rr;
  }
  var tpOk = entryOk && isFinite(tp) && (side === 'buy' ? tp > entry : tp < entry);
  var ok = entryOk && tpOk && isFinite(rr) && rr > 0;

  var halted = S.live && !S.live.copying;
  var problem = '';
  if (!S.live) problem = 'Starting…';
  else if (!S.live.direct) problem = 'This profile copies a master — switch it to direct trading in the TUI first.';
  else if (halted) problem = 'Copying is halted. Resume to place orders.';
  else if (!S.symbol) problem = 'Name the symbol to trade.';
  else if (!q) problem = S.live.feed ? 'Waiting for ' + S.live.feed + ' to quote ' + S.symbol + '…'
    : 'No feed: connect an MT5 slave to quote prices.';
  else if (!stopIn) problem = 'Set a stop loss — the side and every slave’s size come from it.';
  else if (isPending && !(isFinite(entryIn) && entryIn > 0)) problem = 'Enter an entry price.';
  else if (isPending && parseExpiry($('f-expiry').value) === null) problem = 'The expiry did not read — use minutes, 45m, 2h or eod; blank rests it until cancelled.';
  else if (side === null) problem = isPending ? 'Stop equals the entry — move one of them.'
    : 'The stop sits inside the spread — nudge it to pick a side.';
  else if (!isFinite(rr) || rr <= 0) problem = 'Enter a risk : reward or a take profit.';
  else if (!tpOk) problem = side === 'buy' ? 'Take profit must be above the entry for a buy.'
    : 'Take profit must be below the entry for a sell.';

  return {
    isPending: isPending, side: side, entry: entry, entryOk: entryOk,
    dist: dist, rr: rr, tp: tp, ok: ok, problem: problem, halted: !!halted, stopIn: stopIn
  };
}

function renderTicket() {
  if (S.mode === 'modify') {
    renderModify();
    return null;
  }
  var t = compute();
  var q = S.live && S.live.quote;

  $('quote-line').textContent = q ? 'bid ' + q.bid + ' · ask ' + q.ask : '';

  var note = $('side-note');
  if (t.stopIn && t.side === 'buy') {
    note.textContent = 'stop below the ' + (t.isPending ? 'entry' : 'price') + ' → buying';
    note.className = 'field-note blue';
  } else if (t.stopIn && t.side === 'sell') {
    note.textContent = 'stop above the ' + (t.isPending ? 'entry' : 'price') + ' → selling';
    note.className = 'field-note red';
  } else {
    note.textContent = '';
    note.className = 'field-note';
  }

  $('stop-delta').textContent = t.ok ? '−' + fmt(t.dist) : '';
  $('target-delta').textContent = t.ok ? '+' + fmt(Math.abs(t.tp - t.entry)) : '';

  // The field not touched last follows the one that was.
  if (S.lastTouched === 'rr') {
    var tpBox = $('f-tp');
    if (document.activeElement !== tpBox) tpBox.value = t.ok ? fmt(t.tp) : tpBox.value;
  } else {
    var rrBox = $('f-rr');
    if (document.activeElement !== rrBox && t.ok) {
      rrBox.value = String(Math.round(t.rr * 100) / 100);
    }
  }

  // Slaves size themselves from their own risk: a row per slave, name left, lots right.
  var block = $('sizing-block');
  var rowsEl = $('sizing-rows');
  var sizing = (S.live && S.live.sizing) || [];
  if (S.live && S.live.direct && !sizing.length) {
    block.hidden = false;
    rowsEl.textContent = '';
    $('sizing-sub').textContent = '';
    $('sizing-note').textContent = 'No enabled slaves — nothing will be copied.';
  } else if (t.ok && sizing.length) {
    block.hidden = false;
    $('sizing-sub').textContent = 'each from its own risk per trade';
    rowsEl.textContent = '';
    sizing.forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'sizing-row';
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = s.name;
      var lots = document.createElement('span');
      if (s.budget === null || !t.dist) {
        lots.className = 'lots unknown';
        lots.textContent = s.risk + ' · balance unknown';
      } else {
        lots.className = 'lots';
        lots.textContent = '≈' + Math.max(0.01, s.budget / t.dist).toFixed(2) + ' lots';
      }
      row.appendChild(name);
      row.appendChild(lots);
      rowsEl.appendChild(row);
    });
    $('sizing-note').textContent =
      '≈ estimated — each terminal prices the stop and adds its commission';
  } else {
    block.hidden = true;
  }

  // The button narrates the act: Placing… while the engine rules, ✓ Placed while the
  // order is fresh — dead both times, so an impatient second click cannot double an order.
  var place = $('place-btn');
  var typeWord = t.isPending ? ' ' + S.type : '';
  if (S.placing) {
    place.textContent = 'Placing…';
    place.disabled = true;
    place.className = t.side || '';
  } else if (Date.now() < S.placedUntil) {
    place.textContent = '✓ Placed';
    place.disabled = true;
    place.className = 'placed';
  } else {
    place.textContent = t.side
      ? (t.side === 'buy' ? 'Buy' : 'Sell') + typeWord + ' ' + S.symbol
      : 'Set the levels first';
    place.disabled = t.problem !== '';
    place.className = t.problem === '' ? t.side : '';
  }

  var noticeEl = $('notice');
  if (t.problem && S.live && S.live.direct) {
    noticeEl.textContent = t.problem;
    noticeEl.className = 'notice warn';
  } else if (!S.live || S.live.direct) {
    noticeEl.textContent = S.notice;
    noticeEl.className = 'notice ' + S.noticeTone;
  } else {
    noticeEl.textContent = t.problem;
    noticeEl.className = 'notice warn';
  }

  // The chart carries the same levels.
  setLine('entry', t.entryOk ? t.entry : (t.isPending ? num($('f-entry').value) : NaN), {
    color: '#7ea6e0', lineStyle: LightweightCharts.LineStyle.Dotted,
    title: t.side ? (t.side === 'buy' ? 'buy' : 'sell') : 'entry'
  });
  var slv = num($('f-stop').value);
  setLine('sl', isFinite(slv) && slv > 0 ? slv : NaN, {
    color: '#e5645a', lineStyle: LightweightCharts.LineStyle.Dashed, title: 'SL'
  });
  // The TP line follows the field, valid or not: dragging the stop across the price flips
  // the side and may make the target momentarily wrong, but a vanishing line reads as a
  // bug — the notice already says what is wrong, so the line stays put to be dragged.
  var tpLine = S.lastTouched === 'tp' ? num($('f-tp').value) : (isFinite(t.tp) ? t.tp : num($('f-tp').value));
  setLine('tp', isFinite(tpLine) && tpLine > 0 ? tpLine : NaN, {
    color: '#7fbf6e', lineStyle: LightweightCharts.LineStyle.Dashed, title: 'TP'
  });

  return t;
}

// --- shell chrome --------------------------------------------------------------------------
function renderShell() {
  var live = S.live;
  if (!live) return;
  $('profile-name').textContent = live.profile;
  var pill = $('live-pill');
  pill.className = 'live-pill' + (live.copying ? '' : ' halted');
  $('live-label').textContent = live.copying ? (live.dryRun ? 'LIVE · DRY' : 'LIVE') : 'HALTED';
  var halt = $('halt-btn');
  halt.textContent = live.copying ? 'Halt all' : 'Resume copying';
  halt.className = 'halt-btn' + (live.copying ? '' : ' resume');
  halt.disabled = S.haltBusy;

  $('page-sub').textContent = live.direct
    ? 'Orders placed here are copied to ' + live.slaves + ' slave' + (live.slaves === 1 ? '' : 's')
    : 'This profile copies a master — direct trading is off.';

  // With a real master, orders are placed in its own terminal and only copied from
  // there — the ticket has nothing to do. The tab greys out and carries the reason,
  // and anyone already on the page is walked off it.
  var tradingTab = document.querySelector('#sidebar .nav-item[data-page="trading"]');
  tradingTab.classList.toggle('disabled', !live.direct);
  tradingTab.title = live.direct
    ? 'Trading'
    : 'This profile copies a master terminal' + (live.feed ? ' (' + live.feed + ')' : '')
      + ' — place trades there. Direct trading (no master) turns this page back on.';
  if (!live.direct && S.page === 'trading') showPage('terminals');

  $('dot-worker').className = 'dot ' + (live.workersLive > 0 ? 'green' : 'red');
  $('status-worker').textContent = live.workersLive + '/' + live.workersTotal + ' workers';
  $('dot-feed').className = 'dot ' + (live.feed ? 'green' : 'red');
  $('status-feed').textContent = live.feed ? 'feed: ' + live.feed : 'no feed';
  $('status-copying').textContent = live.copying
    ? 'Copying to ' + live.slaves + ' slave' + (live.slaves === 1 ? '' : 's')
    : 'Copying halted';
  $('status-dry').hidden = !live.dryRun;
}

// --- the book ------------------------------------------------------------------------------
var bookDrawn = '';
var bookTickets = null; // tickets drawn last time; a row not among them flashes as new

function copiesSummary(r) {
  // What still stands behind the ticket on the slaves. 'all closed' is the cue that
  // closing the master would close nothing real; '?' is a copy that cannot be verified
  // right now (slave unreadable, or a dry run's copy, which never exists on a broker).
  var c = (r.open && r.copies) || [];
  if (!c.length) return { text: '—', cls: 'muted', title: 'no copies tracked' };
  var live = 0, closed = 0, unknown = 0;
  c.forEach(function (s) {
    if (s.state === 'live') live++;
    else if (s.state === 'closed') closed++;
    else unknown++;
  });
  var title = c.map(function (s) {
    return s.name + ' — ' + (s.state === 'unknown' ? 'unverified' : s.state);
  }).join('\n');
  if (closed === c.length) return { text: 'all closed', cls: 'cp-dead', title: title };
  var parts = [];
  if (live) parts.push(live + ' live');
  if (closed) parts.push(closed + ' closed');
  if (unknown) parts.push(unknown + ' ?');
  return {
    text: parts.join(' · '),
    cls: closed ? 'cp-part' : (live ? 'cp-live' : 'muted'),
    title: title
  };
}

function renderBook() {
  var rows = (S.live && S.live.book) || [];
  var open = rows.filter(function (r) { return r.open; }).length;
  $('book-summary').textContent = open + ' open · ' + (rows.length - open) + ' resting';
  $('book-empty').hidden = rows.length > 0;

  var key = JSON.stringify(rows);
  if (key === bookDrawn) return;
  bookDrawn = key;

  var body = $('book-body');
  body.textContent = '';
  var seen = {};
  rows.forEach(function (r) {
    seen[r.ticket] = true;
    var tr = document.createElement('tr');
    if (bookTickets && !bookTickets[r.ticket]) tr.className = 'row-new';
    function cell(text, cls) {
      var td = document.createElement('td');
      if (cls) td.className = cls;
      td.textContent = text;
      tr.appendChild(td);
      return td;
    }
    var status = document.createElement('td');
    var pillEl = document.createElement('span');
    pillEl.className = 'status-pill ' + (r.open ? 'open' : 'resting');
    pillEl.textContent = r.open ? 'Open' : 'Resting';
    status.appendChild(pillEl);
    tr.appendChild(status);
    cell('#' + r.ticket, 'muted');
    cell(r.symbol, 'sym');
    cell(r.side + (r.kind === 'market' ? '' : ' ' + r.kind), r.side === 'buy' ? 'side-buy' : 'side-sell');
    cell(String(r.lots));
    cell(String(r.entry));
    cell(r.sl ? String(r.sl) : '—', r.sl ? '' : 'muted');
    cell(r.tp ? String(r.tp) : '—', r.tp ? '' : 'muted');
    cell(r.open ? (r.now === null ? '—' : String(r.now))
      : (r.expiresMin === null ? 'GTC' : 'expires ' + r.expiresMin + 'm'), 'muted');
    var cp = copiesSummary(r);
    var cpCell = cell(r.open ? cp.text : '—', r.open ? cp.cls : 'muted');
    if (r.open && cp.title) cpCell.title = cp.title;

    var actions = document.createElement('td');
    actions.className = 'actions-cell';
    if (r.open) {
      var be = document.createElement('button');
      be.type = 'button';
      be.className = 'row-btn';
      be.textContent = 'BE';
      be.title = 'Breakeven: move the stop to the entry price';
      be.addEventListener('click', function () {
        act(api().modify_position(r.ticket, r.entry, r.tp || 0));
      });
      actions.appendChild(be);
    }
    var modify = document.createElement('button');
    modify.type = 'button';
    modify.className = 'row-btn';
    modify.textContent = 'Modify';
    modify.addEventListener('click', function () { enterModify(r); });
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'row-btn danger';
    remove.textContent = r.open ? 'Close' : 'Cancel';
    remove.addEventListener('click', function () {
      act(r.open ? api().close(r.ticket) : api().cancel(r.ticket));
    });
    actions.appendChild(modify);
    actions.appendChild(remove);
    tr.appendChild(actions);
    body.appendChild(tr);
  });
  bookTickets = seen;
}

// --- talking to the engine -----------------------------------------------------------------
function api() { return window.pywebview.api; }

function say(message, tone) {
  S.notice = message;
  S.noticeTone = tone;
  renderTicket();
}

function toast(message, tone, ms) {
  var t = document.createElement('div');
  t.className = 'toast ' + (tone || '');
  t.textContent = message;
  $('toasts').appendChild(t);
  function leave() {
    if (!t.parentNode) return;
    t.classList.add('leaving');
    setTimeout(function () { t.remove(); }, 260);
  }
  t.addEventListener('click', leave);
  setTimeout(leave, ms || 4500);
}

// A guard acting on a copy says so the moment it happens, wherever the person is —
// not on the Guards page only, found later. The state poll carries the last couple of
// minutes of actions on every account; the first poll primes what is already old news,
// and the seen set is rebuilt from the payload each time, so it prunes itself.
function noteGuardEvents(events) {
  var seen = S.guardSeen;
  S.guardSeen = {};
  events.forEach(function (e) { S.guardSeen[e.key] = true; });
  if (seen === null) return;
  events.forEach(function (e) {
    if (seen[e.key]) return;
    var head = e.rule + ' ' + e.action
      + (e.trade ? ' ' + e.trade : '')
      + (e.slave ? ' on ' + e.slave : '');
    var tone = actionTone(e.action) === 'red' ? 'bad' : '';
    // These carry the reason a trade did not go through; give them time to be read.
    toast(head + (e.detail ? ' — ' + e.detail : ''), tone, 9000);
  });
}

function act(promise) {
  promise.then(function (r) {
    toast(r.message, r.ok ? 'ok' : 'bad');
    say(r.ok ? '' : r.message, r.ok ? '' : 'bad');
    poll();
  });
}

function parseExpiry(text) {
  // Anything a person would type: "45" (minutes), "45m", "2h", "1d", "eod"; blank = GTC.
  // null means it will not read, which the ticket surfaces before placing.
  text = (text || '').trim().toLowerCase();
  if (!text || text === 'gtc' || text.indexOf('until') === 0) return 0;
  if (text === 'eod' || text.indexOf('end') === 0) {
    var now = new Date();
    var end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0);
    return Math.max(1, Math.round((end - now) / 60000));
  }
  var m = text.match(/^(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)?$/);
  if (!m) return null;
  var v = parseFloat(m[1]);
  if (!isFinite(v) || v <= 0) return null;
  var unit = (m[2] || 'm').charAt(0);
  if (unit === 'h') v *= 60;
  else if (unit === 'd') v *= 1440;
  return v;
}

function seedEntry() {
  // A pending ticket starts with its entry line on the chart, ready to drag: a limit on
  // the pullback side of the market, a stop on the breakout side once the stop has said
  // which way the trade points, the middle of the spread until then. Only an empty,
  // unfocused field is seeded — a number the person typed or dragged is theirs.
  if (S.mode !== 'new' || S.type === 'market') return;
  var box = $('f-entry');
  if (box.value.trim() || document.activeElement === box) return;
  var q = S.live && S.live.quote;
  if (!q) return;
  var span = 0;
  if (S.candles.length > 1) {
    var hi = -Infinity, lo = Infinity;
    S.candles.forEach(function (c) { hi = Math.max(hi, c.high); lo = Math.min(lo, c.low); });
    span = hi - lo;
  }
  if (!span) span = q.ask * 0.004;
  var stop = num($('f-stop').value);
  var side = isFinite(stop) && stop > 0 ? (stop < q.bid ? 'buy' : stop > q.ask ? 'sell' : null) : null;
  var price;
  if (side === 'buy') price = S.type === 'limit' ? q.bid - 0.15 * span : q.ask + 0.15 * span;
  else if (side === 'sell') price = S.type === 'limit' ? q.ask + 0.15 * span : q.bid - 0.15 * span;
  else price = (q.bid + q.ask) / 2;
  box.value = fmt(price);
  renderTicket();
}

function placeOrder() {
  var t = renderTicket();
  if (!t || t.problem || S.placing) return;
  S.placing = true;
  api().place({
    symbol: S.symbol,
    side: t.side,
    kind: S.type,
    entry: t.isPending ? num($('f-entry').value) : 0,
    sl: num($('f-stop').value),
    tp: t.tp,
    expiryMinutes: t.isPending ? (parseExpiry($('f-expiry').value) || 0) : 0
  }).then(function (r) {
    S.placing = false;
    if (r.ok) {
      S.placedUntil = Date.now() + 1500;
      setTimeout(renderTicket, 1600);
      toast(r.message, 'ok');
      say('', '');
    } else {
      toast(r.message, 'bad');
      say(r.message, 'bad');
    }
    poll();
  }, function () {
    S.placing = false;
    toast('The engine did not answer; try again.', 'bad');
    say('The engine did not answer; try again.', 'bad');
  });
  renderTicket();
}

var polling = false;
function poll() {
  if (polling || !window.pywebview) return;
  polling = true;
  api().state(S.symbol, S.tf).then(function (live) {
    polling = false;
    S.live = live;
    noteGuardEvents(live.guardEvents || []);
    renderShell();
    renderBook();
    if (S.mode === 'modify' && S.modRow) {
      // The trade may have closed, filled or been partially closed under us.
      var held = null;
      (live.book || []).forEach(function (r) { if (r.ticket === S.modRow.ticket) held = r; });
      if (held === null) {
        toast('#' + S.modRow.ticket + ' is no longer in the book.', 'bad');
        exitModify();
      } else {
        S.modRow = held;
      }
    }
    seedEntry();
    renderTicket();
    if (live.quote) nudgeLastBar(live.quote.bid);
  }, function () { polling = false; });
}

var ratesBusy = false;
function symbolChanged() {
  // A new symbol invalidates everything priced in the old one: the typed levels, the
  // bars, and the lines drawn from both. Left standing, the old stop draws thousands
  // away from the new price and the old candles pin the scale to the wrong range.
  ['f-stop', 'f-entry', 'f-tp'].forEach(function (id) { $(id).value = ''; });
  S.candlesKey = '';
  showCandles([]);
}

function pollRates() {
  if (ratesBusy || !window.pywebview) return;
  if (!S.symbol) { showCandles([]); setMsg('type a symbol to chart it'); return; }
  ratesBusy = true;
  api().rates(S.symbol, S.tf).then(function (r) {
    ratesBusy = false;
    if (r.error) { showCandles([]); setMsg(r.error); return; }
    if (r.waiting || !r.candles.length) {
      var feed = S.live && S.live.feed;
      setMsg(feed ? 'waiting for ' + feed + ' to send bars…' : 'no feed: connect an MT5 slave to chart prices');
      if (!r.candles.length) return;
    }
    setMsg('');
    showCandles(r.candles);
  }, function () { ratesBusy = false; });
}

function setMsg(text) { $('chart-msg').textContent = text; }

// --- the terminals page --------------------------------------------------------------------
// Who is master, who copies, what was detected: the TUI Map's job, as three drop zones.
// Dragging a card between zones is the assignment; the engine still rules on every move,
// and a refused drop bounces back with the reason in a toast.
var T = {
  live: null,       // last terminals_state() answer
  drawn: '',        // what the zones were last built from
  drag: null,       // { card, ghost, el } while a card is held
  dragPending: false, // pointer down on a card, drag not yet begun: rebuilds must wait
  rescanning: false
};
var modalOnConfirm = null;

var PAGES = ['trading', 'terminals', 'guards', 'news', 'history'];

function showPage(name) {
  if (PAGES.indexOf(name) < 0) name = 'trading';
  S.page = name;
  try { localStorage.setItem('dt-page', name); } catch (e) { /* fine */ }
  PAGES.forEach(function (p) { $('page-' + p).hidden = p !== name; });
  document.querySelectorAll('#sidebar .nav-item[data-page]').forEach(function (a) {
    var active = a.dataset.page === name;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (name === 'terminals') pollTerminals();
  if (name === 'guards') pollGuards();
  if (name === 'news') pollNews();
  if (name === 'history') pollHistory();
}

var terminalsBusy = false;
function pollTerminals() {
  if (terminalsBusy || !window.pywebview || S.page !== 'terminals') return;
  terminalsBusy = true;
  api().terminals_state().then(function (live) {
    terminalsBusy = false;
    T.live = live;
    renderTerminals();
  }, function () { terminalsBusy = false; });
}

function tact(promise) {
  // A terminals action: toast the outcome and redraw from fresh state either way.
  promise.then(function (r) {
    toast(r.message, r.ok ? 'ok' : 'bad');
    T.drawn = '';
    pollTerminals();
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

function money(v) {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  var parts = Math.abs(v).toFixed(2).split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  return (v < 0 ? '−' : '') + parts.join('.');
}

function renderTerminals() {
  var live = T.live;
  // From pointer-down to drop the source card must stay the element the pointer started
  // on — a rebuild in that window detaches it, and a detached card measures 0 wide, so
  // the ghost cloned from it collapsed into a scrambled strip. The rebuild waits; the
  // next poll after the drop catches everything up.
  if (!live || T.drag || T.dragPending) return;
  var key = JSON.stringify(live);
  if (key === T.drawn) return;
  T.drawn = key;

  var mc = $('master-cards');
  mc.textContent = '';
  if (live.virtual) mc.appendChild(virtualCard(live));
  else if (live.master) mc.appendChild(terminalCard(live.master, live));
  $('master-note').textContent = live.virtual
    ? 'This profile trades directly: the order ticket is the master, so no terminal can take the role.'
    : (live.master
      ? 'Only one terminal can be master. Dropping a second one here bounces back with the reason.'
      : 'none yet — drag a detected terminal here to make it the master');

  var sc = $('slave-cards');
  sc.textContent = '';
  live.slaves.forEach(function (c) { sc.appendChild(terminalCard(c, live)); });
  $('slaves-note').hidden = live.slaves.length > 0;

  var dc = $('detected-cards');
  dc.textContent = '';
  live.detected.forEach(function (c) { dc.appendChild(terminalCard(c, live)); });
  $('detected-note').hidden = live.detected.length > 0;

  // A rebuild replaced the card the open popover was anchored to; hand it the new
  // element and the fresh numbers, so it stays up and current under the pointer.
  if (P.tid) {
    var pc = findLiveCard(P.tid);
    var pe = document.querySelector('.tcard[data-tid="' + P.tid + '"]');
    if (pc && pe) showPop(pe, pc);
    else hidePop();
  }
}

function pill(text, tone) {
  var p = document.createElement('span');
  p.className = 'pill ' + (tone || 'muted');
  p.textContent = text;
  return p;
}

function stateWord(c, live) {
  // The card's top-right word, with the TUI card's exact precedence: a link that is
  // down outranks the demo guard, which outranks a hold, which outranks a pause.
  // A claim another profile holds would arrive as a whole sentence in statusText —
  // too long for this corner, so the word stays short and the info line explains.
  if (c.claimRefused) return { text: 'in use elsewhere', cls: 'yellow' };
  var down = c.tier === 'serious' || c.tier === 'critical' || c.tier === 'muted'
    || (c.tier === 'warning' && !c.holdReason);
  if (c.role === 'master') {
    if (down) return { text: c.statusText, cls: c.tier === 'muted' ? 'muted' : 'red' };
    return { text: 'connected', cls: 'blue' };
  }
  if (down) {
    var cls = c.tier === 'warning' ? 'yellow' : c.tier === 'muted' ? 'muted' : 'red';
    return { text: c.statusText, cls: cls };
  }
  if (c.demoBlocked) return { text: 'HELD BACK', cls: 'red' };
  if (c.holdReason) return { text: 'HELD', cls: 'red' };
  if (c.paused) return { text: 'PAUSED', cls: 'blue' };
  if (!live.copying) return { text: 'not copying', cls: 'muted' };
  if (live.dryRun) return { text: 'dry run', cls: 'yellow' };
  return { text: 'copying', cls: 'green' };
}

function cardHead(c, dotTone) {
  var head = document.createElement('div');
  head.className = 'tcard-head';
  var dot = document.createElement('span');
  dot.className = 'dot ' + (dotTone || '');
  dot.setAttribute('aria-hidden', 'true');
  head.appendChild(dot);
  var name = document.createElement('span');
  name.className = 'tcard-name';
  name.textContent = c.name;
  head.appendChild(name);
  var space = document.createElement('span');
  space.className = 'tcard-space';
  head.appendChild(space);
  return head;
}

function cardLine(cls, text) {
  var s = document.createElement('span');
  s.className = cls;
  s.textContent = text;
  return s;
}

function acctText(c) {
  return c.login
    ? c.login + (c.server ? ' · ' + c.server : '')
    : (c.isCtrader ? 'cTrader · Spotware' : 'not logged in');
}

function acctLine(c) {
  return cardLine('tcard-acct mono', acctText(c));
}

function openWord(c) {
  return c.openTrades === null ? '— open' : c.openTrades + ' open';
}

function marginRow(c) {
  // The TUI card's last line: margin ███░░░ 30%, with the guard badge at the right so
  // a card is no taller for having guards.
  var row = document.createElement('div');
  row.className = 'tcard-margin mono';
  row.appendChild(cardLine('', 'margin'));
  var bar = document.createElement('span');
  bar.className = 'mbar';
  var fill = document.createElement('span');
  var tier = { good: 'blue', warning: 'yellow', serious: 'red', critical: 'red' }[c.marginTier] || 'muted';
  fill.className = 'mbar-fill ' + tier;
  fill.style.width = c.marginUsed === null || c.marginUsed === undefined
    ? '0%' : Math.round(c.marginUsed * 100) + '%';
  bar.appendChild(fill);
  row.appendChild(bar);
  row.appendChild(cardLine('', c.marginUsed === null || c.marginUsed === undefined
    ? '—' : Math.round(c.marginUsed * 100) + '%'));
  var space = document.createElement('span');
  space.className = 'tcard-space';
  row.appendChild(space);
  // Only a guard with something to say earns this corner (the TUI's rule): GUARD 82%,
  // GUARD BREACHED, GUARD: NO NEW TRADES. The quiet rule count sits on the info line,
  // where a tight card does not squeeze it into "guards 2…".
  if (c.guard && c.guard.text) {
    var badge = cardLine('tcard-guard ' + (c.guard.tone || ''), c.guard.text);
    badge.title = c.guard.text;
    row.appendChild(badge);
  }
  return row;
}

function terminalCard(c, live) {
  var el = document.createElement('article');
  el.className = 'tcard';
  el.dataset.tid = c.tid;
  el.setAttribute('aria-label', c.name);
  var dotTone = { good: 'green', warning: 'yellow', serious: 'red', critical: 'red' }[c.tier] || '';
  var head = cardHead(c, dotTone);

  if (c.role === 'unassigned') {
    // A detected terminal: what it is and whether it could connect, nothing more.
    if (c.isCtrader) head.appendChild(pill('SLAVE ONLY', 'muted'));
    else if (!c.hasExe) head.appendChild(pill('NOT INSTALLED', 'muted'));
    else if (!c.login) head.appendChild(pill('NO ACCOUNT', 'muted'));
    el.appendChild(head);
    el.appendChild(acctLine(c));
    var said;
    if (!c.hasExe && !c.isCtrader) said = 'found in config · cannot be launched from here';
    else {
      said = c.login ? 'logged in' : 'not logged in';
      if (c.isCtrader) said += ' · can copy, cannot feed';
    }
    el.appendChild(cardLine('tcard-info', said));
    wireCard(el, c);
    return el;
  }

  // An assigned card reads like the TUI's: state word top-right, the balance big, the
  // floating line under it, then risk · open, then the margin bar. Every row has its
  // fixed place, so cards line up with each other whatever their content.
  var word = stateWord(c, live);
  if (word.cls !== 'muted') el.classList.add('edge-' + word.cls);
  head.appendChild(cardLine('tcard-word ' + word.cls, word.text));
  el.appendChild(head);
  el.appendChild(acctLine(c));
  el.appendChild(cardLine('tcard-big mono', c.balance === null ? '—' : money(c.balance)));

  var pl = c.floatingPl;
  var plSaid = pl === null || pl === undefined
    ? '— floating'
    : (pl >= 0 ? '+' : '') + money(pl) + ' floating';
  var plCls = pl > 0 ? 'green' : pl < 0 ? 'red' : 'muted';
  el.appendChild(cardLine('tcard-float mono ' + plCls, plSaid));

  var info, infoCls = 'tcard-info', infoTitle = '';
  if (c.claimRefused) {
    // The whole story in one line: whose it is, and that no rescan is needed.
    info = c.claimRefused + ' — frees itself when that app closes';
    infoTitle = info;
    infoCls += ' yellow';
  } else if (c.role === 'slave') {
    if (c.demoBlocked) {
      info = 'not a demo — right-click to allow live trading';
      infoCls += ' red';
    } else if (c.holdReason) {
      info = c.holdReason;
      infoCls += ' yellow';
    } else {
      // The rule count lives here, where there is room — the margin line's corner is
      // kept for a guard that is actually saying something.
      var ruled = c.guard && c.guard.rules
        ? ' · ' + c.guard.rules + ' guard rule' + (c.guard.rules === 1 ? '' : 's') : '';
      info = (c.risk ? c.risk + ' · ' : '') + openWord(c) + ruled;
    }
  } else {
    info = openWord(c) + ' · feeds ' + live.slaves.length
      + ' slave' + (live.slaves.length === 1 ? '' : 's');
  }
  var infoLine = cardLine(infoCls, info);
  if (infoTitle) infoLine.title = infoTitle;
  el.appendChild(infoLine);
  el.appendChild(marginRow(c));

  wireCard(el, c);
  return el;
}

function virtualCard(live) {
  var el = document.createElement('article');
  el.className = 'tcard tcard-still edge-blue';
  var v = live.virtual;
  var head = cardHead({ name: 'Direct trading' }, v.feed ? 'green' : 'red');
  head.appendChild(cardLine('tcard-word ' + (v.feed ? 'blue' : 'red'),
    v.feed ? 'feed ' + v.feed : 'no feed'));
  el.appendChild(head);
  el.appendChild(cardLine('tcard-acct', 'you are the master'));
  el.appendChild(cardLine('tcard-book',
    v.positions ? v.positions + ' open · ' + v.orders + ' resting' : 'nothing open'));
  var feeds = 'feeds ' + live.slaves.length + ' slave' + (live.slaves.length === 1 ? '' : 's');
  el.appendChild(cardLine('tcard-info',
    feeds + (v.feed ? '' : ' · connect an MT5 slave for prices')));

  var go = document.createElement('button');
  go.type = 'button';
  go.className = 'row-btn tcard-btn';
  go.textContent = 'Order ticket';
  go.addEventListener('click', function () { showPage('trading'); });
  el.appendChild(go);
  return el;
}

// --- the card's hover popover ---------------------------------------------------------------
// The cards keep every row to one clipped line so they line up; the popover is the full
// readout — the whole server name, equity, the exact margin, the guard's word, and any
// hold or claim sentence unabridged. It is display only (pointer-events: none in the
// CSS), so it can never swallow a hover or a drag, and it re-anchors itself across the
// rebuilds the poll causes, so it does not flicker while held open.
var P = { tid: null, el: null, show: 0, hide: 0 };

function findLiveCard(tid) {
  var live = T.live;
  if (!live) return null;
  if (live.master && live.master.tid === tid) return live.master;
  var pools = [live.slaves, live.detected];
  for (var p = 0; p < pools.length; p++)
    for (var i = 0; i < pools[p].length; i++)
      if (pools[p][i].tid === tid) return pools[p][i];
  return null;
}

function popDiv(cls, text) {
  var d = document.createElement('div');
  d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}

function popHr() {
  var h = document.createElement('hr');
  h.className = 'pop-hr';
  return h;
}

function popGauge(c) {
  var wrap = document.createElement('span');
  wrap.className = 'pop-gauge';
  var bar = document.createElement('span');
  bar.className = 'mbar';
  var fill = document.createElement('span');
  var tier = { good: 'blue', warning: 'yellow', serious: 'red', critical: 'red' }[c.marginTier] || 'muted';
  fill.className = 'mbar-fill ' + tier;
  fill.style.width = c.marginUsed === null || c.marginUsed === undefined
    ? '0%' : Math.min(100, Math.round(c.marginUsed * 100)) + '%';
  bar.appendChild(fill);
  wrap.appendChild(bar);
  wrap.appendChild(document.createTextNode(c.marginUsed === null || c.marginUsed === undefined
    ? '—' : (c.marginUsed * 100).toFixed(1).replace(/\.0$/, '') + '%'));
  return wrap;
}

function popContent(c, live) {
  var frag = document.createDocumentFragment();

  var head = popDiv('pop-head');
  var dot = document.createElement('span');
  dot.className = 'dot ' + ({ good: 'green', warning: 'yellow', serious: 'red', critical: 'red' }[c.tier] || '');
  dot.setAttribute('aria-hidden', 'true');
  head.appendChild(dot);
  var name = document.createElement('span');
  name.className = 'pop-name';
  name.textContent = c.name;
  head.appendChild(name);
  var word = null;
  if (c.role === 'unassigned') {
    var tag = c.isCtrader ? 'SLAVE ONLY' : !c.hasExe ? 'NOT INSTALLED' : !c.login ? 'NO ACCOUNT' : '';
    if (tag) head.appendChild(cardLine('pop-word muted', tag));
  } else {
    word = stateWord(c, live);
    head.appendChild(cardLine('pop-word ' + word.cls, word.text));
  }
  frag.appendChild(head);
  frag.appendChild(popDiv('pop-acct', acctText(c)));

  if (c.role === 'unassigned') {
    // The card's info line, with room to finish its sentence.
    var said;
    if (!c.hasExe && !c.isCtrader) said = 'found in config · cannot be launched from here';
    else {
      said = c.login ? 'logged in' : 'not logged in';
      if (c.isCtrader) said += ' · can copy, cannot feed';
    }
    frag.appendChild(popDiv('pop-status', said));
  } else {
    if (c.statusText && c.statusText !== word.text) frag.appendChild(popDiv('pop-status', c.statusText));

    frag.appendChild(popHr());
    var dl = document.createElement('dl');
    dl.className = 'pop-grid';
    function row(label, node) {
      var dt = document.createElement('dt');
      dt.textContent = label;
      dl.appendChild(dt);
      var dd = document.createElement('dd');
      dd.appendChild(node);
      dl.appendChild(dd);
    }
    var bal = document.createElement('span');
    bal.textContent = c.balance === null ? '—' : money(c.balance);
    if (c.currency) {
      var cur = document.createElement('span');
      cur.className = 'pop-cur';
      cur.textContent = c.currency;
      bal.appendChild(cur);
    }
    row('balance', bal);
    row('equity', document.createTextNode(
      c.equity === null || c.equity === undefined ? '—' : money(c.equity)));
    var pl = c.floatingPl;
    row('floating', cardLine(pl > 0 ? 'green' : pl < 0 ? 'red' : 'muted',
      pl === null || pl === undefined ? '—' : (pl >= 0 ? '+' : '') + money(pl)));
    row('margin', popGauge(c));
    frag.appendChild(dl);

    frag.appendChild(popHr());
    var copy = popDiv('pop-copy');
    if (c.role === 'slave') {
      var risk = document.createElement('b');
      risk.textContent = c.risk || 'risk —';
      copy.appendChild(risk);
      var rules = c.guard ? c.guard.rules : 0;
      copy.appendChild(document.createTextNode(' · ' + openWord(c) + ' · '
        + rules + ' guard rule' + (rules === 1 ? '' : 's')));
      if (c.guard && c.guard.text) {
        copy.appendChild(document.createTextNode(' — '));
        copy.appendChild(cardLine('mono ' + (c.guard.tone || ''), c.guard.text));
      }
    } else {
      copy.textContent = openWord(c) + ' · feeds ' + live.slaves.length
        + ' slave' + (live.slaves.length === 1 ? '' : 's');
    }
    frag.appendChild(copy);
  }

  // Anything wrong gets its whole sentence, stacked in the card's own precedence.
  if (c.claimRefused) {
    frag.appendChild(popDiv('pop-alert', c.claimRefused + ' — frees itself when that app closes'));
  }
  if (c.demoBlocked) {
    frag.appendChild(popDiv('pop-alert red', 'not a demo account — right-click the card to allow live trading'));
  }
  if (c.holdReason) frag.appendChild(popDiv('pop-alert', c.holdReason));

  var facts = [];
  if (c.role === 'slave') {
    facts.push(c.allowLive ? 'live trading allowed' : 'demo-only (live trading not allowed)');
    if (c.paused) facts.push('paused');
    if (live.dryRun) facts.push('dry run');
    facts.push(live.copying ? 'copying on' : 'copying halted');
  } else if (c.role === 'master') {
    facts.push(live.copying ? 'copying on' : 'copying halted');
  } else if ((c.hasExe || c.isCtrader) && c.login) {
    facts.push('ready to connect — drag it to a zone above');
  }
  if (facts.length) frag.appendChild(popDiv('pop-foot', facts.join(' · ')));
  return frag;
}

function placePop(el) {
  // Prefer the card's right; flip left, then drop below, and clamp to the window.
  var pop = $('tcard-pop');
  var r = el.getBoundingClientRect();
  var pw = pop.offsetWidth, ph = pop.offsetHeight, gap = 10, pad = 12;
  var x = r.right + gap, y = r.top;
  if (x + pw > window.innerWidth - pad) x = r.left - gap - pw;
  if (x < pad) {
    x = Math.min(Math.max(pad, r.left), window.innerWidth - pw - pad);
    y = r.bottom + gap;
  }
  if (y + ph > window.innerHeight - pad) y = Math.max(pad, window.innerHeight - ph - pad);
  pop.style.left = x + 'px';
  pop.style.top = y + 'px';
}

function showPop(el, c) {
  var live = T.live;
  if (!live || T.drag || T.dragPending || !$('ctx-menu').hidden) return;
  var pop = $('tcard-pop');
  pop.textContent = '';
  pop.appendChild(popContent(c, live));
  pop.hidden = false;
  P.tid = c.tid;
  P.el = el;
  placePop(el);
}

function hidePop() {
  clearTimeout(P.show);
  clearTimeout(P.hide);
  P.tid = null;
  P.el = null;
  $('tcard-pop').hidden = true;
}

function wirePop(el, c) {
  el.addEventListener('mouseenter', function () {
    clearTimeout(P.show);
    clearTimeout(P.hide);
    // Already open on another card: follow the pointer at once, no second wait.
    if (P.tid) showPop(el, c);
    else P.show = setTimeout(function () { showPop(el, c); }, 250);
  });
  el.addEventListener('mouseleave', function () {
    clearTimeout(P.show);
    P.hide = setTimeout(hidePop, 120);
  });
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') hidePop();
});
window.addEventListener('scroll', function () {
  if (P.tid) hidePop();
}, true);

// --- dragging a card between zones ---------------------------------------------------------
function wireCard(el, c) {
  wirePop(el, c);
  el.addEventListener('contextmenu', function (ev) {
    ev.preventDefault();
    openMenu(c, ev.clientX, ev.clientY);
  });
  // The browser must never start its own drag of the card's text — a native drag eats
  // the pointer events and strands our ghost mid-screen.
  el.addEventListener('dragstart', function (ev) { ev.preventDefault(); });
  el.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0 || ev.target.closest('button')) return;
    hidePop();
    var startX = ev.clientX, startY = ev.clientY;
    var started = false;
    T.dragPending = true;
    function letGo() {
      T.dragPending = false;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
    }
    function move(e) {
      if (!started) {
        if (Math.abs(e.clientX - startX) < 6 && Math.abs(e.clientY - startY) < 6) return;
        started = true;
        beginDrag(el, c, e);
      }
      moveDrag(e);
    }
    function up(e) {
      letGo();
      if (started) endDrag(e);
    }
    function cancel() {
      letGo();
      if (started) cleanupDrag();
    }
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
    ev.preventDefault();
  });
}

function beginDrag(el, c, e) {
  closeMenu();
  hidePop();
  if (!el.isConnected) {
    // A rebuild slipped in before the hold took effect: the grabbed element is out of
    // the page. Its replacement carries the same terminal, so the drag follows it.
    el = document.querySelector('.tcard[data-tid="' + c.tid + '"]') || el;
  }
  var width = el.getBoundingClientRect().width;
  var ghost = el.cloneNode(true);
  ghost.className = 'tcard tcard-ghost';
  if (width) ghost.style.width = width + 'px';
  document.body.appendChild(ghost);
  el.classList.add('drag-src');
  document.body.classList.add('dragging-terminals');
  T.drag = { card: c, ghost: ghost, el: el };
  moveDrag(e);
}

function zoneUnder(e) {
  var at = document.elementFromPoint(e.clientX, e.clientY);
  return at ? at.closest('.zone') : null;
}

function moveDrag(e) {
  if (!T.drag) return;
  T.drag.ghost.style.left = (e.clientX + 10) + 'px';
  T.drag.ghost.style.top = (e.clientY - 20) + 'px';
  var over = zoneUnder(e);
  document.querySelectorAll('.zone').forEach(function (z) {
    z.classList.toggle('drop-hot', z === over);
  });
}

function cleanupDrag() {
  var drag = T.drag;
  if (!drag) return null;
  T.drag = null;
  drag.ghost.remove();
  drag.el.classList.remove('drag-src');
  document.body.classList.remove('dragging-terminals');
  document.querySelectorAll('.zone').forEach(function (z) { z.classList.remove('drop-hot'); });
  return drag;
}

function endDrag(e) {
  var drag = cleanupDrag();
  if (!drag) return;
  var zone = zoneUnder(e);
  if (!zone) return;
  var c = drag.card;
  var want = zone.dataset.zone;
  if (want === 'master' && c.role !== 'master') tact(api().assign_master(c.tid));
  else if (want === 'slaves' && c.role !== 'slave') tact(api().assign_slave(c.tid));
  else if (want === 'detected' && c.role !== 'unassigned') unassignFlow(c);
}

// --- the card's menu and the asking dialogs -------------------------------------------------
function closeMenu() {
  $('ctx-menu').hidden = true;
}

function openMenu(c, x, y) {
  var live = T.live;
  if (!live) return;
  hidePop();
  var menu = $('ctx-menu');
  menu.textContent = '';
  var head = document.createElement('span');
  head.className = 'menu-head';
  head.textContent = c.name + (c.login ? ' · ' + c.login : '');
  menu.appendChild(head);

  var items = menuItems(c, live);
  items.forEach(function (item) {
    if (item.sep) {
      var sep = document.createElement('div');
      sep.className = 'menu-sep';
      sep.setAttribute('aria-hidden', 'true');
      menu.appendChild(sep);
      return;
    }
    if (!item.fn) {
      var note = document.createElement('span');
      note.className = 'menu-note';
      note.textContent = item.label;
      menu.appendChild(note);
      return;
    }
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('role', 'menuitem');
    btn.className = item.cls || '';
    btn.textContent = item.label;
    btn.addEventListener('click', function () {
      closeMenu();
      item.fn();
    });
    menu.appendChild(btn);
  });

  menu.hidden = false;
  // Clamp to the window once its size is real.
  var w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
}

function menuItems(c, live) {
  var items = [];
  function does(label, fn, cls) { items.push({ label: label, fn: fn, cls: cls }); }
  function says(label) { items.push({ label: label }); }

  if (c.role === 'unassigned') {
    if (c.isCtrader) {
      if (live.hasMaster) does('Make it a slave', function () { tact(api().assign_slave(c.tid)); });
      else says('a cTrader account can only be a slave: make an MT5 master first');
    } else if (!c.hasExe) {
      says('not installed here, so it cannot be launched');
    } else if (live.hasMaster) {
      does('Make it a slave', function () { tact(api().assign_slave(c.tid)); });
    } else {
      does('Make it the master', function () { tact(api().assign_master(c.tid)); });
    }
    says('account login and per-terminal settings live in the TUI for now');
    return items;
  }

  if (c.role === 'slave') {
    if (c.orphans) {
      does('Acknowledge orphans: sign off its untracked positions', function () {
        tact(api().acknowledge_orphans(c.tid));
      });
    }
    if (c.paused) does('Resume copying', function () { tact(api().set_pause(c.tid, false)); });
    else does('Pause: take no new trades', function () { tact(api().set_pause(c.tid, true)); });
  }
  if (c.allowLive) {
    does('Live trading is allowed — turn it off', function () { liveOverrideFlow(c, false); });
  } else if (c.role === 'slave' && c.demoBlocked) {
    does('Allow live trading: not a demo account…', function () { liveOverrideFlow(c, true); });
  }
  if (c.statusText !== 'connected') {
    does('Reconnect to the terminal', function () { tact(api().reconnect(c.tid)); });
    does('Restart its worker process', function () { tact(api().restart_worker(c.tid)); });
  }
  items.push({ sep: true });
  does(
    c.role === 'slave' ? 'Unassign: stop copying to it' : 'Unassign the master',
    function () { unassignFlow(c); },
    'danger'
  );
  return items;
}

function showModal(opts) {
  $('modal-title').textContent = opts.title;
  var body = $('modal-body');
  body.textContent = '';
  if (opts.body) body.appendChild(opts.body);
  var ok = $('modal-confirm');
  ok.hidden = !opts.confirm;
  ok.textContent = opts.confirm || '';
  ok.className = opts.danger ? 'danger' : '';
  modalOnConfirm = opts.onConfirm || null;
  $('modal-back').hidden = false;
  if (opts.confirm) ok.focus();
}

function closeModal() {
  $('modal-back').hidden = true;
  modalOnConfirm = null;
}

function unassignFlow(c) {
  // The engine says what unassigning strands; stranding anything has to be agreed to
  // first, exactly as in the TUI. Nothing at stake commits straight away.
  api().unassign_stake(c.tid).then(function (r) {
    if (!r.ok) { toast(r.message, 'bad'); return; }
    var stake = r.stake || [];
    if (!stake.length) { tact(api().unassign(c.tid, null)); return; }

    var agreed = {};
    var total = 0;
    var body = document.createElement('div');
    var warn = document.createElement('p');
    warn.textContent = 'These copies stay open and stop being managed — nothing will close '
      + 'them with the master, move their stops, or mirror partials. They become yours to '
      + 'close by hand.';
    body.appendChild(warn);
    stake.forEach(function (s) {
      agreed[s.tid] = s.copies.map(function (cp) { return cp.masterTicket; });
      total += s.copies.length;
      var h = document.createElement('div');
      h.className = 'modal-sub';
      h.textContent = s.name;
      body.appendChild(h);
      s.copies.forEach(function (cp) {
        var line = document.createElement('div');
        line.className = 'modal-line mono';
        line.textContent = (cp.resting ? 'resting · ' : '') + cp.symbol
          + (cp.volume ? ' · ' + cp.volume + ' lots' : '')
          + (cp.slaveTicket ? ' · #' + cp.slaveTicket : '');
        body.appendChild(line);
      });
    });
    showModal({
      title: 'Unassign ' + c.name + '?',
      body: body,
      confirm: 'Unassign — orphan ' + total + (total === 1 ? ' copy' : ' copies'),
      danger: true,
      onConfirm: function () { tact(api().unassign(c.tid, agreed)); }
    });
  });
}

function liveOverrideFlow(c, allow) {
  if (!allow) {
    // Putting the guard back on risks nothing; it needs no ceremony.
    tact(api().live_override(c.tid, false));
    return;
  }
  var body = document.createElement('div');
  var p1 = document.createElement('p');
  p1.textContent = 'This account is not a demo. With the override on, real orders with real '
    + 'money are sent to it whenever copying is live.';
  var p2 = document.createElement('p');
  p2.textContent = 'The override stays until you turn it off from this same menu.';
  body.appendChild(p1);
  body.appendChild(p2);
  showModal({
    title: 'Allow live trading on ' + c.name + '?',
    body: body,
    confirm: 'Allow live trading',
    danger: true,
    onConfirm: function () { tact(api().live_override(c.tid, true)); }
  });
}

// --- the guards page -----------------------------------------------------------------------
// The TUI guards page's report, drawn as the design's table: one account at a time, its
// rules row by row, and everything the rules did. What a rule means is the engine's; this
// page only draws the report and edits guards.json through the bridge.
var G = {
  live: null,
  drawn: '',
  tid: '',          // the chip picked; '' lets the bridge pick the first slave
  selected: null,   // the selected rule's id
  form: null        // { tid, type, ruleId, fields } while the rule form is open
};

var guardsBusy = false;
function pollGuards() {
  if (guardsBusy || !window.pywebview || S.page !== 'guards') return;
  guardsBusy = true;
  api().guards_state(G.tid).then(function (live) {
    guardsBusy = false;
    G.live = live;
    renderGuards();
  }, function () { guardsBusy = false; });
}

function gact(promise) {
  promise.then(function (r) {
    toast(r.message, r.ok ? 'ok' : 'bad');
    G.drawn = '';
    pollGuards();
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

function renderGuards() {
  var live = G.live;
  if (!live) return;
  var key = JSON.stringify(live) + '|' + G.selected;
  if (key === G.drawn) return;
  G.drawn = key;

  var none = !live.accounts.length;
  $('guard-none').hidden = !none;
  $('guard-panel').hidden = none;
  $('guard-actions-panel').hidden = none;
  $('guard-pick').hidden = none;
  if (none) return;
  G.tid = live.selected;
  renderGuardPickBtn(live);
  // The open menu is the person's: rebuilding it under their search would fight them.
  // It is rebuilt fresh each time it opens.

  var initial = $('g-initial');
  if (document.activeElement !== initial) {
    initial.value = live.initialBalance === null || live.initialBalance === undefined
      ? '' : String(live.initialBalance);
  }
  initial.disabled = !live.hasAccount;
  $('g-all').checked = !!live.enabled;
  var notes = [];
  if (!live.hasAccount) {
    notes.push("this terminal's account is not known yet: connect it once, then its rules can be set");
  } else {
    if (!live.enabled) notes.push('ALL OFF — every rule is ignored until All on is ticked again');
    if (!live.isSlave) {
      notes.push('not copying as a slave yet — the rules stay with the account and act from '
        + 'its first copied trade');
    }
  }
  $('guard-note').textContent = notes.join(' · ');

  // The rules, each row selectable; the footer buttons act on the selection.
  if (G.selected && !live.rules.some(function (r) { return r.id === G.selected; })) G.selected = null;
  var rows = $('guard-rows');
  rows.textContent = '';
  live.rules.forEach(function (r) { rows.appendChild(guardRow(r)); });
  $('guard-empty').hidden = live.rules.length > 0;
  var picked = live.rules.filter(function (r) { return r.id === G.selected; })[0] || null;
  $('g-change').disabled = !picked;
  $('g-toggle').disabled = !picked;
  $('g-remove').disabled = !picked;
  $('g-toggle').textContent = picked && !picked.enabled ? 'Rule on' : picked ? 'Rule off' : 'Rule on/off';
  $('g-clear').hidden = !(picked && picked.latched);
  $('g-clear').disabled = !(picked && picked.latched);

  var acts = $('guard-actions');
  acts.textContent = '';
  live.actions.forEach(function (a) {
    var line = document.createElement('div');
    line.className = 'gact-line';
    line.appendChild(cardLine('gact-when', a.when));
    line.appendChild(cardLine('gact-rule', a.rule));
    if (a.trade) line.appendChild(cardLine('gact-trade', a.trade));
    line.appendChild(cardLine('gact-act ' + actionTone(a.action), a.action.toUpperCase()));
    if (a.detail) line.appendChild(cardLine('gact-detail', a.detail));
    acts.appendChild(line);
  });
  $('guard-actions-empty').hidden = live.actions.length > 0;
}

// --- the account picker: one dropdown, searchable, slaves apart from the rest --------------
function renderGuardPickBtn(live) {
  var sel = null;
  live.accounts.forEach(function (a) { if (a.tid === live.selected) sel = a; });
  var btn = $('guard-pick-btn');
  btn.textContent = '';
  if (sel) {
    btn.appendChild(cardLine('gp-name', sel.name));
    var sub = (sel.login ? String(sel.login) : '') + (sel.server ? ' · ' + sel.server : '');
    if (sub) btn.appendChild(cardLine('gp-sub mono muted', sub));
    if (sel.holding) btn.appendChild(cardLine('chip-flag red', 'HELD'));
    if (!sel.slave) btn.appendChild(cardLine('chip-flag quiet', 'not a slave'));
  } else {
    btn.appendChild(cardLine('gp-name muted', 'pick an account…'));
  }
  var chev = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chev.setAttribute('width', '14');
  chev.setAttribute('height', '14');
  chev.setAttribute('viewBox', '0 0 24 24');
  chev.setAttribute('aria-hidden', 'true');
  chev.innerHTML = '<path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>';
  chev.classList.add('gp-chev');
  btn.appendChild(chev);
}

function guardPickMatches(query) {
  var accounts = G.live ? G.live.accounts : [];
  var terms = query.toLowerCase().split(/\s+/).filter(function (t) { return t; });
  return accounts.filter(function (a) {
    var hay = (a.name + ' ' + (a.login || '') + ' ' + (a.server || '')).toLowerCase();
    return terms.every(function (t) { return hay.indexOf(t) >= 0; });
  });
}

function buildGuardPickList() {
  var list = $('guard-pick-list');
  list.textContent = '';
  var found = guardPickMatches($('guard-pick-search').value);
  [['SLAVES — guarded as they copy', true],
   ['NOT A SLAVE — rules wait for the first copied trade', false]].forEach(function (group) {
    var mine = found.filter(function (a) { return a.slave === group[1]; });
    if (!mine.length) return;
    list.appendChild(cardLine('pick-head', group[0]));
    mine.forEach(function (a) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'pick-item gp-item' + (a.tid === G.tid ? ' current' : '');
      b.setAttribute('role', 'option');
      var top = document.createElement('span');
      top.className = 'gp-item-top';
      top.appendChild(cardLine('pick-label', a.name));
      if (a.holding) top.appendChild(cardLine('chip-flag red', 'HELD'));
      else if (a.worst === 'near') top.appendChild(cardLine('chip-flag yellow', 'NEAR'));
      top.appendChild(cardLine('gp-item-rules muted',
        a.rules ? a.rules + ' rule' + (a.rules === 1 ? '' : 's') : 'no rules'));
      b.appendChild(top);
      var sub = (a.login ? String(a.login) : 'account not known yet')
        + (a.server ? ' · ' + a.server : '');
      b.appendChild(cardLine('pick-sub mono muted', sub));
      b.addEventListener('click', function () {
        closeDropdowns();
        if (a.tid === G.tid) return;
        G.tid = a.tid;
        G.selected = null;
        G.drawn = '';
        pollGuards();
      });
      list.appendChild(b);
    });
  });
  if (!list.childNodes.length) {
    list.appendChild(cardLine('muted gp-none', 'nothing matches that search'));
  }
}

function actionTone(action) {
  if (action === 'declined' || action === 'stopped new trades' || action === 'closed'
    || action === 'cancelled') return 'red';
  if (action === 'let new trades through') return 'blue';
  return 'yellow';
}

function guardRow(r) {
  var row = document.createElement('div');
  row.setAttribute('role', 'row');
  row.className = 'grule' + (r.id === G.selected ? ' selected' : '')
    + (r.latched ? ' latched' : '') + (r.enabled ? '' : ' off');

  var title = document.createElement('span');
  title.setAttribute('role', 'cell');
  title.className = 'grule-title';
  title.textContent = r.title;
  if (!r.enabled) title.appendChild(cardLine('cell-sub muted', 'off'));
  else if (r.latched) title.appendChild(cardLine('pill red', 'HELD'));
  row.appendChild(title);

  var limit = document.createElement('span');
  limit.setAttribute('role', 'cell');
  limit.className = 'mono grule-limit';
  limit.textContent = r.limitText || '—';
  if (r.basis) limit.appendChild(cardLine('cell-sub muted', r.basis));
  row.appendChild(limit);

  row.appendChild(guardUsedCell(r));

  var breach = document.createElement('span');
  breach.setAttribute('role', 'cell');
  breach.textContent = r.onBreach || '—';
  if (r.actAt !== null && r.actAt !== undefined) {
    breach.appendChild(cardLine('cell-sub muted', 'at ' + Math.round(r.actAt * 100) + '% of it'));
  }
  row.appendChild(breach);

  var last = document.createElement('span');
  last.setAttribute('role', 'cell');
  last.className = 'grule-last';
  if (r.last) {
    var head = document.createElement('span');
    head.className = 'cell-line';
    head.appendChild(cardLine('muted', r.last.when + (r.last.ticket ? ' ' + r.last.ticket : '')));
    head.appendChild(cardLine('gact-act ' + actionTone(r.last.action), r.last.action));
    last.appendChild(head);
    if (r.last.detail) last.appendChild(cardLine('cell-sub muted', r.last.detail));
    if (r.latched) {
      var clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'row-btn';
      clear.textContent = 'Clear';
      clear.addEventListener('click', function (ev) {
        ev.stopPropagation();
        clearLatchFlow(r);
      });
      last.appendChild(clear);
    }
  } else {
    last.appendChild(cardLine('muted', 'nothing yet'));
  }
  row.appendChild(last);

  row.addEventListener('click', function () {
    G.selected = r.id === G.selected ? null : r.id;
    G.drawn = '';
    renderGuards();
  });
  row.addEventListener('dblclick', function () { openRuleForm(r.type, r.id); });
  return row;
}

function guardUsedCell(r) {
  var cell = document.createElement('span');
  cell.setAttribute('role', 'cell');
  cell.className = 'grule-used';
  var u = r.used;
  if (u.kind === 'latched') {
    cell.appendChild(cardLine('gused-hold red', 'NO NEW TRADES'));
    cell.appendChild(cardLine('cell-sub red', u.note + ' · Clear releases it'));
    return cell;
  }
  if (u.kind === 'off') {
    cell.appendChild(cardLine('muted', 'off'));
    return cell;
  }
  if (u.kind === 'text') {
    cell.appendChild(cardLine(u.tone || 'muted', u.text || '—'));
    return cell;
  }
  var line = document.createElement('span');
  line.className = 'gused-row';
  var track = document.createElement('span');
  track.className = 'gbar';
  var fill = document.createElement('span');
  fill.className = 'gbar-fill ' + r.tone;
  fill.style.width = Math.min(100, Math.round(u.fraction * 100)) + '%';
  track.appendChild(fill);
  if (r.actAt !== null && r.actAt !== undefined) {
    var tick = document.createElement('span');
    tick.className = 'gbar-tick';
    tick.style.left = Math.min(100, Math.round(r.actAt * 100)) + '%';
    track.appendChild(tick);
  }
  line.appendChild(track);
  line.appendChild(cardLine('mono ' + (r.tone === 'blue' ? 'muted' : r.tone),
    Math.round(u.fraction * 100) + '%'));
  if (u.holdsBack) line.appendChild(cardLine('gused-hold red', 'NO NEW TRADES'));
  cell.appendChild(line);
  if (u.moneyLine) cell.appendChild(cardLine('cell-sub muted mono', u.moneyLine));
  return cell;
}

// --- guards: asking and editing -------------------------------------------------------------
function pickModal(title, options) {
  // A list to pick from: each option {label, sub, fn} is a row; picking closes the modal.
  var body = document.createElement('div');
  body.className = 'pick-list';
  options.forEach(function (o) {
    if (!o.fn) {
      body.appendChild(cardLine('pick-head', o.label));
      return;
    }
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'pick-item';
    b.appendChild(cardLine('pick-label', o.label));
    if (o.sub) b.appendChild(cardLine('pick-sub muted', o.sub));
    b.addEventListener('click', function () {
      closeModal();
      o.fn();
    });
    body.appendChild(b);
  });
  showModal({ title: title, body: body, confirm: '' });
}

function confirmModal(title, said, go, danger, fn) {
  var body = document.createElement('div');
  var p = document.createElement('p');
  p.textContent = said;
  body.appendChild(p);
  showModal({ title: title, body: body, confirm: go, danger: danger, onConfirm: fn });
}

function addRuleFlow() {
  var live = G.live;
  if (!live || !live.hasAccount) {
    toast("this terminal's account is not known yet: connect it once", 'bad');
    return;
  }
  var options = [];
  [['THE ACCOUNT', false], ['EACH NEW TRADE', true]].forEach(function (group) {
    options.push({ label: group[0] });
    live.ruleTypes.forEach(function (t) {
      if (t.perTrade !== group[1]) return;
      options.push({
        label: t.title,
        sub: t.blurb,
        fn: function () { openRuleForm(t.type, null); }
      });
    });
  });
  pickModal('Add a rule', options);
}

function templatesFlow() {
  var live = G.live;
  if (!live || !live.hasAccount) {
    toast("this terminal's account is not known yet: connect it once", 'bad');
    return;
  }
  var options = [];
  if (live.rules.length) {
    options.push({
      label: 'Save these rules as a template',
      sub: live.rules.length + ' rule' + (live.rules.length === 1 ? '' : 's') + ' as they stand',
      fn: saveTemplateFlow
    });
  }
  live.templates.forEach(function (t) {
    options.push({
      label: 'Apply ' + t.name,
      sub: t.rules + ' rule' + (t.rules === 1 ? '' : 's'),
      fn: function () { replaceRulesFlow({ template: t.name }, 'template ' + t.name, t.rules); }
    });
  });
  if (!options.length) {
    toast('no templates yet: add rules, then save them as one', 'bad');
    return;
  }
  pickModal('Templates', options);
}

function saveTemplateFlow() {
  var body = document.createElement('div');
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'gt-name');
  label.textContent = 'Template name';
  var input = document.createElement('input');
  input.id = 'gt-name';
  input.autocomplete = 'off';
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: 'Save as a template',
    body: body,
    confirm: 'Save template',
    onConfirm: function () { gact(api().guard_save_template(G.tid, input.value)); }
  });
  input.focus();
}

function copyFromFlow() {
  var live = G.live;
  if (!live || !live.hasAccount) {
    toast("this terminal's account is not known yet: connect it once", 'bad');
    return;
  }
  if (!live.copyFrom.length) {
    toast('no other slave has guards to copy yet', 'bad');
    return;
  }
  pickModal('Copy rules from', live.copyFrom.map(function (o) {
    return {
      label: o.name,
      sub: o.rules + ' rule' + (o.rules === 1 ? '' : 's'),
      fn: function () { replaceRulesFlow({ account: o.key, name: o.name }, o.name, o.rules); }
    };
  }));
}

function replaceRulesFlow(source, said, count) {
  var have = G.live ? G.live.rules.length : 0;
  if (!have) {
    gact(api().guard_apply_rules(G.tid, source));
    return;
  }
  confirmModal(
    'Replace ' + have + ' rule' + (have === 1 ? '' : 's') + '?',
    "This account's rules are replaced by the " + count + ' from ' + said
    + '. Its initial balance is kept.',
    'Replace them', true,
    function () { gact(api().guard_apply_rules(G.tid, source)); }
  );
}

function clearLatchFlow(r) {
  confirmModal(
    "Clear " + r.title + "'s breach?",
    'The slave may take new trades again as soon as the rule is back under the point it '
    + 'acts at. Clear it only if the firm has reset this account, or the breach was a mistake.',
    'Clear it', true,
    function () { gact(api().guard_clear_latch(G.tid, r.id)); }
  );
}

function removeRuleFlow(r) {
  confirmModal(
    'Remove ' + r.title + '?',
    'The rule and how it is set are forgotten.',
    'Remove it', true,
    function () { gact(api().guard_remove_rule(G.tid, r.id)); }
  );
}

// The rule form, built from the parameters the rule declares -- a new rule needs nothing
// here. The engine checks values and words the sentence; the form only asks.
function openRuleForm(type, ruleId) {
  api().guard_form(G.tid, type, ruleId).then(function (f) {
    if (!f.ok) { toast(f.message, 'bad'); return; }
    var body = document.createElement('div');
    body.className = 'gform';
    var inputs = {};

    f.fields.forEach(function (field) {
      var wrap = document.createElement('div');
      wrap.className = 'field gform-field';
      wrap.dataset.name = field.name;
      var label = document.createElement('label');
      label.className = 'flabel';
      label.textContent = field.label;
      wrap.appendChild(label);
      var line = document.createElement('div');
      line.className = 'gform-line';
      if (field.kind === 'choice') {
        var dd = makeChoiceDropdown(field, function () { formChanged(); });
        inputs[field.name] = dd.value;
        line.appendChild(dd.el);
      } else {
        var input = document.createElement('input');
        input.value = field.value;
        input.autocomplete = 'off';
        if (field.kind === 'number') input.inputMode = 'decimal';
        input.addEventListener('input', function () { formChanged(); });
        inputs[field.name] = function () { return input.value; };
        line.appendChild(input);
      }
      if (field.after) line.appendChild(cardLine('muted small', field.after));
      wrap.appendChild(line);
      if (field.hint) wrap.appendChild(cardLine('field-note muted', field.hint));
      wrap.appendChild(cardLine('field-note red gform-err', ''));
      body.appendChild(wrap);
    });

    var tickWrap = document.createElement('div');
    tickWrap.className = 'tick-wrap';
    var tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.id = 'gf-enabled';
    tick.checked = f.enabled;
    var tickLabel = document.createElement('label');
    tickLabel.className = 'flabel';
    tickLabel.setAttribute('for', 'gf-enabled');
    tickLabel.textContent = 'this rule is switched on';
    tickWrap.appendChild(tick);
    tickWrap.appendChild(tickLabel);
    body.appendChild(tickWrap);

    var sentence = document.createElement('div');
    sentence.className = 'gform-sentence';
    sentence.appendChild(cardLine('flabel', 'READS AS'));
    var sentenceBody = cardLine('gform-reads', '');
    sentence.appendChild(sentenceBody);
    body.appendChild(sentence);

    function collect() {
      var out = {};
      Object.keys(inputs).forEach(function (name) { out[name] = inputs[name](); });
      return out;
    }

    function showParts() {
      f.fields.forEach(function (field) {
        if (!field.onlyIf) return;
        var wrap = body.querySelector('.gform-field[data-name="' + field.name + '"]');
        wrap.hidden = collect()[field.onlyIf[0]] !== field.onlyIf[1];
      });
    }

    var checkTimer = null;
    function formChanged() {
      showParts();
      clearTimeout(checkTimer);
      checkTimer = setTimeout(function () {
        api().guard_check(G.tid, type, collect()).then(function (r) {
          f.fields.forEach(function (field) {
            var wrap = body.querySelector('.gform-field[data-name="' + field.name + '"]');
            wrap.querySelector('.gform-err').textContent = r.errors[field.name] || '';
          });
          sentenceBody.textContent = r.sentence
            || (Object.keys(r.errors).length ? 'Correct the values above to read the rule.' : '');
        });
      }, 250);
    }

    showModal({
      title: (ruleId ? 'Change ' : 'Add ') + f.title,
      body: body,
      confirm: 'Save',
      onConfirm: function () {
        gact(api().guard_save_rule(G.tid, type, ruleId, collect(), tick.checked));
      }
    });
    showParts();
    formChanged();
  });
}

function makeChoiceDropdown(field, onChange) {
  var root = document.createElement('div');
  root.className = 'dd gform-dd';
  var current = field.value;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'dd-btn';
  btn.setAttribute('aria-haspopup', 'listbox');
  function wordsFor(v) {
    var hit = field.choices.filter(function (c) { return c[0] === v; })[0];
    return hit ? hit[1] : v;
  }
  btn.textContent = wordsFor(current);
  var menu = document.createElement('div');
  menu.className = 'dd-menu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;
  field.choices.forEach(function (c) {
    var opt = document.createElement('button');
    opt.type = 'button';
    opt.setAttribute('role', 'option');
    opt.textContent = c[1];
    opt.addEventListener('click', function (ev) {
      ev.stopPropagation();
      current = c[0];
      btn.textContent = c[1];
      menu.hidden = true;
      onChange();
    });
    menu.appendChild(opt);
  });
  btn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var opening = menu.hidden;
    closeDropdowns();
    menu.hidden = !opening;
  });
  root.appendChild(btn);
  root.appendChild(menu);
  return { el: root, value: function () { return current; } };
}

// --- the news page -------------------------------------------------------------------------
// The week's calendar, and against each release what the news rules will do. The engine
// words the GUARD column from the rules' own parameters; this page only filters and draws.
var N = {
  live: null,
  drawn: '',
  ccy: 'all',       // 'all' or a currency code
  impact: 'all',    // 'all' | 'high' | 'highmed'
  mine: false,      // only currencies the open book touches
  ccyDd: null,      // the currency dropdown, rebuilt only when the week's currencies change
  ccyKey: '',
  impactDd: null,
  scanning: false
};
try {
  N.ccy = localStorage.getItem('dt-news-ccy') || 'all';
  N.impact = localStorage.getItem('dt-news-impact') || 'all';
  N.mine = localStorage.getItem('dt-news-mine') === '1';
} catch (e) { /* defaults are fine */ }

var newsBusy = false;
function pollNews() {
  if (newsBusy || !window.pywebview || S.page !== 'news') return;
  newsBusy = true;
  api().news_state().then(function (live) {
    newsBusy = false;
    N.live = live;
    renderNews();
  }, function () { newsBusy = false; });
}

function newsFilterPass(e, mineList) {
  // An 'ALL' release (an OPEC meeting) belongs to no one currency and moves everything,
  // so no currency narrowing ever hides it.
  if (N.ccy !== 'all' && e.ccy !== N.ccy && e.ccy !== 'ALL') return false;
  if (N.impact === 'high' && e.impact !== 'High') return false;
  if (N.impact === 'highmed' && e.impact !== 'High' && e.impact !== 'Medium') return false;
  if (N.mine && mineList && mineList.length && e.ccy !== 'ALL'
    && mineList.indexOf(e.ccy) < 0) return false;
  return true;
}

function impactPill(impact) {
  var map = { High: ['HIGH', 'red'], Medium: ['MED', 'yellow'], Low: ['LOW', 'muted'], Holiday: ['HOL', 'muted'] };
  var hit = map[impact] || [String(impact).toUpperCase(), 'muted'];
  var s = document.createElement('span');
  s.className = 'pill ' + hit[1];
  s.textContent = hit[0];
  return s;
}

function renderNews() {
  var live = N.live;
  if (!live) return;
  var key = JSON.stringify(live) + '|' + N.ccy + '|' + N.impact + '|' + N.mine;
  if (key === N.drawn) return;
  N.drawn = key;

  $('news-scanned').textContent = 'Forex Factory weekly feed · '
    + (live.scanned ? 'scanned ' + live.scanned : 'not scanned yet') + ' · times are local';
  $('news-problem').hidden = live.known;
  if (!live.known) $('news-problem').textContent = live.said;
  $('news-norule').hidden = live.ruled;
  $('news-filters').hidden = !live.known;
  $('news-how').hidden = !live.ruled;

  var nx = live.next;
  $('news-next').hidden = !nx;
  if (nx) {
    var pill = $('news-next-impact');
    pill.textContent = nx.impact === 'High' ? 'HIGH' : nx.impact === 'Medium' ? 'MED' : nx.impact.toUpperCase();
    pill.className = 'pill ' + (nx.impact === 'High' ? 'red' : 'yellow');
    $('news-next-title').textContent = nx.ccy + ' · ' + nx.title;
    $('news-next-when').textContent = nx.clock + ' · ' + nx.inWords;
    $('news-next-guard').textContent = nx.guard;
  }

  // The dropdowns: impact once, currency again whenever the week's list changes.
  if (!N.impactDd) {
    var idd = makeChoiceDropdown({
      value: N.impact,
      choices: [['all', 'Everything'], ['high', 'High only'], ['highmed', 'High + medium']]
    }, function () {
      N.impact = idd.value();
      try { localStorage.setItem('dt-news-impact', N.impact); } catch (e) { /* fine */ }
      renderNews();
    });
    N.impactDd = idd;
    $('news-impact').appendChild(idd.el);
  }
  var ccyKey = live.currencies.join(',');
  if (!N.ccyDd || N.ccyKey !== ccyKey) {
    N.ccyKey = ccyKey;
    var choices = [['all', 'All currencies']];
    live.currencies.forEach(function (c) { choices.push([c, c]); });
    if (!choices.some(function (c) { return c[0] === N.ccy; })) N.ccy = 'all';
    var dd = makeChoiceDropdown({ value: N.ccy, choices: choices }, function () {
      N.ccy = dd.value();
      try { localStorage.setItem('dt-news-ccy', N.ccy); } catch (e) { /* fine */ }
      renderNews();
    });
    N.ccyDd = dd;
    var ccyRoot = $('news-ccy');
    ccyRoot.textContent = '';
    ccyRoot.appendChild(dd.el);
  }

  // The narrowing tick is honest about when it cannot narrow.
  var mc = live.myCurrencies;
  var mine = $('news-mine');
  mine.checked = N.mine;
  mine.disabled = !mc || !mc.length;
  var mineLabel = document.querySelector('label[for="news-mine"]');
  if (mc === null) {
    mineLabel.title = 'a symbol in the book cannot be read, so any news moves it';
  } else if (!mc.length) {
    mineLabel.title = 'nothing is open to narrow by';
  } else {
    mineLabel.title = 'shows ' + mc.join(', ');
  }

  var days = $('news-days');
  days.textContent = '';
  var sections = [];
  var shown = 0;
  live.events.forEach(function (e) {
    if (!newsFilterPass(e, mc)) return;
    shown++;
    var last = sections[sections.length - 1];
    if (!last || last.day !== e.day) {
      last = { day: e.day, rows: [] };
      sections.push(last);
    }
    last.rows.push(e);
  });
  sections.forEach(function (sec) { days.appendChild(newsDaySection(sec)); });
  if (live.known && !live.events.length) {
    $('news-empty').textContent = 'No more releases this week.';
    $('news-empty').hidden = false;
  } else {
    $('news-empty').textContent = 'Nothing matches the filter.';
    $('news-empty').hidden = !live.known || shown > 0;
  }
}

function newsDaySection(sec) {
  var panel = document.createElement('section');
  panel.className = 'panel news-day';
  panel.setAttribute('aria-label', sec.day);
  var h = document.createElement('h2');
  h.className = 'news-day-head';
  h.textContent = sec.day;
  panel.appendChild(h);
  var scroll = document.createElement('div');
  scroll.className = 'table-scroll';
  var table = document.createElement('div');
  table.setAttribute('role', 'table');
  table.setAttribute('aria-label', sec.day + ' releases');
  table.className = 'news-table';
  var head = document.createElement('div');
  head.setAttribute('role', 'row');
  head.className = 'nrow nrow-head';
  ['TIME', 'CCY', 'IMPACT', 'EVENT', 'FORECAST', 'PREVIOUS', 'GUARD'].forEach(function (t) {
    var c = document.createElement('span');
    c.setAttribute('role', 'columnheader');
    c.textContent = t;
    head.appendChild(c);
  });
  table.appendChild(head);
  sec.rows.forEach(function (e) { table.appendChild(newsRow(e)); });
  scroll.appendChild(table);
  panel.appendChild(scroll);
  return panel;
}

function newsRow(e) {
  var row = document.createElement('div');
  row.setAttribute('role', 'row');
  row.className = 'nrow' + (e.guarded ? ' guarded' : '') + (e.past ? ' past' : '');
  function cell(cls, text) {
    var c = document.createElement('span');
    c.setAttribute('role', 'cell');
    c.className = cls;
    c.textContent = text;
    return c;
  }
  row.appendChild(cell('mono nc-time', e.time));
  row.appendChild(cell('mono nc-ccy', e.ccy));
  var ic = document.createElement('span');
  ic.setAttribute('role', 'cell');
  ic.appendChild(impactPill(e.impact));
  row.appendChild(ic);
  row.appendChild(cell('nc-event', e.title));
  row.appendChild(cell('mono nc-num', e.forecast || '—'));
  row.appendChild(cell('mono nc-num muted', e.previous || '—'));
  row.appendChild(cell(e.guarded ? 'nc-guard' : 'nc-guard muted', e.guard || '—'));
  return row;
}

// --- the history page ----------------------------------------------------------------------
// What became of each copied trade on each slave, read back from the on-disk history: a card
// per copied trade, every step taken with it under it. The bridge narrows to the filter and
// words every row; this page only asks and draws.
var H = {
  live: null,
  drawn: '',
  tid: 'all',       // 'all' or one slave's terminal id
  symbol: 'all',    // 'all' or one symbol
  failed: false,    // only trades with a failed step
  find: '',         // substring of ticket / symbol / label / slave name
  slaveDd: null, slaveKey: '',
  symbolDd: null, symbolKey: ''
};

var histAsked = 0;   // stamps each ask, so a slow answer can never cover a newer one
var histAnswered = 0;
function pollHistory(fresh) {
  if (!window.pywebview || S.page !== 'history') return;
  if (!fresh && histAnswered < histAsked) return; // one in flight is enough for a poll
  var stamp = ++histAsked;
  api().history_state(H.tid, H.symbol, H.failed, H.find).then(function (live) {
    if (stamp <= histAnswered) return;
    histAnswered = stamp;
    H.live = live;
    renderHistory();
  }, function () { if (stamp > histAnswered) histAnswered = stamp; });
}

function histFilterChanged() {
  // The narrowing happens in the bridge, so a changed filter asks afresh at once.
  H.drawn = '';
  pollHistory(true);
}

var HIST_STATUS_TONE = {
  open: 'blue', resting: 'blue', closed: 'green',
  cancelled: 'muted', orphaned: 'yellow', 'not copied': 'red', seen: 'muted'
};

function renderHistory() {
  var live = H.live;
  if (!live) return;
  var key = JSON.stringify(live);
  if (key === H.drawn) return;
  H.drawn = key;

  $('hist-sub').textContent =
    'One card per copied trade, its steps under it · kept ' + live.days + ' days';
  $('hist-filters').hidden = !live.total && !live.slaves.length;

  // The dropdowns, rebuilt only when their choices change under them.
  var slaveKey = live.slaves.map(function (s) { return s.tid + '|' + s.name; }).join(',');
  if (!H.slaveDd || H.slaveKey !== slaveKey) {
    H.slaveKey = slaveKey;
    var sChoices = [['all', 'All slaves']];
    live.slaves.forEach(function (s) { sChoices.push([s.tid, s.name]); });
    if (!sChoices.some(function (c) { return c[0] === H.tid; })) H.tid = 'all';
    var sdd = makeChoiceDropdown({ value: H.tid, choices: sChoices }, function () {
      H.tid = sdd.value();
      histFilterChanged();
    });
    H.slaveDd = sdd;
    var sRoot = $('hist-slave');
    sRoot.textContent = '';
    sRoot.appendChild(sdd.el);
  }
  var symbolKey = live.symbols.join(',');
  if (!H.symbolDd || H.symbolKey !== symbolKey) {
    H.symbolKey = symbolKey;
    var yChoices = [['all', 'All symbols']];
    live.symbols.forEach(function (s) { yChoices.push([s, s]); });
    if (!yChoices.some(function (c) { return c[0] === H.symbol; })) H.symbol = 'all';
    var ydd = makeChoiceDropdown({ value: H.symbol, choices: yChoices }, function () {
      H.symbol = ydd.value();
      histFilterChanged();
    });
    H.symbolDd = ydd;
    var yRoot = $('hist-symbol');
    yRoot.textContent = '';
    yRoot.appendChild(ydd.el);
  }

  $('hist-count').textContent = live.matched === live.total
    ? live.total + (live.total === 1 ? ' trade' : ' trades')
    : live.matched + ' of ' + live.total;

  var cards = $('hist-cards');
  cards.textContent = '';
  live.trades.forEach(function (t) { cards.appendChild(histCard(t)); });

  var empty = $('hist-empty');
  if (!live.total) {
    empty.textContent = live.slaves.length
      ? 'No trade copied yet: each one appears here when it is copied, with every step '
        + 'taken with it after.'
      : 'No slave yet — trades appear here as they are copied.';
    empty.hidden = false;
  } else if (!live.matched) {
    empty.textContent = 'Nothing matches the filter.';
    empty.hidden = false;
  } else {
    empty.hidden = true;
  }

  var more = $('hist-more');
  more.hidden = live.matched <= live.trades.length;
  if (!more.hidden) {
    more.textContent = 'Showing the ' + live.trades.length
      + ' most recently copied — narrow the filter to reach the older ones.';
  }
}

function histCard(t) {
  var card = document.createElement('article');
  card.className = 'hcard' + (t.problems ? ' failed' : '');
  card.setAttribute('aria-label', t.symbol + ' ' + t.label + ' on ' + t.slave);

  var head = document.createElement('div');
  head.className = 'hcard-head';
  var title = document.createElement('span');
  title.className = 'hcard-title';
  title.appendChild(document.createTextNode(t.symbol + ' '));
  var side = document.createElement('span');
  side.className = t.label.indexOf('sell') === 0 ? 'red' : 'blue';
  side.textContent = t.label + (t.lots === null || t.lots === undefined ? '' : ' ' + t.lots);
  title.appendChild(side);
  head.appendChild(title);
  var route = document.createElement('span');
  route.className = 'mono hcard-route';
  route.textContent = 'master #' + t.ticket + ' → ' + t.slave;
  head.appendChild(route);
  head.appendChild(pill(t.status.toUpperCase(), HIST_STATUS_TONE[t.status] || 'muted'));
  if (t.problems) head.appendChild(pill(t.problems + ' FAILED', 'red'));
  card.appendChild(head);

  var steps = document.createElement('div');
  steps.className = 'hsteps';
  t.steps.forEach(function (s) { steps.appendChild(histStep(s)); });
  card.appendChild(steps);
  return card;
}

function histStep(s) {
  var row = document.createElement('div');
  row.className = 'hstep' + (s.tone ? ' tone-' + s.tone : '');
  var line = document.createElement('div');
  line.className = 'hstep-line mono';
  var when = document.createElement('span');
  when.className = 'hstep-when';
  when.textContent = s.when;
  line.appendChild(when);
  var body = document.createElement('span');
  body.className = 'hstep-body';
  if (s.action) {
    var act = document.createElement('span');
    act.className = 'hstep-act';
    act.textContent = s.action.toUpperCase();
    body.appendChild(act);
    body.appendChild(document.createTextNode('  '));
  }
  var detail = document.createElement('span');
  detail.className = 'hstep-text';
  detail.textContent = s.detail;
  body.appendChild(detail);
  if (s.master) {
    var m = document.createElement('span');
    m.className = 'muted';
    m.textContent = ' · ' + s.master;
    body.appendChild(m);
  }
  line.appendChild(body);
  row.appendChild(line);
  if (s.guard) {
    // Under its step, so a trade a guard resized never reads as sized that way.
    var g = document.createElement('div');
    g.className = 'hstep-guard mono';
    var gw = document.createElement('span');
    gw.className = 'hstep-act';
    gw.textContent = 'GUARD';
    g.appendChild(gw);
    g.appendChild(document.createTextNode('  ' + s.guard));
    row.appendChild(g);
  }
  return row;
}

// --- events --------------------------------------------------------------------------------
function closeDropdowns() {
  document.querySelectorAll('.dd-menu').forEach(function (m) { m.hidden = true; });
  document.querySelectorAll('.dd-btn').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
}

function wireDropdown(id, onPick) {
  var root = $(id);
  var btn = root.querySelector('.dd-btn');
  var menu = root.querySelector('.dd-menu');
  btn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var opening = menu.hidden;
    closeDropdowns();
    menu.hidden = !opening;
    btn.setAttribute('aria-expanded', String(opening));
  });
  menu.addEventListener('click', function (ev) {
    var opt = ev.target.closest('button[role="option"]');
    if (!opt) return;
    onPick(opt.dataset.value);
    closeDropdowns();
  });
}

function onLevelTyped(which) {
  if (which === 'rr') {
    S.lastTouched = 'rr';
    rememberRr($('f-rr').value.trim());
  } else if (which === 'tp') {
    S.lastTouched = 'tp';
  }
  S.notice = '';
  renderTicket();
}

function wire() {
  $('f-symbol').value = S.symbol;
  if (defaultRr) $('f-rr').value = defaultRr; // last used rr, back as the default
  $('f-symbol').addEventListener('input', function (ev) {
    var up = ev.target.value.toUpperCase();
    if (up !== ev.target.value) {
      var at = ev.target.selectionStart;
      ev.target.value = up;
      ev.target.setSelectionRange(at, at);
    }
    S.symbol = up.trim();
    symbolChanged();
    try { localStorage.setItem('dt-symbol', S.symbol); } catch (e) { /* fine */ }
    $('chart-title').firstChild.textContent = (S.symbol || '—') + ' ';
    S.notice = '';
    renderTicket();
    poll();
    pollRates();
  });
  $('chart-title').firstChild.textContent = (S.symbol || '—') + ' ';

  ['f-stop', 'f-entry'].forEach(function (id) {
    $(id).addEventListener('input', function () { onLevelTyped(id === 'f-stop' ? 'sl' : 'entry'); });
  });
  $('f-rr').addEventListener('input', function () { onLevelTyped('rr'); });
  $('f-tp').addEventListener('input', function () { onLevelTyped('tp'); });
  $('f-expiry').addEventListener('input', function () { S.notice = ''; renderTicket(); });

  wireDropdown('dd-rr', function (value) {
    $('f-rr').value = value;
    onLevelTyped('rr');
  });
  wireDropdown('dd-expiry', function (value) {
    $('f-expiry').value = value;
    S.notice = '';
    renderTicket();
  });

  $('type-row').addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-type]');
    if (!btn) return;
    S.type = btn.dataset.type;
    document.querySelectorAll('#type-row button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b === btn));
    });
    $('pending-fields').hidden = S.type === 'market';
    S.notice = '';
    seedEntry();
    renderTicket();
  });

  $('tf-row').addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-tf]');
    if (!btn) return;
    S.tf = btn.dataset.tf;
    try { localStorage.setItem('dt-tf', S.tf); } catch (e) { /* fine */ }
    document.querySelectorAll('#tf-row button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b === btn));
    });
    document.querySelector('.chart-tf').textContent = '· ' + S.tf;
    S.candlesKey = '';
    pollRates();
  });
  document.querySelectorAll('#tf-row button').forEach(function (b) {
    b.setAttribute('aria-pressed', String(b.dataset.tf === S.tf));
  });
  document.querySelector('.chart-tf').textContent = '· ' + S.tf;

  $('place-btn').addEventListener('click', placeOrder);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') {
      if (S.mode === 'new' && ev.target.id.indexOf('f-') === 0) placeOrder();
      else if (S.mode === 'modify' && ev.target.id.indexOf('m-') === 0) applyModify();
    }
    if (ev.key === 'Escape') {
      closeDropdowns();
      if (!$('modal-back').hidden) { closeModal(); return; }
      if (!$('ctx-menu').hidden) { closeMenu(); return; }
      if (S.mode === 'modify') exitModify();
    }
  });
  document.addEventListener('click', function () {
    closeDropdowns();
    closeMenu();
  });

  $('reset-btn').addEventListener('click', function () {
    ['f-stop', 'f-rr', 'f-tp', 'f-entry'].forEach(function (id) { $(id).value = ''; });
    if (defaultRr) $('f-rr').value = defaultRr; // reset keeps the remembered rr
    $('f-expiry').value = '';
    S.type = 'market';
    S.lastTouched = 'rr';
    document.querySelectorAll('#type-row button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.type === 'market'));
    });
    $('pending-fields').hidden = true;
    say('Ticket reset.', '');
  });

  $('halt-btn').addEventListener('click', function () {
    if (S.haltBusy) return;
    S.haltBusy = true;
    api().toggle_halt().then(function (r) {
      S.haltBusy = false;
      toast(r.message, r.ok ? 'ok' : 'bad');
      poll();
    }, function () { S.haltBusy = false; });
  });

  $('win-min').addEventListener('click', function () { api().win_minimize(); });
  $('win-max').addEventListener('click', function () { api().win_toggle_max(); });
  $('win-close').addEventListener('click', function () { api().win_close(); });

  // The title bar drags the window natively — the system move loop gives edge snapping
  // and restore-from-maximized for free. The native loop swallows the mouseups, so a
  // double click is detected by timing two downs rather than waiting for dblclick.
  var lastDragDown = 0;
  document.querySelectorAll('.drag-region').forEach(function (el) {
    el.addEventListener('mousedown', function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      var now = Date.now();
      if (now - lastDragDown < 350) {
        lastDragDown = 0;
        api().win_toggle_max();
        return;
      }
      lastDragDown = now;
      api().win_drag();
    });
  });
  document.querySelectorAll('#resize-zones .rz').forEach(function (el) {
    el.addEventListener('mousedown', function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      api().win_resize(el.dataset.edge);
    });
  });

  $('mod-apply').addEventListener('click', applyModify);
  $('mod-cancel').addEventListener('click', exitModify);
  $('m-be').addEventListener('click', function () {
    if (S.modRow) {
      $('m-sl').value = String(S.modRow.entry);
      $('mod-notice').textContent = '';
      renderModify();
    }
  });
  ['m-entry', 'm-sl', 'm-tp'].forEach(function (id) {
    $(id).addEventListener('input', function () {
      $('mod-notice').textContent = '';
      renderModify();
    });
  });

  document.querySelectorAll('.nav-item.disabled').forEach(function (a) {
    a.addEventListener('click', function (ev) { ev.preventDefault(); });
  });
  document.querySelectorAll('#sidebar .nav-item[data-page]').forEach(function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      if (a.classList.contains('disabled')) return;
      showPage(a.dataset.page);
    });
  });

  // --- terminals page ---
  $('rescan-btn').addEventListener('click', function () {
    if (T.rescanning) return;
    T.rescanning = true;
    var btn = $('rescan-btn');
    btn.textContent = 'Rescanning…';
    btn.disabled = true;
    function done() {
      T.rescanning = false;
      btn.textContent = 'Rescan';
      btn.disabled = false;
    }
    api().rescan().then(function (r) {
      done();
      toast(r.message, r.ok ? 'ok' : 'bad');
      T.drawn = '';
      pollTerminals();
    }, function () {
      done();
      toast('The engine did not answer; try again.', 'bad');
    });
  });
  $('modal-confirm').addEventListener('click', function () {
    var fn = modalOnConfirm;
    closeModal();
    if (fn) fn();
  });
  $('modal-cancel').addEventListener('click', closeModal);
  $('modal-back').addEventListener('click', function (ev) {
    if (ev.target === $('modal-back')) closeModal();
  });
  $('ctx-menu').addEventListener('click', function (ev) { ev.stopPropagation(); });

  // --- guards page ---
  function selRule() {
    var live = G.live;
    if (!live) return null;
    return live.rules.filter(function (r) { return r.id === G.selected; })[0] || null;
  }
  var pickBtn = $('guard-pick-btn');
  var pickMenu = $('guard-pick-menu');
  var pickSearch = $('guard-pick-search');
  pickBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var opening = pickMenu.hidden;
    closeDropdowns();
    if (opening) {
      pickSearch.value = '';
      buildGuardPickList();
      pickMenu.hidden = false;
      pickBtn.setAttribute('aria-expanded', 'true');
      pickSearch.focus();
    }
  });
  pickMenu.addEventListener('click', function (ev) { ev.stopPropagation(); });
  pickSearch.addEventListener('input', buildGuardPickList);
  pickSearch.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      var first = $('guard-pick-list').querySelector('.gp-item');
      if (first) first.click();
    }
  });
  $('g-add').addEventListener('click', addRuleFlow);
  $('g-templates').addEventListener('click', templatesFlow);
  $('g-copy').addEventListener('click', copyFromFlow);
  $('g-all').addEventListener('change', function () { gact(api().guard_toggle_all(G.tid)); });
  $('g-change').addEventListener('click', function () {
    var r = selRule();
    if (r) openRuleForm(r.type, r.id);
  });
  $('g-toggle').addEventListener('click', function () {
    var r = selRule();
    if (r) gact(api().guard_toggle_rule(G.tid, r.id));
  });
  $('g-remove').addEventListener('click', function () {
    var r = selRule();
    if (r) removeRuleFlow(r);
  });
  $('g-clear').addEventListener('click', function () {
    var r = selRule();
    if (r) clearLatchFlow(r);
  });

  $('news-mine').addEventListener('change', function () {
    N.mine = $('news-mine').checked;
    try { localStorage.setItem('dt-news-mine', N.mine ? '1' : ''); } catch (e) { /* fine */ }
    renderNews();
  });
  $('news-rescan').addEventListener('click', function () {
    if (N.scanning) return;
    N.scanning = true;
    var b = $('news-rescan');
    b.disabled = true;
    b.textContent = 'Scanning…';
    function back() {
      N.scanning = false;
      b.disabled = false;
      b.textContent = 'Rescan now';
    }
    api().news_rescan().then(function (r) {
      back();
      toast(r.message, r.ok ? 'ok' : 'bad');
      N.drawn = '';
      pollNews();
    }, function () {
      back();
      toast('The engine did not answer; try again.', 'bad');
    });
  });
  // --- history page ---
  $('hist-failed').addEventListener('change', function () {
    H.failed = $('hist-failed').checked;
    histFilterChanged();
  });
  var histFindTimer = null;
  $('hist-find').addEventListener('input', function () {
    clearTimeout(histFindTimer);
    histFindTimer = setTimeout(function () {
      var now = $('hist-find').value.trim();
      if (now === H.find) return;
      H.find = now;
      histFilterChanged();
    }, 250);
  });

  function commitInitial() {
    var live = G.live;
    if (!live || !live.hasAccount) return;
    var now = $('g-initial').value.trim();
    var had = live.initialBalance === null || live.initialBalance === undefined
      ? '' : String(live.initialBalance);
    if (now !== had) gact(api().guard_set_initial(G.tid, now));
  }
  $('g-initial').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.target.blur(); }
  });
  $('g-initial').addEventListener('blur', commitInitial);
}

function start() {
  makeChart();
  wire();
  showPage(S.page);
  poll();
  pollRates();
  setInterval(poll, 600);
  setInterval(pollRates, 1500);
  setInterval(pollTerminals, 1000);
  setInterval(pollGuards, 1000);
  setInterval(pollNews, 1000);
  // The history is an after-the-fact read of everything kept; two seconds is the TUI's
  // own pace for it, and the answer is the heaviest poll the bridge serves.
  setInterval(pollHistory, 2000);
}

if (window.pywebview) start();
else window.addEventListener('pywebviewready', start);
