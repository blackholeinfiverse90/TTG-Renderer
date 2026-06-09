/**
 * ttg_engine_connector.js — Atharva's TTG Engine Repo
 *
 * Bridges Atharva's server.py (FastAPI, port 8080) to Rudra's backend
 * (Node.js, port 3000) via Socket.IO /engine namespace.
 *
 * Flow:
 *   Rudra backend  →  job:dispatch  →  this connector
 *   this connector →  POST /execute →  Atharva's server.py  (launches game)
 *   server.py WS   →  telemetry     →  this connector
 *   this connector →  telemetry     →  Rudra backend  →  Dashboard
 *
 * Setup (run once):
 *   npm install
 *
 * Run:
 *   node ttg_engine_connector.js
 *
 * With remote Rudra backend:
 *   RUDRA_URL=http://<rudra-ip>:3000 node ttg_engine_connector.js
 */

'use strict';

const { io }     = require('socket.io-client');
const jwt        = require('jsonwebtoken');
const crypto     = require('crypto');
const WebSocket  = require('ws');

// ── Config ────────────────────────────────────────────────────────────────────
const RUDRA_URL            = process.env.RUDRA_URL             || 'http://localhost:3000';
const ATHARVA_HTTP         = process.env.ATHARVA_HTTP          || 'http://localhost:8080';
const ATHARVA_WS           = process.env.ATHARVA_WS            || 'ws://localhost:8080/ws';
const JWT_SECRET           = process.env.JWT_SECRET            || 'JWT_SECRET_123456789';
const ENGINE_SHARED_SECRET = process.env.ENGINE_SHARED_SECRET  || 'ENGINE_SHARED_SECRET_123';
const ENGINE_ID            = process.env.ENGINE_ID             || 'atharva_ttg_engine_01';

// ── Auth token for Rudra's /engine namespace ──────────────────────────────────
const token = jwt.sign(
  { engineId: ENGINE_ID, role: 'engine' },
  JWT_SECRET,
  { expiresIn: '24h' }
);

// ── HMAC signer — required by Rudra's backend for all outbound messages ───────
function sign(payload) {
  const nonce = crypto.randomBytes(16).toString('hex');
  const ts    = Date.now();
  const sig   = crypto
    .createHmac('sha256', ENGINE_SHARED_SECRET)
    .update(JSON.stringify(payload) + nonce + ts)
    .digest('hex');
  return { payload, nonce, ts, sig };
}

// ── WebSocket connection to Atharva's server.py /ws ───────────────────────────
// One persistent WS to the game engine; shared across all jobs.
let engineWs       = null;
let engineWsReady  = false;
let telemetryHooks = {}; // jobId → { contract, resolve }

function connectToEngineWs() {
  engineWs = new WebSocket(ATHARVA_WS);

  engineWs.on('open', () => {
    engineWsReady = true;
    console.log('[CONNECTOR] ✅ Connected to server.py WebSocket');
  });

  engineWs.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    handleEngineMessage(msg);
  });

  engineWs.on('close', () => {
    engineWsReady = false;
    console.warn('[CONNECTOR] server.py WS closed — reconnecting in 3s...');
    setTimeout(connectToEngineWs, 3000);
  });

  engineWs.on('error', (e) => {
    console.error('[CONNECTOR] server.py WS error:', e.message);
  });
}

// ── Handle messages coming back from server.py ────────────────────────────────
function handleEngineMessage(msg) {
  const eventType = msg.event_type;
  if (!eventType) return;

  const activeHook = Object.values(telemetryHooks)[0];
  const contract   = activeHook?.contract || {};
  const jobId      = activeHook?.jobId;

  if (eventType === 'game_start') {
    rudraSocket.emit('game:started', {
      game_mode:         msg.data?.game_mode || contract.game_mode,
      gameplay_contract: contract,
      trace_id:          contract.trace_id     || msg.trace_id || null,
      execution_id:      contract.execution_id || msg.execution_id || null
    });
    console.log(`[CONNECTOR] \uD83C\uDFAE game:started — mode=${msg.data?.game_mode}`);
    return;
  }

  if (eventType === 'telemetry') {
    rudraSocket.emit('telemetry', {
      trace_id:     contract.trace_id     || msg.trace_id     || null,
      execution_id: contract.execution_id || msg.execution_id || null,
      event_type:   'telemetry',
      timestamp:    Date.now(),
      fps:          msg.data?.fps      || 60,
      score:        msg.data?.score    || 0,
      lives:        msg.data?.lives    || 3,
      duration:     msg.data?.duration || 0,
      game_mode:    msg.data?.game_mode || contract.game_mode
    });
    return;
  }

  if (eventType === 'game_over' || eventType === 'game_exit') {
    const reason = eventType === 'game_over' ? 'game_over' : 'player_exit';
    rudraSocket.emit('game:ended', {
      trace_id:     contract.trace_id     || null,
      execution_id: contract.execution_id || null,
      event_type:   'game_ended',
      timestamp:    Date.now(),
      reason,
      final_score:  msg.data?.score    || 0,
      duration:     msg.data?.duration || 0,
      game_mode:    msg.data?.game_mode || contract.game_mode
    });
    console.log(`[CONNECTOR] \uD83C\uDFC1 game:ended — reason=${reason}`);
    if (jobId) delete telemetryHooks[jobId];
    return;
  }

  if (eventType === 'contract_accepted') {
    console.log(`[CONNECTOR] \u2705 contract accepted — trace=${msg.data?.trace_id}`);
    return;
  }

  if (eventType === 'contract_rejected' && jobId) {
    console.error(`[CONNECTOR] \u274C contract rejected — ${msg.data?.reason}`);
    rudraSocket.emit('job_failed', sign({
      job_id: jobId, error: 'contract_rejected',
      details: msg.data?.reason || 'rejected', timestamp: Date.now()
    }));
    delete telemetryHooks[jobId];
  }
}

