import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import assert from "node:assert/strict";

const html = readFileSync(new URL("../agent/static/index.html", import.meta.url), "utf8");
const source = html.match(/<script id="document-functions">([\s\S]*?)<\/script>/)[1];
const context = vm.createContext({ TextEncoder });
vm.runInContext(source, context);
const { splitDocument, chunkMessage, agentBody, workflowProgressText, documentDemo,
  estimateROI, evaluationCases, scoreEvaluation } = context;
const encoder = new TextEncoder();

test("EVAL explains its questions, result meanings, and limits without changing the checks", () => {
  const panel = html.split('<section id="eval-panel"')[1].split('<section id="documentation-panel"')[0];
  for (const text of ["EVAL means evaluation", "How to try it", "Understand your results",
    "<strong>Pass:</strong>", "<strong>Fail:</strong>", "<strong>Error:</strong>",
    "<strong>Not run:</strong>", "3/3 passed means", "not mean the model is 100% accurate",
    'id="eval-questions-guide"', 'id="eval-cost-guide"', "shared by everyone",
    "without automatic retries", "browser's memory"]) {
    assert.ok(panel.includes(text), `Missing plain-language EVAL guidance: ${text}`);
  }
  const samples = evaluationCases();
  assert.equal(samples.length, 3);
  assert.ok(panel.includes(samples.find(sample => sample.id === "ticket").expected));
  for (const sample of samples.filter(sample => sample.number !== undefined)) {
    assert.ok(panel.includes(`<code>${sample.number}</code>`));
  }
});

const roiInputs = { volume: 200, manual: 10, review: 6, rate: 30, adoption: 50,
  hosting: 60, tokens: 5, setup: 500 };

test("ROI measures net first-year value, total-cost return, and setup payback", () => {
  const result = estimateROI(roiInputs);
  assert.ok(Math.abs(result.gross - 200) < 0.000001);
  assert.ok(Math.abs(result.net - 135) < 0.000001);
  assert.ok(Math.abs(result.yearNet - 1120) < 0.000001);
  assert.ok(Math.abs(result.roi - 87.5) < 0.000001);
  assert.ok(Math.abs(result.payback - 500 / 135) < 0.000001);
  assert.equal(result.breakEven, 65);
});

test("ROI retains losses and handles zero adoption, zero costs, and immediate payback", () => {
  const loss = estimateROI({ ...roiInputs, review: 12 });
  assert.ok(loss.hours < 0 && loss.net < 0);
  assert.equal(loss.payback, null);
  assert.equal(loss.breakEven, null);
  assert.equal(estimateROI({ ...roiInputs, adoption: 0 }).net, -65);
  assert.equal(estimateROI({ ...roiInputs, rate: 0 }).breakEven, null);
  const free = estimateROI({ ...roiInputs, hosting: 0, tokens: 0, setup: 0 });
  assert.equal(free.roi, null);
  assert.equal(free.payback, 0);
  assert.equal(free.breakEven, 0);
});

test("ROI rejects missing, nonfinite, negative, fractional-volume, and out-of-range inputs", () => {
  for (const [name, value] of [["volume", -1], ["volume", 1.5], ["adoption", 101],
    ["rate", NaN], ["hosting", Infinity], ["tokens", undefined], ["setup", "500"]]) {
    assert.throws(() => estimateROI({ ...roiInputs, [name]: value }));
  }
});

test("synthetic evaluation scores output contracts without accepting prose or altered identifiers", () => {
  assert.equal(evaluationCases().length, 3);
  for (const sample of evaluationCases()) assert.doesNotThrow(() => agentBody("azure", sample.prompt));
  assert.equal(scoreEvaluation("arithmetic", "42.0").passed, true);
  assert.equal(scoreEvaluation("arithmetic", "The result is 42").passed, false);
  assert.equal(scoreEvaluation("arithmetic", "142").passed, false);
  assert.equal(scoreEvaluation("discount", "68").passed, true);
  assert.equal(scoreEvaluation("discount", "$68").passed, false);
  assert.equal(scoreEvaluation("ticket", '{"owner":"Nia","priority":"high","ticket_id":"DEMO-42"}').passed, true);
  for (const answer of ['{"ticket_id":"DEM-42","priority":"high","owner":"Nia"}',
    '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia","extra":1}', "null", "[]", "```json\n{}\n```"]) {
    assert.equal(scoreEvaluation("ticket", answer).passed, false);
  }
  assert.throws(() => scoreEvaluation("unknown", "42"));
  assert.throws(() => scoreEvaluation("arithmetic", null));
});

