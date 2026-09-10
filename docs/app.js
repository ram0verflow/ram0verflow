/* ROFL block explorer — all consensus logic, ported from the Python reference.
   No dependencies. Everything below runs in the visitor's browser. */

const REPO = document.documentElement.dataset.repo || "ram0verflow/ram0verflow";
const RAW = `https://raw.githubusercontent.com/${REPO}/main/`;

/* ---------- consensus constants (must match rofl/consensus.py) ---------- */
const COIN = 100000000n;
const HALVING_INTERVAL = 210;
const RETARGET_INTERVAL = 16;
const TARGET_SPACING = 600;
const TARGET_TIMESPAN = RETARGET_INTERVAL * TARGET_SPACING;
const MAX_ADJUST = 4;
const POW_LIMIT_BITS = 0x1e100000;
const GENESIS_BITS = 0x1e010000;
const COINBASE_MATURITY = 10;
const INITIAL_SUBSIDY = 50n * COIN;
/* subset-sum work function (rofl/pow.py) */
const PN = 40, PB = 38, UNIT_WORK = 1n << 20n, SOL_BYTES = 7;
const K_MAX = 1024n;        // puzzles per block before RETARGET_V2_HEIGHT
const K_MAX_V2 = 3000n;     // after it; ~the most that still fits a comment
const RETARGET_V2_HEIGHT = 1500;

/* ---------- SHA-256, synchronous ---------- */
const K256 = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);

function sha256(bytes) {
  const ml = bytes.length;
  const withOne = ml + 1;
  const blocks = Math.ceil((withOne + 8) / 64);
  const buf = new Uint8Array(blocks * 64);
  buf.set(bytes); buf[ml] = 0x80;
  const dv = new DataView(buf.buffer);
  dv.setUint32(blocks * 64 - 4, (ml * 8) >>> 0);
  dv.setUint32(blocks * 64 - 8, Math.floor(ml / 536870912));

  let h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a,
      h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19;
  const w = new Uint32Array(64);
  for (let b = 0; b < blocks; b++) {
    const off = b * 64;
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const a = w[i-15], c = w[i-2];
      const s0 = ((a>>>7)|(a<<25)) ^ ((a>>>18)|(a<<14)) ^ (a>>>3);
      const s1 = ((c>>>17)|(c<<15)) ^ ((c>>>19)|(c<<13)) ^ (c>>>10);
      w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0;
    }
    let a=h0,b2=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
    for (let i = 0; i < 64; i++) {
      const S1 = ((e>>>6)|(e<<26)) ^ ((e>>>11)|(e<<21)) ^ ((e>>>25)|(e<<7));
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K256[i] + w[i]) >>> 0;
      const S0 = ((a>>>2)|(a<<30)) ^ ((a>>>13)|(a<<19)) ^ ((a>>>22)|(a<<10));
      const mj = (a & b2) ^ (a & c) ^ (b2 & c);
      const t2 = (S0 + mj) >>> 0;
      h=g; g=f; f=e; e=(d+t1)>>>0; d=c; c=b2; b2=a; a=(t1+t2)>>>0;
    }
    h0=(h0+a)>>>0; h1=(h1+b2)>>>0; h2=(h2+c)>>>0; h3=(h3+d)>>>0;
    h4=(h4+e)>>>0; h5=(h5+f)>>>0; h6=(h6+g)>>>0; h7=(h7+h)>>>0;
  }
  const out = new Uint8Array(32), odv = new DataView(out.buffer);
  [h0,h1,h2,h3,h4,h5,h6,h7].forEach((v,i)=>odv.setUint32(i*4,v));
  return out;
}
const sha256d = b => sha256(sha256(b));
const enc = new TextEncoder();
const utf8 = s => enc.encode(s);
const hex = b => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
const unhex = s => new Uint8Array(s.match(/../g)?.map(h => parseInt(h, 16)) ?? []);
const cat = (...arrs) => {
  const n = arrs.reduce((a, x) => a + x.length, 0);
  const out = new Uint8Array(n); let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
};
const toBig = b => BigInt("0x" + (hex(b) || "0"));

