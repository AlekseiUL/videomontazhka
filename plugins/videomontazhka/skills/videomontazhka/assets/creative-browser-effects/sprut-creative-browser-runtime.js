(function () {
  "use strict";

  var PALETTE = {
    background: "#070707",
    panel: "#121212",
    accent: "#FF6A00",
    primary: "#FFFFFF",
    secondary: "#A8A8A8"
  };

  function fail(message) { throw new Error("SPRUT Creative Browser: " + message); }

  function isRemote(value) {
    return typeof value === "string" && /^(?:https?:|wss?:|ftp:|file:|\/\/)/i.test(value.trim());
  }

  function localPath(value, label) {
    if (typeof value !== "string" || !value || value.charAt(0) === "/" || value.indexOf("://") !== -1) {
      fail(label + " must be a non-empty relative local path");
    }
    if (value.split("/").indexOf("..") !== -1 || value.indexOf("\\") !== -1) {
      fail(label + " must remain inside the source instance");
    }
    return value;
  }

  function installNetworkBarrier() {
    if (window.__sprutNetworkBarrierInstalled) return;
    window.__sprutNetworkBarrierInstalled = true;
    if (typeof window.fetch === "function") {
      var nativeFetch = window.fetch.bind(window);
      window.fetch = function (input, init) {
        var url = typeof input === "string" ? input : input && input.url;
        if (isRemote(url)) fail("network fetch is prohibited");
        return nativeFetch(input, init);
      };
    }
    if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
      var nativeOpen = window.XMLHttpRequest.prototype.open;
      window.XMLHttpRequest.prototype.open = function (method, url) {
        if (isRemote(url)) fail("network XMLHttpRequest is prohibited");
        return nativeOpen.apply(this, arguments);
      };
    }
    if (navigator.sendBeacon) {
      navigator.sendBeacon = function () { fail("sendBeacon is prohibited"); };
    }
    window.WebSocket = function () { fail("WebSocket is prohibited"); };
    window.EventSource = function () { fail("EventSource is prohibited"); };
  }

  function addFontFace(role, descriptor) {
    if (!descriptor) fail("missing bundled " + role + " font descriptor");
    var family = role === "display" ? "SPRUT Display" : role === "body" ? "SPRUT Body" : "SPRUT Mono";
    var path = localPath(descriptor.file, "fonts." + role + ".file");
    var style = document.createElement("style");
    style.setAttribute("data-sprut-font", role);
    style.textContent = "@font-face{font-family:'" + family + "';src:url('" + path.replace(/'/g, "%27") + "') format('truetype');font-style:normal;font-weight:100 900;font-display:block;}";
    document.head.appendChild(style);
  }

  function prepare(effectId) {
    installNetworkBarrier();
    var config = window.SPRUT_CREATIVE_EFFECT_CONFIG;
    if (!config || config.version !== 1) fail("config.js must define version 1");
    if (!config.effect || config.effect.type !== effectId) fail("effect id differs from config");
    Object.keys(PALETTE).forEach(function (key) {
      if (!config.brand || config.brand[key] !== PALETTE[key]) fail("brand color " + key + " differs from DESIGN.md");
    });
    if (!config.runtime || config.runtime.network_allowed !== false || config.runtime.remotion !== false || (config.runtime.paid_apis || []).length) {
      fail("network, Remotion, and paid APIs must remain disabled");
    }
    localPath(config.runtime.engine_file, "runtime.engine_file");
    localPath(config.runtime.gsap_file, "runtime.gsap_file");
    if (typeof window.gsap === "undefined") fail("the copied local GSAP bundle did not load");
    var root = document.querySelector('[data-composition-id="' + effectId + '"]');
    if (!root) fail("composition root not found: " + effectId);
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
    Array.prototype.forEach.call(root.querySelectorAll("[data-sprut-approved-text]"), function (node) {
      var text = config.content.approved_text || "";
      node.textContent = text;
      if (!text) node.setAttribute("data-sprut-text-block", "hidden");
    });
    return {config: config, root: root, effect: config.effect, duration: composition.duration_s};
  }

  function register(effectId, timeline) {
    if (!timeline || typeof timeline.seek !== "function") fail("a seekable GSAP timeline is required");
    timeline.pause(0);
    window.__timelines = window.__timelines || {};
    window.__timelines[effectId] = timeline;
    return timeline;
  }

  function seeded(seed) {
    var state = seed >>> 0;
    return function () {
      state += 0x6D2B79F5;
      var value = state;
      value = Math.imul(value ^ value >>> 15, value | 1);
      value ^= value + Math.imul(value ^ value >>> 7, value | 61);
      return ((value ^ value >>> 14) >>> 0) / 4294967296;
    };
  }

  function finalExitAt(duration, exitDuration) { return Math.max(0.3, duration - exitDuration); }

  window.SPRUTCreative = {
    prepare: prepare,
    register: register,
    seeded: seeded,
    finalExitAt: finalExitAt,
    localPath: localPath
  };
})();
