(function () {
  "use strict";

  function initSidebarDrawer() {
    var shell = document.querySelector("[data-shell]");
    if (!shell) {
      return;
    }
    var openBtn = shell.querySelector("[data-sidebar-open]");
    var scrim = shell.querySelector("[data-sidebar-close]");

    function open() {
      shell.classList.add("is-open");
      if (openBtn) {
        openBtn.setAttribute("aria-expanded", "true");
      }
      if (scrim) {
        scrim.hidden = false;
      }
    }

    function close() {
      shell.classList.remove("is-open");
      if (openBtn) {
        openBtn.setAttribute("aria-expanded", "false");
      }
      if (scrim) {
        scrim.hidden = true;
      }
    }

    if (openBtn) {
      openBtn.addEventListener("click", function () {
        if (shell.classList.contains("is-open")) {
          close();
        } else {
          open();
        }
      });
    }
    if (scrim) {
      scrim.addEventListener("click", close);
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && shell.classList.contains("is-open")) {
        close();
      }
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 820 && shell.classList.contains("is-open")) {
        close();
      }
    });
  }

  function initTenantSelector() {
    var selector = document.querySelector("[data-tenant-selector]");
    if (!selector) {
      return;
    }
    var toggle = selector.querySelector("[data-tenant-toggle]");
    var panel = selector.querySelector("[data-tenant-panel]");
    if (!toggle || !panel) {
      return;
    }
    toggle.addEventListener("click", function () {
      var isHidden = panel.hasAttribute("hidden");
      if (isHidden) {
        panel.removeAttribute("hidden");
        toggle.setAttribute("aria-expanded", "true");
        selector.classList.add("is-open");
      } else {
        panel.setAttribute("hidden", "");
        toggle.setAttribute("aria-expanded", "false");
        selector.classList.remove("is-open");
      }
    });

    var searchInput = selector.querySelector("[data-tenant-search]");
    var filterButtons = selector.querySelectorAll("[data-tenant-filter]");
    var items = selector.querySelectorAll("[data-tenant-item]");
    var emptyNote = selector.querySelector("[data-tenant-empty]");
    var countEl = selector.querySelector("[data-tenant-count]");
    var totalCount = items.length;
    var activeFilter = "all";

    function applyFilter() {
      var query = (searchInput && searchInput.value || "").trim().toLowerCase();
      var visible = 0;
      items.forEach(function (item) {
        var name = item.getAttribute("data-tenant-name") || "";
        var tags = (item.getAttribute("data-tenant-tags") || "").split(" ");
        var matchesQuery = !query || name.indexOf(query) !== -1;
        var matchesFilter = activeFilter === "all" || tags.indexOf(activeFilter) !== -1;
        if (matchesQuery && matchesFilter) {
          item.removeAttribute("hidden");
          visible += 1;
        } else {
          item.setAttribute("hidden", "");
        }
      });
      if (emptyNote) {
        if (visible === 0) {
          emptyNote.removeAttribute("hidden");
        } else {
          emptyNote.setAttribute("hidden", "");
        }
      }
      if (countEl) {
        if (activeFilter === "all" && !query) {
          countEl.textContent = String(totalCount);
        } else {
          countEl.textContent = visible + "/" + totalCount;
        }
      }
    }

    if (searchInput) {
      searchInput.addEventListener("input", applyFilter);
    }
    filterButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        activeFilter = btn.getAttribute("data-tenant-filter") || "all";
        filterButtons.forEach(function (other) {
          other.classList.toggle("is-active", other === btn);
        });
        applyFilter();
      });
    });
  }

  function initCreateTenantWizard() {
    var nameInp = document.querySelector("[data-ct-name]");
    var slugInp = document.querySelector("[data-ct-slug]");
    if (!nameInp || !slugInp) {
      return;
    }
    var slugTouched = false;
    slugInp.addEventListener("input", function () { slugTouched = true; });
    nameInp.addEventListener("input", function () {
      if (slugTouched && slugInp.value.length > 0) return;
      var s = (nameInp.value || "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^[^a-z]+/, "")
        .replace(/-+$/, "")
        .slice(0, 50);
      slugInp.value = s;
    });
  }

  function initTenantCreatedActions() {
    // J3-BE-50: Copy + Download buttons on the tenant-created
    // success page. Lift the JSON text out of a <pre> element by
    // id; trim trailing whitespace because the template indents
    // the contents.
    function targetText(btn, attr) {
      var id = btn.getAttribute(attr);
      var el = id ? document.getElementById(id) : null;
      return el ? (el.textContent || "").replace(/^\s*\n/, "").trimEnd() : "";
    }

    function showFeedback(msg) {
      var fb = document.querySelector("[data-ct-copy-feedback]");
      if (!fb) return;
      fb.textContent = msg;
      window.clearTimeout(showFeedback._t);
      showFeedback._t = window.setTimeout(function () {
        fb.textContent = "";
      }, 2500);
    }

    document.querySelectorAll("[data-ct-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var text = targetText(btn, "data-ct-copy-target");
        if (!text) {
          showFeedback("Nothing to copy.");
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            function () { showFeedback("Copied to clipboard."); },
            function () { showFeedback("Copy failed — select the JSON and copy manually."); }
          );
          return;
        }
        // Fallback for older browsers.
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.left = "-9999px";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          ta.remove();
          showFeedback("Copied to clipboard.");
        } catch (_) {
          showFeedback("Copy failed — select the JSON and copy manually.");
        }
      });
    });

    document.querySelectorAll("[data-ct-download]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var text = targetText(btn, "data-ct-download-target");
        if (!text) {
          showFeedback("Nothing to download.");
          return;
        }
        var filename = btn.getAttribute("data-ct-download-filename") || "client.json";
        var blob = new Blob([text + "\n"], { type: "application/json" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        showFeedback("Downloaded " + filename + ".");
      });
    });
  }

  function init() {
    initSidebarDrawer();
    initTenantSelector();
    initCreateTenantWizard();
    initTenantCreatedActions();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