/* ---------- compact targets ---------- */
function bitsToTarget(bits) {
  const exp = BigInt(bits >>> 24), man = BigInt(bits & 0x007fffff);
  if (bits & 0x00800000) throw new Error("negative target");
  return exp <= 3n ? man >> (8n * (3n - exp)) : man << (8n * (exp - 3n));
}
const targetToWork = t => (1n << 256n) / (t + 1n);
const difficultyOf = bits =>
  Number(bitsToTarget(POW_LIMIT_BITS) * 1000n / bitsToTarget(bits)) / 1000;
const kMaxFor = height => (height >= RETARGET_V2_HEIGHT ? K_MAX_V2 : K_MAX);
function kForWork(work, height = 0) {
  const cap = kMaxFor(height);
  const k = work / UNIT_WORK;
  return k < 1n ? 1n : (k > cap ? cap : k);
}
/* Hardest target allowed at a height. Difficulty past the puzzle cap buys no
   extra work, so above V2 the target is not allowed below this floor. */
const ceilingTarget = height =>
  height < RETARGET_V2_HEIGHT ? 0n : (1n << 256n) / (K_MAX_V2 * UNIT_WORK);
function subsidy(height) {
  const halvings = Math.floor(height / HALVING_INTERVAL);
  return halvings >= 64 ? 0n : INITIAL_SUBSIDY >> BigInt(halvings);
}
function nextBitsFrom(height, prevBits, firstTs, lastTs) {
  let t;
  if (height % RETARGET_INTERVAL !== 0 || height === 0) {
    t = bitsToTarget(prevBits);
  } else {
    let actual = lastTs - firstTs;
    actual = Math.max(TARGET_TIMESPAN / MAX_ADJUST, Math.min(TARGET_TIMESPAN * MAX_ADJUST, actual));
    t = bitsToTarget(prevBits) * BigInt(Math.trunc(actual)) / BigInt(TARGET_TIMESPAN);
  }
  const lim = bitsToTarget(POW_LIMIT_BITS);
  if (t > lim) t = lim;
  const floor = ceilingTarget(height);
  if (floor && t < floor) t = floor;
  return targetToBits(t);
}
function nextBits(height, blocks) {
  if (height === 0) return GENESIS_BITS;
  const prev = blocks[height - 1];
  if (height % RETARGET_INTERVAL !== 0) return nextBitsFrom(height, prev.bits, 0, 0);
  /* From V2 the window starts one block earlier, so it spans 16 real
     intervals rather than 15 — the off-by-one Bitcoin has carried since 2009. */
  const offset = height >= RETARGET_V2_HEIGHT ? RETARGET_INTERVAL + 1 : RETARGET_INTERVAL;
  const first = blocks[height - offset];
  if (!first) return nextBitsFrom(height, prev.bits, 0, 0);
  return nextBitsFrom(height, prev.bits, first.timestamp, prev.timestamp);
}
function targetToBits(target) {
  let raw = target.toString(16); if (raw.length % 2) raw = "0" + raw;
  let bytes = unhex(raw);
  let i = 0; while (i < bytes.length - 1 && bytes[i] === 0) i++;
  bytes = bytes.slice(i);
  if (bytes[0] & 0x80) bytes = cat(new Uint8Array([0]), bytes);
  const expo = bytes.length;
  const man = (bytes[0] << 16) | ((bytes[1] ?? 0) << 8) | (bytes[2] ?? 0);
  return ((expo << 24) | man) >>> 0;
}

/* ---------- serialisation ---------- */
function txCore(tx) {
  const p = [`v${tx.version ?? 1}`];
  const cb = !tx.inputs || tx.inputs.length === 0;
  if (cb) p.push(`cb:${tx.cb_height ?? 0}:` + hex(utf8(tx.coinbase ?? "")));
  else {
    for (const i of tx.inputs) p.push(`in:${i.txid}:${i.vout}:${i.pubkey}`);
    p.push("memo:" + hex(utf8(tx.memo ?? "")));
  }
  for (const o of tx.outputs) p.push(`out:${o.value}:${o.address}`);
  p.push(`lt${tx.locktime ?? 0}`);
  return utf8(p.join("|"));
}
const txid = tx => hex(sha256d(txCore(tx)));
const isCoinbase = tx => !tx.inputs || tx.inputs.length === 0;