test("all prebuilt chat prompts fit the real request shape", () => {
  const prompts = [...html.matchAll(/data-prompt="([^"]+)"/g)].map(match => match[1]);
  assert.equal(prompts.length, 6);
  assert.equal(new Set(prompts).size, 6);
  assert.ok(prompts.some(prompt => prompt.includes("preserving every character") &&
    prompt.includes("ticket_id=DEMO-42; priority=high; owner=Nia")));
  for (const prompt of prompts) {
    assert.equal(JSON.parse(agentBody("local-qwen3-tiny", prompt)).message, prompt);
  }
});

for (const id of ["meeting", "support", "invoice"]) {
  test(`${id} synthetic document splits losslessly and fits chunk requests`, () => {
    const example = documentDemo(id);
    assert.match(example.name, /^synthetic-.*\.txt$/);
    assert.ok(example.text.startsWith("SYNTHETIC ONLY:"));
    assert.ok(example.instruction.trim().length > 0 && example.instruction.length <= 600);
    assert.ok(html.includes(`<option value="${id}">`));
    const chunks = splitDocument(example.text, 256, 32);
    assert.ok(chunks.length > 1);
    let covered = 0;
    for (const chunk of chunks) {
      assert.ok(chunk.start <= covered && chunk.end > covered);
      assert.equal(chunk.text, example.text.slice(chunk.start, chunk.end));
      const body = JSON.parse(agentBody("local-qwen3-tiny", chunkMessage(example.instruction, chunk)));
      assert.equal(body.model, "local-qwen3-tiny");
      assert.ok(body.message.includes(chunk.text));
      covered = chunk.end;
    }
    assert.equal(covered, example.text.length);
    example.text = "edited";
    assert.notEqual(documentDemo(id).text, "edited");
  });
}

test("unknown document examples fail explicitly", () => {
  for (const id of ["", "missing", "__proto__", "toString"]) {
    assert.throws(() => documentDemo(id), /Choose a known synthetic document example/);
  }
});

