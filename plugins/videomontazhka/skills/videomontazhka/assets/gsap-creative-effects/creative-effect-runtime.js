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
    throw new Error("SPRUT GSAP Creative: " + message);
  }

  function localPath(value, label) {
    if (typeof value !== "string" || !value || value.indexOf("://") !== -1 || value.charAt(0) === "/") {
      fail(label + " must be a non-empty relative local path");
    }
    if (value.split("/").indexOf("..") !== -1) {
      fail(label + " must not escape the instance directory");
    }
    return value;
  }

  function requireConfig(expectedEffect) {
    var config = window.SPRUT_GSAP_CREATIVE_CONFIG;
    if (!config || config.version !== 1 || config.effect_type !== expectedEffect) {
      fail("config.js must define the requested v1 effect");
    }
    Object.keys(PALETTE).forEach(function (key) {
      if (!config.brand || config.brand[key] !== PALETTE[key]) {
        fail("brand color " + key + " differs from DESIGN.md");
      }
    });
    if (!config.runtime || config.runtime.network_allowed !== false || (config.runtime.paid_apis || []).length) {
      fail("network and paid APIs must remain disabled");
    }
    (config.runtime.bundles || []).forEach(function (value, index) {
      localPath(value, "runtime.bundles[" + index + "]");
    });
    if (!config.content || typeof config.content.approved_text !== "string" || !config.content.approved_text.trim()) {
      fail("approved text is missing");
    }
    return config;
  }

  function addFontFace(role, descriptor) {
    if (!descriptor) {
      fail("missing bundled " + role + " font descriptor");
    }
    var family = role === "display" ? "SPRUT Display" : role === "body" ? "SPRUT Body" : "SPRUT Mono";
    var file = localPath(descriptor.file, "fonts." + role + ".file");
    var style = document.createElement("style");
    style.setAttribute("data-sprut-font", role);
    style.textContent = "@font-face{font-family:'" + family + "';src:url('" +
      file.replace(/'/g, "%27") + "') format('truetype');font-style:normal;font-weight:100 900;font-display:block;}";
    document.head.appendChild(style);
  }

  function populate(root, config) {
    Array.prototype.forEach.call(root.querySelectorAll("[data-sprut-approved-text]"), function (node) {
      node.textContent = config.content.approved_text;
    });
    Array.prototype.forEach.call(root.querySelectorAll("[data-sprut-fragment]"), function (node) {
      var index = Number(node.getAttribute("data-sprut-fragment"));
      var value = config.content.fragments[index] || "";
      node.textContent = value;
      if (!value) {
        node.setAttribute("data-sprut-hidden", "true");
      }
    });
  }

  function prepare(compositionId, expectedEffect) {
    var config = requireConfig(expectedEffect);
    if (typeof window.gsap === "undefined") {
      fail("the copied local GSAP core did not create window.gsap");
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
    populate(root, config);
    return {config: config, root: root, duration: composition.duration_s};
  }

  function plugin(name) {
    var value = window[name];
    if (!value) {
      fail("required copied plugin did not create window." + name);
    }
    window.gsap.registerPlugin(value);
    return value;
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
    return Math.max(0.6, duration - exitDuration);
  }

  window.SPRUTGSAPCreative = {
    prepare: prepare,
    plugin: plugin,
    register: register,
    finalExitAt: finalExitAt,
  };
})();
