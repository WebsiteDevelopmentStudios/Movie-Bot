const textEncoder = new TextEncoder();

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

async function verifyDiscordRequest(request, publicKeyHex) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");

  if (!signature || !timestamp || !publicKeyHex) {
    return false;
  }

  const body = await request.clone().text();

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKeyHex),
      { name: "Ed25519" },
      false,
      ["verify"],
    );

    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      hexToBytes(signature),
      textEncoder.encode(timestamp + body),
    );
  } catch {
    return false;
  }
}

function interactionResponse(payload) {
  if (payload.type === 2) return { type: 5 };
  if (payload.type === 3) return { type: 6 };
  if (payload.type === 4) return { type: 8, data: { choices: [] } };
  if (payload.type === 5) return { type: 5 };
  return { type: 5 };
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Movie-Bot Discord endpoint", { status: 200 });
    }

    if (!(await verifyDiscordRequest(request, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return new Response("invalid JSON", { status: 400 });
    }

    if (payload.type === 1) {
      return new Response(JSON.stringify({ type: 1 }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!env.BOT_INTERACTION_URL || !env.WAKE_SECRET) {
      return new Response("Worker is not configured", { status: 503 });
    }

    ctx.waitUntil(
      fetch(env.BOT_INTERACTION_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Movie-Bot-Secret": env.WAKE_SECRET,
        },
        body: JSON.stringify(payload),
      }).catch(() => {
        // Render may be cold-starting; Discord already received its ACK.
      }),
    );

    return new Response(JSON.stringify(interactionResponse(payload)), {
      headers: { "Content-Type": "application/json" },
    });
  },
};
