#!/usr/bin/env node

import amqp from 'amqplib';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function loadDotEnvFallback() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const candidate = path.resolve(here, '../../bloodbank/.env');
  if (!fs.existsSync(candidate)) return {};

  const out = {};
  const text = fs.readFileSync(candidate, 'utf8');
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx < 1) continue;
    const key = trimmed.slice(0, idx).trim();
    let value = trimmed.slice(idx + 1).trim();
    value = value.replace(/^['\"]|['\"]$/g, '');
    out[key] = value;
  }
  return out;
}

const envFallback = loadDotEnvFallback();

const exchange = process.env.EXCHANGE_NAME || envFallback.EXCHANGE_NAME || process.env.CANDYBAR_EXCHANGE || 'bloodbank.events.v1';

const user = process.env.DEFAULT_USERNAME || process.env.RABBIT_USER || 'guest';
const pass = process.env.DEFAULT_PASSWORD || process.env.RABBIT_PASS || 'guest';
const host = process.env.RABBIT_HOST || 'localhost';
const port = process.env.RABBIT_PORT || '5672';
const rabbitUrl = process.env.RABBIT_URL || envFallback.RABBIT_URL || `amqp://${user}:${pass}@${host}:${port}/`;

const routingKeysArg = process.argv[2] || '#';
const routingKeys = routingKeysArg.split(',').map((s) => s.trim()).filter(Boolean);

function fmtNow() {
  return new Date().toLocaleTimeString();
}

function short(value, max = 120) {
  const s = typeof value === 'string' ? value : JSON.stringify(value);
  return s.length > max ? `${s.slice(0, max - 3)}...` : s;
}

async function main() {
  console.log(`[candybar-stream] connecting: ${rabbitUrl}`);
  console.log(`[candybar-stream] exchange: ${exchange}`);
  console.log(`[candybar-stream] routing keys: ${routingKeys.join(', ')}`);

  const conn = await amqp.connect(rabbitUrl);
  const ch = await conn.createChannel();

  await ch.assertExchange(exchange, 'topic', { durable: true });
  const q = await ch.assertQueue('', { exclusive: true, autoDelete: true });

  for (const key of routingKeys) {
    await ch.bindQueue(q.queue, exchange, key);
  }

  console.log(`[candybar-stream] queue: ${q.queue}`);
  console.log('[candybar-stream] listening... (Ctrl+C to stop)\n');

  await ch.consume(
    q.queue,
    (msg) => {
      if (!msg) return;

      const body = msg.content.toString('utf8');
      try {
        const event = JSON.parse(body);
        const ts = fmtNow();
        const eventType = event?.event_type || msg.fields.routingKey || 'unknown';
        const source = event?.source?.app ? `${event.source.app}@${event.source.host || '?'}` : 'unknown';

        console.log(`${ts}  ${eventType}  ${source}`);

        if (event?.payload && typeof event.payload === 'object') {
          const previewKeys = ['event', 'state', 'title', 'id', 'project_id'];
          for (const k of previewKeys) {
            if (k in event.payload) {
              console.log(`      ${k}: ${short(event.payload[k])}`);
            }
          }
        }
      } catch {
        console.log(`${fmtNow()}  ${msg.fields.routingKey}  raw=${short(body)}`);
      } finally {
        ch.ack(msg);
      }
    },
    { noAck: false }
  );

  const shutdown = async () => {
    try {
      await ch.close();
      await conn.close();
    } catch {
      // ignore
    }
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('[candybar-stream] fatal:', err.message || err);
  process.exit(1);
});