// ── POST job contract to Atharva's server.py /execute ─────────────────────────
async function postToEngine(jobId, contract) {
  const body = {
    trace_id:       contract.trace_id     || `trace-${jobId}`,
    execution_id:   contract.execution_id || jobId,
    mitra_decision: 'ALLOW',
    game_mode:      contract.game_mode    || 'runner',
    parameters:     contract,
    jobs:           []
  };

  const res = await fetch(`${ATHARVA_HTTP}/execute`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body)
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`server.py /execute returned ${res.status}: ${text}`);
  }

  return res.json();
}

// ── Main job runner ───────────────────────────────────────────────────────────
async function runGame(jobId, jobType, contract) {
  // Register telemetry hook
  telemetryHooks[jobId] = { jobId, contract };

  // Signal job started immediately
  rudraSocket.emit('job_started', sign({ job_id: jobId, timestamp: Date.now() }));
  console.log(`[CONNECTOR] ⚙️  job_started: ${jobId}`);

  // Only POST to server.py on START_LOOP — that's when the game actually launches
  if (jobType !== 'START_LOOP') {
    await new Promise(r => setTimeout(r, 300));
    rudraSocket.emit('job_completed', sign({
      job_id: jobId, result: { success: true }, timestamp: Date.now()
    }));
    console.log(`[CONNECTOR] ✅ job_completed: ${jobId} (${jobType} — no engine call needed)`);
    return;
  }

  try {
    const result = await postToEngine(jobId, contract);
    console.log(`[CONNECTOR] 📨 server.py /execute accepted: ${JSON.stringify(result)}`);

    await new Promise(r => setTimeout(r, 300));
    rudraSocket.emit('job_completed', sign({
      job_id: jobId, result: { success: true, game_mode: contract.game_mode || 'runner' }, timestamp: Date.now()
    }));
    console.log(`[CONNECTOR] ✅ job_completed: ${jobId} — game launched on server.py`);
    console.log(`[CONNECTOR] 🎮 Open http://localhost:8080 to see the game`);

  } catch (err) {
    console.error(`[CONNECTOR] ❌ Failed to launch game: ${err.message}`);
    rudraSocket.emit('job_failed', sign({
      job_id: jobId, error: 'engine_launch_failed', details: err.message, timestamp: Date.now()
    }));
    delete telemetryHooks[jobId];
  }
  // Keep telemetryHooks alive — game is running, telemetry still flows
}

// ── Connect to Rudra's backend /engine namespace ──────────────────────────────
console.log(`[CONNECTOR] Connecting to Rudra backend: ${RUDRA_URL}/engine`);
console.log(`[CONNECTOR] Atharva engine:              ${ATHARVA_HTTP}`);

const rudraSocket = io(`${RUDRA_URL}/engine`, {
  auth:             { token },
  reconnection:     true,
  reconnectionDelay: 3000
});

rudraSocket.on('connect', () => {
  console.log(`[CONNECTOR] ✅ Connected to Rudra — socket.id=${rudraSocket.id}`);
  rudraSocket.emit('engine_ready');
  setInterval(() => rudraSocket.emit('engine_heartbeat'), 3000);
});

rudraSocket.on('ready_ack',     () => console.log('[CONNECTOR] Ready acknowledged by Rudra'));
rudraSocket.on('heartbeat_ack', () => {});

// ── Receive jobs dispatched by Rudra's backend ────────────────────────────────
rudraSocket.on('job:dispatch', (job) => {
  const jobId    = job.job_id   || job.jobId;
  const jobType  = job.job_type || job.jobType;
  const contract = job.gameplay_contract || {};

  if (!jobId || !jobType) {
    console.error('[CONNECTOR] ❌ Rejected — missing job_id or job_type');
    return;
  }

  console.log(`\n[CONNECTOR] 📦 Job received: ${jobId} (${jobType})`);
  console.log(`[CONNECTOR]    game_mode : ${contract.game_mode || 'runner'}`);
  console.log(`[CONNECTOR]    speed     : ${contract.movement?.speed || 5}`);

  // Ack receipt to Rudra
  rudraSocket.emit('job_ack', sign({ jobId, status: 'received' }));

  runGame(jobId, jobType, contract);
});

rudraSocket.on('disconnect',    () => console.log('[CONNECTOR] ❌ Disconnected from Rudra — reconnecting...'));
rudraSocket.on('connect_error', (e) => console.error('[CONNECTOR] ❌ Connect error:', e.message));

// ── Connect to Atharva's server.py WebSocket ──────────────────────────────────
connectToEngineWs();