test("document example loader is wired at startup, stays local, and resets consent", async () => {
  const elements = new Map(), events = new Map(), requests = [];
  let modelData = { models: [], cloud_enabled: false, enabled: false };
  let confirmCloud = false, confirmations = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", hidden: false, disabled: false, checked: false,
      children: [], dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {} },
      setAttribute() {}, focus() {}, scrollIntoView() {},
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
      addEventListener(name, callback) { events.set(`${id}:${name}`, callback); },
    });
    return elements.get(id);
  }
  element("chunk-size").value = "256";
  element("chunk-overlap").value = "32";
  element("document-example").value = "meeting";
  for (const [name, value] of Object.entries(roiInputs)) element(`roi-${name}`).value = String(value);
  let clock = 0;
  const browser = vm.createContext({
    TextEncoder, TextDecoder,
    performance: { now: () => clock += 10 },
    document: {
      body: { dataset: {} },
      getElementById: element,
      createElement: name => element(`created-${name}-${elements.size}`),
      createTextNode: text => ({ textContent: text }),
      querySelector: element,
      querySelectorAll: () => [],
      addEventListener: (name, callback) => events.set(`document:${name}`, callback),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => url === "/ui-api/agent" ? { answer: "Synthetic answer" } : modelData };
    },
    window: { confirm() { ++confirmations; return confirmCloud; } },
    setInterval() {}, setTimeout() {}, clearTimeout() {},
  });
  for (const match of html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)) {
    vm.runInContext(match[1], browser);
  }
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof browser.prepareDocument, "function");
  assert.equal(element("roi-net").textContent, "$135.00");
  element("roi-volume").value = "";
  browser.renderROI();
  assert.equal(element("roi-results").hidden, true);
  assert.match(element("roi-status").textContent, /Enter a value/);
  element("roi-volume").value = "200";
  browser.renderROI();
  assert.equal(element("roi-results").hidden, false);
  const load = events.get("document-example-load:click");
  assert.equal(typeof load, "function");
  const requestCount = requests.length;
  load();
  assert.equal(element("document-results").hidden, false);
  assert.ok(Number(element("chunk-number").max) > 1);
  assert.match(element("chunk-preview").textContent, /^SYNTHETIC ONLY:/);
  assert.match(element("document-status").textContent, /Nothing sent/);
  assert.equal(element("chunk-instruction").value, documentDemo("meeting").instruction);
  element("workflow-consent").checked = true;
  element("document-example").value = "support";
  load();
  assert.equal(element("workflow-consent").checked, false);
  assert.equal(requests.length, requestCount);
  browser.prepareDocument("uploaded.txt", "Synthetic uploaded text.", 24);
  assert.equal(element("chunk-preview").textContent, "Synthetic uploaded text.");
  const cloud = { id: "azure", label: "Azure nano", kind: "cloud", ready: true, enabled: true,
    input_usd_per_million: 0.1, output_usd_per_million: 0.4 };
  modelData = { models: [cloud, { id: "local", label: "Local", kind: "local", ready: true, enabled: true }],
    cloud_enabled: true, cloud_budget: { remaining_usd: 0.9, daily_limit_usd: 1, utc_day: "2026-10-24" } };
  await browser.refresh();
  assert.equal(element("model").value, "local", "paid models must not be selected automatically");
  assert.equal(browser.document.body.dataset.route, "local");
  assert.match(element("cloud-budget").textContent, /\$0.900 of \$1.00/);
  assert.match(element("model").children[0].textContent, /Paid cloud.*\$0.1 in, \$0.4 out/);
  element("model").value = "azure";
  events.get("model:change")();
  assert.equal(browser.document.body.dataset.route, "cloud");
  const beforeDocumentation = requests.length;
  events.get("documentation-tab:click")();
  assert.equal(element("documentation-panel").hidden, false);
  assert.equal(element("chat-panel").hidden, true);
  assert.equal(element("documents-panel").hidden, true);
  assert.equal(element("page-title").textContent, "Documentation");
  assert.equal(browser.document.body.dataset.route, "cloud");
  assert.equal(requests.length, beforeDocumentation);
  events.get(".tab-list:keydown")({ key: "Home", preventDefault() {} });
  assert.equal(element("chat-panel").hidden, false);
  assert.equal(element("documentation-panel").hidden, true);
  events.get(".tab-list:keydown")({ key: "End", preventDefault() {} });
  assert.equal(element("eval-panel").hidden, false);
  events.get("document:click")({ target: { closest: () => ({
    getAttribute: name => name === "href" ? "/#component-agent" : null,
  }) }, preventDefault() {} });
  assert.equal(element("component-agent").open, true);
  assert.equal(element("documentation-panel").hidden, false);
  element("model").value = "local";
  events.get("model:change")();
  assert.equal(browser.document.body.dataset.route, "local");
  element("model").value = "azure";
  element("prompt").value = "Synthetic cloud request";
  const beforeDecline = requests.length;
  await browser.sendMessage(element("prompt").value);
  assert.equal(confirmations, 1);
  assert.equal(requests.length, beforeDecline);
  assert.equal(element("prompt").value, "Synthetic cloud request");
  confirmCloud = true;
  await browser.sendMessage(element("prompt").value);
  assert.equal(confirmations, 2);
  assert.equal(requests.length, beforeDecline + 1);
  assert.equal(JSON.parse(requests.at(-1).options.body).model, "azure");
  const evaluationCount = requests.length;
  confirmCloud = false;
  await browser.runEvaluation();
  assert.equal(requests.length, evaluationCount, "declining an evaluation must send nothing");
  confirmCloud = true;
  const standardFetch = browser.fetch;
  const expectedAnswers = ["42", "68", '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia"}'];
  browser.fetch = async (url, options) => {
    requests.push({ url, options });
    assert.equal(url, "/ui-api/agent");
    assert.equal(JSON.parse(options.body).model, "azure");
    assert.equal(JSON.parse(options.body).tool_choice, expectedAnswers.length === 1 ? "none" : undefined);
    return { ok: true, json: async () => ({ model: "azure", answer: expectedAnswers.shift() }) };
  };
  await browser.runEvaluation();
  assert.equal(requests.length, evaluationCount + 3);
  assert.match(element("eval-score").textContent, /^3\/3 passed/);
  assert.equal(vm.runInContext("evaluationReport.status", browser), "completed");
  assert.equal(element("eval-download").disabled, false);
  browser.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: false, status: 429, json: async () => ({ detail: "Shared cloud rate limit reached" }) };
  };
  const beforeError = requests.length;
  await browser.runEvaluation();
  assert.equal(requests.length, beforeError + 1, "errors must stop remaining checks without retry");
  assert.match(element("eval-score").textContent, /1 request errors; 2 not run/);
  assert.equal(vm.runInContext("evaluationReport.status", browser), "error");
  browser.fetch = async (url, options) => {
    requests.push({ url, options });
    events.get("eval-stop:click")();
    return { ok: true, json: async () => ({ model: "azure", answer: "42" }) };
  };
  const beforeStop = requests.length;
  await browser.runEvaluation();
  assert.equal(requests.length, beforeStop + 1);
  assert.equal(vm.runInContext("evaluationReport.status", browser), "stopped");
  assert.match(element("eval-score").textContent, /2 not run/);
  element("model").value = "local";
  confirmCloud = false;
  const beforeLocalConfirmation = confirmations;
  const localAnswers = ["41", "68", '{"ticket_id":"DEMO-42","priority":"high","owner":"Nia"}'];
  browser.fetch = async (url, options) => {
    requests.push({ url, options });
    assert.equal(JSON.parse(options.body).model, "local");
    return { ok: true, json: async () => ({ model: "local", answer: localAnswers.shift() }) };
  };
  await browser.runEvaluation();
  assert.equal(confirmations, beforeLocalConfirmation, "local evaluation needs no paid confirmation");
  assert.match(element("eval-score").textContent, /^2\/3 passed; 1 failed output checks/);
  assert.equal(vm.runInContext("evaluationReport.status", browser), "completed");
  browser.fetch = standardFetch;
  element("model").value = "";
  modelData.models = [cloud];
  await browser.refresh();
  assert.equal(element("model").value, "");
  assert.equal(element("send").disabled, true);
  assert.equal(browser.document.body.dataset.route, "local");
});

