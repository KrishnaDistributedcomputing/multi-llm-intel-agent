import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const svgPath = path.join(root, "agent", "static", "architecture.svg");
execFileSync(process.execPath, [
  path.join(root, "node_modules", "@mermaid-js", "mermaid-cli", "src", "cli.js"),
  "-i", path.join(root, "docs", "architecture.mmd"),
  "-o", svgPath, "-b", "transparent", "-t", "neutral",
  "-c", path.join(root, "docs", "mermaid-config.json"),
  ...process.argv.slice(2),
], { stdio: "inherit" });
let svg = readFileSync(svgPath, "utf8");
if (/<script\b|\son[a-z]+\s*=/i.test(svg)) throw new Error("Active diagram content is forbidden.");
const links = [...svg.matchAll(/\b(?:xlink:)?href="([^"]*)"/g)];
if (links.length !== 10 || links.some(([, href]) => !/^#component-[a-z]+$/.test(href))) {
  throw new Error("The diagram must have ten internal component links.");
}
svg = svg.replace(/((?:xlink:)?href=)"(#component-[a-z]+)"/g, '$1"/$2"');
const htmlPath = path.join(root, "agent", "static", "index.html");
const html = readFileSync(htmlPath, "utf8");
const marker = /<!-- architecture:start -->[\s\S]*?<!-- architecture:end -->/g;
if ([...html.matchAll(marker)].length !== 1) throw new Error("Expected one architecture placeholder.");
const updated = html.replace(marker, () => `<!-- architecture:start -->\n${svg}\n<!-- architecture:end -->`);
writeFileSync(svgPath, svg);
writeFileSync(htmlPath, updated);
