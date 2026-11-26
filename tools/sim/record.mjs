#!/usr/bin/env node
// Record the README GIFs from the app simulator.
// Usage: node tools/sim/record.mjs [--only status,printnow,...] [--fps 7] [--out assets]
// Needs Playwright (npm i -g playwright && npx playwright install chromium) and ffmpeg.
// Frames are screenshots of the phone alone (#hold, 422x894) with a transparent background, so the
// rounded corners stay transparent on GitHub's dark theme; ffmpeg scales them to 300 px wide.
import { execFileSync, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
const argv = process.argv.slice(2);
const arg = (name, def) => { const i = argv.indexOf("--" + name); return i >= 0 ? argv[i + 1] : def; };
const fps = Number(arg("fps", 7));
const outDir = path.resolve(root, arg("out", "assets"));
const only = arg("only", "") ? new Set(arg("only", "").split(",")) : null;

const require = createRequire(import.meta.url);
const { chromium } = (() => {
  for (const id of ["playwright", "playwright-core", path.join(os.homedir(), "node_modules/playwright")]) {
    try { return require(id); } catch (e) {}
  }
  console.error("Playwright not found: npm install -g playwright && npx playwright install chromium");
  process.exit(1);
})();
const ffmpeg = (() => {
  if (process.env.FFMPEG) return process.env.FFMPEG;
  if (!spawnSync("ffmpeg", ["-version"], { stdio: "ignore" }).error) return "ffmpeg";
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), "Library/Caches/ms-playwright");
  try {
    for (const d of fs.readdirSync(base).filter(x => x.startsWith("ffmpeg-")))
      for (const f of ["ffmpeg-mac", "ffmpeg-linux", "ffmpeg-linux-arm64"]) {
        const p = path.join(base, d, f);
        if (fs.existsSync(p)) return p;
      }
  } catch (e) {}
  process.exit("ffmpeg not found: brew install ffmpeg");
})();

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "pygmy-sim-"));
const simUrl = "file://" + path.join(scratch, "sim.html");
execFileSync("python3", [path.join(here, "build.py"), path.join(scratch, "sim.html")], { stdio: "inherit" });

// PW_CHANNEL=chrome uses the installed Google Chrome instead of Playwright's own Chromium.
const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
// Wide enough for the shell to keep the phone at 100 % (its fit() scales it below 894 px of height).
// HQ=1 renders at 2x for videos; KEEP=dir keeps the PNG frames of each scene (and skips the README GIF) for a better encoder.
const view = { viewport: { width: 1400, height: 1100 }, deviceScaleFactor: process.env.HQ ? 2 : 1 };
// Installed Chrome quits when its last context closes: one idle context keeps it up between scenarios.
await browser.newContext();

let frames = 0, dir = "", ctx, page, app, frame;
const grab = async () => {
  const png = await page.locator("#hold").screenshot({ omitBackground: true });
  fs.writeFileSync(path.join(dir, `f${String(++frames).padStart(4, "0")}.png`), png);
};
// Hold the shot for `ms` of GIF time: one frame every 1/fps second.
const shot = async (ms = 700) => {
  const n = Math.max(1, Math.round(ms / (1000 / fps)));
  for (let i = 0; i < n; i++) { await grab(); await page.waitForTimeout(1000 / fps); }
};
const click = sel => app.locator(sel).click();
const fill = (sel, v) => app.locator(sel).fill(v);
const ready = async () => { await app.locator("#fname").waitFor(); await shot(1200); };
const when = async (fn, timeout = 20000) => frame.waitForFunction(fn, null, { timeout });
// A fresh context per scenario: the app remembers the printer it was on in localStorage.
const open = async () => {
  ctx = await browser.newContext(view);
  page = await ctx.newPage();
  page.on("dialog", d => d.accept());
  await page.goto(simUrl);
  // Only the phone: the page's own background would fill the rounded corners.
  await page.addStyleTag({ content: "html,body{background:transparent !important}" });
  app = page.frameLocator("#f");
  await ready();
  frame = page.frames().find(f => f !== page.mainFrame());
};
const close = () => ctx.close();
// The app follows the printer picked in the printers sheet.
const pickPrinter = async name => {
  await click("#pick");
  await app.locator(".prn", { hasText: name }).click();
  await shot(600);
};

