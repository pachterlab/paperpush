// Loaded in <head> before first paint, so a dark page never flashes light: the
// author's choice (viewer.js's toggle), else the system's. A file rather than
// an inline <script> so the Content-Security-Policy can forbid inline script.
(function () {
  var t = null;
  try { t = localStorage.getItem("paperpush:theme"); } catch (e) { /* storage unavailable */ }
  if (t !== "light" && t !== "dark") t = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
})();
