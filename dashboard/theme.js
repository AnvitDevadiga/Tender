/* Loaded synchronously in <head> on every Tender page so a stored dark-theme
 * choice is applied before first paint (no flash). Light is the default for a
 * new visitor; the operating-system preference is not consulted. External
 * file because the dashboard CSP forbids inline scripts. */
(function () {
  try {
    if (window.localStorage.getItem("tender.theme") === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    }
  } catch (error) {
    /* storage unavailable: stay light */
  }
})();