const scenarios = {
  // Status: a print in progress, its numbers, the nozzle sheet and the switches.
  async status() {
    await open();
    await click("#noztile"); await shot(1600);
    await click("#dclose"); await shot(500);
    await click("#optbtn"); await shot(1800);
    await click("#opts .btn:has-text('Done')"); await shot(800);
    await close();
  },

  // Print now: a link, the summary, Slice, the plate photo, start.
  async printnow() {
    await open();
    await pickPrinter("Desk A1 mini");  // idle, so Print is offered instead of the queue
    await click('.tabs button[data-t="tab-new"]'); await shot(600);
    await fill("#mwurl", "https://makerworld.com/models/1234-cable-clip"); await shot(700);
    await click("#mwsend"); await shot(1800);
    await when(() => !document.getElementById("pnslice").disabled); await shot(700);
    await click("#pnslice"); await shot(1200);
    await when(() => !document.getElementById("pnresult").hidden); await shot(1500);
    await click("#pnprint"); await when(() => !document.getElementById("confirm").hidden);
    await frame.evaluate(() => { const i = document.getElementById("cphoto"); i.src = SIM.photo(PRINTER); i.hidden = false; });
    await shot(1500);
    await click("#cyes"); await shot(2500);
    await close();
  },

  // Several meshes from one link: untick the one you don't want, then slice.
  async multifile() {
    await open();
    // Photos are optional (tools/sim/cam is gitignored): use the sample printer that has them, made idle.
    const alias = await frame.evaluate(() => {
      const have = Object.keys(window.SIM_PHOTOS || {});
      const a = have.includes("p1s") ? "p1s" : have[0] || "p1s";
      SIM.finish(a);  // a sample printer with photos starts out printing; SIM.finish is a no-op when idle
      return a;
    });
    const names = { a1: "Workshop A1", a1mini: "Desk A1 mini", p1s: "Studio P1S", p2s: "Lab P2S" };
    // Off camera: pick it, clear the finished plate, let the banner go and the camera load.
    await click("#pick");
    await app.locator(".prn", { hasText: names[alias] || "Studio P1S" }).click();
    await app.locator("button", { hasText: "Plate is clear" }).click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(6000);
    await shot(800);
    await click('.tabs button[data-t="tab-new"]'); await shot(600);
    await fill("#mwurl", "https://www.printables.com/model/1234-handle-holder/files"); await shot(700);
    await click("#mwsend"); await shot(2000);
    await when(() => !document.getElementById("s1files").hidden); await shot(1500);
    await app.locator("#s1files").scrollIntoViewIfNeeded().catch(() => {});
    await frame.evaluate(() => { const t = document.querySelectorAll("#s1files input")[1]; t.checked = false; t.dispatchEvent(new Event("change", { bubbles: true })); }); await shot(1200);
    await when(() => !document.getElementById("pnslice").disabled);
    await click("#pnslice"); await shot(4500);
    await when(() => !document.getElementById("pnresult").hidden); await shot(1800);
    await click("#pnprint"); await when(() => !document.getElementById("confirm").hidden);
    await frame.evaluate(() => { const i = document.getElementById("cphoto"); i.src = SIM.photo(PRINTER); i.hidden = false; });
    await shot(1500);
    await click("#cyes"); await shot(2500);
    await close();
  },

  // Video for a post: a printing P2S with its camera, the nozzle sheet, then AMS drying.
  async promo_p2s() {
    await open();
    await pickPrinter("Lab P2S"); await page.waitForTimeout(3000); await shot(2500);
    await click("#noztile"); await shot(1600);
    await click("#dclose"); await shot(500);
    await click('.tabs button[data-t="tab-fil"]'); await shot(1500);
    await when(() => !!document.getElementById("drybtn")); await shot(800);
    await click("#drybtn"); await shot(1400);
    await click("#drygo"); await shot(2500);
    await click('.tabs button[data-t="main"]'); await shot(1500);
    await close();
  },

  // AMS drying on a printer that has a heater in its AMS.
  async drying() {
    await open();
    await pickPrinter("Lab P2S");
    await click('.tabs button[data-t="tab-fil"]'); await shot(1200);
    await when(() => !!document.getElementById("drybtn")); await shot(800);
    await click("#drybtn"); await shot(1400);
    await click("#drygo"); await shot(3000);
    await close();
  },

  // Add a Bambu printer that answered on the network.
  async addprinter() {
    await open();
    await frame.evaluate(() => SIM.found(true));
    await click("#pick"); await shot(600);
    await when(() => !!document.querySelector("[data-act=addfound]"));  // discovery answers a moment later
    await app.locator(".prow.found", { hasText: "P1P" }).locator("button").click(); await shot(1200);
    await fill("#apcode", "12345678"); await shot(900);
    await click("#apadd"); await shot(2500);
    await close();
  },

  // Add a Klipper printer found on the network, then its toolhead and Motion card.
  async klipper() {
    await open();
    await frame.evaluate(() => SIM.found(true));
    await click("#pick"); await shot(600);
    await when(() => !!document.querySelector("[data-act=addfound]"));
    await app.locator(".prow.found", { hasText: "Voron 2.4" }).locator("button").click(); await shot(1500);
    await click("#apadd"); await shot(2000);
    await app.locator("#xyzbtn").scrollIntoViewIfNeeded(); await shot(900);
    await click("#xyzbtn"); await shot(1800);
    await click('#kstep .opt[data-v="100"]'); await shot(600);
    await click('#kjog button[data-a="x"][data-d="1"]'); await shot(1200);
    await click("#khomexy"); await shot(1400);
    await click("#xyz .btn:has-text('Done')"); await shot(1000);
    await close();
  },

  // Pause on spaghetti: the switch, then the alarm while the print waits for a hand.
  async spaghetti() {
    await open();
    await click("#optbtn"); await shot(700);
    // The switch sits low in a scrolling sheet: scroll it in, then tap it like a finger would.
    await frame.evaluate(() => { const s = document.querySelector('#optrows .sw[data-k="bff_spaghetti_pause"]'); s.scrollIntoView({ block: "center" }); s.click(); });
    await shot(1600);
    await click("#opts .btn:has-text('Done')"); await shot(600);
    await page.click('[data-a="spaghetti"]'); await shot(1800);
    await app.locator("#alert").scrollIntoViewIfNeeded(); await shot(2200);
    await close();
  },
};

