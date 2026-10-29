/*
 * LibraryHub - app.js
 * Small, plain JavaScript (no frameworks).  Everything is attached with
 * data-* attributes in the HTML, so no inline scripts are needed.
 *
 *   data-confirm="message"     on a <form>   -> asks for confirmation before submit
 *   data-autosubmit            on a field    -> submits its form when changed
 *   data-toggle-menu="id"      on a button   -> opens/closes a dropdown menu
 *   #category-chart (data-labels / data-values as JSON)  -> draws a doughnut chart
 */
(function () {
  "use strict";

  // ---- Sidebar (mobile) ---------------------------------------------------
  var sidebar = document.getElementById("sidebar");
  var scrim = document.getElementById("scrim");
  function setSidebar(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    scrim.classList.toggle("open", open);
  }
  var menuBtn = document.getElementById("menu-btn");
  if (menuBtn) menuBtn.addEventListener("click", function () { setSidebar(!sidebar.classList.contains("open")); });
  if (scrim) scrim.addEventListener("click", function () { setSidebar(false); });

  // ---- Dropdown menus (notifications, profile) ---------------------------------
  document.querySelectorAll("[data-toggle-menu]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      var menu = document.getElementById(button.getAttribute("data-toggle-menu"));
      var willOpen = !menu.classList.contains("open");
      document.querySelectorAll(".menu.open").forEach(function (m) { m.classList.remove("open"); });
      menu.classList.toggle("open", willOpen);
      button.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".menu")) {
      document.querySelectorAll(".menu.open").forEach(function (m) { m.classList.remove("open"); });
    }
  });

  // ---- Flash messages: close button + auto-hide ---------------------------------
  document.querySelectorAll(".flash").forEach(function (flash) {
    var close = flash.querySelector(".close");
    if (close) close.addEventListener("click", function () { flash.remove(); });
    if (!flash.classList.contains("error")) {          // errors stay until closed
      setTimeout(function () { flash.remove(); }, 7000);
    }
  });

  // ---- Confirmation dialog ------------------------------------------------------
  var backdrop = document.getElementById("confirm-modal");
  var pendingForm = null;
  function closeModal() {
    backdrop.classList.remove("open");
    pendingForm = null;
  }
  if (backdrop) {
    document.getElementById("confirm-cancel").addEventListener("click", closeModal);
    backdrop.addEventListener("click", function (e) { if (e.target === backdrop) closeModal(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
    document.getElementById("confirm-ok").addEventListener("click", function () {
      var form = pendingForm;
      closeModal();
      if (form) { form.setAttribute("data-confirmed", "1"); submitWithLoading(form); }
    });
  }

  // ---- Form submit handling: confirm, validate, loading state ----------------------
  function submitWithLoading(form) {
    form.submit();
  }
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.getAttribute("data-confirmed") === "1") return;   // already confirmed
      var message = form.getAttribute("data-confirm");
      if (message && backdrop) {
        event.preventDefault();
        document.getElementById("confirm-text").textContent = message;
        var okButton = document.getElementById("confirm-ok");
        okButton.textContent = form.getAttribute("data-confirm-button") || "Yes, continue";
        okButton.className = "btn " + (form.getAttribute("data-confirm-style") || "danger");
        pendingForm = form;
        backdrop.classList.add("open");
        okButton.focus();
        return;
      }
      if (!form.checkValidity()) return;     // the browser shows its own validation hints
      // Disable the submit button so a double click cannot send the form twice.
      var submit = form.querySelector("button[type=submit]:not([data-no-loading])");
      if (submit && !form.hasAttribute("data-no-loading")) {
        setTimeout(function () { submit.classList.add("is-loading"); submit.setAttribute("aria-busy", "true"); }, 0);
      }
    });
  });
  // Coming back with the browser's Back button should not leave buttons stuck in "loading".
  window.addEventListener("pageshow", function () {
    document.querySelectorAll(".btn.is-loading").forEach(function (b) { b.classList.remove("is-loading"); });
  });

  // ---- Auto-submit filters / date pickers ---------------------------------------------
  document.querySelectorAll("[data-autosubmit]").forEach(function (field) {
    field.addEventListener("change", function () { field.form.submit(); });
  });

  // ---- Issue form: keep the due date in step with the issue date ----------------------
  var issueDate = document.getElementById("issue_date");
  var dueDate = document.getElementById("due_date");
  if (issueDate && dueDate && issueDate.getAttribute("data-loan-days")) {
    issueDate.addEventListener("change", function () {
      var days = parseInt(issueDate.getAttribute("data-loan-days"), 10);
      var start = new Date(issueDate.value + "T00:00:00");
      if (!isNaN(start.getTime())) {
        start.setDate(start.getDate() + days);
        var m = String(start.getMonth() + 1).padStart(2, "0");
        var d = String(start.getDate()).padStart(2, "0");
        dueDate.value = start.getFullYear() + "-" + m + "-" + d;
      }
    });
  }

  // ---- Dashboard doughnut chart (Chart.js) ---------------------------------------------
  var chartCanvas = document.getElementById("category-chart");
  if (chartCanvas && window.Chart) {
    var labels = JSON.parse(chartCanvas.getAttribute("data-labels") || "[]");
    var values = JSON.parse(chartCanvas.getAttribute("data-values") || "[]");
    var colours = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#64748b"];
    new Chart(chartCanvas, {
      type: "doughnut",
      data: { labels: labels, datasets: [{ data: values, backgroundColor: labels.map(function (_, i) { return colours[i % colours.length]; }), borderWidth: 2, borderColor: "#fff" }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right", labels: { boxWidth: 12, usePointStyle: true } } } }
    });
  }
})();