function headerCore(b) {
  return utf8(`${b.height}|${b.prev_hash}|${b.merkle_root}|${b.timestamp}|` +
              `${b.bits.toString(16).padStart(8, "0")}|${b.miner}|${b.algo ?? 1}`);
}
const blockHash = b => hex(sha256d(cat(headerCore(b), utf8("|" + (b.solution ?? "")))));

function merkleRoot(ids) {
  if (!ids.length) return "00".repeat(32);
  let layer = ids.map(unhex);
  while (layer.length > 1) {
    if (layer.length % 2) layer.push(layer[layer.length - 1]);
    const next = [];
    for (let i = 0; i < layer.length; i += 2) next.push(sha256d(cat(layer[i], layer[i + 1])));
    layer = next;
  }
  return hex(layer[0]);
}

/* ---------- subset-sum proof of work ---------- */
function puzzleInstance(core, j, nonce) {
  const seed = sha256d(cat(core, utf8(`|${j}|${nonce}`)));
  const mask = (1n << BigInt(PB)) - 1n;
  const nums = [];
  for (let i = 0; i < PN; i++) {
    const idx = new Uint8Array(4); new DataView(idx.buffer).setUint32(0, i);
    let v = toBig(sha256(cat(seed, idx))) & mask;
    nums.push(v === 0n ? 1n : v);
  }
  const total = nums.reduce((a, b) => a + b, 0n);
  let spread = total / 16n; if (spread < 1n) spread = 1n;
  const off = toBig(sha256(cat(seed, utf8("target")))) % (2n * spread);
  return { nums, target: total / 2n + off - spread };
}
function decodeSolutions(blob) {
  const raw = unhex(blob || "");
  if (raw.length % SOL_BYTES) throw new Error("solution blob is not a whole number of entries");
  const out = [];
  for (let o = 0; o < raw.length; o += SOL_BYTES) {
    out.push([(raw[o] << 8) | raw[o + 1], toBig(raw.slice(o + 2, o + SOL_BYTES))]);
  }
  return out;
}
function checkPow(b) {
  const k = Number(kForWork(targetToWork(bitsToTarget(b.bits)), b.height));
  let sols;
  try { sols = decodeSolutions(b.solution); }
  catch (e) { return `malformed solution: ${e.message}`; }
  if (sols.length !== k) return `difficulty requires ${k} solved puzzle(s), block carries ${sols.length}`;
  const core = headerCore(b);
  for (let j = 0; j < k; j++) {
    const [nonce, subset] = sols[j];
    if (subset <= 0n || subset >= (1n << BigInt(PN))) return `puzzle ${j}: subset out of range`;
    const { nums, target } = puzzleInstance(core, j, nonce);
    let sum = 0n;
    for (let i = 0; i < PN; i++) if ((subset >> BigInt(i)) & 1n) sum += nums[i];
    if (sum !== target) return `puzzle ${j} is not solved by the submitted subset`;
  }
  return null;
}

/* ---------- secp256k1, for signature checking ---------- */
const P = (1n << 256n) - (1n << 32n) - 977n;
const NORD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141n;
const G = [0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798n,
           0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8n];
