// LCClips share server — one Cloudflare Worker + one R2 bucket. No dependencies.
//
// Public:
//   GET  /c/:id          share page (player + Open Graph tags so Discord/X embed the video)
//   GET  /v/:id.mp4      video (supports Range requests for seeking)
//   GET  /t/:id.jpg      thumbnail
// Private (Authorization: Bearer <UPLOAD_TOKEN>):
//   POST   /api/upload               {title,width,height,duration,size} -> {id, uploadId}
//   PUT    /api/upload/:id/part/:n?uploadId=...   raw bytes            -> {partNumber, etag}
//   PUT    /api/upload/:id/thumb     jpeg bytes
//   POST   /api/upload/:id/complete  {uploadId, parts:[{partNumber,etag}]} -> {id, url}
//   DELETE /api/clips/:id
//
// Uploads go through R2 multipart so clips of any size work despite the
// Workers 100 MB request-body limit (the client sends ~50 MB parts).

const ID_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ";
const ID_RE = /^[23456789a-zA-Z]{6,16}$/;

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (err) {
      if (err instanceof HttpError) return json({ error: err.message }, err.status);
      console.error(err);
      return json({ error: "internal error" }, 500);
    }
  },
};

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function route(request, env) {
  const url = new URL(request.url);
  const p = url.pathname;
  const m = request.method;
  let r;

  if (m === "GET" || m === "HEAD") {
    if ((r = p.match(/^\/c\/([^/]+)\/?$/))) return sharePage(env, url, id(r[1]));
    if ((r = p.match(/^\/v\/([^/]+)\.mp4$/))) return serveObject(env, request, `v/${id(r[1])}.mp4`, "video/mp4");
    if ((r = p.match(/^\/t\/([^/]+)\.jpg$/))) return serveObject(env, request, `t/${id(r[1])}.jpg`, "image/jpeg");
    if (p === "/" || p === "") return new Response(`${env.SITE_NAME || "LCClips"} share server is running.`, { headers: { "content-type": "text/plain" } });
    if (p === "/api/ping") { auth(request, env); return json({ ok: true }); }
    throw new HttpError(404, "not found");
  }

  auth(request, env);

  if (m === "POST" && p === "/api/upload") {
    const meta = await request.json().catch(() => ({}));
    const clipId = newId();
    const mp = await env.CLIPS.createMultipartUpload(`v/${clipId}.mp4`, {
      httpMetadata: { contentType: "video/mp4", cacheControl: "public, max-age=31536000, immutable" },
    });
    await putMeta(env, clipId, {
      title: String(meta.title || "Clip").slice(0, 120),
      width: int(meta.width), height: int(meta.height),
      duration: Number(meta.duration) || 0,
      size: int(meta.size),
      created: new Date().toISOString(),
      complete: false,
    });
    return json({ id: clipId, uploadId: mp.uploadId });
  }

  if (m === "PUT" && (r = p.match(/^\/api\/upload\/([^/]+)\/part\/(\d+)$/))) {
    const clipId = id(r[1]);
    const partNumber = Number(r[2]);
    const uploadId = url.searchParams.get("uploadId");
    if (!uploadId || partNumber < 1 || partNumber > 10000) throw new HttpError(400, "bad part");
    const mp = env.CLIPS.resumeMultipartUpload(`v/${clipId}.mp4`, uploadId);
    const part = await mp.uploadPart(partNumber, request.body);
    return json({ partNumber: part.partNumber, etag: part.etag });
  }

  if (m === "PUT" && (r = p.match(/^\/api\/upload\/([^/]+)\/thumb$/))) {
    const clipId = id(r[1]);
    await env.CLIPS.put(`t/${clipId}.jpg`, request.body, {
      httpMetadata: { contentType: "image/jpeg", cacheControl: "public, max-age=31536000, immutable" },
    });
    return json({ ok: true });
  }

  if (m === "POST" && (r = p.match(/^\/api\/upload\/([^/]+)\/complete$/))) {
    const clipId = id(r[1]);
    const body = await request.json();
    const mp = env.CLIPS.resumeMultipartUpload(`v/${clipId}.mp4`, body.uploadId);
    await mp.complete(body.parts);
    const meta = (await getMeta(env, clipId)) || {};
    meta.complete = true;
    meta.thumb = !!(await env.CLIPS.head(`t/${clipId}.jpg`));
    await putMeta(env, clipId, meta);
    return json({ id: clipId, url: `${url.origin}/c/${clipId}` });
  }

  if (m === "DELETE" && (r = p.match(/^\/api\/clips\/([^/]+)$/))) {
    const clipId = id(r[1]);
    await env.CLIPS.delete([`v/${clipId}.mp4`, `t/${clipId}.jpg`, `m/${clipId}.json`]);
    return json({ ok: true });
  }

  throw new HttpError(404, "not found");
}

// ---------------------------------------------------------------- helpers

function auth(request, env) {
  const want = env.UPLOAD_TOKEN;
  if (!want) throw new HttpError(500, "UPLOAD_TOKEN secret is not set on the worker");
  const got = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!timingSafeEqual(got, want)) throw new HttpError(401, "bad upload token");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function id(s) {
  if (!ID_RE.test(s)) throw new HttpError(404, "not found");
  return s;
}

function newId(len = 8) {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  let out = "";
  for (const b of bytes) out += ID_ALPHABET[b % ID_ALPHABET.length];
  return out;
}

