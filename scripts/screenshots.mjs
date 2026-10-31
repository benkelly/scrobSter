// Takes the screenshots for docs/images. Run scripts/screenshots.py, which
// starts the app with fake data and then runs this with SHOTS_* set.
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  console.error("Cannot find the playwright package. Run `npm install playwright`"
    + " and `npx playwright install chromium`, or set NODE_PATH to a global install.");
  process.exit(2);
}

const base = process.env.SHOTS_BASE_URL;
const out = process.env.SHOTS_OUT;
const roomId = process.env.SHOTS_ROOM_ID;
const origin = new URL(base).origin;
const problems = [];

const browser = await chromium.launch({
  // A fake microphone, allowed without asking, for the browser-mic picture.
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
});

async function signedIn(options = {}) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2,
    locale: "en-GB", timezoneId: "Europe/London", colorScheme: "dark", ...options,
  });
  // Nothing leaves this machine: the page links to Shazam and Last.fm, but
  // it must never load anything from them.
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(origin) || url.startsWith("data:")) return route.continue();
    problems.push(`blocked ${url}`);
    return route.abort();
  });
  const r = await context.request.post(`${base}/api/login`, {
    data: { username: process.env.SHOTS_USER, password: process.env.SHOTS_PASSWORD } });
  if (!r.ok()) throw new Error(`sign in failed: ${r.status()}`);
  const page = await context.newPage();
  page.on("pageerror", e => problems.push(`page error: ${e.message}`));
  page.on("console", m => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });
  return { context, page };
}

async function settle(page) {
  // Lazy covers below the fold would never load for a full-page picture.
  await page.evaluate(() => document.querySelectorAll("img[loading=lazy]")
    .forEach(i => { i.loading = "eager"; }));
  await page.waitForFunction(() => [...document.images].every(i => i.complete));
  if (await page.locator("#toasts .toast.err").count()) problems.push("an error toast shows");
  await page.waitForTimeout(300);
}

// One picture of the box around these elements, with a margin of page around it.
async function shot(page, name, selectors, { pad = 20, padTop = 12, padBottom = pad, maxHeight } = {}) {
  await settle(page);
  const boxes = [];
  for (const s of selectors) {
    const box = await page.locator(s).first().boundingBox();
    if (!box) throw new Error(`${name}: nothing visible at ${s}`);
    boxes.push(box);
  }
  const scroll = await page.evaluate(() => ({ x: scrollX, y: scrollY }));
  const left = Math.min(...boxes.map(b => b.x)) - pad + scroll.x;
  const top = Math.max(0, Math.min(...boxes.map(b => b.y)) - padTop + scroll.y);
  const right = Math.max(...boxes.map(b => b.x + b.width)) + pad + scroll.x;
  let bottom = Math.max(...boxes.map(b => b.y + b.height)) + padBottom + scroll.y;
  if (maxHeight) bottom = Math.min(bottom, top + maxHeight);
  await page.screenshot({
    path: path.join(out, `${name}.png`), fullPage: true, animations: "disabled", caret: "hide",
    clip: { x: Math.max(0, left), y: top, width: right - Math.max(0, left), height: bottom - top },
  });
  console.log(`  took ${name}`);
}

async function view(page, name) {
  await page.goto(`${base}/#${name}`);
  await page.waitForSelector(`#view-${name}:not(.hidden)`);
}

const { page } = await signedIn();

// Listening: what plays now, and the history.
await view(page, "listen");
await page.waitForSelector("#now img");
await page.waitForFunction(() => document.querySelectorAll("#history tr").length > 20);
await page.waitForFunction(() => /listening/.test(document.querySelector("#status").textContent));
await page.mouse.move(0, 0);
// The page down to the third scrobble, so it ends between two rows.
await shot(page, "hero", [".wrap > .between", "nav", "#history tr:nth-child(3)"],
           { pad: 24, padTop: 24, padBottom: 0 });

// Rooms: live meters, then the recognition rates.
await view(page, "rooms");
await page.waitForFunction(() =>
  document.querySelectorAll("#rooms [data-meter]:not(.hidden)").length >= 2);
await page.waitForSelector("#rates_stats:not(:empty)");
await page.waitForTimeout(1200);  // a few level polls, so the bars sit mid-song
await shot(page, "rooms", ["#view-rooms > h2:first-child", "#rooms", "#view-rooms > p.note"]);
await shot(page, "rooms-recognition", ["#rates-section"]);

// The form, editing the turntable, with the device list open and a level test.
await page.click(`[data-edit="${roomId}"]`);
await page.waitForFunction(() => /^Edit /.test(document.querySelector("#roomform-title").textContent));
await page.click("#audio_test");
await page.waitForSelector("#audio_note.ok");
// A native drop-down does not show in a screenshot, so show it as a list.
await page.evaluate(() => {
  const select = document.querySelector("#audio_device");
  select.size = select.options.length;
  select.style.overflow = "hidden";
  document.activeElement?.blur();
});
await page.mouse.move(0, 0);
await shot(page, "rooms-edit", ["#roomform-section"]);

// Settings: the services, then the API token.
await view(page, "settings");
await page.waitForFunction(() => document.querySelectorAll("#services .service").length === 4);
await shot(page, "settings-services", ["#view-settings > h2:first-child", "#services"]);
await shot(page, "settings-token", ["#view-settings > h2:nth-of-type(2)",
                                    "#view-settings > .card:nth-of-type(2)"]);

// Users.
await view(page, "users");
await page.waitForFunction(() => document.querySelectorAll("#users tr:not(.hidden)").length === 2);
await shot(page, "users", ["#view-users"]);

// A phone using its own microphone.
{
  const { context, page } = await signedIn({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
    permissions: ["microphone"],
  });
  // Chromium names its fake inputs "Fake Audio Input". Show one, as a phone would.
  await context.addInitScript(() => {
    const md = navigator.mediaDevices;
    const list = md.enumerateDevices.bind(md);
    md.enumerateDevices = async () => {
      const all = await list();
      const mic = all.find(d => d.kind === "audioinput");
      return all.filter(d => d.kind !== "audioinput").concat(mic ? [{
        deviceId: mic.deviceId, groupId: mic.groupId, kind: "audioinput",
        label: "iPhone Microphone", toJSON() { return this; } }] : []);
    };
  });
  await view(page, "listen");
  await page.waitForFunction(() => document.querySelectorAll("#history tr").length > 20);
  await page.click("#mic");
  await page.waitForSelector("#micmeter:not(.hidden)");
  // One 12 second clip, posted and matched.
  await page.waitForFunction(() => /^Matched/.test(document.querySelector("#micnote").textContent),
                             null, { timeout: 40000 });
  await page.waitForFunction(() => /via this device/.test(document.querySelector("#now").textContent));
  await page.waitForTimeout(500);
  await settle(page);
  await page.screenshot({ path: path.join(out, "phone-mic.png"), animations: "disabled", caret: "hide" });
  console.log("  took phone-mic");
  await context.close();
}

await browser.close();
if (problems.length) {
  console.error("Problems:\n  " + [...new Set(problems)].join("\n  "));
  process.exit(1);
}
