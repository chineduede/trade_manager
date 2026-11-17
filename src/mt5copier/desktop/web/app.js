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
  digits: 2,           // decimals for formatting prices: the feed's own, or seen in the data
  meta: null,          // the feed's pricing facts for the charted symbol, from rates()
  metaSymbol: '',      // the symbol S.meta belongs to; stale meta must never price another
  priceFmtKey: '',     // the precision|minMove the chart's axis was last told to print
  dragging: null,      // 'sl' | 'tp' | 'entry' while a line is held, whichever mode owns it
  notice: '', noticeTone: '',
  mode: 'new',         // 'new' places orders; 'modify' takes the panel AND the chart for one trade
  modRow: null,        // the trade being modified, as its book row read at entry
  placing: false,
  placedUntil: 0,      // while now < this, the place button says "✓ Placed" and stays dead
  haltBusy: false,
  guardSeen: null,     // guard-event keys already toasted; null until the first poll primes it
  problemSeen: null,   // plain-failure keys already toasted, primed the same way
  page: 'trading'      // which page owns #main: 'trading' or 'terminals'
};

var defaultRr = '';
try {
  S.symbol = localStorage.getItem('dt-symbol') || '';
  S.tf = localStorage.getItem('dt-tf') || 'M5';
  defaultRr = localStorage.getItem('dt-rr') || '';
  var savedPage = localStorage.getItem('dt-page');
  if (['terminals', 'guards', 'news', 'history', 'setups', 'settings'].indexOf(savedPage) >= 0) S.page = savedPage;
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

function symbolMeta() {
  // The feed's pricing facts, but only while they are the charted symbol's own.
  return S.meta && S.metaSymbol === S.symbol ? S.meta : null;
}

function pipSize() {
  // The quoting convention's pip: the point on most symbols, ten points where the broker
  // quotes an extra fractional digit (5-digit FX, 3-digit JPY pairs).
  var m = symbolMeta();
  if (!m || !(m.point > 0)) return 0;
  return m.digits === 3 || m.digits === 5 ? m.point * 10 : m.point;
}

function pips(dist) {
  var p = pipSize();
  if (!(p > 0)) return null;
  var v = dist / p;
  return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + ' pips';
}

function distText(dist) {
  // A distance said the way a trader thinks in: pips when the feed has given the symbol's
  // point, the formatted price delta until then.
  return pips(dist) || fmt(dist);
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
  var m = symbolMeta();
  if (m && m.digits > 0) {
    S.digits = m.digits;
  } else {
    var d = 0;
    for (var i = Math.max(0, data.length - 20); i < data.length; i++) d = Math.max(d, decimalsOf(data[i].close));
    S.digits = Math.max(d, 1);
  }
  // The axis must print the symbol's own precision: the series' default is 2 decimals,
  // which collapsed every EURUSD label to "1.10" while XAUUSD happened to fit.
  var fmtKey = S.digits + '|' + (m && m.point > 0 ? m.point : 0);
  if (fmtKey !== S.priceFmtKey) {
    S.priceFmtKey = fmtKey;
    series.applyOptions({
      priceFormat: {
        type: 'price',
        precision: S.digits,
        minMove: m && m.point > 0 ? m.point : Math.pow(10, -S.digits)
      }
    });
  }
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
      slDelta.textContent = '−' + distText(risk) + ' from the entry';
      slDelta.className = 'field-note red';
    } else if (risk === 0) {
      slDelta.textContent = 'at the entry — breakeven';
      slDelta.className = 'field-note blue';
    } else {
      slDelta.textContent = '+' + distText(-risk) + ' past the entry — profit locked';
      slDelta.className = 'field-note blue';
    }
  } else {
    slDelta.textContent = '';
    slDelta.className = 'field-note';
  }
  $('m-tp-delta').textContent = tpIn && entryOk
    ? (way * (tp - entry) >= 0 ? '+' : '−') + distText(Math.abs(tp - entry))
    : '';
  if (!row.open && entryOk && q) {
    var away = entry - (row.side === 'buy' ? q.ask : q.bid);
    $('m-entry-delta').textContent = (away >= 0 ? '+' : '−') + distText(Math.abs(away)) + ' from price';
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
  else if (!S.live.direct) problem = 'This profile copies a master — switch it to direct trading on the Settings page first.';
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

  var m = symbolMeta();
  var qf = function (v) { return m && m.digits > 0 ? v.toFixed(m.digits) : String(v); };
  $('quote-line').textContent = q ? 'bid ' + qf(q.bid) + ' · ask ' + qf(q.ask) : '';

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

  $('stop-delta').textContent = t.ok ? '−' + distText(t.dist) : '';
  $('target-delta').textContent = t.ok ? '+' + distText(Math.abs(t.tp - t.entry)) : '';

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
      var vpu = m ? m.valuePerUnit : 0;
      if (s.budget === null || !t.dist) {
        lots.className = 'lots unknown';
        lots.textContent = s.risk + ' · balance unknown';
      } else if (!(vpu > 0)) {
        // Without the feed's tick value, budget / distance is off by the contract size —
        // the 1,249,718-lots bug. Better no number than that one.
        lots.className = 'lots unknown';
        lots.textContent = s.risk + ' · sized at the terminal';
      } else {
        lots.className = 'lots';
        lots.textContent = '≈' + Math.max(0.01, s.budget / (t.dist * vpu)).toFixed(2) + ' lots';
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

function copiesCell(r) {
  // The summary the cell always carried — "2 live · 1 closed" — now opens a small menu
  // naming every copy, each live one a link to the ticket it holds on its own terminal.
  // The row itself stays one line tall.
  var td = document.createElement('td');
  var cp = copiesSummary(r);
  if (!r.open || !(r.copies || []).length) {
    td.className = !r.open ? 'muted' : cp.cls;
    td.textContent = !r.open ? '—' : cp.text;
    if (r.open && cp.title) td.title = cp.title;
    return td;
  }
  td.className = cp.cls;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'linklike copies-dd';
  btn.textContent = cp.text + ' ▾';
  btn.title = 'The copies behind this ticket — each live one links to its terminal page';
  btn.addEventListener('click', function (ev) {
    ev.stopPropagation(); // the document's click-anywhere close must not eat the opening
    openCopiesMenu(r, btn);
  });
  td.appendChild(btn);
  return td;
}

function openCopiesMenu(r, anchor) {
  // The card menu's element, reused: same look, same close-anywhere behaviour.
  var menu = $('ctx-menu');
  menu.textContent = '';
  var head = document.createElement('span');
  head.className = 'menu-head';
  head.textContent = '#' + r.ticket + ' ' + r.symbol + ' — copies';
  menu.appendChild(head);
  (r.copies || []).forEach(function (s) {
    var word = s.state === 'unknown' ? 'unverified' : s.state;
    var name = s.name + (s.ticket ? ' #' + s.ticket : '');
    if (s.state === 'live' && s.tid && s.ticket) {
      var item = document.createElement('button');
      item.type = 'button';
      item.setAttribute('role', 'menuitem');
      item.textContent = name + ' — ' + word;
      item.addEventListener('click', function () {
        closeMenu();
        openTerminalDetail(s.tid, 'overview', s.ticket);
      });
      menu.appendChild(item);
    } else {
      var note = document.createElement('span');
      note.className = 'menu-note';
      note.textContent = name + ' — ' + word;
      menu.appendChild(note);
    }
  });
  menu.hidden = false;
  var at = anchor.getBoundingClientRect();
  var w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(8, Math.min(at.left, window.innerWidth - w - 8)) + 'px';
  var y = at.bottom + 6;
  if (y + h > window.innerHeight - 8) y = Math.max(8, at.top - h - 6);
  menu.style.top = y + 'px';
}

function jumpToBook(ticket) {
  // The way back: a copy's row on a terminal page returns to its ticket in the book.
  S.focusBookTicket = ticket;
  bookDrawn = ''; // the rows may be unchanged; the flash still needs a redraw
  showPage('trading');
  if (S.live) renderBook();
}

function renderBook() {
  var rows = (S.live && S.live.book) || [];
  var open = rows.filter(function (r) { return r.open; }).length;
  $('book-summary').textContent = open + ' open · ' + (rows.length - open) + ' resting';
  $('book-empty').hidden = rows.length > 0;
  $('flatten-btn').hidden = rows.length === 0;

  var key = JSON.stringify(rows);
  if (key === bookDrawn) return;
  bookDrawn = key;

  var body = $('book-body');
  body.textContent = '';
  var seen = {};
  var focused = null;
  rows.forEach(function (r) {
    seen[r.ticket] = true;
    var tr = document.createElement('tr');
    if (bookTickets && !bookTickets[r.ticket]) tr.className = 'row-new';
    if (S.focusBookTicket && r.ticket === S.focusBookTicket) {
      tr.className = 'row-new'; // arrived by the back-link: flash as a new row would
      focused = tr;
    }
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
    tr.appendChild(copiesCell(r));

    var actions = document.createElement('td');
    actions.className = 'actions-cell';
    if (r.open) {
      var be = document.createElement('button');
      be.type = 'button';
      be.className = 'row-btn' + (r.beAt ? ' armed' : '');
      be.textContent = r.beAt ? 'BE @ ' + r.beAt : 'BE';
      be.title = r.beAt
        ? 'Breakeven armed: once the price touches ' + r.beAt
          + ', the stop moves to the entry. Click to change or disarm.'
        : 'Breakeven: move the stop to the entry price — now, or once the price touches a level';
      be.addEventListener('click', function () { breakevenFlow(r); });
      actions.appendChild(be);
      var part = document.createElement('button');
      part.type = 'button';
      part.className = 'row-btn';
      part.textContent = 'Partial';
      part.title = 'Close a percent of it; every copy mirrors the same fraction';
      part.addEventListener('click', function () { partialCloseFlow(r); });
      actions.appendChild(part);
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
  if (focused && focused.scrollIntoView) focused.scrollIntoView({ block: 'center' });
  S.focusBookTicket = 0; // one flash; a later redraw must not hunt again
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

// The plain failures beside them: a broker refusal, a link failure, a copy never answered —
// no guard behind it, so no rule to name. These only reached the log before; a minimum-lot
// sizing refusal on one slave was the case that showed it.
function noteProblemEvents(events) {
  var seen = S.problemSeen;
  S.problemSeen = {};
  events.forEach(function (e) { S.problemSeen[e.key] = true; });
  if (seen === null) return;
  events.forEach(function (e) {
    if (seen[e.key]) return;
    var head = (e.did || 'copy failed')
      + (e.trade ? ' ' + e.trade : '')
      + (e.slave ? ' on ' + e.slave : '');
    toast(head + (e.detail ? ' — ' + e.detail : ''), 'bad', 9000);
  });
}

function breakevenFlow(r) {
  // The BE button's dialog: empty means now (the button's old immediate move), a price
  // arms it — the book moves the stop to the entry when the feed touches the price, and
  // every copy follows. The engine has the last word on the number.
  var body = document.createElement('div');
  var p = document.createElement('p');
  p.textContent = '#' + r.ticket + ' ' + r.symbol + ' ' + r.side
    + ' — the stop moves to the entry (' + r.entry + ').';
  body.appendChild(p);
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'be-price');
  label.textContent = 'Once the price touches';
  var input = document.createElement('input');
  input.id = 'be-price';
  input.autocomplete = 'off';
  input.inputMode = 'decimal';
  input.placeholder = 'now';
  input.value = r.beAt ? String(r.beAt) : '';
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  var note = document.createElement('span');
  note.className = 'field-note muted';
  note.textContent = r.beAt
    ? 'A new price re-arms it, clearing the field disarms it, and "now" moves the stop '
      + 'right away.'
    : 'Leave it empty to move the stop now; type a price to arm it for when the market '
      + 'gets there.';
  body.appendChild(note);
  showModal({
    title: 'Breakeven #' + r.ticket,
    body: body,
    confirm: 'Breakeven',
    onConfirm: function () {
      var v = input.value.trim();
      if (!v && r.beAt) act(api().breakeven_at(r.ticket, 0));
      else if (!v || v.toLowerCase() === 'now') {
        act(api().modify_position(r.ticket, r.entry, r.tp || 0));
      } else act(api().breakeven_at(r.ticket, v));
    }
  });
  input.focus();
  input.select();
}

function partialCloseFlow(r) {
  // The TUI's x key as a dialog: a percent, 50 to begin with; the slice closes and every
  // copy mirrors the same fraction. The engine has the last word on the number.
  var body = document.createElement('div');
  var p = document.createElement('p');
  p.textContent = '#' + r.ticket + ' ' + r.symbol + ' ' + r.side + ' ' + r.lots
    + ' lots. The rest stays open with the same stop and target.';
  body.appendChild(p);
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'partial-pct');
  label.textContent = 'Percent to close';
  var input = document.createElement('input');
  input.id = 'partial-pct';
  input.autocomplete = 'off';
  input.inputMode = 'decimal';
  input.value = '50';
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: 'Partial close #' + r.ticket,
    body: body,
    confirm: 'Close that part',
    onConfirm: function () {
      act(api().partial_close(r.ticket, input.value.trim()));
    }
  });
  input.focus();
  input.select();
}

function flattenFlow() {
  var rows = (S.live && S.live.book) || [];
  if (!rows.length) return;
  var open = rows.filter(function (r) { return r.open; }).length;
  var body = document.createElement('div');
  var p = document.createElement('p');
  p.textContent = 'Close ' + open + ' open position' + (open === 1 ? '' : 's')
    + ' and cancel ' + (rows.length - open) + ' resting order'
    + (rows.length - open === 1 ? '' : 's') + '? Every copy follows on the next tick.';
  body.appendChild(p);
  showModal({
    title: 'Close everything?',
    body: body,
    confirm: 'Close everything',
    danger: true,
    onConfirm: function () { act(api().flatten()); }
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
    noteProblemEvents(live.problemEvents || []);
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
  // The TP the person touched belonged to the old symbol and was just cleared with it,
  // so rr speaks for the target again — otherwise the ticket keeps deferring to a blank
  // TP field and the target line never comes back on the new symbol.
  S.lastTouched = 'rr';
  S.candlesKey = '';
  S.meta = null;
  S.metaSymbol = '';
  showCandles([]);
}

function pollRates() {
  if (ratesBusy || !window.pywebview) return;
  if (!S.symbol) { showCandles([]); setMsg('type a symbol to chart it'); return; }
  ratesBusy = true;
  var asked = S.symbol;
  api().rates(S.symbol, S.tf).then(function (r) {
    ratesBusy = false;
    if (r.meta && asked === S.symbol) {
      S.meta = r.meta;
      S.metaSymbol = asked;
    }
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
var modalOnClose = null;

var PAGES = ['trading', 'terminals', 'guards', 'news', 'history', 'setups', 'settings', 'help', 'tdetail'];

var RET = null;
// A one-shot way back: set by a flow that sends the person to another page mid-task (the
// rule form's link to the symbol catalog), holding what it takes to resume -- the page, the
// form and what was typed. Any navigation clears it, so it never lingers past its moment;
// the flow that wants it sets it right after its own showPage call.

function updateCatBack() {
  var el = $('cat-back');
  el.hidden = !RET;
  el.textContent = RET ? '← Back to ' + RET.title : '';
}

function showPage(name) {
  if (PAGES.indexOf(name) < 0) name = 'trading';
  S.page = name;
  RET = null;
  updateCatBack();
  // A restored 'tdetail' would land with no terminal under it, so it remembers as the
  // Terminals page it came from — which its sidebar highlight already says it is.
  try {
    localStorage.setItem('dt-page', name === 'tdetail' ? 'terminals' : name);
  } catch (e) { /* fine */ }
  PAGES.forEach(function (p) { $('page-' + p).hidden = p !== name; });
  var navName = name === 'tdetail' ? 'terminals' : name;
  document.querySelectorAll('#sidebar .nav-item[data-page]').forEach(function (a) {
    var active = a.dataset.page === navName;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (name === 'terminals') pollTerminals();
  if (name === 'guards') pollGuards();
  if (name === 'news') pollNews();
  if (name === 'history') pollHistory();
  if (name === 'setups') pollSetups();
  if (name === 'settings') { pollSettings(); pollCatalog(); }
  if (name === 'tdetail') pollTdetail();
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
  // An empty zone means two different things: on a working profile every terminal is in
  // use, but on a fresh one nothing has been found at all — say which, or the first
  // thing a new user reads is "every terminal is assigned" over an empty app.
  $('detected-note').textContent =
    (live.detected.length || live.master || live.virtual || live.slaves.length)
      ? 'every terminal found is assigned · Rescan looks again'
      : 'none found yet — start your MT5 terminals and Rescan; a cTrader account joins through cTrader login…';
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
  if (c.loggingIn) return { text: 'logging in…', cls: 'yellow' };
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
    if (c.loggingIn) {
      el.appendChild(cardLine('tcard-login yellow', 'logging in…'));
    } else if (c.canSwitch) {
      // The menu alone hid this: the card itself says an account can be picked. Only
      // when there is a pick — a terminal with one saved account has nowhere to go.
      var login = document.createElement('button');
      login.type = 'button';
      login.className = 'linklike tcard-login';
      login.textContent = c.login ? 'log in to a different account…' : 'log in to an account…';
      login.addEventListener('click', function () { accountLoginFlow(c); });
      el.appendChild(login);
    }
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
  if (e.key === 'F1') { e.preventDefault(); showPage('help'); }
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
      if (started) { endDrag(e); return; }
      // A plain click — the pointer never left the 6px threshold — opens the
      // terminal's own page: its settings, or what it needs before it has any.
      openTerminalDetail(c.tid);
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
    if (c.isCtrader) {
      does('Log in to cTrader…', ctraderLoginFlow);
    } else if (c.loggingIn) {
      says('logging in — the card says when it is done');
    } else if (c.canSwitch) {
      does(
        c.login ? 'Log in to a different account…' : 'Log in to an account…',
        function () { accountLoginFlow(c); }
      );
    }
    if (c.login) {
      does(c.accountName ? 'Rename this account…' : 'Name this account…',
        function () { accountNameFlow(c); });
    }
    return items;
  }

  does('Overview — its own page', function () { openTerminalDetail(c.tid); });
  does('Settings', function () { openTerminalDetail(c.tid, 'settings'); });
  if (c.login) {
    does(c.accountName ? 'Rename this account…' : 'Name this account…',
      function () { accountNameFlow(c); });
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
  ok.disabled = false; // a flow may disable it again until something is typed
  $('modal-cancel').textContent = opts.cancel || 'Cancel';
  modalOnConfirm = opts.onConfirm || null;
  modalOnClose = opts.onClose || null;
  $('modal-back').hidden = false;
  if (opts.confirm) ok.focus();
}

function closeModal() {
  $('modal-back').hidden = true;
  modalOnConfirm = null;
  // Runs on every way out — the button, Escape, a click on the backdrop — so a flow that
  // must notice the dismissal itself (What's new marking itself read) hears all of them.
  var fin = modalOnClose;
  modalOnClose = null;
  if (fin) fin();
}

function accountNameFlow(c) {
  // Names the *account* (login@server), not the terminal: the engine files it under the
  // account key, so the name follows a login to another terminal. Blank clears it.
  var account = c.login + (c.server ? '@' + c.server : '');
  var body = document.createElement('div');
  var note = document.createElement('p');
  note.className = 'muted';
  note.textContent = 'The name is for account ' + account + ' and follows it wherever it is '
    + 'logged in, on any terminal. Leave it blank to clear the name. Names are letters, '
    + 'digits, spaces, - or _ (up to 32).';
  body.appendChild(note);
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'an-name');
  label.textContent = 'Account name';
  var input = document.createElement('input');
  input.id = 'an-name';
  input.autocomplete = 'off';
  input.maxLength = 32;
  input.placeholder = account;
  input.value = c.accountName || '';
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: c.accountName ? 'Rename ' + c.accountName : 'Name this account',
    body: body,
    confirm: 'Save the name',
    onConfirm: function () { tact(api().rename_account(c.tid, input.value)); }
  });
  input.focus();
  input.select();
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

function accountLoginFlow(c) {
  // The TUI's i flow: pick one of the accounts that have connected in this terminal; MT5
  // logs in with the password the terminal itself saved. The login can take a couple of
  // minutes (it starts the terminal), so the card narrates — "logging in…" — until the
  // promise lands in a toast.
  api().account_login_ask(c.tid).then(function (a) {
    if (!a.ok) { toast(a.why, 'bad'); return; }
    pickModal('Log ' + a.name + ' in to', a.accounts.map(function (acct) {
      // One account on one terminal at a time: a row another terminal holds is kept
      // visible — so the list is the whole truth — but greyed and refused with the why.
      var sub = acct.now ? 'logged in now'
        : acct.usedBy ? 'in use by ' + acct.usedBy : '';
      return {
        label: acct.words,
        sub: sub || undefined,
        cls: (acct.now || acct.usedBy) ? 'pick-off' : '',
        fn: function () {
          if (acct.now) {
            toast(a.name + ' is already logged in to ' + acct.words, '');
            return;
          }
          if (acct.usedBy) {
            toast(acct.words + ' is in use by ' + acct.usedBy
              + ': an account is used on one terminal at a time', 'bad');
            return;
          }
          toast(a.name + ': logging in to ' + acct.words + '…', '');
          T.drawn = '';
          pollTerminals();
          tact(api().account_login(c.tid, acct.key));
        }
      };
    }));
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

function ctraderLoginFlow() {
  // The TUI's t flow: the Open API app's details, then the browser does the login and the
  // allowed accounts land under DETECTED. The bridge call blocks for as long as the person
  // takes in the browser, so the start and the outcome each get a toast; asking again
  // while one waits gives the waiting login up first (the bridge's own rule).
  api().ctrader_login_state().then(function (s) {
    var body = document.createElement('div');
    var p = document.createElement('p');
    p.textContent = 'Your browser opens on cTrader’s login. Tick the accounts the '
      + 'copier may use; they appear under DETECTED, and only ever as slaves.';
    body.appendChild(p);
    if (s.loggedIn) {
      var now = document.createElement('p');
      now.className = 'muted';
      now.textContent = 'Logged in now, with ' + s.accounts + ' account'
        + (s.accounts === 1 ? '' : 's') + '. Logging in again picks them afresh.';
      body.appendChild(now);
    }
    if (s.waiting) {
      var waiting = document.createElement('p');
      waiting.className = 'yellow';
      waiting.textContent = 'A login is already waiting in the browser. Logging in again '
        + 'gives it up.';
      body.appendChild(waiting);
    }
    var sub = document.createElement('p');
    sub.className = 'modal-sub';
    sub.textContent = 'The Open API app (openapi.ctrader.com):';
    body.appendChild(sub);

    function field(labelText, id, value, placeholder, password) {
      var wrap = document.createElement('div');
      wrap.className = 'field';
      var label = document.createElement('label');
      label.className = 'flabel';
      label.setAttribute('for', id);
      label.textContent = labelText;
      var input = document.createElement('input');
      input.id = id;
      input.className = 'mono';
      input.autocomplete = 'off';
      input.spellcheck = false;
      if (password) input.type = 'password';
      input.value = value || '';
      if (placeholder) input.placeholder = placeholder;
      wrap.appendChild(label);
      wrap.appendChild(input);
      body.appendChild(wrap);
      return input;
    }
    var idInput = field('Client id', 'ct-client-id', s.clientId);
    var secretInput = field('Secret', 'ct-secret', '',
      s.hasSecret ? 'unchanged — the saved secret is used' : '', true);
    var redirectInput = field('Redirect URI', 'ct-redirect', s.redirectUri);

    var note = document.createElement('p');
    note.className = 'muted small';
    note.textContent = 'The redirect URI must be exactly as registered with the app, on '
      + '127.0.0.1 or localhost. The id and secret are kept encrypted in ctrader.json, '
      + 'not in the config.';
    body.appendChild(note);
    var err = document.createElement('p');
    err.className = 'red small';
    body.appendChild(err);

    function go() {
      var clientId = idInput.value.trim();
      var secret = secretInput.value.trim();
      var redirect = redirectInput.value.trim();
      var problem = null;
      if (!clientId || !(secret || s.hasSecret)) {
        problem = 'The client id and the secret are both needed.';
      } else if (redirect.indexOf('http://127.0.0.1:') !== 0
        && redirect.indexOf('http://localhost:') !== 0) {
        problem = 'The redirect URI must be http://127.0.0.1:<port>/… or localhost.';
      }
      if (problem) {
        // The confirm closed the modal; the same body, values intact, comes back with
        // the problem under the fields.
        err.textContent = problem;
        showModal({ title: 'Log in to cTrader', body: body, confirm: 'Log in', onConfirm: go });
        return;
      }
      toast('Log in to cTrader in your browser…', '', 10000);
      api().ctrader_login(clientId, secret, redirect).then(function (r) {
        toast(r.message, r.ok ? 'ok' : 'bad', 9000);
        T.drawn = '';
        pollTerminals();
      }, function () {
        toast('The engine did not answer; try again.', 'bad');
      });
    }

    showModal({ title: 'Log in to cTrader', body: body, confirm: 'Log in', onConfirm: go });
    (s.clientId ? redirectInput : idInput).focus();
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

// --- the profiles dialog ---------------------------------------------------------------
// The TUI picker, behind the titlebar's profile pill. Every profile is its own window, so
// Open launches a new copier process on that profile; the single-instance lock in that
// process still has the last word. Rename and delete follow the model's own refusals
// (the default keeps its name and cannot be deleted; anything running, this window
// included, must close first).

function profilesFlow() {
  api().profiles_state().then(function (s) {
    var body = document.createElement('div');
    body.className = 'prof-list';
    s.profiles.forEach(function (p) {
      var row = document.createElement('div');
      row.className = 'prof-row';
      var left = document.createElement('div');
      left.className = 'prof-words';
      var name = document.createElement('div');
      name.className = 'prof-name';
      var nm = document.createElement('span');
      nm.textContent = p.name;
      name.appendChild(nm);
      if (p.current) name.appendChild(cardLine('pill blue', 'THIS WINDOW'));
      else if (p.running) name.appendChild(cardLine('pill yellow', 'RUNNING'));
      left.appendChild(name);
      var facts = [];
      facts.push(p.trading ? 'direct trading' : (p.master ? 'master ' + p.master : 'no master'));
      facts.push(p.slaves + ' slave' + (p.slaves === 1 ? '' : 's'));
      facts.push(p.lastOpened);
      if (p.running && !p.current) facts.push('pid ' + p.pid);
      left.appendChild(cardLine('prof-facts muted small', facts.join(' · ')));
      row.appendChild(left);

      var btns = document.createElement('div');
      btns.className = 'prof-btns';
      function btn(label, fn, danger) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = danger ? 'danger' : '';
        b.textContent = label;
        b.addEventListener('click', fn);
        btns.appendChild(b);
      }
      if (!p.current && !p.running) {
        btn('Open', function () {
          closeModal();
          api().profile_open(p.name).then(function (r) {
            toast(r.message, r.ok ? 'ok' : 'bad');
          }, function () {
            toast('The engine did not answer; try again.', 'bad');
          });
        });
      }
      if (!p.isDefault && !p.current && !p.running) {
        btn('Rename', function () { closeModal(); profileRenameFlow(p.name); });
        btn('Delete', function () { closeModal(); profileDeleteFlow(p.name); }, true);
      }
      row.appendChild(btns);
      body.appendChild(row);
    });

    var make = document.createElement('div');
    make.className = 'prof-new';
    var input = document.createElement('input');
    input.placeholder = 'new profile name';
    input.autocomplete = 'off';
    input.spellcheck = false;
    var go = document.createElement('button');
    go.type = 'button';
    go.textContent = 'Make it';
    function createIt() {
      var name = input.value.trim();
      if (!name) return;
      api().profile_create(name).then(function (r) {
        toast(r.message, r.ok ? 'ok' : 'bad');
        if (r.ok) { closeModal(); profilesFlow(); }
      }, function () {
        toast('The engine did not answer; try again.', 'bad');
      });
    }
    go.addEventListener('click', createIt);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') createIt(); });
    make.appendChild(input);
    make.appendChild(go);
    body.appendChild(make);

    var note = document.createElement('p');
    note.className = 'muted small prof-note';
    note.textContent = 'Each profile is a whole copier of its own — config, accounts, '
      + 'guards, setups and history — in its own window. Names are letters, digits, '
      + '- or _ (up to 32).';
    body.appendChild(note);

    showModal({ title: 'Profiles', body: body, cancel: 'Close' });
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

function profileRenameFlow(name) {
  var body = document.createElement('div');
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'prof-rename');
  label.textContent = 'New name';
  var input = document.createElement('input');
  input.id = 'prof-rename';
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.value = name;
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: 'Rename ' + name,
    body: body,
    confirm: 'Rename',
    onConfirm: function () {
      api().profile_rename(name, input.value).then(function (r) {
        toast(r.message, r.ok ? 'ok' : 'bad');
        profilesFlow();
      }, function () {
        toast('The engine did not answer; try again.', 'bad');
      });
    }
  });
  input.focus();
  input.select();
}

function profileDeleteFlow(name) {
  var body = document.createElement('div');
  var p = document.createElement('p');
  p.textContent = 'Everything ' + name + ' keeps goes with it: its config, accounts list, '
    + 'guards, setups, history and logs. Broker accounts and their positions are '
    + 'untouched, but any copies it still tracked are orphaned.';
  body.appendChild(p);
  showModal({
    title: 'Delete ' + name + '?',
    body: body,
    confirm: 'Delete ' + name,
    danger: true,
    onConfirm: function () {
      api().profile_delete(name).then(function (r) {
        toast(r.message, r.ok ? 'ok' : 'bad');
        profilesFlow();
      }, function () {
        toast('The engine did not answer; try again.', 'bad');
      });
    }
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
  form: null,       // { tid, type, ruleId, fields } while the rule form is open
  from: null        // { tid, name } when a terminal page's guard line led here: the way back
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
  // The way back, when a terminal page's guard line led here. Outside the drawn-key
  // gate: it changes with navigation, not with the state the key watches.
  var back = $('guard-back');
  back.hidden = !G.from;
  if (G.from) back.textContent = '← ' + (G.from.name || 'its terminal') + ' — its page';
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
    b.className = 'pick-item' + (o.cls ? ' ' + o.cls : '');
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
function openRuleForm(type, ruleId, preset) {
  api().guard_form(G.tid, type, ruleId).then(function (f) {
    if (!f.ok) { toast(f.message, 'bad'); return; }
    var body = document.createElement('div');
    body.className = 'gform';
    var inputs = {};

    if (f.catalog) {
      // The rule reads the shared symbol catalog, so the form says where that is edited
      // before the fields, not only in the sentence below the fold.
      var catNote = document.createElement('div');
      catNote.className = 'field-note muted gform-catnote';
      catNote.appendChild(document.createTextNode(
        'Which symbol is in which class comes from the shared symbol catalog — '));
      var catLink = document.createElement('button');
      catLink.type = 'button';
      catLink.className = 'linklike';
      catLink.textContent = 'edit it on Settings';
      catLink.addEventListener('click', function () {
        // Remember the form as it stands, so the catalog card can offer the way back and
        // the reopened form holds what was already typed.
        var held = {
          tid: G.tid, type: type, ruleId: ruleId, title: f.title,
          values: collect(), enabled: tick.checked
        };
        closeModal();
        showPage('settings'); // clears RET, so it is set after
        RET = held;
        updateCatBack();
        var card = document.querySelector('#page-settings section[aria-label="Symbol catalog"]');
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      catNote.appendChild(catLink);
      catNote.appendChild(document.createTextNode('.'));
      body.appendChild(catNote);
    }

    f.fields.forEach(function (field) {
      // Coming back from the catalog link, the form resumes with what was typed.
      if (preset && preset.values && preset.values[field.name] !== undefined) {
        field.value = String(preset.values[field.name]);
      }
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
    tick.checked = preset ? !!preset.enabled : f.enabled;
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

// --- the log dock --------------------------------------------------------------------------
// copier.log and the day book merged, docked under whatever page is up: the TUI log page's
// read and filters. The bridge narrows (terminal, problems only, word search) and the dock
// only draws, following the tail while the reader stays at the bottom.
var LG = {
  open: false,
  tid: '',          // '' = all terminals
  problems: false,
  find: '',
  h: 260,           // the dock's height; its top edge drags, and the choice is kept
  live: null,
  drawn: '',
  dd: null,
  ddKey: '',
  everDrawn: false  // first render pins the view to the newest line
};
try {
  LG.open = localStorage.getItem('dt-log-open') === '1';
  LG.problems = localStorage.getItem('dt-log-problems') === '1';
  var lgH = parseInt(localStorage.getItem('dt-log-h'), 10);
  if (isFinite(lgH)) LG.h = lgH;
} catch (e) { /* fine */ }

function logClampH(h) {
  // Never shorter than a few lines, never most of the window.
  return Math.max(120, Math.min(h, Math.round(window.innerHeight * 0.7)));
}

var logAsked = 0;
var logAnswered = 0;
function pollLog(fresh) {
  if (!window.pywebview || !LG.open) return;
  if (!fresh && logAnswered < logAsked) return; // one in flight is enough for a poll
  var stamp = ++logAsked;
  api().log_state(LG.tid, LG.problems, LG.find).then(function (live) {
    if (stamp <= logAnswered) return;
    logAnswered = stamp;
    LG.live = live;
    renderLog();
  }, function () { if (stamp > logAnswered) logAnswered = stamp; });
}

function logFilterChanged() {
  LG.drawn = '';
  pollLog(true);
}

function setLogOpen(open) {
  LG.open = open;
  $('logdock').hidden = !open;
  var btn = $('log-btn');
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  $('log-btn-chev').textContent = open ? '▾' : '▴';
  try { localStorage.setItem('dt-log-open', open ? '1' : ''); } catch (e) { /* fine */ }
  if (open) {
    $('logdock').style.height = logClampH(LG.h) + 'px';
    LG.drawn = '';
    LG.everDrawn = false;
    pollLog(true);
  }
}

function renderLog() {
  var live = LG.live;
  if (!live || !LG.open) return;
  var key = JSON.stringify(live) + '|' + LG.problems;
  if (key === LG.drawn) return;
  LG.drawn = key;

  // The terminal dropdown, rebuilt only when the choices change under it.
  var ddKey = live.terminals.map(function (t) { return t.tid + '|' + t.name; }).join(',');
  if (!LG.dd || LG.ddKey !== ddKey) {
    LG.ddKey = ddKey;
    var choices = [['', 'All terminals']];
    live.terminals.forEach(function (t) { choices.push([t.tid, t.name]); });
    if (!choices.some(function (c) { return c[0] === LG.tid; })) {
      // The picked terminal is gone: widen to everything and ask again at once, so the
      // rows never sit filtered by a terminal the dropdown no longer says.
      LG.tid = '';
      logFilterChanged();
    }
    var dd = makeChoiceDropdown({ value: LG.tid, choices: choices }, function () {
      LG.tid = dd.value();
      logFilterChanged();
    });
    LG.dd = dd;
    var root = $('log-from');
    root.textContent = '';
    root.appendChild(dd.el);
  }

  $('log-problems').checked = LG.problems;
  $('log-problems-label').textContent = 'problems only · ' + live.problems;
  $('log-count').textContent = live.matched > live.rows.length
    ? 'last ' + live.rows.length + ' of ' + live.matched
    : (live.matched ? live.matched + ' lines' : '');

  var rows = $('log-rows');
  var following = !LG.everDrawn
    || rows.scrollTop + rows.clientHeight >= rows.scrollHeight - 8;
  rows.textContent = '';
  live.rows.forEach(function (r) {
    var row = document.createElement('div');
    row.className = 'log-row lg-' + r.tone;
    row.appendChild(cardLine('lg-when', r.when));
    row.appendChild(cardLine('lg-who', r.who));
    row.appendChild(cardLine('lg-text', r.text));
    rows.appendChild(row);
  });
  $('log-empty').hidden = live.rows.length > 0;
  if (following) rows.scrollTop = rows.scrollHeight;
  LG.everDrawn = true;
}

// --- the setups page -------------------------------------------------------------------
// Who copies from whom — the master, its slaves, the account each is logged into, every
// one's settings, copying on or off and the mode — saved to put back later: the TUI w
// page as a desktop table. What a setup holds and what loading one does are the
// engine's; this page asks and draws, and every load is agreed to first.
var W = {
  live: null,
  drawn: '',
  selected: '',   // the selected setup's name
  loading: ''     // the setup being loaded, while the bridge logs terminals in and applies it
};

var setupsBusy = false;
function pollSetups() {
  if (setupsBusy || !window.pywebview || S.page !== 'setups') return;
  setupsBusy = true;
  api().setups_state().then(function (live) {
    setupsBusy = false;
    W.live = live;
    renderSetups();
  }, function () { setupsBusy = false; });
}

function wact(promise) {
  promise.then(function (r) {
    toast(r.message, r.ok ? 'ok' : 'bad');
    W.drawn = '';
    pollSetups();
  }, function () {
    toast('The engine did not answer; try again.', 'bad');
  });
}

function selSetup() {
  var live = W.live;
  if (!live) return null;
  return live.setups.filter(function (s) { return s.name === W.selected; })[0] || null;
}

function renderSetups() {
  var live = W.live;
  if (!live) return;
  var key = JSON.stringify(live) + '|' + W.selected + '|' + W.loading;
  if (key === W.drawn) return;
  W.drawn = key;

  // The NOW strip: the roles in place, and whether they are a saved setup.
  var roles = $('setups-now-roles');
  var state = $('setups-now-state');
  state.textContent = '';
  if (live.now === null) {
    roles.textContent = live.direct
      ? 'no slaves assigned — drag terminals into the SLAVES zone on Terminals'
      : 'no master assigned — drag one into the MASTER zone on Terminals';
    roles.className = 'yellow';
  } else {
    roles.textContent = live.now;
    roles.className = '';
    if (live.current) {
      var dot = document.createElement('span');
      dot.className = 'strow-dot';
      dot.textContent = '●';
      state.appendChild(dot);
      state.appendChild(document.createTextNode('setup ' + live.current));
      state.className = 'small green';
    } else {
      state.textContent = 'not saved as a setup';
      state.className = 'small yellow';
    }
  }
  var note = $('setups-now-note');
  note.hidden = !live.changed.length;
  if (live.changed.length) {
    note.textContent = 'settings changed since saved on ' + live.changed.join(', ')
      + ' — Update with current keeps the change, loading puts the saved ones back';
  }
  $('st-save').disabled = live.now === null || !!W.loading || !!live.loading;

  $('setups-count').textContent = live.setups.length
    ? live.setups.length + ' saved' : '';

  if (!live.setups.some(function (s) { return s.name === W.selected; })) W.selected = '';
  var rows = $('setups-rows');
  rows.textContent = '';
  live.setups.forEach(function (s) { rows.appendChild(setupRow(s)); });
  $('setups-empty').hidden = !!live.setups.length;

  var picked = selSetup();
  var busy = !!W.loading || !!live.loading; // ours, or a load the bridge is still running
  $('st-load').textContent = busy ? 'Loading…' : 'Load';
  $('st-load').disabled = !picked || busy || !!(picked && picked.problems.length);
  $('st-update').disabled = !picked || busy || live.now === null;
  $('st-rename').disabled = !picked || busy;
  $('st-delete').disabled = !picked || busy;
}

function setupRow(s) {
  var row = document.createElement('div');
  row.className = 'strow' + (s.name === W.selected ? ' selected' : '');
  row.setAttribute('role', 'row');

  function cell(build) {
    var el = document.createElement('span');
    el.setAttribute('role', 'cell');
    build(el);
    row.appendChild(el);
  }

  cell(function (el) {
    el.className = 'strow-name';
    if (s.current) {
      var dot = document.createElement('span');
      dot.className = 'strow-dot';
      dot.textContent = '● ';
      dot.title = 'the roles in place now';
      el.appendChild(dot);
    }
    el.appendChild(document.createTextNode(s.name));
  });
  cell(function (el) { el.textContent = s.master; el.title = s.master; });
  cell(function (el) { el.textContent = s.slaves; el.title = s.slaves; });
  cell(function (el) {
    var on = document.createElement('span');
    on.className = s.copying ? 'green' : 'muted';
    on.textContent = s.copying ? 'on' : 'off';
    el.appendChild(on);
    // Which mode the setup puts back deserves a glance, not a dialog's small print.
    if (s.dryRun === false) {
      var hot = document.createElement('span');
      hot.className = 'red';
      hot.textContent = ' · LIVE';
      el.appendChild(hot);
    } else if (s.dryRun === true) {
      var dry = document.createElement('span');
      dry.className = 'muted';
      dry.textContent = ' · dry run';
      el.appendChild(dry);
    }
  });
  cell(function (el) { el.className = 'muted'; el.textContent = s.saved; });

  if (s.problems.length) {
    var why = document.createElement('div');
    why.className = 'strow-note';
    why.textContent = 'cannot load: ' + s.problems.join('; ');
    row.appendChild(why);
  }

  row.addEventListener('click', function () {
    W.selected = s.name;
    W.drawn = '';
    renderSetups();
  });
  row.addEventListener('dblclick', function () {
    if (!s.problems.length) loadSetupFlow(s.name);
  });
  return row;
}

function setupNameModal(title, confirmWord, initial, fn) {
  var body = document.createElement('div');
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'st-name');
  label.textContent = 'Setup name';
  var input = document.createElement('input');
  input.id = 'st-name';
  input.autocomplete = 'off';
  input.value = initial || '';
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: title,
    body: body,
    confirm: confirmWord,
    onConfirm: function () { fn(input.value); }
  });
  input.focus();
  input.select();
}

function saveSetupFlow() {
  var live = W.live;
  if (!live || live.direct || live.now === null || W.loading) return;
  setupNameModal('Save the roles in place now as', 'Save it', '', function (name) {
    wact(api().setup_save(name));
  });
}

function updateSetupFlow() {
  var s = selSetup();
  var live = W.live;
  if (!s || !live || live.now === null || W.loading) return;
  confirmModal(
    'Update ' + s.name + '?',
    s.name + ' is saved again as the roles in place now: ' + live.now + '.',
    'Update it', false,
    function () { wact(api().setup_update(s.name)); }
  );
}

function renameSetupFlow() {
  var s = selSetup();
  if (!s || W.loading) return;
  setupNameModal('Rename ' + s.name + ' to', 'Rename it', s.name, function (name) {
    wact(api().setup_rename(s.name, name));
  });
}

function deleteSetupFlow() {
  var s = selSetup();
  if (!s || W.loading) return;
  confirmModal(
    'Delete ' + s.name + '?',
    'The saved setup is forgotten. The roles in place now do not change.',
    'Delete it', true,
    function () { wact(api().setup_delete(s.name)); }
  );
}

function loadSetupFlow(name) {
  if (W.loading) return;
  api().setup_load_ask(name).then(function (a) {
    if (!a.ok) { toast(a.message, a.already ? '' : 'bad'); return; }

    var body = document.createElement('div');
    var p = document.createElement('p');
    p.textContent = 'The roles become: ' + a.changes + '.';
    body.appendChild(p);

    var stake = a.stake || [];
    var agreed = null;
    var total = 0;
    if (stake.length) {
      agreed = {};
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
    }
    if (a.live && !a.goesLive) {
      var hot = document.createElement('p');
      hot.className = 'red';
      hot.textContent = 'Copying starts LIVE: orders will be sent to the slaves.';
      body.appendChild(hot);
    }
    showModal({
      title: 'Load setup ' + name + '?',
      body: body,
      confirm: total
        ? 'Load — orphan ' + total + (total === 1 ? ' copy' : ' copies')
        : 'Load it',
      danger: !!total || a.live,
      onConfirm: function () {
        if (a.goesLive) goLiveModal(name, a, agreed);
        else commitSetupLoad(name, a, agreed, false);
      }
    });
  }, function () { toast('The engine did not answer; try again.', 'bad'); });
}

function goLiveModal(name, a, agreed) {
  // The copier's own gate, as the TUI's GoLiveDialog: a dry run turns into live copying
  // only with LIVE typed; the engine refuses the load without it.
  var body = document.createElement('div');
  var p1 = document.createElement('p');
  p1.textContent = 'Setup ' + name + ' was saved copying live'
    + (a.copying
      ? ', with copying on: it starts as soon as the setup is loaded.'
      : ', with copying off: nothing is sent until copying is resumed.');
  body.appendChild(p1);
  if (a.slaves.length) {
    var p2 = document.createElement('p');
    p2.className = 'red';
    p2.textContent = 'Real orders will be sent to: ' + a.slaves.join(', ') + '.';
    body.appendChild(p2);
  }
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'st-live-word');
  label.textContent = 'Type LIVE to load it live';
  var input = document.createElement('input');
  input.id = 'st-live-word';
  input.autocomplete = 'off';
  input.spellcheck = false;
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: 'Go live?',
    body: body,
    confirm: 'Load it LIVE',
    danger: true,
    onConfirm: function () { commitSetupLoad(name, a, agreed, true); }
  });
  var ok = $('modal-confirm');
  ok.disabled = true;
  input.addEventListener('input', function () {
    ok.disabled = input.value.trim() !== 'LIVE';
  });
  input.focus();
}

function commitSetupLoad(name, a, agreed, goLive) {
  W.loading = name;
  W.drawn = '';
  renderSetups();
  if (a.switching.length) {
    toast('Logging ' + a.switching.join(', ') + ' in first — this can take a minute…', '', 12000);
  }
  function back() {
    W.loading = '';
    W.drawn = '';
    pollSetups();
  }
  api().setup_load(name, agreed, goLive).then(function (r) {
    back();
    (r.messages || []).forEach(function (m) {
      toast(m.text, m.tone, m.tone === 'ok' ? 4500 : 9000);
    });
  }, function () {
    back();
    toast('The engine did not answer; try again.', 'bad');
  });
}

// --- the settings page -----------------------------------------------------------------
// The copier's own settings — the TUI g form as cards. What a value means, its bounds and
// the two gates on the mode (copying stopped, LIVE typed) are general_edit's on the bridge;
// this fills the fields, asks as they change, and never overwrites typing: the form refills
// from the engine only while it holds no edits.
var C = {
  live: null,      // last settings_state() answer
  base: null,      // what the fields were last filled from, as a key; null forces a refill
  serverKey: '',   // the engine's values at that fill, to notice an outside change
  saving: false
};

var CFG_INPUTS = ['cfg-poll', 'cfg-stale', 'cfg-retry', 'cfg-rescan', 'cfg-grace', 'cfg-hist'];
var CFG_NAMES = ['profile_mode', 'dry_run', 'poll_interval_ms', 'heartbeat_timeout_s',
  'reconnect_backoff_s', 'rescan_interval_s', 'join_grace_seconds', 'close_terminals',
  'history_days'];

function segValue(id) {
  var btn = document.querySelector('#' + id + ' button[aria-pressed="true"]');
  return btn ? btn.dataset.value : '';
}

function setSeg(id, value) {
  document.querySelectorAll('#' + id + ' button').forEach(function (b) {
    b.setAttribute('aria-pressed', String(b.dataset.value === value));
  });
}

function cfgCollect() {
  var raw = {
    dry_run: segValue('cfg-dry') === 'dry',
    close_terminals: segValue('cfg-closing')
  };
  CFG_INPUTS.forEach(function (id) { raw[$(id).dataset.name] = $(id).value; });
  return raw;
}

function cfgKeyNow() {
  return JSON.stringify([cfgCollect(), segValue('cfg-profile-mode')]);
}

function cfgDirty() {
  return C.base !== null && cfgKeyNow() !== C.base;
}

function cfgFill(live) {
  var v = live.values;
  CFG_INPUTS.forEach(function (id) { $(id).value = String(v[$(id).dataset.name]); });
  setSeg('cfg-dry', v.dry_run ? 'dry' : 'live');
  setSeg('cfg-closing', v.close_terminals);
  setSeg('cfg-profile-mode', live.profileMode);
  C.base = cfgKeyNow();
  C.serverKey = JSON.stringify([live.values, live.profileMode]);
  cfgShowErrors({});
  $('cfg-changes').textContent = '';
}

function cfgShowErrors(errors) {
  CFG_NAMES.forEach(function (name) {
    $('cfg-err-' + name).textContent = errors[name] || '';
  });
  $('cfg-err-general').textContent = errors[''] || '';
}

var settingsBusy = false;
var cfgGen = 0; // bumped when a save lands: an answer asked before it is stale and re-asked
function pollSettings() {
  if (settingsBusy || !window.pywebview || S.page !== 'settings') return;
  settingsBusy = true;
  var gen = cfgGen;
  api().settings_state().then(function (live) {
    settingsBusy = false;
    if (gen !== cfgGen) { pollSettings(); return; }
    C.live = live;
    renderSettings();
  }, function () { settingsBusy = false; });
}

function renderSettings() {
  var live = C.live;
  if (!live) return;
  $('cfg-profile').textContent = 'profile ' + live.profile;

  // The line above the fields: what is true right now, in the TUI form's own words.
  var nowEl = $('cfg-mode-now');
  nowEl.textContent = '';
  var word = document.createElement('span');
  word.className = 'mode-word' + (live.dryRun ? '' : ' red');
  word.textContent = live.dryRun ? 'DRY RUN' : 'LIVE COPYING';
  nowEl.appendChild(word);
  var said = document.createElement('span');
  said.className = 'muted';
  said.textContent = live.dryRun
    ? ' · copies are only simulated now.'
    : ' · orders are being sent now.';
  nowEl.appendChild(said);
  var gate = document.createElement('span');
  gate.className = live.copying ? 'yellow' : 'muted';
  gate.textContent = live.copying
    ? ' Copying is running: halt it (the title bar) before changing the mode.'
    : ' Going live asks you to type LIVE.';
  nowEl.appendChild(gate);

  // A refill never lands on typing: only on the first show, after a save or revert, or
  // when the engine's truth moved (the TUI changed something, a setup loaded) while the
  // form holds no edits of its own.
  var serverKey = JSON.stringify([live.values, live.profileMode]);
  if (C.base === null || (!cfgDirty() && serverKey !== C.serverKey)) cfgFill(live);

  $('cfg-save').textContent = C.saving ? 'Saving…' : 'Save';
  $('cfg-save').disabled = C.saving;
  $('cfg-revert').disabled = C.saving;
}

// --- the symbol catalog card ------------------------------------------------------------
// Its own save, apart from the copier's whole-or-nothing form: the catalog is shared by
// every profile, so it lands whole on its own button and never rides along with a profile's
// settings. The same refill rule as the form above: never onto typing.
var CAT = { live: null, base: null, serverKey: '', saving: false };
var CAT_CLASSES = ['indices', 'metals', 'energies', 'crypto', 'stocks'];

function catCollect() {
  var out = { currencies: $('cat-currencies').value, classes: {} };
  CAT_CLASSES.forEach(function (c) { out.classes[c] = $('cat-' + c).value; });
  return out;
}

function catDirty() {
  return CAT.base !== null && JSON.stringify(catCollect()) !== CAT.base;
}

function catGrow(el) {
  // The lists are long and scrolling two visible rows hides what is in a class, so the
  // field takes the height its content needs.
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 2 + 'px';
}

function catFill(live) {
  $('cat-currencies').value = live.currencies;
  CAT_CLASSES.forEach(function (c) { $('cat-' + c).value = (live.classes || {})[c] || ''; });
  ['currencies'].concat(CAT_CLASSES).forEach(function (c) { catGrow($('cat-' + c)); });
  CAT.base = JSON.stringify(catCollect());
  catShowErrors({});
}

function catShowErrors(errors) {
  ['currencies'].concat(CAT_CLASSES).forEach(function (c) {
    $('cat-err-' + c).textContent = errors[c] || '';
  });
}

var catBusy = false;
function pollCatalog() {
  if (catBusy || !window.pywebview || S.page !== 'settings') return;
  catBusy = true;
  api().catalog_state().then(function (live) {
    catBusy = false;
    CAT.live = live;
    var key = JSON.stringify(live);
    if (CAT.base === null || (!catDirty() && key !== CAT.serverKey)) {
      catFill(live);
      CAT.serverKey = key;
    }
    $('cat-save').textContent = CAT.saving ? 'Saving…' : 'Save catalog';
    $('cat-save').disabled = CAT.saving;
    $('cat-revert').disabled = CAT.saving;
  }, function () { catBusy = false; });
}

function catSaveFlow() {
  if (CAT.saving || !CAT.live) return;
  CAT.saving = true;
  var held = catCollect();
  api().catalog_save(held.currencies, held.classes).then(function (r) {
    CAT.saving = false;
    if (!r.ok) {
      catShowErrors(r.errors || {});
      if (r.message) toast(r.message, 'bad');
      pollCatalog();
      return;
    }
    toast(r.message, 'ok');
    CAT.base = null; // the next answer refills from what was saved
    pollCatalog();
  }, function () {
    CAT.saving = false;
    toast('The engine did not answer; try again.', 'bad');
    pollCatalog();
  });
}

var cfgCheckTimer = null;
function cfgChanged() {
  cfgShowErrors({});
  clearTimeout(cfgCheckTimer);
  cfgCheckTimer = setTimeout(function () {
    if (S.page !== 'settings' || !C.live) return;
    api().settings_check(cfgCollect()).then(function (r) {
      cfgShowErrors(r.errors || {});
      var parts = [];
      if (r.changes) parts.push('Saves: ' + r.changes);
      if (C.live && segValue('cfg-profile-mode') !== C.live.profileMode) {
        parts.push(segValue('cfg-profile-mode') === 'trading'
          ? 'this profile becomes direct trading'
          : 'this profile goes back to copying a master');
      }
      if (r.goesLive) parts.push('going live asks you to type LIVE');
      $('cfg-changes').textContent = parts.join(' · ');
    });
  }, 250);
}

function saveSettingsFlow(typed) {
  if (C.saving || !C.live) return;
  C.saving = true;
  renderSettings();
  api().settings_save(cfgCollect(), segValue('cfg-profile-mode'),
    typed === undefined ? null : typed
  ).then(function (r) {
    C.saving = false;
    if (r.needLive) { renderSettings(); cfgGoLiveModal(r); return; }
    if (!r.ok) {
      renderSettings();
      cfgShowErrors(r.errors || {});
      if (r.message) toast(r.message, 'bad');
      return;
    }
    (r.messages || []).forEach(function (m) {
      toast(m.text, m.tone, m.tone === 'warn' || m.tone === 'bad' ? 9000 : 4500);
    });
    cfgGen++;
    C.base = null; // the next answer refills the form from what was saved
    pollSettings();
  }, function () {
    C.saving = false;
    renderSettings();
    toast('The engine did not answer; try again.', 'bad');
  });
}

function cfgGoLiveModal(r) {
  // The copier's own gate, the TUI GoLiveDialog: a dry run turns into live copying only
  // with LIVE typed, and the bridge checks the word again before anything is saved.
  var body = document.createElement('div');
  var p1 = document.createElement('p');
  p1.textContent = 'Live, every trade the master opens is sent as a real order '
    + (r.slaves && r.slaves.length
      ? 'to each connected slave (' + r.slaves.join(', ') + ')'
      : 'to each slave, once there are some')
    + ': opens, closes, and stop or target moves. A slave on a real-money account stays '
    + 'held back unless live trading is allowed on it.';
  body.appendChild(p1);
  var p2 = document.createElement('p');
  p2.textContent = 'Copying is halted, and stays halted: nothing is sent until you resume '
    + 'it. Going back to a dry run is one choice in these settings.';
  body.appendChild(p2);
  var field = document.createElement('div');
  field.className = 'field';
  var label = document.createElement('label');
  label.className = 'flabel';
  label.setAttribute('for', 'st-live-word');
  label.textContent = 'Type LIVE to switch';
  var input = document.createElement('input');
  input.id = 'st-live-word';
  input.autocomplete = 'off';
  input.spellcheck = false;
  field.appendChild(label);
  field.appendChild(input);
  body.appendChild(field);
  showModal({
    title: 'Send real orders?',
    body: body,
    confirm: 'Go live',
    danger: true,
    onConfirm: function () { saveSettingsFlow(input.value); }
  });
  var ok = $('modal-confirm');
  ok.disabled = true;
  input.addEventListener('input', function () {
    ok.disabled = input.value.trim() !== 'LIVE';
  });
  input.focus();
}

function revertSettingsFlow() {
  if (!C.live || C.saving) return;
  cfgFill(C.live);
  toast('Put back as saved.', '');
}

// --- the terminal detail page ----------------------------------------------------------
// One terminal's own page, opened by clicking its card on Terminals: the TUI s form for a
// slave, the m form for the master. What a value means and that an edit keeps open copies
// are the bridge's; this fills fields, asks as they change (the worked example comes back
// with each check), and refills from the engine only while the form holds no edits.
var D = {
  tid: '',
  tab: 'overview', // which tab is up: 'overview' (the TUI enter page) or 'settings'
  live: null,
  ov: null,        // last terminal_overview_state() answer
  ovDrawn: '',     // what the overview was last built from, so a quiet poll redraws nothing
  base: null,      // what the fields were last filled from, as a key; null forces a refill
  serverKey: '',
  gen: 0,          // bumped when a save lands, so a stale in-flight poll answer is re-asked
  saving: false
};

var TD_INPUTS = ['risk_percent', 'risk_cash', 'commission_per_lot_round_turn',
  'slippage_buffer_points', 'max_lot', 'max_deviation_points', 'no_stop_lot_multiple',
  'breakeven_trigger_r', 'breakeven_book_percent', 'symbol_prefix', 'symbol_suffix',
  'symbol_map', 'symbol_whitelist', 'symbol_blacklist'];
var TD_TICKS = ['master_manages', 'copy_pending', 'pending_fallback'];
var TD_SLIDERS = ['risk_percent', 'breakeven_trigger_r', 'breakeven_book_percent'];
var TD_ERRS = TD_INPUTS.concat(TD_TICKS,
  ['sizing_mode', 'invert', 'no_stop_policy', 'breakeven_mode']);
var TD_M_INPUTS = ['breakeven_trigger_r', 'breakeven_book_percent',
  'symbol_prefix', 'symbol_suffix'];
var TD_M_SLIDERS = ['breakeven_trigger_r', 'breakeven_book_percent'];
var TD_M_ERRS = TD_M_INPUTS.concat(['management', 'breakeven_mode']);

function openTerminalDetail(tid, tab, focusTicket) {
  D.tid = tid;
  D.live = null;
  D.ov = null;
  D.ovDrawn = '';
  D.base = null;
  D.serverKey = '';
  // A ticket to pick out of the positions table on arrival — the book's copy links
  // carry the slave's own ticket, and the row flashes once, exactly as a new row does.
  D.focusTicket = focusTicket || 0;
  tdShowTab(tab || 'overview', true);
  showPage('tdetail');
}

function tdShowTab(name, opening) {
  D.tab = name;
  $('td-tab-overview').setAttribute('aria-selected', String(name === 'overview'));
  $('td-tab-settings').setAttribute('aria-selected', String(name === 'settings'));
  $('td-overview').hidden = name !== 'overview';
  $('td-settings').hidden = name !== 'settings';
  // The copy-settings-from dropdown belongs to the form; renderTdetail brings it back.
  if (name !== 'settings') $('td-copyfrom').hidden = true;
  D.ovDrawn = '';
  if (!opening) pollTdetail();
}

function tdIsSlave() { return D.live && D.live.role === 'slave'; }

function tdCollect() {
  var raw = {};
  TD_INPUTS.forEach(function (n) { raw[n] = $('td-f-' + n).value; });
  TD_TICKS.forEach(function (n) { raw[n] = $('td-f-' + n).checked; });
  raw.sizing_mode = segValue('td-f-sizing_mode');
  raw.no_stop_policy = segValue('td-f-no_stop_policy');
  raw.breakeven_mode = segValue('td-f-breakeven_mode');
  raw.invert = segValue('td-f-invert') === 'opposite';
  return raw;
}

function tdmCollect() {
  var raw = {};
  TD_M_INPUTS.forEach(function (n) { raw[n] = $('td-m-' + n).value; });
  raw.management = segValue('td-m-management');
  raw.breakeven_mode = segValue('td-m-breakeven_mode');
  return raw;
}

function tdKeyNow() {
  if (!D.live) return '';
  return JSON.stringify(tdIsSlave() ? tdCollect() : tdmCollect());
}

function tdDirty() {
  return D.base !== null && tdKeyNow() !== D.base;
}

function tdSlider(prefix, name) {
  // The slider is the coarse hand on the text box's value; the box stays the truth.
  // A value typed past the slider's usual range stretches the range to hold it, so the
  // thumb always sits at the real value and sliding back starts from there — never a
  // jump from what was typed down to a pegged maximum. Back inside, the range snaps back.
  var slider = $(prefix + 'sl-' + name);
  var v = parseFloat($(prefix + (prefix === 'td-m-' ? '' : 'f-') + name).value);
  if (!isFinite(v)) return;
  if (!slider.dataset.max) {
    slider.dataset.max = slider.max;
    slider.dataset.min = slider.min;
  }
  slider.max = String(Math.max(parseFloat(slider.dataset.max), v));
  slider.min = String(Math.min(parseFloat(slider.dataset.min), Math.max(v, 0)));
  slider.value = String(v);
}

function tdFillSlave(live) {
  var v = live.values;
  TD_INPUTS.forEach(function (n) { $('td-f-' + n).value = String(v[n]); });
  TD_TICKS.forEach(function (n) { $('td-f-' + n).checked = !!v[n]; });
  setSeg('td-f-sizing_mode', v.sizing_mode);
  setSeg('td-f-no_stop_policy', v.no_stop_policy);
  setSeg('td-f-breakeven_mode', v.breakeven_mode);
  setSeg('td-f-invert', v.invert ? 'opposite' : 'same');
  TD_SLIDERS.forEach(function (n) { tdSlider('td-', n); });
}

function tdFillMaster(live) {
  var v = live.values;
  TD_M_INPUTS.forEach(function (n) { $('td-m-' + n).value = String(v[n]); });
  setSeg('td-m-management', v.management);
  setSeg('td-m-breakeven_mode', v.breakeven_mode);
  TD_M_SLIDERS.forEach(function (n) { tdSlider('td-m-', n); });
}

function tdFill(live) {
  if (live.role === 'slave') tdFillSlave(live);
  else if (live.role === 'master') tdFillMaster(live);
  D.base = tdKeyNow();
  D.serverKey = JSON.stringify(live.values || null);
  tdShowErrors({});
  $('td-changes').textContent = '';
  $('td-m-changes').textContent = '';
  tdShowParts();
}

function tdShowErrors(errors) {
  TD_ERRS.forEach(function (n) {
    var el = $('td-err-' + n);
    if (el) el.textContent = errors[n] || '';
  });
  TD_M_ERRS.forEach(function (n) {
    var el = $('td-m-err-' + n);
    if (el) el.textContent = errors[n] || '';
  });
  $('td-err-general').textContent = errors[''] || '';
  $('td-m-err-general').textContent = errors[''] || '';
}

function tdShowParts() {
  var live = D.live;
  if (!live) return;
  if (live.role === 'slave') {
    var percent = segValue('td-f-sizing_mode') === 'percent_of_balance';
    $('td-risk-field').hidden = !percent;
    $('td-cash-field').hidden = percent;
    $('td-multiple-field').hidden = segValue('td-f-no_stop_policy') !== 'lot_multiple';
    var be = segValue('td-f-breakeven_mode') === 'r';
    $('td-trigger-field').hidden = !be;
    $('td-book-field').hidden = !be;
    // Both gates open — the master set to manage and this slave letting it — grey the
    // slave's own breakeven and say why, the TUI form's own behaviour.
    var managed = !!live.masterSays && $('td-f-master_manages').checked;
    $('td-follow-note').hidden = !!live.masterSays;
    $('td-be-note').hidden = !managed;
    ['td-f-breakeven_mode', 'td-trigger-field', 'td-book-field'].forEach(function (id) {
      $(id).classList.toggle('td-disabled', managed);
    });
  } else if (live.role === 'master') {
    var on = segValue('td-m-breakeven_mode') === 'r';
    $('td-m-trigger-field').hidden = !on;
    $('td-m-book-field').hidden = !on;
    $('td-m-mgmt-says').textContent = segValue('td-m-management') === 'master'
      ? 'Slaves follow the master’s stop and target moves and partial closes, and their '
        + 'own breakeven is off. A slave can still keep its own trades, on its settings. '
        + 'Applies to trades opened from now on.'
      : 'Slaves copy only opens and closes, then manage the trade themselves with their own '
        + 'breakeven and booking; the master’s stop moves and partial closes are not '
        + 'copied. Applies to trades opened from now on.';
    var says = $('td-m-be-says');
    says.className = 'field-note muted';
    if (!on) {
      says.textContent = 'Off: the master is only read from.';
    } else {
      says.textContent = 'The copier will trade the master: once a trade is that far in '
        + 'profit it moves its stop past entry by the trade’s commission (doubled for '
        + 'the way out), once and never looser, and closes the share above. Only while '
        + 'copying is live.';
      if (live.accountKind !== 'demo' && !(live.card && live.card.allowLive)) {
        says.className = 'field-note red';
        says.textContent += ' This master is not known to be a demo account, so its worker '
          + 'refuses to trade it until live trading is allowed on it.';
      }
    }
  }
}

function tdRenderExample(ex) {
  if (!ex) return;
  $('td-ex-lead').textContent = ex.lead || '';
  var working = $('td-ex-working');
  working.textContent = ex.working || '';
  if (ex.result) {
    var r = document.createElement('span');
    r.className = 'ex-result';
    r.textContent = ' ' + ex.result;
    working.appendChild(r);
  }
  $('td-ex-note').textContent = ex.note || '';
}

function tdRenderSymex(rows) {
  if (!rows) return;
  var out = $('td-m-symex');
  out.textContent = '';
  rows.forEach(function (row, i) {
    if (i) out.appendChild(cardLine('muted', '  ·  '));
    out.appendChild(cardLine('', row.symbol));
    if (row.plain !== row.symbol) {
      out.appendChild(cardLine('muted', ' → '));
      out.appendChild(cardLine('green', row.plain));
    } else {
      out.appendChild(cardLine('muted', ' as it is'));
    }
  });
}

// --- the detail page's Overview tab ---------------------------------------------------
// The TUI enter page: three ruled columns — the ACCOUNT, the RULE it is copied by (on the
// master: the slaves it feeds), and how COPYING has gone this session — then the open
// positions and resting orders. The bridge sends words and tones for anything worded and
// raw numbers for anything the page formats (money, prices, R). Display only, end to end.

function ovRow(label, value, tone) {
  var row = document.createElement('div');
  row.className = 'ov-row';
  var l = document.createElement('span');
  l.className = 'ov-label';
  l.textContent = label;
  row.appendChild(l);
  var v = document.createElement('span');
  v.className = 'ov-value' + (tone ? ' ' + tone : '');
  if (value && value.nodeType) v.appendChild(value);
  else v.textContent = value;
  row.appendChild(v);
  return row;
}

function ovThousands(v) {
  return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function ovPrice(v, digits) { return v ? v.toFixed(digits) : '—'; }

function ovLots(v) { return String(parseFloat(v.toFixed(8))); }

function ovGauge(a) {
  var wrap = document.createElement('span');
  wrap.className = 'pop-gauge';
  var bar = document.createElement('span');
  bar.className = 'mbar';
  var fill = document.createElement('span');
  var tier = { good: 'blue', warning: 'yellow', serious: 'red', critical: 'red' }[a.levelTier] || 'muted';
  fill.className = 'mbar-fill ' + tier;
  fill.style.width = a.usedFraction === null || a.usedFraction === undefined
    ? '0%' : Math.min(100, Math.round(a.usedFraction * 100)) + '%';
  bar.appendChild(fill);
  wrap.appendChild(bar);
  wrap.appendChild(document.createTextNode(a.usedFraction === null || a.usedFraction === undefined
    ? '—' : Math.round(a.usedFraction * 100) + '%'));
  return wrap;
}

function renderOvBand(live) {
  var c = live.card || {};
  $('ov-equity').textContent = c.equity === null || c.equity === undefined ? '—' : money(c.equity);
  var pl = c.floatingPl;
  var f = $('ov-floating');
  f.textContent = (pl === null || pl === undefined ? '—' : (pl >= 0 ? '+' : '') + money(pl))
    + ' floating';
  f.className = 'mono small ' + (pl > 0 ? 'green' : pl < 0 ? 'red' : 'muted');
  var conn = [];
  if (live.connected) conn.push('connected ' + live.connected);
  if (live.ping) conn.push('ping ' + live.ping);
  $('ov-conn').textContent = conn.join(' · ');
  var acct = $('ov-acct');
  acct.textContent = '';
  var bits = [];
  if (c.login) bits.push({ text: String(c.login) });
  if (c.server) bits.push({ text: c.server });
  if (live.accountKind === 'demo') bits.push({ text: 'demo' });
  else if (live.accountKind === 'live-allowed' || live.accountKind === 'live-held') {
    bits.push({ text: 'LIVE', cls: 'red' });
  }
  if (live.leverage) bits.push({ text: '1:' + live.leverage });
  if (!bits.length) { acct.textContent = 'account not read yet'; return; }
  bits.forEach(function (b, i) {
    if (i) acct.appendChild(document.createTextNode(' · '));
    acct.appendChild(b.cls ? cardLine(b.cls, b.text) : document.createTextNode(b.text));
  });
}

function renderOvAccount(a) {
  var box = $('ov-account');
  box.textContent = '';
  if (!a) {
    box.appendChild(ovRow('Account', 'not connected yet', 'muted'));
    return;
  }
  box.appendChild(ovRow('Balance', money(a.balance)));
  box.appendChild(ovRow('Floating', a.floating === null ? '—' : money(a.floating),
    a.floating !== null && a.floating < 0 ? 'red' : ''));
  box.appendChild(ovRow('Equity', money(a.equity), 'strong'));
  box.appendChild(ovRow('Margin in use', money(a.margin)));
  box.appendChild(ovRow('Margin free', money(a.marginFree)));
  box.appendChild(ovRow('Margin used', ovGauge(a)));
  var tier = { good: 'blue', warning: 'yellow', serious: 'red', critical: 'red' }[a.levelTier] || 'muted';
  if (a.levelPct === null || a.levelPct === undefined) {
    box.appendChild(ovRow('Margin level', a.noMargin ? 'no margin in use' : '—', 'muted'));
  } else {
    box.appendChild(ovRow('Margin level', ovThousands(a.levelPct) + '%', tier));
  }
}

function renderOvMiddle(live) {
  var box = $('ov-rule');
  box.textContent = '';
  var edit = $('ov-rule-edit');
  if (live.copiedTo) {
    $('ov-rule-title').textContent = 'Copied to';
    var n = live.copiedTo.slaves.length;
    $('ov-rule-right').textContent = n + ' slave' + (n === 1 ? '' : 's') + ' · ' + live.copiedTo.who;
    edit.hidden = true;
    if (!n) { box.appendChild(ovRow('', 'no slaves yet', 'muted')); return; }
    live.copiedTo.slaves.forEach(function (s) {
      var word = s.card
        ? stateWord(s.card, { copying: S.live ? S.live.copying : false, dryRun: live.dryRun })
        : { text: 'not connected', cls: 'muted' };
      box.appendChild(ovRow(s.name, word.text, word.cls));
    });
    return;
  }
  $('ov-rule-title').textContent = 'Rule';
  $('ov-rule-right').textContent = '';
  edit.hidden = live.role !== 'slave';
  (live.rule || []).forEach(function (r) {
    box.appendChild(ovRow(r.label, r.text, r.tone));
  });
}

function ovSpark(latencies) {
  var wrap = $('ov-spark-wrap');
  if (!latencies || latencies.length < 2) { wrap.hidden = true; return; }
  wrap.hidden = false;
  var max = Math.max.apply(null, latencies);
  var pts = latencies.map(function (v, i) {
    var x = i / (latencies.length - 1) * 100;
    var y = 34 - (max > 0 ? (v / max) * 30 : 0);
    return x.toFixed(2) + ',' + y.toFixed(2);
  }).join(' ');
  $('ov-spark').innerHTML = '<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
    + 'vector-effect="non-scaling-stroke" points="' + pts + '"></polyline>';
}

function renderOvThird(live) {
  var box = $('ov-copying');
  box.textContent = '';
  var note = $('ov-copy-note');
  if (live.copying) {
    $('ov-copy-title').textContent = 'Copying';
    $('ov-copy-right').textContent = 'this session';
    live.copying.rows.forEach(function (r) { box.appendChild(ovRow(r.label, r.text, r.tone)); });
    note.hidden = true;
    ovSpark(live.copying.latencies);
  } else {
    $('ov-copy-title').textContent = 'Connection';
    $('ov-copy-right').textContent = 'this session';
    ((live.connection && live.connection.rows) || []).forEach(function (r) {
      box.appendChild(ovRow(r.label, r.text, r.tone));
    });
    note.textContent = live.connection ? live.connection.note : '';
    note.hidden = !live.connection;
    $('ov-spark-wrap').hidden = true;
  }
}

function ovCell(tr, text, cls) {
  var td = document.createElement('td');
  if (cls) td.className = cls;
  td.textContent = text;
  tr.appendChild(td);
  return td;
}

function ovHead(table, cols) {
  var thead = table.tHead;
  thead.textContent = '';
  var tr = document.createElement('tr');
  cols.forEach(function (c) {
    var th = document.createElement('th');
    th.textContent = c.label;
    if (c.num) th.className = 'num';
    tr.appendChild(th);
  });
  thead.appendChild(tr);
}

function renderOvPositions(live) {
  var p = live.positions;
  var slave = live.role === 'slave';
  var focusSeen = false;
  $('ov-pos-title').textContent = 'Positions'
    + (p ? (p.unreadable ? ' · could not be read' : ' · ' + p.rows.length) : '');
  $('ov-exposure').textContent = 'net exposure: ' + (p && p.exposure ? p.exposure : '—');
  var note = $('ov-pos-note');
  var scroll = $('ov-pos-scroll');
  if (!p || p.unreadable || !p.rows.length) {
    scroll.hidden = true;
    note.hidden = false;
    note.className = (p && p.unreadable ? 'red' : 'muted') + ' small';
    note.textContent = !p ? 'Not connected yet.'
      : p.unreadable ? 'The open positions could not be read.' : 'Nothing open.';
    return;
  }
  scroll.hidden = false;
  note.hidden = true;
  var cols = [
    { key: 'symbol', label: 'Symbol' },
    { key: 'side', label: 'Side' },
    { key: 'lots', label: 'Lots', num: true },
    { key: 'open', label: 'Open', num: true },
    { key: 'now', label: 'Now', num: true },
    { key: 'stop', label: 'Stop', num: true },
    { key: 'target', label: 'Target', num: true },
    { key: 'r', label: 'R', num: true },
    { key: 'swap', label: 'Swap', num: true },
    { key: 'pl', label: 'P/L', num: true }
  ];
  if (slave) cols.push({ key: 'be', label: 'Breakeven' });
  cols.push({ key: 'link', label: live.role === 'master' ? 'On slaves' : 'Copy of' });
  var table = $('ov-pos-table');
  ovHead(table, cols);
  var body = table.tBodies[0];
  body.textContent = '';
  p.rows.forEach(function (r) {
    var tr = document.createElement('tr');
    if (D.focusTicket && r.ticket === D.focusTicket) {
      tr.className = 'row-new';
      focusSeen = true;
    }
    ovCell(tr, r.symbol, 'sym');
    ovCell(tr, r.side, 'side-' + r.side);
    ovCell(tr, ovLots(r.lots), 'num');
    ovCell(tr, ovPrice(r.open, r.digits), 'num');
    ovCell(tr, r.now === null ? '—' : ovPrice(r.now, r.digits), 'num');
    ovCell(tr, ovPrice(r.sl, r.digits), 'num' + (r.sl ? (r.slHit ? ' red' : '') : ' muted'));
    ovCell(tr, ovPrice(r.tp, r.digits), 'num' + (r.tp ? (r.tpHit ? ' red' : '') : ' muted'));
    var rtd = document.createElement('td');
    rtd.className = 'num';
    if (r.rNow === null || r.rNow === undefined) {
      rtd.textContent = '—';
      rtd.classList.add('muted');
    } else {
      var rn = document.createElement('span');
      rn.className = r.rNow < 0 ? 'red' : r.rNow > 0 ? 'blue' : 'muted';
      rn.textContent = (r.rNow >= 0 ? '+' : '') + r.rNow.toFixed(2) + 'R';
      rtd.appendChild(rn);
      if (r.rTarget !== null && r.rTarget !== undefined) {
        var rt = document.createElement('span');
        rt.className = 'muted';
        rt.textContent = '/' + r.rTarget.toFixed(2) + 'R';
        rtd.appendChild(rt);
      }
    }
    tr.appendChild(rtd);
    ovCell(tr, r.swap === null ? '—' : money(r.swap), 'num muted');
    ovCell(tr, r.profit === null ? '—' : money(r.profit),
      'num' + (r.profit !== null && r.profit < 0 ? ' red' : ''));
    if (slave) {
      var be = r.breakeven;
      ovCell(tr, be ? be.text : '', be && be.tone ? be.tone : '');
    }
    tr.appendChild(ovLinkCell(r));
    body.appendChild(tr);
  });
  if (focusSeen) {
    var flashed = body.querySelector('tr.row-new');
    if (flashed && flashed.scrollIntoView) flashed.scrollIntoView({ block: 'nearest' });
  }
  // Once the table has drawn, the hunt is over either way: a later redraw must not
  // flash the row again, nor wait forever for a ticket that is not here.
  D.focusTicket = 0;
  if (p.total !== null && p.total !== undefined) {
    var totalRow = document.createElement('tr');
    totalRow.className = 'ov-total';
    cols.forEach(function (c) {
      if (c.key === 'symbol') ovCell(totalRow, 'Total', '');
      else if (c.key === 'pl') {
        ovCell(totalRow, money(p.total), 'num' + (p.total < 0 ? ' red' : ''));
      } else ovCell(totalRow, '', '');
    });
    body.appendChild(totalRow);
  }
}

function ovLinkCell(r) {
  // "Copy of #25": in a direct profile the #25 leads back to the Trading page's book,
  // the mirror of the book's copies menu leading here. A copy-mode profile's master
  // ticket lives in a real terminal, so there is nowhere in the app to jump to.
  var td = document.createElement('td');
  if (r.copyOf && S.live && S.live.direct) {
    var lk = document.createElement('button');
    lk.type = 'button';
    lk.className = 'linklike';
    lk.textContent = r.link.text;
    lk.title = 'Back to this ticket on the Trading page';
    lk.addEventListener('click', function () { jumpToBook(r.copyOf); });
    td.appendChild(lk);
  } else {
    td.textContent = r.link.text;
    td.className = r.link.tone || '';
  }
  return td;
}

function renderOvOrders(live) {
  var o = live.orders;
  var panel = $('ov-ord-panel');
  if (!o || (!o.unreadable && !o.rows.length)) { panel.hidden = true; return; }
  panel.hidden = false;
  $('ov-ord-title').textContent = 'Resting orders' + (o.unreadable ? '' : ' · ' + o.rows.length);
  var note = $('ov-ord-note');
  var scroll = $('ov-ord-scroll');
  note.hidden = !o.unreadable;
  scroll.hidden = o.unreadable;
  if (o.unreadable) return;
  var slave = live.role === 'slave';
  var cols = [
    { key: 'symbol', label: 'Symbol' },
    { key: 'type', label: 'Type' },
    { key: 'lots', label: 'Lots', num: true },
    { key: 'price', label: 'Price', num: true }
  ];
  if (o.anyStopLimit) cols.push({ key: 'limit', label: 'Then limit', num: true });
  cols.push({ key: 'stop', label: 'Stop', num: true });
  cols.push({ key: 'target', label: 'Target', num: true });
  cols.push({ key: 'placed', label: 'Placed', num: true });
  if (slave) cols.push({ key: 'link', label: 'Copy of' });
  var table = $('ov-ord-table');
  ovHead(table, cols);
  var body = table.tBodies[0];
  body.textContent = '';
  o.rows.forEach(function (r) {
    var tr = document.createElement('tr');
    ovCell(tr, r.symbol, 'sym');
    ovCell(tr, r.kind, 'side-' + r.side);
    ovCell(tr, ovLots(r.lots), 'num');
    ovCell(tr, ovPrice(r.price, r.digits), 'num');
    if (o.anyStopLimit) {
      ovCell(tr, ovPrice(r.stopLimit, r.digits), 'num' + (r.stopLimit ? '' : ' muted'));
    }
    ovCell(tr, ovPrice(r.sl, r.digits), 'num' + (r.sl ? '' : ' muted'));
    ovCell(tr, ovPrice(r.tp, r.digits), 'num' + (r.tp ? '' : ' muted'));
    ovCell(tr, r.placed || '—', 'num muted');
    if (slave && r.link) tr.appendChild(ovLinkCell(r));
    body.appendChild(tr);
  });
}

function renderTdOverview(live) {
  // Redraw only when something changed, so a quiet poll leaves the DOM alone.
  var key = JSON.stringify(live) + '|' + (S.live ? S.live.copying : '');
  if (key === D.ovDrawn) return;
  D.ovDrawn = key;
  renderOvBand(live);
  renderOvAccount(live.account);
  renderOvMiddle(live);
  renderOvThird(live);
  renderOvPositions(live);
  renderOvOrders(live);
}

var tdetailBusy = false;
function pollTdetail() {
  if (tdetailBusy || !window.pywebview || S.page !== 'tdetail' || !D.tid) return;
  tdetailBusy = true;
  var tid = D.tid;
  if (D.tab === 'overview') {
    api().terminal_overview_state(tid).then(function (live) {
      tdetailBusy = false;
      if (S.page !== 'tdetail' || D.tab !== 'overview' || tid !== D.tid) return;
      if (!live.ok) {
        toast(live.message, 'bad');
        showPage('terminals');
        return;
      }
      D.ov = live;
      renderTdHead(live);
      renderTdOverview(live);
    }, function () { tdetailBusy = false; });
    return;
  }
  var gen = D.gen;
  api().terminal_detail_state(tid).then(function (live) {
    tdetailBusy = false;
    if (gen !== D.gen) { pollTdetail(); return; }
    if (tid !== D.tid || D.tab !== 'settings') return;
    if (!live.ok) {
      toast(live.message, 'bad');
      showPage('terminals');
      return;
    }
    D.live = live;
    renderTdetail();
  }, function () { tdetailBusy = false; });
}

var TD_KIND = {
  unread: { text: 'account not read yet', cls: 'small muted' },
  demo: { text: 'demo account', cls: 'small muted' },
  'live-allowed': { text: 'not a demo: live trading allowed', cls: 'small red' },
  'live-held': { text: 'not a demo: held back', cls: 'small red' }
};

function renderTdHead(live) {
  // The page header above the tabs: both tabs' answers carry the same fields.
  $('td-name').textContent = live.name;

  var word = live.card
    ? stateWord(live.card, { copying: S.live ? S.live.copying : false, dryRun: live.dryRun })
    : { text: 'not connected', cls: 'muted' };
  $('td-status').textContent = word.text;
  $('td-status').className = word.cls;
  var dotTone = word.cls === 'green' || word.cls === 'blue' ? 'green'
    : word.cls === 'yellow' ? 'yellow' : word.cls === 'muted' ? '' : 'red';
  $('td-dot').className = 'dot ' + dotTone;

  $('td-pill').textContent = live.dryRun ? 'DRY RUN' : 'LIVE';
  $('td-pill').className = 'pill ' + (live.dryRun ? 'yellow' : 'red');
  var kind = TD_KIND[live.accountKind] || TD_KIND.unread;
  $('td-kind').textContent = kind.text;
  $('td-kind').className = kind.cls;

  var guard = live.card && live.card.guard;
  var gl = $('td-guard-link');
  gl.hidden = !guard;
  if (guard) {
    gl.textContent = guard.rules + ' guard rule' + (guard.rules === 1 ? '' : 's')
      + (guard.text ? ' · ' + guard.text : '') + ' — Guards page →';
    gl.className = 'linklike small ' + (guard.tone || '');
  }
}

function renderTdetail() {
  var live = D.live;
  if (!live) return;
  renderTdHead(live);

  var tdNoRole = live.role !== 'slave' && live.role !== 'master';
  $('td-other').hidden = !tdNoRole;
  var tdLogin = $('td-login-btn');
  tdLogin.hidden = !(tdNoRole && live.card && live.card.canSwitch && !live.card.loggingIn);
  if (!tdLogin.hidden) {
    tdLogin.textContent = live.card.login
      ? 'Log in to a different account…' : 'Log in to an account…';
  }
  $('td-slave').hidden = live.role !== 'slave';
  $('td-master').hidden = live.role !== 'master';
  $('td-copyfrom').hidden = !(live.role === 'slave' && (live.copyFrom || []).length);

  if (live.role === 'slave') {
    var stake = $('td-stake');
    stake.textContent = live.stake ? live.stake.text : '';
    stake.className = (live.stake && live.stake.tone === 'red' ? 'red' : 'muted') + ' small';
    tdLiveCard(live);
  }

  // A refill never lands on typing: first show, after a save or revert, or when the
  // engine's truth moved while the form holds no edits of its own.
  var serverKey = JSON.stringify(live.values || null);
  if (D.base === null || (!tdDirty() && serverKey !== D.serverKey)) {
    tdFill(live);
    if (live.role === 'slave') tdRenderExample(live.example);
    else if (live.role === 'master') tdRenderSymex(live.symbolExample);
  }

  var save = live.role === 'master' ? $('td-m-save') : $('td-save');
  save.textContent = D.saving ? 'Saving…' : 'Save';
  save.disabled = D.saving;
  (live.role === 'master' ? $('td-m-revert') : $('td-revert')).disabled = D.saving;
}

function tdLiveCard(live) {
  var said = $('td-live-said');
  var btn = $('td-live-btn');
  var allowed = !!(live.card && live.card.allowLive);
  btn.hidden = true;
  if (allowed) {
    said.textContent = 'Live trading is allowed on this terminal: real orders are sent to '
      + 'it whenever copying is live, whatever its account is.';
    said.className = 'small red';
    btn.hidden = false;
    btn.textContent = 'Turn the override off';
    btn.className = 'plain-btn';
  } else if (live.accountKind === 'live-held') {
    said.textContent = 'Not a demo account: the demo-only guard holds its orders back.';
    said.className = 'small yellow';
    btn.hidden = false;
    btn.textContent = 'Allow live trading…';
    btn.className = 'danger-btn';
  } else if (live.accountKind === 'demo') {
    said.textContent = 'A demo account: orders go through, nothing real is at risk.';
    said.className = 'small muted';
  } else {
    said.textContent = 'The account has not been read yet, so the demo-only guard holds '
      + 'orders back for now.';
    said.className = 'small muted';
  }
}

var tdCheckTimer = null;
function tdChanged() {
  tdShowParts();
  clearTimeout(tdCheckTimer);
  tdCheckTimer = setTimeout(function () {
    if (S.page !== 'tdetail' || !D.live) return;
    var slave = tdIsSlave();
    api().terminal_settings_check(D.tid, slave ? tdCollect() : tdmCollect())
      .then(function (r) {
        tdShowErrors(r.errors || {});
        var line = r.changes ? 'Saves: ' + r.changes : '';
        $(slave ? 'td-changes' : 'td-m-changes').textContent = line;
        if (slave) tdRenderExample(r.example);
        else tdRenderSymex(r.symbolExample);
      });
  }, 250);
}

function saveTdetailFlow() {
  if (D.saving || !D.live) return;
  var slave = tdIsSlave();
  D.saving = true;
  renderTdetail();
  api().terminal_settings_save(D.tid, slave ? tdCollect() : tdmCollect()).then(function (r) {
    D.saving = false;
    if (!r.ok) {
      renderTdetail();
      tdShowErrors(r.errors || {});
      if (r.message) toast(r.message, 'bad');
      return;
    }
    (r.messages || []).forEach(function (m) {
      toast(m.text, m.tone, m.tone === 'warn' || m.tone === 'bad' ? 9000 : 4500);
    });
    D.gen++;
    D.base = null; // the next answer refills the form from what was saved
    pollTdetail();
  }, function () {
    D.saving = false;
    renderTdetail();
    toast('The engine did not answer; try again.', 'bad');
  });
}

function revertTdetailFlow() {
  if (!D.live || D.saving) return;
  tdFill(D.live);
  if (tdIsSlave()) tdRenderExample(D.live.example);
  else if (D.live.role === 'master') tdRenderSymex(D.live.symbolExample);
  toast('Put back as saved.', '');
}

function tdCopyFromPicked(src) {
  api().terminal_settings_from(src.tid).then(function (r) {
    if (!r.ok) { toast(r.message, 'bad'); return; }
    var v = r.values;
    TD_INPUTS.forEach(function (n) {
      if (n in v) $('td-f-' + n).value = String(v[n]);
    });
    TD_TICKS.forEach(function (n) { if (n in v) $('td-f-' + n).checked = !!v[n]; });
    setSeg('td-f-sizing_mode', String(v.sizing_mode));
    setSeg('td-f-no_stop_policy', String(v.no_stop_policy));
    setSeg('td-f-breakeven_mode', String(v.breakeven_mode));
    setSeg('td-f-invert', v.invert ? 'opposite' : 'same');
    TD_SLIDERS.forEach(function (n) { tdSlider('td-', n); });
    tdChanged();
    toast('Filled from ' + r.name + '; its symbol prefix, suffix and renames are its '
      + 'broker’s, so this slave keeps its own. Nothing is saved yet.', '', 7000);
  }, function () { toast('The engine did not answer; try again.', 'bad'); });
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

// --- the symbol search dropdown ------------------------------------------------------------
// The ticket's symbol field searches the feed broker's own catalog, which its worker pushes
// once per connection. Matches come back as clean base names — what every slave resolves
// from — prefix matches first. No catalog yet (feed still connecting) means no dropdown,
// and the field stays the free-text input it always was.
var SD = { gen: 0, sel: -1, items: [] };

function symbolSuggest() {
  var gen = ++SD.gen;
  var typed = $('f-symbol').value.trim();
  api().symbol_search(typed).then(function (r) {
    if (gen !== SD.gen) return; // a later keystroke already asked
    var items = r.symbols || [];
    // The one exact match is already chosen; a dropdown repeating it only blinks.
    if (!items.length || (items.length === 1 && items[0] === typed.toUpperCase())) {
      symbolDropHide();
      return;
    }
    SD.items = items;
    SD.sel = -1;
    var drop = $('symbol-drop');
    drop.textContent = '';
    items.forEach(function (name, i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('role', 'option');
      b.textContent = name;
      b.addEventListener('click', function () { symbolPick(name); });
      drop.appendChild(b);
    });
    drop.hidden = false;
    $('f-symbol').setAttribute('aria-expanded', 'true');
  }, function () { /* the engine will answer the next keystroke */ });
}

function symbolDropHide() {
  SD.sel = -1;
  SD.items = [];
  $('symbol-drop').hidden = true;
  $('f-symbol').setAttribute('aria-expanded', 'false');
}

function symbolPick(name) {
  var input = $('f-symbol');
  input.value = name;
  symbolDropHide();
  input.dispatchEvent(new Event('input')); // the one handler does the rest
  SD.gen++; // and its re-suggest is discarded: a picked name needs no list
  symbolDropHide();
}

function symbolDropKey(ev) {
  if ($('symbol-drop').hidden) return;
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    var step = ev.key === 'ArrowDown' ? 1 : -1;
    SD.sel = (SD.sel + step + SD.items.length) % SD.items.length;
    var kids = $('symbol-drop').children;
    for (var i = 0; i < kids.length; i++) kids[i].classList.toggle('sel', i === SD.sel);
    if (kids[SD.sel]) kids[SD.sel].scrollIntoView({ block: 'nearest' });
  } else if (ev.key === 'Enter' && SD.sel >= 0) {
    ev.preventDefault();
    symbolPick(SD.items[SD.sel]);
  } else if (ev.key === 'Escape') {
    symbolDropHide();
  }
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
    symbolSuggest();
  });
  $('f-symbol').addEventListener('focus', symbolSuggest);
  $('f-symbol').addEventListener('blur', function () {
    // After the click on an option lands; mousedown on the drop keeps it from closing early.
    setTimeout(symbolDropHide, 150);
  });
  $('f-symbol').addEventListener('keydown', symbolDropKey);
  $('symbol-drop').addEventListener('mousedown', function (ev) { ev.preventDefault(); });
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
      else if (ev.target.id === 'st-name' || ev.target.id === 'st-live-word'
               || ev.target.id === 'an-name') {
        var ok = $('modal-confirm');
        if (!ok.hidden && !ok.disabled) ok.click();
      }
      else if (ev.target.id.indexOf('cfg-') === 0 && $('modal-back').hidden) saveSettingsFlow();
      else if (ev.target.id.indexOf('td-') === 0 && $('modal-back').hidden) saveTdetailFlow();
    }
    if (ev.key === 'Escape') {
      closeDropdowns();
      if (!$('modal-back').hidden) { closeModal(); return; }
      if (!$('ctx-menu').hidden) { closeMenu(); return; }
      if (S.page === 'tdetail') { showPage('terminals'); return; }
      if (S.page === 'guards' && G.from) {
        var from = G.from;
        G.from = null;
        openTerminalDetail(from.tid);
        return;
      }
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

  $('profile-btn').addEventListener('click', profilesFlow);
  $('flatten-btn').addEventListener('click', flattenFlow);
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
      // Arriving at Guards by the sidebar is arriving fresh: the way back to a
      // terminal's page belongs only to the visit its guard line started.
      if (a.dataset.page === 'guards') G.from = null;
      showPage(a.dataset.page);
    });
  });

  // --- terminals page ---
  $('ctrader-btn').addEventListener('click', ctraderLoginFlow);
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
  // Close on a click on the backdrop itself -- but only when the press began there too.
  // Selecting text in a field can end with the pointer over the backdrop, and the click
  // event then lands on it (the common ancestor); that is a selection, not a dismissal.
  var modalPressOnBack = false;
  $('modal-back').addEventListener('pointerdown', function (ev) {
    modalPressOnBack = ev.target === $('modal-back');
  });
  $('modal-back').addEventListener('click', function (ev) {
    if (ev.target === $('modal-back') && modalPressOnBack) closeModal();
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

  // --- the log dock ---
  $('log-btn').addEventListener('click', function () { setLogOpen(!LG.open); });
  $('log-close').addEventListener('click', function () { setLogOpen(false); });
  var grip = $('logdock-grip');
  grip.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    grip.classList.add('held');
    grip.setPointerCapture(ev.pointerId);
    var startY = ev.clientY, startH = $('logdock').offsetHeight;
    function move(e) {
      LG.h = logClampH(startH + (startY - e.clientY));
      $('logdock').style.height = LG.h + 'px';
    }
    function up() {
      grip.classList.remove('held');
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      grip.removeEventListener('pointercancel', up);
      try { localStorage.setItem('dt-log-h', String(LG.h)); } catch (e2) { /* fine */ }
    }
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
    grip.addEventListener('pointercancel', up);
  });
  $('log-problems').addEventListener('change', function () {
    LG.problems = $('log-problems').checked;
    try { localStorage.setItem('dt-log-problems', LG.problems ? '1' : ''); } catch (e) { /* fine */ }
    logFilterChanged();
  });
  var logFindTimer = null;
  $('log-find').addEventListener('input', function () {
    clearTimeout(logFindTimer);
    logFindTimer = setTimeout(function () {
      var now = $('log-find').value.trim();
      if (now === LG.find) return;
      LG.find = now;
      logFilterChanged();
    }, 250);
  });

  // --- setups page ---
  $('st-save').addEventListener('click', saveSetupFlow);
  $('st-load').addEventListener('click', function () {
    var s = selSetup();
    if (s) loadSetupFlow(s.name);
  });
  $('st-update').addEventListener('click', updateSetupFlow);
  $('st-rename').addEventListener('click', renameSetupFlow);
  $('st-delete').addEventListener('click', deleteSetupFlow);

  // --- settings page ---
  CFG_INPUTS.forEach(function (id) {
    $(id).addEventListener('input', cfgChanged);
  });
  ['cfg-profile-mode', 'cfg-dry', 'cfg-closing'].forEach(function (id) {
    $(id).addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-value]');
      if (!btn) return;
      setSeg(id, btn.dataset.value);
      cfgChanged();
    });
  });
  $('cfg-save').addEventListener('click', function () { saveSettingsFlow(); });
  $('cat-save').addEventListener('click', function () { catSaveFlow(); });
  $('cat-revert').addEventListener('click', function () { if (CAT.live) catFill(CAT.live); });
  $('cat-back').addEventListener('click', function () {
    var r = RET;
    if (!r) return;
    if (r.tid) G.tid = r.tid;
    showPage('guards');
    openRuleForm(r.type, r.ruleId, r);
  });
  ['currencies'].concat(CAT_CLASSES).forEach(function (c) {
    $('cat-' + c).addEventListener('input', function () {
      catShowErrors({});
      catGrow(this);
    });
  });
  $('cfg-revert').addEventListener('click', revertSettingsFlow);

  // --- terminal detail page ---
  $('td-back').addEventListener('click', function () { showPage('terminals'); });
  $('td-tab-overview').addEventListener('click', function () { tdShowTab('overview'); });
  $('td-tab-settings').addEventListener('click', function () { tdShowTab('settings'); });
  $('ov-rule-edit').addEventListener('click', function () { tdShowTab('settings'); });
  $('td-login-btn').addEventListener('click', function () {
    if (D.tid) accountLoginFlow({ tid: D.tid });
  });
  TD_INPUTS.forEach(function (n) {
    $('td-f-' + n).addEventListener('input', function () {
      if (TD_SLIDERS.indexOf(n) >= 0) tdSlider('td-', n);
      tdChanged();
    });
  });
  TD_M_INPUTS.forEach(function (n) {
    $('td-m-' + n).addEventListener('input', function () {
      if (TD_M_SLIDERS.indexOf(n) >= 0) tdSlider('td-m-', n);
      tdChanged();
    });
  });
  TD_SLIDERS.forEach(function (n) {
    $('td-sl-' + n).addEventListener('input', function () {
      $('td-f-' + n).value = $('td-sl-' + n).value;
      tdChanged();
    });
  });
  TD_M_SLIDERS.forEach(function (n) {
    $('td-m-sl-' + n).addEventListener('input', function () {
      $('td-m-' + n).value = $('td-m-sl-' + n).value;
      tdChanged();
    });
  });
  TD_TICKS.forEach(function (n) {
    $('td-f-' + n).addEventListener('change', tdChanged);
  });
  ['td-f-sizing_mode', 'td-f-no_stop_policy', 'td-f-breakeven_mode', 'td-f-invert',
    'td-m-management', 'td-m-breakeven_mode'].forEach(function (id) {
    $(id).addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-value]');
      if (!btn) return;
      setSeg(id, btn.dataset.value);
      tdChanged();
    });
  });
  $('td-save').addEventListener('click', saveTdetailFlow);
  $('td-revert').addEventListener('click', revertTdetailFlow);
  $('td-m-save').addEventListener('click', saveTdetailFlow);
  $('td-m-revert').addEventListener('click', revertTdetailFlow);
  $('td-guard-link').addEventListener('click', function () {
    if (!D.tid) return;
    G.tid = D.tid;
    G.from = { tid: D.tid, name: D.live ? D.live.name : '', tab: D.tab };
    G.drawn = '';
    showPage('guards');
  });
  $('guard-back').addEventListener('click', function () {
    var from = G.from;
    G.from = null;
    if (from) openTerminalDetail(from.tid, from.tab);
  });
  $('td-live-btn').addEventListener('click', function () {
    var live = D.live;
    if (!live || !live.card) return;
    liveOverrideFlow({ tid: D.tid, name: live.name }, !live.card.allowLive);
  });
  var cfBtn = document.querySelector('#td-copyfrom .dd-btn');
  var cfMenu = $('td-copyfrom-menu');
  cfBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var opening = cfMenu.hidden;
    closeDropdowns();
    if (!opening) return;
    cfMenu.textContent = '';
    ((D.live && D.live.copyFrom) || []).forEach(function (src) {
      var opt = document.createElement('button');
      opt.type = 'button';
      opt.setAttribute('role', 'option');
      opt.textContent = src.name;
      opt.addEventListener('click', function (e) {
        e.stopPropagation();
        cfMenu.hidden = true;
        tdCopyFromPicked(src);
      });
      cfMenu.appendChild(opt);
    });
    cfMenu.hidden = false;
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

// The build's own words about itself: every release's notes, newest first, in the one
// modal. Opened from the version label on the status bar, the Help page's link — and once
// by itself on the first launch of a new version, where any way of dismissing it marks the
// notes read, so it never nags. VER is filled once at start; the build cannot change under
// a running window.
var VER = { notes: [], unseen: false };

function whatsNewFlow() {
  var body = document.createElement('div');
  VER.notes.forEach(function (n) {
    var head = document.createElement('div');
    head.className = 'notes-ver';
    head.textContent = 'v' + n.version;
    var when = document.createElement('span');
    when.className = 'muted';
    when.textContent = ' · ' + n.date;
    head.appendChild(when);
    body.appendChild(head);
    var ul = document.createElement('ul');
    ul.className = 'notes-lines';
    n.lines.forEach(function (line) {
      var li = document.createElement('li');
      li.textContent = line;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  });
  showModal({
    title: 'What’s new',
    body: body,
    cancel: 'Close',
    onClose: function () {
      if (!VER.unseen) return;
      VER.unseen = false;
      $('ver-dot').hidden = true;
      api().notes_seen().catch(function () {});
    }
  });
}

function start() {
  // Once, not polled: the build cannot change under a running window. A source run says
  // so, because it works against the dev data root, not the installed app's.
  api().version_state().then((v) => {
    if (!v || !v.version) return;
    const el = document.getElementById('help-sub');
    if (el) el.append(` · v${v.version}${v.dev ? ' · running from source (dev data)' : ''}`);
    VER.notes = v.notes || [];
    VER.unseen = !!v.unseen;
    $('ver-num').textContent = 'v' + v.version;
    $('ver-dot').hidden = !VER.unseen;
    $('ver-btn').hidden = false;
    if (VER.unseen) whatsNewFlow();
  }).catch(() => {});
  $('ver-btn').addEventListener('click', whatsNewFlow);
  $('help-notes').addEventListener('click', whatsNewFlow);
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
  setInterval(pollSetups, 1000);
  setInterval(pollSettings, 1000);
  // The catalog changes rarely and by hand; two seconds notices another window's save.
  setInterval(pollCatalog, 2000);
  setInterval(pollTdetail, 1000);
  // The log dock sits under every page, so its poll follows being open, not the page;
  // two seconds is the TUI log page's own pace.
  if (LG.open) setLogOpen(true);
  setInterval(pollLog, 2000);
}

if (window.pywebview) start();
else window.addEventListener('pywebviewready', start);
