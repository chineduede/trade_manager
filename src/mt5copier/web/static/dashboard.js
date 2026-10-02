// Log panel auto-scroll: stick to the bottom while the viewer hasn't scrolled away, so new
// lines (appearing via htmx's own polling, every 3s) are visible without the viewer having to
// keep scrolling down themselves. Plain MutationObserver + a native scroll listener -- no
// dependency on htmx's own swap-lifecycle events, same reasoning as the rest of this app's JS:
// this session has no way to open a real browser and confirm their exact payload shape, so it
// leans on stable standard DOM APIs that can be reasoned about directly instead.
(function () {
  "use strict";

  var NEAR_BOTTOM_PX = 24;
  var log = document.getElementById("log-panel");
  if (!log) return;

  var stickToBottom = true;

  function isNearBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < NEAR_BOTTOM_PX;
  }

  log.addEventListener("scroll", function () {
    stickToBottom = isNearBottom();
  });

  // htmx's innerHTML swap replaces #log-panel's children, never the element itself, so this
  // observer keeps watching the same live node for as long as the page is open.
  new MutationObserver(function () {
    if (stickToBottom) {
      log.scrollTop = log.scrollHeight;
    }
  }).observe(log, { childList: true, subtree: true, characterData: true });
})();