const mod = (a, m) => ((a % m) + m) % m;
const inv = (a, m) => {
  let [lo, hi] = [mod(a, m), m], [x, y] = [1n, 0n];
  while (lo > 0n) { const q = hi / lo; [lo, hi] = [hi - q * lo, lo]; [x, y] = [y - q * x, x]; }
  return mod(y, m);
};
function ptAdd(p, q) {
  if (!p) return q; if (!q) return p;
  const [x1, y1] = p, [x2, y2] = q;
  let l;
  if (x1 === x2) {
    if (mod(y1 + y2, P) === 0n) return null;
    l = mod(3n * x1 * x1 * inv(2n * y1, P), P);
  } else l = mod((y2 - y1) * inv(x2 - x1, P), P);
  const x3 = mod(l * l - x1 - x2, P);
  return [x3, mod(l * (x1 - x3) - y1, P)];
}
function ptMul(k, p) {
  let r = null, a = p; k = mod(k, NORD);
  while (k > 0n) { if (k & 1n) r = ptAdd(r, a); a = ptAdd(a, a); k >>= 1n; }
  return r;
}
function parsePub(bytes) {
  if (bytes.length !== 33 || (bytes[0] !== 2 && bytes[0] !== 3)) return null;
  const x = toBig(bytes.slice(1));
  if (x >= P) return null;
  const alpha = mod(x ** 3n + 7n, P);
  let beta = powMod(alpha, (P + 1n) / 4n, P);
  if (mod(beta * beta - alpha, P) !== 0n) return null;
  if ((beta & 1n) !== BigInt(bytes[0] & 1)) beta = P - beta;
  return [x, beta];
}
function powMod(b, e, m) {
  let r = 1n; b = mod(b, m);
  while (e > 0n) { if (e & 1n) r = r * b % m; b = b * b % m; e >>= 1n; }
  return r;
}
function verifySig(pubHex, digest, sigHex) {
  try {
    const sig = unhex(sigHex); if (sig.length !== 64) return false;
    const pub = parsePub(unhex(pubHex)); if (!pub) return false;
    const r = toBig(sig.slice(0, 32)), s = toBig(sig.slice(32));
    if (r < 1n || r >= NORD || s < 1n || s >= NORD) return false;
    if (s > NORD / 2n) return false;               // low-s, as consensus requires
    const z = toBig(digest), w = inv(s, NORD);
    const pt = ptAdd(ptMul(z * w % NORD, G), ptMul(r * w % NORD, pub));
    return pt !== null && mod(pt[0], NORD) === r;
  } catch { return false; }
}
/* address = bech32(hrp="rofl", v0, sha256(pubkey)[:20]) */
const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function bechPolymod(v) {
  const gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  let chk = 1;
  for (const x of v) {
    const top = chk >> 25;
    chk = ((chk & 0x1ffffff) << 5) ^ x;
    for (let i = 0; i < 5; i++) if ((top >> i) & 1) chk ^= gen[i];
  }
  return chk;
}
function pubToAddress(pubHex) {
  const h = sha256(unhex(pubHex)).slice(0, 20);
  const data = [0];
  let acc = 0, bits = 0;
  for (const b of h) {
    acc = (acc << 8) | b; bits += 8;
    while (bits >= 5) { bits -= 5; data.push((acc >> bits) & 31); }
  }
  if (bits) data.push((acc << (5 - bits)) & 31);
  const hrp = "rofl";
  const expanded = [...hrp].map(c => c.charCodeAt(0) >> 5).concat([0], [...hrp].map(c => c.charCodeAt(0) & 31));
  const pm = bechPolymod(expanded.concat(data, [0, 0, 0, 0, 0, 0])) ^ 1;
  const chk = []; for (let i = 0; i < 6; i++) chk.push((pm >> (5 * (5 - i))) & 31);
  return hrp + "1" + data.concat(chk).map(d => CHARSET[d]).join("");
}

