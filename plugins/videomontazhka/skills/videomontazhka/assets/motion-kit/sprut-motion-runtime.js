(function () {
  "use strict";

  var PALETTE = {
    background: "#070707",
    panel: "#121212",
    accent: "#FF6A00",
    primary: "#FFFFFF",
    secondary: "#A8A8A8",
  };

  function fail(message) {
    throw new Error("SPRUT Motion Kit: " + message);
  }

  function localPath(value, label) {
    if (typeof value !== "string" || !value || value.indexOf("://") !== -1 || value.charAt(0) === "/") {
      fail(label + " must be a non-empty relative local path");
    }
    var parts = value.split("/");
    if (parts.indexOf("..") !== -1) {
      fail(label + " must not escape the instance directory");
    }
    return value;
  }

  function requireConfig() {
    var config = window.SPRUT_MOTION_CONFIG;
    if (!config || config.version !== 1) {
      fail("config.js must define version 1");
    }
    Object.keys(PALETTE).forEach(function (key) {
      if (!config.brand || config.brand[key] !== PALETTE[key]) {
        fail("brand color " + key + " differs from DESIGN.md");
      }
    });
    if (!config.runtime || config.runtime.network_allowed !== false || (config.runtime.paid_apis || []).length) {
      fail("network and paid APIs must remain disabled");
    }
    localPath(config.runtime.gsap_file, "runtime.gsap_file");
    return config;
  }

  function addFontFace(role, descriptor) {
    if (!descriptor) {
      fail("missing bundled " + role + " font descriptor");
    }
    var family = role === "display" ? "SPRUT Display" : role === "body" ? "SPRUT Body" : "SPRUT Mono";
    var path = localPath(descriptor.file, "fonts." + role + ".file");
    var style = document.createElement("style");
    style.setAttribute("data-sprut-font", role);
    style.textContent =
      "@font-face{font-family:'" + family + "';src:url('" + path.replace(/'/g, "%27") +
      "') format('truetype');font-style:normal;font-weight:100 900;font-display:block;}";
    document.head.appendChild(style);
  }

  function setMedia(root, config, duration) {
    var nodes = root.querySelectorAll("[data-sprut-media]");
    Array.prototype.forEach.call(nodes, function (node) {
      var role = node.getAttribute("data-sprut-media");
      var path = config.media && config.media[role];
      if (!path) {
        node.remove();
        return;
      }
      node.setAttribute("src", localPath(path, "media." + role));
      node.setAttribute("data-start", "0");
      node.setAttribute("data-duration", String(duration));
      node.setAttribute("data-track-index", role === "background" ? "0" : "2");
      node.muted = true;
      node.playsInline = true;
    });
  }

  function prepare(compositionId) {
    var config = requireConfig();
    if (typeof window.gsap === "undefined") {
      fail("the copied local GSAP bundle did not create window.gsap");
    }
    var root = document.querySelector('[data-composition-id="' + compositionId + '"]');
    if (!root) {
      fail("composition root not found: " + compositionId);
    }
    var composition = config.composition;
    root.setAttribute("data-start", "0");
    root.setAttribute("data-duration", String(composition.duration_s));
    root.setAttribute("data-width", String(composition.width));
    root.setAttribute("data-height", String(composition.height));
    root.style.setProperty("--safe-top", config.layout.safe_top + "px");
    root.style.setProperty("--safe-right", config.layout.safe_right + "px");
    root.style.setProperty("--safe-bottom", config.layout.safe_bottom + "px");
    root.style.setProperty("--safe-left", config.layout.safe_left + "px");
    addFontFace("display", config.fonts.display);
    addFontFace("body", config.fonts.body);
    addFontFace("mono", config.fonts.mono);

    var lines = config.content.lines || [];
    var lineNodes = root.querySelectorAll("[data-sprut-line]");
    Array.prototype.forEach.call(lineNodes, function (node) {
      var index = Number(node.getAttribute("data-sprut-line"));
      var value = lines[index] || "";
      node.textContent = value;
      if (!value) {
        node.setAttribute("data-sprut-text-block", "hidden");
      }
    });
    var textNodes = root.querySelectorAll("[data-sprut-approved-text]");
    Array.prototype.forEach.call(textNodes, function (node) {
      var value = config.content.approved_text || "";
      node.textContent = value;
      if (!value) {
        node.setAttribute("data-sprut-text-block", "hidden");
      }
    });
    setMedia(root, config, composition.duration_s);
    return {config: config, root: root, duration: composition.duration_s};
  }

  function register(compositionId, timeline) {
    if (!timeline || typeof timeline.seek !== "function") {
      fail("a seekable GSAP timeline is required");
    }
    timeline.pause(0);
    window.__timelines = window.__timelines || {};
    window.__timelines[compositionId] = timeline;
    return timeline;
  }

  function finalExitAt(duration, exitDuration) {
    return Math.max(0.2, duration - exitDuration);
  }

  window.SPRUTMotion = {
    prepare: prepare,
    register: register,
    finalExitAt: finalExitAt,
  };
})();
