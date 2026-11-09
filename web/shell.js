/* Mark native content before paint; keep browser presentation independent. */
if (new URLSearchParams(location.search).get("desktop") === "1") document.documentElement.classList.add("native-shell");

try { if (localStorage.getItem("ag-tasks.sidebar-collapsed") === "1") document.documentElement.classList.add("sidebar-collapsed"); } catch { /* Storage is optional. */ }