const int = (v) => Math.max(0, Math.round(Number(v) || 0));

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

async function getMeta(env, clipId) {
  const o = await env.CLIPS.get(`m/${clipId}.json`);
  return o ? o.json() : null;
}

function putMeta(env, clipId, meta) {
  return env.CLIPS.put(`m/${clipId}.json`, JSON.stringify(meta), { httpMetadata: { contentType: "application/json" } });
}

function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec((header || "").trim());
  if (!m || (m[1] === "" && m[2] === "")) return null;
  let start, end;
  if (m[1] === "") {
    const n = Math.min(Number(m[2]), size);
    start = size - n;
    end = size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  if (start > end || start >= size) return "invalid";
  return { offset: start, length: end - start + 1 };
}

async function serveObject(env, request, key, type) {
  const head = await env.CLIPS.head(key);
  if (!head) throw new HttpError(404, "not found");
  const size = head.size;
  const headers = new Headers({
    "content-type": type,
    "accept-ranges": "bytes",
    etag: head.httpEtag,
    "cache-control": "public, max-age=31536000, immutable",
    "access-control-allow-origin": "*",
  });
  if (request.headers.get("if-none-match") === head.httpEtag) return new Response(null, { status: 304, headers });

  const range = parseRange(request.headers.get("range"), size);
  if (range === "invalid") {
    headers.set("content-range", `bytes */${size}`);
    return new Response(null, { status: 416, headers });
  }
  if (request.method === "HEAD") {
    headers.set("content-length", String(size));
    return new Response(null, { status: 200, headers });
  }
  const obj = await env.CLIPS.get(key, range ? { range } : undefined);
  if (!obj) throw new HttpError(404, "not found");
  if (range) {
    headers.set("content-range", `bytes ${range.offset}-${range.offset + range.length - 1}/${size}`);
    headers.set("content-length", String(range.length));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("content-length", String(size));
  return new Response(obj.body, { status: 200, headers });
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function sharePage(env, url, clipId) {
  const meta = await getMeta(env, clipId);
  if (!meta || !meta.complete) {
    return new Response(page("Clip not found", `<p class="muted">This clip doesn't exist or was deleted.</p>`, ""), {
      status: 404, headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  const site = env.SITE_NAME || "LCClips";
  const video = `${url.origin}/v/${clipId}.mp4`;
  const thumb = meta.thumb ? `${url.origin}/t/${clipId}.jpg` : "";
  const w = meta.width || 1280, h = meta.height || 720;
  const title = meta.title || "Clip";
  const date = meta.created ? new Date(meta.created).toUTCString().replace(/:\d\d GMT$/, " UTC") : "";
  const og = `
  <meta property="og:site_name" content="${esc(site)}">
  <meta property="og:title" content="${esc(title)}">
  <meta property="og:type" content="video.other">
  <meta property="og:url" content="${esc(url.href)}">
  <meta property="og:video" content="${esc(video)}">
  <meta property="og:video:url" content="${esc(video)}">
  <meta property="og:video:secure_url" content="${esc(video)}">
  <meta property="og:video:type" content="video/mp4">
  <meta property="og:video:width" content="${w}">
  <meta property="og:video:height" content="${h}">
  ${thumb ? `<meta property="og:image" content="${esc(thumb)}">` : ""}
  <meta name="twitter:card" content="player">
  <meta name="twitter:title" content="${esc(title)}">
  <meta name="twitter:player" content="${esc(video)}">
  <meta name="twitter:player:stream" content="${esc(video)}">
  <meta name="twitter:player:stream:content_type" content="video/mp4">
  <meta name="twitter:player:width" content="${w}">
  <meta name="twitter:player:height" content="${h}">
  ${thumb ? `<meta name="twitter:image" content="${esc(thumb)}">` : ""}
  <meta name="theme-color" content="#e0323c">`;
  const body = `
    <video controls autoplay playsinline preload="metadata" ${thumb ? `poster="${esc(thumb)}"` : ""} style="aspect-ratio:${w}/${h}">
      <source src="${esc(video)}" type="video/mp4">
    </video>
    <div class="row">
      <div><h1>${esc(title)}</h1><p class="muted">${esc(date)}${meta.duration ? ` · ${Math.round(meta.duration)}s` : ""}</p></div>
      <a class="btn" href="${esc(video)}" download="${esc(title)}.mp4">Download</a>
    </div>`;
  return new Response(page(`${title} · ${site}`, body, og), {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=60" },
  });
}

function page(title, body, head) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${esc(title)}</title>${head}
  <style>
    :root{color-scheme:dark}
    body{margin:0;background:#0f1115;color:#e8e9ed;font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
    main{max-width:1100px;margin:0 auto;padding:24px 16px}
    video{width:100%;max-height:80vh;background:#000;border-radius:10px;display:block}
    .row{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-top:14px}
    h1{font-size:18px;margin:0 0 4px;word-break:break-word}
    .muted{color:#8b8f9a;margin:0}
    .btn{flex:none;background:#e0323c;color:#fff;text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:600}
    .btn:hover{background:#f04450}
    .brand{color:#8b8f9a;font-weight:700;letter-spacing:.04em;margin-bottom:14px;font-size:13px}
  </style></head><body><main><div class="brand">LCCLIPS</div>${body}</main></body></html>`;
}
