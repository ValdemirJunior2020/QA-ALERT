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
  let base = String(value || "").trim().replace(/\/+$/, "");
  if (!base) return "";
  if (!/^https?:\/\//i.test(base)) base = `https://${base}`;
  return base;
}

export default async (req) => {
  const backend = cleanBase(Netlify.env.get("QA_ALERT_BACKEND_URL"));
  if (!backend) {
    return Response.json(
      { detail: "Netlify is ready, but QA_ALERT_BACKEND_URL is not configured yet." },
      { status: 503 },
    );
  }

  let backendUrl;
  try {
    backendUrl = new URL(backend);
  } catch {
    return Response.json(
      {
        detail: "QA_ALERT_BACKEND_URL is invalid.",
        expected: "Use a full Cloudflare backend address such as https://qa-api.hotelplannerqa.com",
      },
      { status: 503 },
    );
  }

  const incomingUrl = new URL(req.url);

  // Prevent an accidental proxy loop if the Netlify frontend domain is entered
  // as the local QA backend address.
  if (backendUrl.hostname === incomingUrl.hostname) {
    return Response.json(
      {
        detail: "QA_ALERT_BACKEND_URL points back to this Netlify site. Use a separate Cloudflare Tunnel hostname for the local backend, for example https://qa-api.hotelplannerqa.com.",
      },
      { status: 503 },
    );
  }

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
        backend,
        error: String(error?.message || error),
      },
      { status: 502 },
    );
  }
};

export const config = {
  path: "/api/*",
};
