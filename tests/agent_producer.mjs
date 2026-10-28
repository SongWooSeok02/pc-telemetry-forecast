// Independent producer: never imports the Python API's parser/canonicalizer.
// Decimal tokens are preserved; JSON.parse's binary64 conversion is not used.
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

class Numeric { constructor(raw) { this.raw = raw; } }
export function parse(raw) {
  let at = 0;
  const ws = () => { while (/[ \t\r\n]/.test(raw[at] ?? '') && at < raw.length) at++; };
  function value() {
    ws();
    if (raw[at] === '"') {
      const start = at++;
      while (at < raw.length) {
        if (raw[at++] === '\\') { at++; continue; }
        if (raw[at-1] === '"') return JSON.parse(raw.slice(start, at));
      }
      throw Error('unterminated string');
    }
    if (raw[at] === '[') {
      at++; ws(); const values = [];
      if (raw[at] === ']') { at++; return values; }
      while (true) {
        values.push(value()); ws();
        if (raw[at++] === ']') return values;
        if (raw[at-1] !== ',') throw Error('array separator');
      }
    }
    if (raw[at] === '{') {
      at++; ws(); const obj = Object.create(null);
      if (raw[at] === '}') { at++; return obj; }
      while (true) {
        ws(); if (raw[at] !== '"') throw Error('object key');
        const key = value(); ws();
        if (Object.hasOwn(obj, key) || raw[at++] !== ':') throw Error('duplicate key/colon');
        obj[key] = value(); ws();
        if (raw[at++] === '}') return obj;
        if (raw[at-1] !== ',') throw Error('object separator');
      }
    }
    for (const [word, result] of [['true', true], ['false', false], ['null', null]]) {
      if (raw.startsWith(word, at)) { at += word.length; return result; }
    }
    const token = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(raw.slice(at));
    if (!token) throw Error('JSON token');
    at += token[0].length;
    return new Numeric(token[0]);
  }
  const out = value(); ws();
  if (at !== raw.length) throw Error('trailing input');
  return out;
}

function decimal(raw) {
  if (raw.length > 32) throw Error('numeric length');
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(raw);
  if (!match) throw Error('number');
  let digits = (match[2] + (match[3] ?? '')).replace(/^0+/, '');
  if (!digits) return '0';
  let scale = (match[3]?.length ?? 0) - Number(match[4] ?? 0);
  if (!Number.isSafeInteger(scale) || Math.abs(scale) > 100) throw Error('numeric range');
  while (digits.endsWith('0')) { digits = digits.slice(0, -1); scale--; }
  if (scale > 6) throw Error('fraction precision');
  let out;
  if (scale <= 0) out = digits + '0'.repeat(-scale);
  else if (scale >= digits.length) out = '0.' + '0'.repeat(scale-digits.length) + digits;
  else out = digits.slice(0, -scale) + '.' + digits.slice(-scale);
  const [whole, fraction = ''] = out.split('.');
  if (BigInt(whole) > 9007199254740991n || (BigInt(whole) === 9007199254740991n && /[1-9]/.test(fraction))) throw Error('numeric range');
  return match[1] + out;
}

export function canonical(v) {
  if (v instanceof Numeric) return decimal(v.raw);
  if (v === null) return 'null';
  if (typeof v === 'boolean') return String(v);
  if (typeof v === 'string') {
    if (!/^[\x20-\x7e]*$/.test(v)) throw Error('ASCII');
    return '"' + v.replaceAll('\\', '\\\\').replaceAll('"', '\\"') + '"';
  }
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  return '{' + Object.keys(v).sort().map(k => canonical(k)+':'+canonical(v[k])).join(',') + '}';
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const text = canonical(parse(readFileSync(process.argv[2], 'utf8')));
  process.stdout.write(JSON.stringify({body: text, sha256: createHash('sha256').update(text, 'ascii').digest('hex')}));
}
