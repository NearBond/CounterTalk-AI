// The browser checks run by e2e/run.mjs (BUILD_PLAN batch 7; CODING_GUIDELINES §9.9 to §9.13).
// Each gets a fresh browser context with a fake microphone, the API and web addresses, and the
// seeded visits. A check throws to fail.

import { COMPLETE_ATTEMPTS, POLL_MS } from "../src/lib/limits.ts";

/** Runs in the page before its scripts: records every stream and recorder the app makes, and can
 * slow getUserMedia (to leave mid-start) or break MediaRecorder (to fail a start). */
function instrument({ delayMs = 0, failRecorder = false }) {
  const state = { streams: [], recorders: [] };
  window.__e2e = state;
  const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    const stream = await getUserMedia(constraints);
    state.streams.push(stream);
    if (delayMs > 0) await new Promise((done) => setTimeout(done, delayMs));
    return stream;
  };
  const Real = window.MediaRecorder;
  window.MediaRecorder = class extends Real {
    constructor(...args) {
      if (failRecorder) throw new DOMException("No recorder here", "NotSupportedError");
      super(...args);
      state.recorders.push(this);
    }
  };
}

const inUse = (page) =>
  page.evaluate(() => ({
    tracks: window.__e2e.streams.flatMap((s) => s.getTracks()).filter((t) => t.readyState === "live").length,
    recorders: window.__e2e.recorders.filter((r) => r.state !== "inactive").length,
  }));

function expect(condition, message) {
  if (!condition) throw new Error(message);
}

async function visitCount(api) {
  const response = await fetch(`${api}/api/visits`);
  return (await response.json()).items.length;
}

async function released(page) {
  const used = await inUse(page);
  expect(used.tracks === 0 && used.recorders === 0, `still in use: ${JSON.stringify(used)}`);
}

/** /record, reached from the visits list, so going back is a route change, not a reload. */
async function openRecorder(page, web) {
  await page.goto(`${web}/`);
  await page.getByRole("link", { name: "Record a visit" }).click();
  await page.getByLabel("Shop").selectOption({ label: "Sunrise Kirana, Old Market" });
}

async function startRecording(page) {
  await page.getByRole("button", { name: "Start recording" }).click();
  await page.getByText(/^Recording \d/).waitFor();
}

const visitIdIn = (url) => new URL(url).pathname.split("/visits/")[1];

/** Batch 15c's click check: K6 loaded, W6 is last week, and E's issue is merged into P1's. */
const CLICK_CHECK = {
  name: "as finance, D2's 5 shops are a click from last week's summary, and each shop's ▶ plays its quote",
  async run({ page, k6, seeded }) {
    await page.goto(`${k6}/summaries?team=finance`);
    const first = page.getByRole("list", { name: "Issues, ranked" }).getByRole("listitem").first();
    await first.waitFor();
    expect((await first.textContent()).includes("Kurkure Diwali payout"), "P1 isn't first on last week's summary");
    await first.getByRole("link", { name: "Its reps, shops and quotes" }).click(); // click 1
    const shops = page.locator('ul[aria-label$="\'s shops"] > li');
    await shops.first().waitFor();
    const count = await shops.count();
    expect(count === 5, `the drill-down shows ${count} shops, not P1's 5`);
    for (let i = 0; i < count; i += 1) {
      const shop = shops.nth(i);
      expect((await shop.textContent()).includes(seeded.k6.d2), `shop ${i + 1} isn't D2's`);
      const before = await page.evaluate(() => {
        const audio = document.querySelector("audio");
        audio?.pause();
        return audio?.currentSrc ?? "";
      });
      await shop.getByRole("button", { name: /^Play from/ }).first().click(); // click 2: it plays
      // Its own visit's recording (each shop's is another visit), playing, past its start (0 s).
      await page.waitForFunction(
        (previous) => {
          const audio = document.querySelector("audio");
          return audio !== null && audio.currentSrc !== previous && !audio.paused && audio.currentTime > 0.2;
        },
        before,
        { timeout: 15_000 },
      );
    }
    // The same answer from the plain inbox, for the demo: its filters and clicks.
    await page.goto(`${k6}/inbox?team=finance`);
    await page.getByLabel("Category").selectOption({ label: "Schemes and promotions" });
    await page.getByLabel("Distributor").selectOption({ label: seeded.k6.d2 });
    await page.getByRole("button", { name: /Kurkure Diwali payout/ }).click();
    const insights = page.getByRole("list", { name: "Its insights" }).getByRole("listitem");
    await insights.first().waitFor();
    process.stdout.write(
      `      the summary: 1 click to D2's ${count} shops, 1 more to play a quote; ` +
        `the inbox: 2 filters and 1 click to ${await insights.count()} insights, with no week marked\n`,
    );
  },
};

