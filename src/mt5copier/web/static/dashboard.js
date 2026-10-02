// Stage A dashboard behavior: log auto-scroll with a DOM-growth cap, and a brief highlight on
// anything htmx just swapped in. Vanilla JS, no framework, no build step -- consistent with
// everything else on this page. Both effects use plain MutationObserver rather than htmx's own
// swap-lifecycle events: the exact payload shape of htmx's internal events wasn't something
// this could be checked against a real browser while writing it, whereas MutationObserver's
// contract (addedNodes on a childList mutation) is a stable, standard DOM API.
(function () {
  "use strict";

  var LOG_MAX_CHILDREN = 400; // bounds DOM growth on a long-running session
  var NEAR_BOTTOM_PX = 24; // "stuck to the bottom" tolerance, in pixels
  var FLASH_MS = 700;

  var log = document.getElementById("log-tail");

  function isNearBottom() {
    if (!log) return true;
    return log.scrollHeight - log.scrollTop - log.clientHeight < NEAR_BOTTOM_PX;
  }

  if (log) {
    new MutationObserver(function () {
      var stuck = isNearBottom();
      while (log.children.length > LOG_MAX_CHILDREN) {
        log.removeChild(log.firstChild);
      }
      if (stuck) {
        log.scrollTop = log.scrollHeight;
      }
    }).observe(log, { childList: true });
  }

  // Briefly highlight any element htmx just swapped in out-of-band (a terminal row, the health
  // bar) so an update away from where the viewer is looking is still noticeable. The log panel
  // is excluded -- every log append would otherwise flash the whole panel on every tick.
  var flashTimers = new WeakMap();
  function flash(el) {
    if (!el || (log && (el === log || (log.contains && log.contains(el))))) return;
    el.classList.add("flash");
    window.clearTimeout(flashTimers.get(el));
    flashTimers.set(
      el,
      window.setTimeout(function () {
        el.classList.remove("flash");
      }, FLASH_MS)
    );
  }

  new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(function (node) {
        if (node.nodeType === 1 && node.id) {
          flash(node);
        }
      });
    });
  }).observe(document.body, { childList: true, subtree: true });
})();
