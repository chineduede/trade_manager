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
  page: 'trading'      // which page owns #main: 'trading' or 'terminals'
};

var defaultRr = '';
try {
  S.symbol = localStorage.getItem('dt-symbol') || '';
  S.tf = localStorage.getItem('dt-tf') || 'M5';
  defaultRr = localStorage.getItem('dt-rr') || '';
  var savedPage = localStorage.getItem('dt-page');
  if (savedPage === 'terminals') S.page = savedPage;
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

function toast(message, tone) {
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
  setTimeout(leave, 4500);
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
  rescanning: false
};
var modalOnConfirm = null;

function showPage(name) {
  S.page = name;
  try { localStorage.setItem('dt-page', name); } catch (e) { /* fine */ }
  $('page-trading').hidden = name !== 'trading';
  $('page-terminals').hidden = name !== 'terminals';
  document.querySelectorAll('#sidebar .nav-item[data-page]').forEach(function (a) {
    var active = a.dataset.page === name;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (name === 'terminals') pollTerminals();
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
  // Mid-drag the source card must stay the element the pointer started on, so the
  // rebuild waits; the next poll after the drop catches everything up.
  if (!live || T.drag) return;
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
}

function pill(text, tone) {
  var p = document.createElement('span');
  p.className = 'pill ' + (tone || 'muted');
  p.textContent = text;
  return p;
}

function cardPills(c, live) {
  var out = [];
  if (c.role === 'master') {
    out.push(pill('MASTER', 'blue'));
    out.push(live.dryRun ? pill('DRY RUN', 'yellow') : pill('LIVE', 'red'));
    return out;
  }
  if (c.role === 'slave') {
    if (c.demoBlocked) out.push(pill('HELD BACK', 'red'));
    else if (c.holdReason) out.push(pill('HELD', 'red'));
    out.push(live.dryRun ? pill('DRY RUN', 'yellow') : pill('LIVE', 'red'));
    if (c.paused) out.push(pill('PAUSED', 'muted'));
    return out;
  }
  if (c.isCtrader) out.push(pill('SLAVE ONLY', 'muted'));
  else if (!c.hasExe) out.push(pill('NOT INSTALLED', 'muted'));
  else if (!c.login) out.push(pill('NO ACCOUNT', 'muted'));
  return out;
}

function stateLine(c, live) {
  if (c.role === 'unassigned') {
    if (!c.hasExe && !c.isCtrader) return 'found in config · cannot be launched from here';
    var who = c.isCtrader ? 'can copy, cannot feed' : '';
    var logged = c.login ? 'logged in' : 'not logged in';
    return who ? logged + ' · ' + who : logged;
  }
  var open = c.openTrades === null ? null
    : c.openTrades + ' position' + (c.openTrades === 1 ? '' : 's');
  var first;
  if (c.tier === 'serious' || c.tier === 'critical' || c.tier === 'muted'
    || (c.tier === 'warning' && !c.holdReason)) {
    first = c.role === 'slave' && c.badge ? c.badge.toLowerCase() : c.statusText;
  } else if (c.role === 'master') {
    first = 'connected';
  } else if (c.demoBlocked) {
    first = 'held back: not a demo';
  } else if (c.holdReason) {
    first = c.holdReason.toLowerCase();
  } else if (c.paused) {
    first = 'paused';
  } else if (!live.copying) {
    first = 'not copying';
  } else {
    first = live.dryRun ? 'dry run' : 'copying';
  }
  return open === null ? first : first + ' · ' + open;
}

function terminalCard(c, live) {
  var el = document.createElement('article');
  el.className = 'tcard';
  el.dataset.tid = c.tid;
  el.setAttribute('aria-label', c.name);

  var head = document.createElement('div');
  head.className = 'tcard-head';
  var dot = document.createElement('span');
  var dotTone = { good: 'green', warning: 'yellow', serious: 'red', critical: 'red' }[c.tier] || '';
  dot.className = 'dot ' + dotTone;
  dot.setAttribute('aria-hidden', 'true');
  head.appendChild(dot);
  var name = document.createElement('span');
  name.className = 'tcard-name';
  name.textContent = c.name;
  head.appendChild(name);
  var space = document.createElement('span');
  space.className = 'tcard-space';
  head.appendChild(space);
  cardPills(c, live).forEach(function (p) { head.appendChild(p); });
  el.appendChild(head);

  var acct = document.createElement('span');
  acct.className = 'tcard-acct mono';
  acct.textContent = c.login
    ? c.login + (c.server ? ' · ' + c.server : '')
    : (c.isCtrader ? 'cTrader · Spotware' : 'not logged in');
  el.appendChild(acct);

  if (c.equity !== null && c.equity !== undefined) {
    var eq = document.createElement('div');
    eq.className = 'tcard-eq mono';
    var left = document.createElement('span');
    left.textContent = 'equity ' + money(c.equity);
    var right = document.createElement('span');
    var pl = c.floatingPl;
    if (pl !== null && pl !== undefined) {
      right.textContent = (pl >= 0 ? '+' : '') + money(pl);
      right.className = pl > 0 ? 'green' : pl < 0 ? 'red' : 'muted';
    }
    eq.appendChild(left);
    eq.appendChild(right);
    el.appendChild(eq);
  }

  var state = document.createElement('span');
  state.className = 'tcard-state';
  state.textContent = stateLine(c, live);
  el.appendChild(state);

  if (c.guard && (c.guard.text || c.guard.rules)) {
    var g = document.createElement('span');
    g.className = 'tcard-guard ' + (c.guard.text ? (c.guard.tone || '') : 'quiet');
    g.textContent = c.guard.text
      || ('guards ' + c.guard.rules + ' rule' + (c.guard.rules === 1 ? '' : 's'));
    el.appendChild(g);
  }

  wireCard(el, c);
  return el;
}

function virtualCard(live) {
  var el = document.createElement('article');
  el.className = 'tcard tcard-still';
  var v = live.virtual;

  var head = document.createElement('div');
  head.className = 'tcard-head';
  var dot = document.createElement('span');
  dot.className = 'dot ' + (v.feed ? 'green' : 'red');
  dot.setAttribute('aria-hidden', 'true');
  head.appendChild(dot);
  var name = document.createElement('span');
  name.className = 'tcard-name';
  name.textContent = 'Direct trading';
  head.appendChild(name);
  var space = document.createElement('span');
  space.className = 'tcard-space';
  head.appendChild(space);
  head.appendChild(pill('MASTER', 'blue'));
  el.appendChild(head);

  var acct = document.createElement('span');
  acct.className = 'tcard-acct';
  acct.textContent = 'you are the master';
  el.appendChild(acct);

  var book = document.createElement('span');
  book.className = 'tcard-eq mono';
  book.textContent = v.positions + ' open · ' + v.orders + ' resting';
  el.appendChild(book);

  var state = document.createElement('span');
  state.className = 'tcard-state';
  var feeds = 'feeds ' + live.slaves.length + ' slave' + (live.slaves.length === 1 ? '' : 's');
  state.textContent = (v.feed ? 'feed ' + v.feed : 'no feed — connect an MT5 slave') + ' · ' + feeds;
  el.appendChild(state);

  var go = document.createElement('button');
  go.type = 'button';
  go.className = 'row-btn tcard-btn';
  go.textContent = 'Order ticket';
  go.addEventListener('click', function () { showPage('trading'); });
  el.appendChild(go);
  return el;
}

// --- dragging a card between zones ---------------------------------------------------------
function wireCard(el, c) {
  el.addEventListener('contextmenu', function (ev) {
    ev.preventDefault();
    openMenu(c, ev.clientX, ev.clientY);
  });
  el.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0 || ev.target.closest('button')) return;
    var startX = ev.clientX, startY = ev.clientY;
    var started = false;
    function move(e) {
      if (!started) {
        if (Math.abs(e.clientX - startX) < 6 && Math.abs(e.clientY - startY) < 6) return;
        started = true;
        beginDrag(el, c, e);
      }
      moveDrag(e);
    }
    function up(e) {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      if (started) endDrag(e);
    }
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    ev.preventDefault();
  });
}

function beginDrag(el, c, e) {
  closeMenu();
  var ghost = el.cloneNode(true);
  ghost.className = 'tcard tcard-ghost';
  ghost.style.width = el.offsetWidth + 'px';
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

function endDrag(e) {
  var drag = T.drag;
  if (!drag) return;
  T.drag = null;
  drag.ghost.remove();
  drag.el.classList.remove('drag-src');
  document.body.classList.remove('dragging-terminals');
  document.querySelectorAll('.zone').forEach(function (z) { z.classList.remove('drop-hot'); });
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
  ok.textContent = opts.confirm;
  ok.className = opts.danger ? 'danger' : '';
  modalOnConfirm = opts.onConfirm;
  $('modal-back').hidden = false;
  ok.focus();
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
}

if (window.pywebview) start();
else window.addEventListener('pywebviewready', start);
