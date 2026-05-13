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
    var list = selector.querySelector(".tenant-selector-list");
    if (!toggle || !list) {
      return;
    }
    toggle.addEventListener("click", function () {
      var isHidden = list.hasAttribute("hidden");
      if (isHidden) {
        list.removeAttribute("hidden");
        toggle.setAttribute("aria-expanded", "true");
        selector.classList.add("is-open");
      } else {
        list.setAttribute("hidden", "");
        toggle.setAttribute("aria-expanded", "false");
        selector.classList.remove("is-open");
      }
    });
  }

  function init() {
    initSidebarDrawer();
    initTenantSelector();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
