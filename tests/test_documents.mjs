import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import assert from "node:assert/strict";

const html = readFileSync(new URL("../agent/static/index.html", import.meta.url), "utf8");
const source = html.match(/<script id="document-functions">([\s\S]*?)<\/script>/)[1];
const context = vm.createContext({ TextEncoder });
vm.runInContext(source, context);
const { splitDocument, chunkMessage, agentBody } = context;
const encoder = new TextEncoder();

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