try {
  for (const [name, play] of Object.entries(scenarios)) {
    if (only && !only.has(name)) continue;
    dir = path.join(scratch, name);
    fs.mkdirSync(dir);
    frames = 0;
    console.log("recording", name);
    await play();
    if (process.env.KEEP) { fs.cpSync(dir, path.join(process.env.KEEP, name), { recursive: true }); console.log("kept", name, frames, "frames"); continue; }
    const out = path.join(outDir, name + ".gif");
    // 32 colours and a bayer dither like the earlier README GIFs; transparent pixels stay transparent.
    // diff_mode=rectangle re-encodes only the changed area of each frame (the camera, a sheet), so GIFs stay small.
    execFileSync(ffmpeg, ["-y", "-loglevel", "error", "-framerate", String(fps), "-i", path.join(dir, "f%04d.png"),
      // mpdecimate drops repeated frames and vfr turns the gap into a longer GIF delay: same timing, a fraction of the size.
      "-fps_mode", "vfr",
      "-filter_complex", "mpdecimate=hi=1:lo=1:frac=0,scale=300:-1:flags=lanczos,split[a][b];"
        + "[a]palettegen=max_colors=32:reserve_transparent=1:stats_mode=full[p];"
        + "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle:alpha_threshold=128",
      "-loop", "0", out], { stdio: "inherit" });
    console.log("wrote", path.relative(root, out), (fs.statSync(out).size / 1024).toFixed(0) + " KB,", frames, "frames");
  }
} finally {
  await browser.close();
  fs.rmSync(scratch, { recursive: true, force: true });
}