for (const [label, text] of [
  ["paragraphs", "Paragraph one. Words and spaces.\r\n\r\n".repeat(400)],
  ["unbroken words", "x".repeat(10001)],
  ["Unicode", "\u{1F600}\u4E2D\u6587 caf\u00e9\n".repeat(800)],
  ["JSON escapes", 'quote" slash\\ tab\t newline\n'.repeat(500)],
]) {
  for (const overlap of [0, 100, 128]) {
    test(`${label}, overlap ${overlap}: bounded, lossless and progressing`, () => {
      const chunks = splitDocument(text, 256, overlap);
      let covered = 0;
      for (const chunk of chunks) {
        assert.ok(chunk.start <= covered);
        assert.ok(chunk.end > covered);
        assert.equal(chunk.text, text.slice(chunk.start, chunk.end));
        assert.ok(chunk.bytes <= 256);
        assert.equal(chunk.bytes, encoder.encode(chunk.text).length);
        assert.ok(!/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/.test(chunk.text));
        if (chunk.start < covered) assert.ok(encoder.encode(text.slice(chunk.start, covered)).length <= overlap);
        covered = chunk.end;
      }
      assert.equal(covered, text.length);
    });
  }
}

test("invalid inputs fail explicitly", () => {
  for (const [text, size, overlap] of [[" ", 1800, 160], ["a\0b", 1800, 160],
    ["abc", 255, 0], ["abc", 2401, 0], ["abc", 256, 129],
    ["abc", 256, -1], ["abc", 256.5, 0], ["abc", 256, NaN],
    ["x".repeat(10 * 1024 * 1024 + 1), 1800, 160]]) {
    assert.throws(() => splitDocument(text, size, overlap));
  }
});

