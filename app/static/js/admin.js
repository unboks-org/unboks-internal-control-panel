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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSidebarDrawer);
  } else {
    initSidebarDrawer();
  }
})();