export const CHECKS = [
  {
    name: "a recorder that fails to start releases the microphone, and no visit is created",
    async run({ page, web, api }) {
      await page.addInitScript(instrument, { failRecorder: true });
      const before = await visitCount(api);
      await openRecorder(page, web);
      await page.getByRole("button", { name: "Start recording" }).click();
      await page
        .getByText("Recording couldn't start in this browser. Try Chrome or Edge. Press Start recording to try again.", {
          exact: true,
        })
        .waitFor();
      await released(page);
      expect((await visitCount(api)) === before, "a visit was created");
    },
  },
  {
    name: "leaving the page while starting stops everything",
    async run({ page, web, api }) {
      await page.addInitScript(instrument, { delayMs: 1_500 });
      const before = await visitCount(api);
      await openRecorder(page, web);
      await page.getByRole("button", { name: "Start recording" }).click();
      await page.getByText("Starting…").waitFor();
      await page.goBack();
      await page.waitForTimeout(2_500); // past the slow microphone
      await released(page);
      expect((await visitCount(api)) === before, "a visit was created after leaving");
    },
  },
  {
    name: "leaving the page while finishing stops everything",
    async run({ page, web }) {
      await page.addInitScript(instrument, {});
      const completes = [];
      await page.route("**/complete", (route) => completes.push(route)); // never answered
      const aborted = [];
      page.on("requestfailed", (request) => request.url().endsWith("/complete") && aborted.push(request.url()));
      await openRecorder(page, web);
      await startRecording(page);
      await page.getByRole("button", { name: "Stop" }).click();
      await page.getByText(/^Finishing/).waitFor();
      await page.waitForTimeout(1_000);
      await page.goBack();
      await page.waitForTimeout(3_000);
      await released(page);
      expect(aborted.length === 1, `the waiting complete wasn't cancelled (${aborted.length})`);
      expect(completes.length === 1, `complete was sent again after leaving (${completes.length})`);
    },
  },
  {
    name: "complete is retried, then shows Try again, which finishes the visit",
    async run({ page, web }) {
      let tries = 0;
      await page.route("**/complete", (route) => {
        tries += 1;
        return route.fulfill({ status: 503, json: { error: { code: "busy", message: "The server is busy" } } });
      });
      await openRecorder(page, web);
      await startRecording(page);
      await page.getByRole("button", { name: "Stop" }).click();
      await page.getByRole("button", { name: "Try again" }).waitFor({ timeout: 90_000 });
      expect(tries === COMPLETE_ATTEMPTS, `complete was tried ${tries} times, not ${COMPLETE_ATTEMPTS}`);
      await page.unroute("**/complete");
      await page.getByRole("button", { name: "Try again" }).click();
      await page.waitForURL(/\/visits\//);
    },
  },
  {
    name: "the microphone stopping is reported, and what was recorded is sent",
    async run({ page, web }) {
      await page.addInitScript(instrument, {});
      await openRecorder(page, web);
      await startRecording(page);
      await page.evaluate(() => {
        for (const track of window.__e2e.streams[0].getAudioTracks()) track.dispatchEvent(new Event("ended"));
      });
      await page
        .getByText("The microphone stopped. What was recorded is being sent; start a new visit to carry on.", { exact: true })
        .waitFor();
      await page.waitForURL(/\/visits\//);
    },
  },
  {
    name: "inputs are locked while recording",
    async run({ page, web }) {
      await openRecorder(page, web);
      await startRecording(page);
      expect(await page.getByLabel("Shop").isDisabled(), "the shop can be changed while recording");
      expect(await page.getByLabel("Recording file").isDisabled(), "a file can be sent while recording");
      expect((await page.getByRole("link", { name: "Visits" }).count()) === 0, "the Visits link still leaves the page");
      await page.getByRole("button", { name: "Stop" }).click();
      await page.waitForURL(/\/visits\//);
    },
  },
  {
    name: "the Upload panel sends a phone recording and completes it",
    async run({ page, web, api }) {
      await openRecorder(page, web);
      await page.getByLabel("Recording file").setInputFiles({ name: "phone.wav", mimeType: "audio/wav", buffer: silentWav(1) });
      await page.getByRole("button", { name: "Send" }).click();
      await page.waitForURL(/\/visits\//);
      const visit = await (await fetch(`${api}/api/visits/${visitIdIn(page.url())}`)).json();
      expect(visit.client === "upload" && visit.completed_at !== null, `not completed: ${JSON.stringify(visit)}`);
    },
  },
  {
    name: "a failed transcript request is retried",
    async run({ page, web, seeded }) {
      let tries = 0;
      await page.route(`**/api/visits/${seeded.transcribed}/transcript`, (route) => {
        tries += 1;
        if (tries <= 2) return route.fulfill({ status: 503, json: { error: { code: "busy", message: "busy" } } });
        return route.continue();
      });
      await page.goto(`${web}/visits/${seeded.transcribed}`);
      await page.getByText("नमस्ते जी, कैसे हैं?").waitFor();
      expect(tries === 3, `the transcript was asked for ${tries} times`);
    },
  },
  {
    name: "clicking a transcript line plays the audio from there",
    async run({ page, web, seeded }) {
      await page.goto(`${web}/visits/${seeded.transcribed}`);
      await page.getByRole("button", { name: /scheme/ }).click(); // the turn at 11 s
      await page.waitForFunction(() => {
        const audio = document.querySelector("audio");
        return audio !== null && Math.abs(audio.currentTime - 11) <= 2;
      });
    },
  },
  {
    name: "a recording visit isn't polled",
    async run({ page, web, seeded }) {
      // Requests that finished: in development, StrictMode mounts twice and aborts the first.
      let reads = 0;
      page.on("requestfinished", (request) => new URL(request.url()).pathname === `/api/visits/${seeded.recording}` && (reads += 1));
      await page.goto(`${web}/visits/${seeded.recording}`);
      await page.getByText("Recording", { exact: true }).waitFor();
      await page.waitForTimeout(POLL_MS * 3 + 1_000);
      expect(reads === 1, `it was read ${reads} times`);
    },
  },
  CLICK_CHECK,
];

/** A WAV of silence, as a phone's voice recorder might save. */
function silentWav(seconds) {
  const rate = 16_000;
  const data = rate * seconds * 2;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data, 40);
  return Buffer.concat([header, Buffer.alloc(data)]);
}