test("short documents and exact limits preserve all source text", () => {
  for (const text of ["Short.", "a".repeat(256), "\uFEFF Title\r\n\r\nBody"]) {
    const chunks = splitDocument(text, 256, 0);
    assert.equal(chunks.length, 1);
    assert.equal(chunks[0].text, text);
  }
  assert.equal(splitDocument("abc", 256, 0)[0].index, 1);
});

test("a 10 MiB document is accepted without gaps", () => {
  const text = "x".repeat(10 * 1024 * 1024);
  const chunks = splitDocument(text, 1800, 160);
  assert.equal(chunks[0].start, 0);
  assert.equal(chunks.at(-1).end, text.length);
  assert.ok(chunks.every((chunk, index) => chunk.bytes <= 1800 && (!index || chunk.start === chunks[index - 1].end - 160)));
});

test("prefer paragraph boundaries and retain overlap", () => {
  const text = "a".repeat(210) + "\n\n" + "b".repeat(300);
  const chunks = splitDocument(text, 256, 20);
  assert.equal(chunks[0].end, 212);
  assert.equal(chunks[1].start, 192);
});

test("prompt and actual escaped request fit API limits", () => {
  const message = chunkMessage("Summarize", { text: "x".repeat(2400) });
  const body = agentBody("local-llama32-small", message);
  assert.equal(JSON.parse(body).message, message);
  assert.throws(() => chunkMessage(" ", { text: "x" }));
  assert.throws(() => chunkMessage("x".repeat(601), { text: "x" }));
  assert.throws(() => agentBody("model", "x".repeat(6001)));
  assert.throws(() => agentBody("model", "\u0001".repeat(3000)));
  assert.throws(() => agentBody("model", chunkMessage("\u0001".repeat(600), { text: "\u0001".repeat(2400) })));
});

test("all inline scripts parse", () => {
  for (const match of html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});

test("workflow progress identifies the active chunk instead of guessing from completion", () => {
  assert.equal(workflowProgressText({
    status: "RUNNING", completed: 2, total: 20, current_chunk: 7, chunk_state: "processing",
  }), "Processing chunk 7 of 20. 2 of 20 chunks completed.");
});

test("queued, legacy and terminal progress do not claim an active chunk", () => {
  const base = { status: "RUNNING", completed: 2, total: 20 };
  assert.equal(workflowProgressText({ ...base, current_chunk: null, chunk_state: "queued" }),
    "Waiting for a worker to start the next chunk. 2 of 20 chunks completed.");
  assert.equal(workflowProgressText({ ...base, current_chunk: null, chunk_state: "processing" }),
    "Processing a chunk (waiting for its index update). 2 of 20 chunks completed.");
  assert.equal(workflowProgressText({ ...base, status: "COMPLETED", completed: 20,
    current_chunk: 20, chunk_state: "processing" }), "COMPLETED: 20 of 20 chunks completed.");
  assert.equal(workflowProgressText({ ...base, status: "FAILED", current_chunk: 3,
    chunk_state: "processing" }), "FAILED: 2 of 20 chunks completed.");
});

test("workflow cancellation identifies the actual chunk", () => {
  assert.equal(workflowProgressText({
    status: "RUNNING", completed: 2, total: 20, current_chunk: 3, chunk_state: "cancelling",
  }), "Cancellation requested for chunk 3 of 20. 2 of 20 chunks completed.");
});
