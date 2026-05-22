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

  function initWhatsAppConnectionCard() {
    var card = document.querySelector("[data-whatsapp-connect]");
    if (!card || !window.fetch) {
      return;
    }
    var tenantId = card.getAttribute("data-tenant-id");
    if (!tenantId) {
      return;
    }

    var statusEl = card.querySelector("[data-wa-status]");
    var summaryEl = card.querySelector("[data-wa-summary]");
    var phoneEl = card.querySelector("[data-wa-phone]");
    var accountEl = card.querySelector("[data-wa-account]");
    var startBtn = card.querySelector("[data-wa-start]");
    var refreshBtn = card.querySelector("[data-wa-refresh]");
    var linkBox = card.querySelector("[data-wa-link-box]");
    var linkInput = card.querySelector("[data-wa-auth-url]");
    var copyBtn = card.querySelector("[data-wa-copy]");
    var feedbackEl = card.querySelector("[data-wa-feedback]");
    var phoneOptionsEl = card.querySelector("[data-wa-phone-options]");

    function setFeedback(message) {
      if (feedbackEl) {
        feedbackEl.textContent = message || "";
      }
    }

    function setStatus(label, tone) {
      if (!statusEl) {
        return;
      }
      statusEl.textContent = label;
      statusEl.className = "status-chip status-" + tone;
    }

    function endpoint(path) {
      return "/internal/api/tenants/" + encodeURIComponent(tenantId) + path;
    }

    function requestJson(url, options) {
      return fetch(url, Object.assign({ credentials: "same-origin" }, options || {}))
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (body) {
            if (!response.ok) {
              throw new Error(body.detail || "Request failed.");
            }
            return body;
          });
        });
    }

    function renderPhoneOptions(payload) {
      if (!phoneOptionsEl) {
        return;
      }
      phoneOptionsEl.innerHTML = "";
      var phones = payload.phoneNumbers || [];
      if (phones.length <= 1) {
        phoneOptionsEl.setAttribute("hidden", "");
        return;
      }
      var heading = document.createElement("p");
      heading.className = "muted compact-note";
      heading.textContent = "Choose the WhatsApp phone number for this tenant.";
      phoneOptionsEl.appendChild(heading);
      phones.forEach(function (phone) {
        var row = document.createElement("div");
        row.className = "whatsapp-phone-option";
        var label = document.createElement("span");
        label.textContent = phone.displayPhoneNumber || phone.username || phone.phoneNumberId || "Unknown number";
        var button = document.createElement("button");
        button.type = "button";
        button.className = "button-secondary button-small";
        button.textContent = "Select";
        button.addEventListener("click", function () {
          selectPhone(phone);
        });
        row.appendChild(label);
        row.appendChild(button);
        phoneOptionsEl.appendChild(row);
      });
      phoneOptionsEl.removeAttribute("hidden");
    }

    function loadPhoneOptions() {
      return requestJson(endpoint("/channels/whatsapp/phone-numbers"))
        .then(renderPhoneOptions)
        .catch(function (error) {
          setFeedback(error.message);
        });
    }

    function renderStatus(payload) {
      var status = payload.status || ["not", "connected"].join("_");
      var phone = payload.displayPhoneNumber || "Not connected";
      var account = payload.providerAccountId || "Not connected";
      if (phoneEl) phoneEl.textContent = phone;
      if (accountEl) accountEl.textContent = account;

      if (status === "connected") {
        setStatus("Connected", "ok");
        if (summaryEl) summaryEl.textContent = "WhatsApp is connected for this tenant.";
        if (phoneOptionsEl) phoneOptionsEl.setAttribute("hidden", "");
      } else if (status === "pending") {
        setStatus("Pending", "warn");
        if (summaryEl) summaryEl.textContent = "Authorization was received. Select or confirm the phone number.";
        loadPhoneOptions();
      } else if (status === "failed") {
        setStatus("Failed", "error");
        if (summaryEl) summaryEl.textContent = payload.lastError || "WhatsApp connection failed.";
      } else {
        setStatus("Not connected", "unknown");
        if (summaryEl) summaryEl.textContent = "Generate a secure link when the client is ready to authorize WhatsApp.";
        if (phoneOptionsEl) phoneOptionsEl.setAttribute("hidden", "");
      }
    }

    function loadStatus() {
      setFeedback("");
      return requestJson(endpoint("/channels/whatsapp/status"))
        .then(renderStatus)
        .catch(function (error) {
          setStatus("Unavailable", "error");
          if (summaryEl) summaryEl.textContent = error.message;
        });
    }

    function startConnection() {
      setFeedback("Generating secure link...");
      if (startBtn) startBtn.disabled = true;
      requestJson(endpoint("/channels/whatsapp/connect/start"), { method: "POST" })
        .then(function (payload) {
          if (linkInput) {
            linkInput.value = payload.authUrl || "";
          }
          if (linkBox) {
            linkBox.removeAttribute("hidden");
          }
          setStatus("Link generated", "warn");
          if (summaryEl) summaryEl.textContent = "Send the link to the client separately.";
          setFeedback("Authorization link ready.");
        })
        .catch(function (error) {
          setFeedback(error.message);
        })
        .finally(function () {
          if (startBtn) startBtn.disabled = false;
        });
    }

    function selectPhone(phone) {
      setFeedback("Saving phone number...");
      requestJson(endpoint("/channels/whatsapp/phone-numbers/select"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phoneNumberId: phone.phoneNumberId,
          accountId: phone.accountId
        })
      })
        .then(function (payload) {
          renderStatus(payload);
          setFeedback("Phone number connected.");
        })
        .catch(function (error) {
          setFeedback(error.message);
        });
    }

    if (startBtn) {
      startBtn.addEventListener("click", startConnection);
    }
    if (refreshBtn) {
      refreshBtn.addEventListener("click", loadStatus);
    }
    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        var text = linkInput ? linkInput.value : "";
        if (!text) {
          setFeedback("No link to copy.");
          return;
        }
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          if (linkInput) {
            linkInput.select();
          }
          setFeedback("Select the link and copy manually.");
          return;
        }
        navigator.clipboard.writeText(text).then(
          function () { setFeedback("Copied link."); },
          function () { setFeedback("Copy failed. Select the link and copy manually."); }
        );
      });
    }

    loadStatus();
  }

  function init() {
    initSidebarDrawer();
    initTenantSelector();
    initCreateTenantWizard();
    initTenantCreatedActions();
    initWhatsAppConnectionCard();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
