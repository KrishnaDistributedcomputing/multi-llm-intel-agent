import http from "node:http";
import { readFile } from "node:fs/promises";

const routes = new Set([
  "/models", "/agent", "/healthz", "/readyz", "/usage",
  "/ui-api/models", "/ui-api/agent", "/ui-api/usage",
]);
http.createServer(async (req, res) => {
  if (req.url === "/" && req.method === "GET") {
    res.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
      "X-Frame-Options": "DENY",
      "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; " +
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; " +
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    });
    try {
      res.end(await readFile(new URL("./agent/static/index.html", import.meta.url)));
    } catch (error) {
      console.error("Cannot read chat UI:", error.message);
      res.destroy(error);
    }
    return;
  }
  const pathname = new URL(req.url, "http://localhost").pathname;
  const documentRoute = /^\/(?:ui-api\/)?document-workflows(?:\/(?:config|[0-9a-f-]{36})(?:\/cancel)?)?$/.test(pathname);
  if (!routes.has(pathname) && !documentRoute) {
    res.writeHead(404).end();
    return;
  }
  const upstream = http.request({
    hostname: "127.0.0.1", port: 8000, path: req.url, method: req.method,
    headers: req.headers,
    timeout: 490000,
  }, (response) => {
    res.writeHead(response.statusCode, response.headers);
    response.pipe(res);
  });
  upstream.on("timeout", () => upstream.destroy());
  upstream.on("error", () => {
    if (!res.headersSent) res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Local API is unavailable on port 8000" }));
  });
  req.pipe(upstream);
}).listen(Number(process.env.PORT || 3001), "127.0.0.1", () => {
  console.log(`Chat preview: http://localhost:${process.env.PORT || 3001}`);
});