/* ---------- chain replay ---------- */
function replay(blocks, opts = {}) {
  const utxos = new Map();
  const miners = new Map();
  let chainwork = 0n, txCount = 0, emitted = 0n;
  const problems = [];
  const txIndex = new Map();
  const powFrom = opts.fullPow ? 0 : Math.max(0, blocks.length - (opts.powTail ?? 5));

  blocks.forEach((b, h) => {
    const fail = m => problems.push({ height: h, message: m });
    if (b.height !== h) fail(`claims height ${b.height} at position ${h}`);
    if (h > 0 && b.prev_hash !== blockHash(blocks[h - 1]))
      fail("prev_hash does not match the previous block");
    if (h === 0 && b.prev_hash !== "00".repeat(32)) fail("genesis prev_hash is not null");

    const expected = nextBits(h, blocks);
    if (b.bits !== expected)
      fail(`wrong difficulty: expected ${fmtBits(expected)}, got ${fmtBits(b.bits)}`);

    const ids = b.txs.map(txid);
    if (new Set(ids).size !== ids.length) fail("duplicate transactions");
    if (merkleRoot(ids) !== b.merkle_root) fail("merkle root does not commit to these transactions");

    if (h >= powFrom || opts.fullPow) {
      const e = checkPow(b);
      if (e) fail(e);
    }

    const cb = b.txs[0];
    if (!isCoinbase(cb)) fail("first transaction is not a coinbase");
    else if ((cb.cb_height ?? 0) !== h) fail(`BIP 34: coinbase commits to height ${cb.cb_height}`);

    let fees = 0n;
    b.txs.forEach((t, ti) => {
      const id = ids[ti];
      txIndex.set(id, { tx: t, block: b, height: h });
      if (ti === 0) return;
      let inSum = 0n;
      const digest = sha256d(txCore(t));
      for (const i of t.inputs) {
        const key = `${i.txid}:${i.vout}`;
        const u = utxos.get(key);
        if (!u) { fail(`input ${i.txid.slice(0, 12)}…:${i.vout} is unknown or already spent`); continue; }
        if (u.coinbase && h - u.height < COINBASE_MATURITY) fail("spends an immature coinbase");
        if (pubToAddress(i.pubkey) !== u.address) fail("public key does not match the spent address");
        if (opts.fullPow && !verifySig(i.pubkey, digest, i.sig)) fail("invalid signature");
        inSum += BigInt(u.value);
        utxos.delete(key);
      }
      const outSum = t.outputs.reduce((a, o) => a + BigInt(o.value), 0n);
      if (outSum > inSum) fail("outputs exceed inputs");
      fees += inSum - outSum;
      t.outputs.forEach((o, vi) =>
        utxos.set(`${id}:${vi}`, { value: o.value, address: o.address, height: h, coinbase: false }));
    });

    const paid = cb.outputs.reduce((a, o) => a + BigInt(o.value), 0n);
    const due = subsidy(h) + fees;
    if (paid !== due) fail(`coinbase pays ${paid}, expected ${due}`);
    cb.outputs.forEach((o, vi) =>
      utxos.set(`${ids[0]}:${vi}`, { value: o.value, address: o.address, height: h, coinbase: true }));

    emitted += subsidy(h);
    chainwork += targetToWork(bitsToTarget(b.bits));
    miners.set(b.miner, (miners.get(b.miner) ?? 0) + 1);
    txCount += b.txs.length;
  });

  const balances = new Map();
  let unspent = 0n;
  for (const u of utxos.values()) {
    balances.set(u.address, (balances.get(u.address) ?? 0n) + BigInt(u.value));
    unspent += BigInt(u.value);
  }
  if (emitted !== unspent && !problems.length)
    problems.push({ height: blocks.length - 1, message: `supply mismatch: ${emitted} emitted, ${unspent} unspent` });

  return { blocks, utxos, balances, miners, chainwork, txCount, emitted, unspent, problems, txIndex,
           tip: blocks[blocks.length - 1], height: blocks.length - 1 };
}

/* ---------- formatting ---------- */
const fmtBits = b => "0x" + (b >>> 0).toString(16).padStart(8, "0");
function fmtAmount(laffs) {
  const v = BigInt(laffs);
  return `${v / COIN}.${(v % COIN).toString().padStart(8, "0")}`;
}
function ago(ts) {
  const d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (d < 90) return `${d}s ago`;
  if (d < 5400) return `${Math.floor(d / 60)}m ago`;
  if (d < 172800) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const short = (s, n = 12) => s.length <= n * 2 ? s : `${s.slice(0, n)}…${s.slice(-4)}`;
