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
  haltBusy: false
};

var defaultRr = '';
try {
  S.symbol = localStorage.getItem('dt-symbol') || '';
  S.tf = localStorage.getItem('dt-tf') || 'M5';
  defaultRr = localStorage.getItem('dt-rr') || '';
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
  var fresh = key !== S.candlesKey || data.length !== S.candles.length ||
    (data.length && S.candles.length && data[data.length - 1].time !== S.candles[S.candles.length - 1].time);
  S.candles = data;
  S.candlesKey = key;
  if (!data.length) { series.setData([]); return; }
  var d = 0;
  for (var i = Math.max(0, data.length - 20); i < data.length; i++) d = Math.max(d, decimalsOf(data[i].close));
  S.digits = Math.max(d, 1);
  if (fresh) series.setData(data);
  else series.update(data[data.length - 1]);
}

// --- modify mode: the panel and the chart belong to one trade ------------------------------
function enterModify(row) {
  S.mode = 'modify';
  S.modRow = row;
  if (row.symbol !== S.symbol) {
    // The chart follows the trade: modifying is meaningless over another symbol's bars.
    S.symbol = row.symbol;
    $('f-symbol').value = row.symbol;
    S.candlesKey = '';
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
    S.candlesKey = '';
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
      if (S.mode === 'modify') exitModify();
    }
  });
  document.addEventListener('click', closeDropdowns);

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
}

function start() {
  makeChart();
  wire();
  poll();
  pollRates();
  setInterval(poll, 600);
  setInterval(pollRates, 1500);
}

if (window.pywebview) start();
else window.addEventListener('pywebviewready', start);
