/**
 * Deterministic fixture server for the fetch_url bulk vs single benchmark.
 *
 * Serves N self-contained article pages at /page/<i>. Each page is a real
 * HTML document (nav/header/footer chrome + an <article> body of ~1500 words)
 * so fetch_url's extractor does real work and returns a non-trivial payload —
 * the per-response token cost is then measured, not assumed. Content is fixed
 * (seeded by index) so both benchmark arms read byte-identical pages and the
 * only variable is sequential-vs-bulk call shape.
 */

import { createServer } from "node:http";

const TOPICS = [
  "token compression",
  "content extraction",
  "graph-backed retrieval",
  "cache reuse",
  "passage ranking",
  "blast-radius analysis",
  "drift detection",
  "wire-cap pagination",
];

function paragraph(topic, i) {
  return `<p>Section ${i + 1} on ${topic}: A coding agent that cannot hold a large repository in context still has to change it safely. The system hands the agent the live call graph and the rules anchored to each entity at the moment it edits, then re-anchors those rules when the code moves so they never go silently stale. ${topic} is the part of the pipeline that decides which bytes are worth putting on the wire: it scores each candidate node by link density and content-text ratio so the article body wins over navigation chrome, then converts the survivors to heading-bounded markdown passages that paginate naturally. This paragraph is deliberately long enough to keep the extractor's content-density score well above the noise floor it compares every node against, which is what lets the article body survive while the surrounding boilerplate is dropped before conversion.</p>`;
}

export function articleHtml(index) {
  const topic = TOPICS[index % TOPICS.length];
  const body = Array.from({ length: 12 }, (_, i) => paragraph(topic, i)).join(
    "\n"
  );
  return `<!doctype html><html lang="en">
<head><title>Article ${index + 1}: ${topic}</title>
<meta name="description" content="A fixture article about ${topic} for the fetch_url benchmark."></head>
<body>
  <nav><a href="/page/0">Home</a> <a href="/about">About</a> <a href="/docs">Docs</a></nav>
  <header><div class="logo">FixtureSite</div><div class="ad">Subscribe now for chrome you do not want</div></header>
  <main><article>
    <h1>Article ${index + 1}: ${topic}</h1>
    <p class="byline">Posted by the benchmark harness</p>
    ${body}
    <h2>How ${topic} works in practice</h2>
    <p>The pipeline runs the primary extractor first and falls back to a secondary reader when the first returns no content, then to a raw-body tag-strip when both time out. Each stage shares one accounting path so before/after byte counts are apples-to-apples.</p>
    <h2>Conclusion</h2>
    <p>Heading-bounded passages make pagination natural and let a ranking signal use the heading hierarchy as relevance evidence. ${topic} is one of several stages; the others are covered in the linked docs.</p>
  </article></main>
  <footer>Footer chrome — copyright, social links, and a cookie banner nobody reads.</footer>
</body></html>`;
}

export function startFixtureServer(pageCount) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const url = req.url ?? "/";
      const m = url.match(/^\/page\/(\d+)/);
      if (!m) {
        res.writeHead(404, { "content-type": "text/html; charset=utf-8" });
        res.end("<html><body>Not found</body></html>");
        return;
      }
      const idx = Number(m[1]);
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(articleHtml(idx));
    });
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      const origin = `http://127.0.0.1:${port}`;
      const urls = Array.from(
        { length: pageCount },
        (_, i) => `${origin}/page/${i}`
      );
      resolve({ server, origin, urls });
    });
  });
}
