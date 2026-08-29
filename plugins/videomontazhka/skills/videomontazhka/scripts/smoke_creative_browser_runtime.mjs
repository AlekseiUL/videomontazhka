#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`missing ${name}`);
  }
  return path.resolve(process.argv[index + 1]);
}

const runtime = argument("--runtime");
const chrome = argument("--chrome");
const puppeteerRoot = argument("--puppeteer-root");
const require = createRequire(import.meta.url);
const puppeteer = require(path.join(puppeteerRoot, "node_modules", "puppeteer-core"));

for (const required of [
  chrome,
  path.join(runtime, "vendor", "sprut-pixi.js"),
  path.join(runtime, "vendor", "sprut-three.js"),
  path.join(runtime, "vendor", "rough-notation.iife.js"),
  path.join(runtime, "vendor", "lottie-light.min.js"),
]) {
  if (!fs.statSync(required).isFile()) {
    throw new Error(`smoke input is not a regular file: ${required}`);
  }
}

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: true,
  args: ["--disable-gpu-sandbox", "--no-sandbox"],
});

try {
  const page = await browser.newPage();
  const externalRequests = [];
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const url = request.url();
    if (/^https?:/i.test(url)) {
      externalRequests.push(url);
      request.abort();
    } else {
      request.continue();
    }
  });
  await page.setContent(
    "<!doctype html><meta charset='utf-8'><div id='rough'>ПАМЯТЬ</div><div id='lottie'></div>",
  );
  await page.addScriptTag({ path: path.join(runtime, "vendor", "sprut-pixi.js") });
  await page.addScriptTag({ path: path.join(runtime, "vendor", "sprut-three.js") });
  await page.addScriptTag({ path: path.join(runtime, "vendor", "rough-notation.iife.js") });
  await page.addScriptTag({ path: path.join(runtime, "vendor", "lottie-light.min.js") });
  const result = await page.evaluate(() => {
    const graphic = new globalThis.SPRUT_PIXI.Graphics().rect(0, 0, 24, 24).fill(0xff6a00);
    const filters = [
      new globalThis.SPRUT_PIXI.AdvancedBloomFilter(),
      new globalThis.SPRUT_PIXI.GlitchFilter(),
      new globalThis.SPRUT_PIXI.MotionBlurFilter(),
      new globalThis.SPRUT_PIXI.OutlineFilter(),
      new globalThis.SPRUT_PIXI.PixelateFilter(),
      new globalThis.SPRUT_PIXI.RGBSplitFilter(),
      new globalThis.SPRUT_PIXI.ShockwaveFilter(),
      new globalThis.SPRUT_PIXI.ZoomBlurFilter(),
    ];
    const scene = new globalThis.SPRUT_THREE.Scene();
    const geometry = new globalThis.SPRUT_THREE.BoxGeometry(1, 1, 1);
    const annotation = globalThis.RoughNotation.annotate(document.querySelector("#rough"), {
      type: "underline",
      color: "#FF6A00",
      animate: false,
    });
    annotation.show();
    const animation = globalThis.lottie.loadAnimation({
      container: document.querySelector("#lottie"),
      renderer: "svg",
      autoplay: false,
      loop: false,
      animationData: {
        v: "5.13.0",
        fr: 30,
        ip: 0,
        op: 30,
        w: 100,
        h: 100,
        nm: "sprut-local-smoke",
        ddd: 0,
        assets: [],
        layers: [],
      },
    });
    animation.goToAndStop(0, true);
    const payload = {
      pixi_graphics: graphic.constructor.name,
      pixi_filters: filters.map((item) => item.constructor.name),
      rough_svg_count: document.querySelectorAll("svg").length,
      lottie_loaded: Boolean(animation),
      three_scene: scene.type,
      three_geometry: geometry.type,
    };
    animation.destroy();
    geometry.dispose();
    graphic.destroy();
    for (const filter of filters) filter.destroy();
    return payload;
  });
  if (externalRequests.length) {
    throw new Error(`runtime attempted external requests: ${externalRequests.join(", ")}`);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, external_requests: [], ...result })}\n`);
} finally {
  await browser.close();
}
