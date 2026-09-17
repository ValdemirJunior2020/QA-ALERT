const HOP_BY_HOP = new Set([
  'connection','keep-alive','proxy-authenticate','proxy-authorization',
  'te','trailers','transfer-encoding','upgrade','host','content-length'
]);

function cleanBase(value) {
  return String(value || '').trim().replace(/\/+$/, '');
}

exports.handler = async function handler(event) {
  const backend = cleanBase(process.env.QA_ALERT_BACKEND_URL);
  if (!backend) {
    return {
      statusCode: 503,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        detail: 'Netlify is ready, but QA_ALERT_BACKEND_URL is not configured yet.'
      })
    };
  }

  const path = event.path.replace(/^\/\.netlify\/functions\/api-proxy/, '') || '/';
  const query = event.rawQuery ? `?${event.rawQuery}` : '';
  const target = `${backend}/api${path}${query}`;

  const headers = {};
  for (const [key, value] of Object.entries(event.headers || {})) {
    const lower = key.toLowerCase();
    if (!HOP_BY_HOP.has(lower) && value != null) headers[lower] = value;
  }

  // Optional Cloudflare Access service-token support.
  if (process.env.CF_ACCESS_CLIENT_ID && process.env.CF_ACCESS_CLIENT_SECRET) {
    headers['cf-access-client-id'] = process.env.CF_ACCESS_CLIENT_ID;
    headers['cf-access-client-secret'] = process.env.CF_ACCESS_CLIENT_SECRET;
  }

  const method = event.httpMethod || 'GET';
  const init = { method, headers, redirect: 'manual' };
  if (!['GET', 'HEAD'].includes(method) && event.body != null) {
    init.body = event.isBase64Encoded
      ? Buffer.from(event.body, 'base64')
      : event.body;
  }

  try {
    const response = await fetch(target, init);
    const responseHeaders = {};
    response.headers.forEach((value, key) => {
      const lower = key.toLowerCase();
      if (!HOP_BY_HOP.has(lower) && lower !== 'set-cookie') responseHeaders[key] = value;
    });

    const contentType = response.headers.get('content-type') || '';
    const disposition = response.headers.get('content-disposition');
    if (disposition) responseHeaders['content-disposition'] = disposition;

    const buffer = Buffer.from(await response.arrayBuffer());
    const isText =
      contentType.startsWith('text/') ||
      contentType.includes('json') ||
      contentType.includes('javascript') ||
      contentType.includes('xml');

    return {
      statusCode: response.status,
      headers: responseHeaders,
      body: isText ? buffer.toString('utf8') : buffer.toString('base64'),
      isBase64Encoded: !isText
    };
  } catch (error) {
    return {
      statusCode: 502,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        detail: 'QA ALERT backend is unreachable through the configured Cloudflare URL.',
        error: String(error && error.message ? error.message : error)
      })
    };
  }
};
