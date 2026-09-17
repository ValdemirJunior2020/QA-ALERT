const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

function cleanBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

export default async (req) => {
  const backend = cleanBase(Netlify.env.get("QA_ALERT_BACKEND_URL"));
  if (!backend) {
    return Response.json(
      { detail: "Netlify is ready, but QA_ALERT_BACKEND_URL is not configured yet." },
      { status: 503 },
    );
  }

  const incomingUrl = new URL(req.url);
  const target = `${backend}${incomingUrl.pathname}${incomingUrl.search}`;

  const headers = new Headers(req.headers);
  for (const key of HOP_BY_HOP) headers.delete(key);

  const cfClientId = Netlify.env.get("CF_ACCESS_CLIENT_ID");
  const cfClientSecret = Netlify.env.get("CF_ACCESS_CLIENT_SECRET");
  if (cfClientId && cfClientSecret) {
    headers.set("cf-access-client-id", cfClientId);
    headers.set("cf-access-client-secret", cfClientSecret);
  }

  const init = {
    method: req.method,
    headers,
    redirect: "manual",
  };

  if (!["GET", "HEAD"].includes(req.method)) {
    init.body = await req.arrayBuffer();
  }

  try {
    const response = await fetch(target, init);
    const responseHeaders = new Headers(response.headers);
    for (const key of HOP_BY_HOP) responseHeaders.delete(key);
    responseHeaders.delete("set-cookie");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    return Response.json(
      {
        detail: "QA ALERT backend is unreachable through the configured Cloudflare URL.",
        error: String(error?.message || error),
      },
      { status: 502 },
    );
  }
};

export const config = {
  path: "/api/*",
};
