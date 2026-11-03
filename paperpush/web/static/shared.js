// Encryption for shared forms ("Share for editing", see web/shared.py).
//
// A shared form's link is /#/f/<id>/<key>. The key is 32 random bytes that
// never leave the browser (the #fragment is not sent to the server). From it,
// HKDF derives two things:
//   - an AES-GCM key that encrypts the .sub (compressed first), so the server
//     stores only ciphertext it cannot read;
//   - a write token the server keeps the hash of, so only someone with the
//     link can change or delete the form.
// Ciphertext is base64url(iv || AES-GCM(deflate-raw(text))).
(function (root) {
  "use strict";

  const enc = new TextEncoder();
  const subtle = () => (root.crypto || globalThis.crypto).subtle;

  function toB64url(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function fromB64url(text) {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  }

  async function pipe(bytes, stream) {
    return new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer());
  }

  function newKey() {
    return toB64url((root.crypto || globalThis.crypto).getRandomValues(new Uint8Array(32)));
  }

  // { aes: CryptoKey, token: string } for a link's key.
  async function derive(key) {
    const raw = fromB64url(key);
    if (raw.length !== 32) throw new Error("the link's key is the wrong length");
    const base = await subtle().importKey("raw", raw, "HKDF", false, ["deriveKey", "deriveBits"]);
    const params = (info) => ({ name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: enc.encode(info) });
    const aes = await subtle().deriveKey(params("paperpush shared form: encryption"), base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    const token = toB64url(new Uint8Array(await subtle().deriveBits(params("paperpush shared form: write token"), base, 256)));
    return { aes, token };
  }

  async function encrypt(text, aes) {
    const iv = (root.crypto || globalThis.crypto).getRandomValues(new Uint8Array(12));
    const packed = await pipe(enc.encode(text), new CompressionStream("deflate-raw"));
    const sealed = new Uint8Array(await subtle().encrypt({ name: "AES-GCM", iv }, aes, packed));
    const out = new Uint8Array(iv.length + sealed.length);
    out.set(iv);
    out.set(sealed, iv.length);
    return toB64url(out);
  }

  async function decrypt(ciphertext, aes) {
    const bytes = fromB64url(ciphertext);
    const packed = await subtle().decrypt({ name: "AES-GCM", iv: bytes.subarray(0, 12) }, aes, bytes.subarray(12));
    return new TextDecoder().decode(await pipe(new Uint8Array(packed), new DecompressionStream("deflate-raw")));
  }

  const api = { newKey, derive, encrypt, decrypt };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SharedCrypto = api;
})(typeof self !== "undefined" ? self : this);
