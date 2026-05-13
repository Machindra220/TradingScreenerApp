let sortState = JSON.parse(localStorage.getItem("sortState") || "[]");

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    alert(`Copied: ${text}`);
  });
}
// function copyToClipboard(id) {
//   const el = document.getElementById(id);
//   if (el) {
//     navigator.clipboard.writeText(el.textContent);
//   }
// }



function toggleSort(key) {
  const header = document.querySelector(`th[onclick="toggleSort('${key}')"]`);
  const currentOrder = header.getAttribute("data-sort");
  const newOrder = currentOrder === "asc" ? "desc" : "asc";
  header.setAttribute("data-sort", newOrder);

  // Update icon
  header.innerHTML = header.innerHTML.replace(/▲|▼/, newOrder === "asc" ? "▼" : "▲");

  // Update sortState
  sortState = sortState.filter(s => s.key !== key);
  sortState.unshift({ key, order: newOrder });

  localStorage.setItem("sortState", JSON.stringify(sortState));
  applyMultiSort();
  highlightSortedColumns();
}

function applyMultiSort() {
  const rows = Array.from(document.querySelectorAll("tbody tr"));
  rows.sort((a, b) => {
    for (const { key, order } of sortState) {
      const valA = parseFloat(a.querySelector(`[data-key="${key}"]`).textContent) || 0;
      const valB = parseFloat(b.querySelector(`[data-key="${key}"]`).textContent) || 0;
      if (valA !== valB) {
        return order === "asc" ? valA - valB : valB - valA;
      }
    }
    return 0;
  });

  const tbody = document.querySelector("tbody");
  tbody.innerHTML = "";
  rows.forEach(row => tbody.appendChild(row));
}

function highlightSortedColumns() {
  document.querySelectorAll("th.sortable").forEach(th => th.classList.remove("bg-yellow-100"));
  sortState.forEach(({ key }) => {
    const th = document.querySelector(`th[onclick="toggleSort('${key}')"]`);
    if (th) th.classList.add("bg-yellow-100");
  });
}

function resetSort() {
  sortState = [];
  localStorage.removeItem("sortState");
  document.querySelectorAll("th.sortable").forEach(th => {
    th.classList.remove("bg-yellow-100");
    th.innerHTML = th.innerHTML.replace(/▲|▼/, "▲");
    th.setAttribute("data-sort", "desc");
  });
  applyMultiSort();
}

document.addEventListener("DOMContentLoaded", () => {
  if (sortState.length > 0) {
    applyMultiSort();
    highlightSortedColumns();
  }
});

// Here is  Stage 2 Screener button scripts
document.getElementById("screenerForm").addEventListener("submit", function(e) {
  const btn = document.getElementById("runButton");
  const spinner = document.getElementById("spinner");

  btn.disabled = true;
  btn.textContent = "Processing...";
  spinner.classList.remove("hidden");
});

window.addEventListener("load", function() {
  const el = document.getElementById("summaryMessage");
  const message = el?.dataset.message;
  if (message) {
    const toast = document.createElement("div");
    toast.textContent = message;
    toast.className = "fixed bottom-4 right-4 bg-green-600 text-white px-4 py-2 rounded shadow-lg z-50";
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
  }
});



