import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const WS_URL = (() => {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
})();
const MOUSE_SENSITIVITY = 0.12;
const FOV             = 60;
const NEAR            = 0.1;
const FAR             = 200;
const FOG_NEAR        = 30;
const FOG_FAR         = 120;
const FOG_COLOR       = 0x2e2e38;

// ---------------------------------------------------------------------------
// Three.js setup
// ---------------------------------------------------------------------------
const canvas   = document.getElementById('canvas');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.shadowMap.enabled = false;

const scene  = new THREE.Scene();
scene.background = new THREE.Color(FOG_COLOR);
scene.fog        = new THREE.Fog(FOG_COLOR, FOG_NEAR, FOG_FAR);

const camera = new THREE.PerspectiveCamera(FOV, canvas.clientWidth / canvas.clientHeight, NEAR, FAR);
camera.position.set(0, 5, 15);

// Lighting — matches the GLSL: ambient + directional key + rim is approximated
const ambient = new THREE.AmbientLight(0xffffff, 0.45);
scene.add(ambient);
const dirLight = new THREE.DirectionalLight(0xffffff, 0.55);
dirLight.position.set(0.5, 1.0, 0.5).normalize();
scene.add(dirLight);

// Resize handler
function onResize() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
}
window.addEventListener('resize', onResize);
onResize();

// ---------------------------------------------------------------------------
// Entity / mesh registry
// ---------------------------------------------------------------------------
const entities = new Map();   // id → { mesh, ... }

// Geometry cache — reuse primitives, scale via mesh.scale
const _geoCache = {};
function getGeometry(collider) {
    if (!_geoCache[collider]) {
        if (collider === 'plane') {
            _geoCache[collider] = new THREE.BoxGeometry(1, 1, 1); // scaled flat
        } else {
            _geoCache[collider] = new THREE.BoxGeometry(1, 1, 1); // cube
        }
    }
    return _geoCache[collider];
}

function spawnEntity(payload) {
    const { id, transform, material } = payload;
    const collider = payload.components?.collider ?? 'cube';

    const geo = getGeometry(collider);
    const mat = new THREE.MeshLambertMaterial({
        color: new THREE.Color(material.color[0], material.color[1], material.color[2]),
        side: THREE.DoubleSide,
    });
    const mesh = new THREE.Mesh(geo, mat);

    mesh.position.set(...transform.position);
    mesh.rotation.set(
        THREE.MathUtils.degToRad(transform.rotation[0]),
        THREE.MathUtils.degToRad(transform.rotation[1]),
        THREE.MathUtils.degToRad(transform.rotation[2]),
    );
    mesh.scale.set(...transform.scale);

    scene.add(mesh);
    entities.set(id, { mesh });
}

function destroyEntity(id) {
    const e = entities.get(id);
    if (!e) return;
    scene.remove(e.mesh);
    e.mesh.geometry.dispose();
    e.mesh.material.dispose();
    entities.delete(id);
}

function clearScene() {
    for (const [id] of entities) destroyEntity(id);
    entities.clear();
}

function updateTransform(payload) {
    const e = entities.get(payload.id);
    if (!e) return;
    if (payload.position) e.mesh.position.set(...payload.position);
    if (payload.rotation) e.mesh.rotation.set(
        THREE.MathUtils.degToRad(payload.rotation[0]),
        THREE.MathUtils.degToRad(payload.rotation[1]),
        THREE.MathUtils.degToRad(payload.rotation[2]),
    );
    if (payload.scale) e.mesh.scale.set(...payload.scale);
}

function updateCamera(payload) {
    if (payload.position) camera.position.set(...payload.position);
    if (payload.lookAt)   camera.lookAt(...payload.lookAt);
}

function updateUI(payload) {
    if (payload.title) document.title = payload.title;
    if (window.__setHud) window.__setHud(payload.title || '');
    const el = document.getElementById('hud-text');
    if (el && payload.title) el.textContent = payload.title;
}

// ---------------------------------------------------------------------------
// Mouse capture
// ---------------------------------------------------------------------------
let mouseCaptured = false;

function setMouseMode(mode) {
    if (mode === 'capture') {
        canvas.requestPointerLock();
    } else {
        document.exitPointerLock?.();
    }
}

document.addEventListener('pointerlockchange', () => {
    mouseCaptured = (document.pointerLockElement === canvas);
});

canvas.addEventListener('click', () => {
    // clicking the canvas when not captured re-captures (free roam UX)
    if (!mouseCaptured && document.pointerLockElement !== canvas) {
        // only auto-capture if server has requested it
    }
});

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
let ws = null;
let reconnectTimer = null;

function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        console.log('[WS] connected');
        if (window.__setConnected) window.__setConnected(true);
        clearTimeout(reconnectTimer);
    };

    ws.onclose = () => {
        console.log('[WS] disconnected — retrying in 2s');
        if (window.__setConnected) window.__setConnected(false);
        reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = (e) => console.error('[WS] error', e);

    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        handleServerMessage(msg);
    };
}

function sendToServer(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
    }
}

// Output contract shape — every event sent to server carries trace context
// (server sets it; client just echoes it back on input events)
let traceCtx = { trace_id: '', execution_id: '' };

function emitInput(key, action) {
    sendToServer({
        ...traceCtx,
        event_type: 'input',
        timestamp:  new Date().toISOString(),
        data: { key, action },
    });
}

function emitMouse(dx, dy) {
    sendToServer({
        ...traceCtx,
        event_type: 'mouse',
        timestamp:  new Date().toISOString(),
        data: { dx: dx * MOUSE_SENSITIVITY, dy: dy * MOUSE_SENSITIVITY },
    });
}

// ---------------------------------------------------------------------------
// Server message handler — mirrors C++ processJobs()
// ---------------------------------------------------------------------------
function handleServerMessage(msg) {
    // Contract-shaped telemetry events from server → update local trace context
    if (msg.trace_id)     traceCtx.trace_id     = msg.trace_id;
    if (msg.execution_id) traceCtx.execution_id = msg.execution_id;

    // Job messages (jobType field)
    if (msg.jobType) {
        const jt = msg.jobType.toUpperCase();
        const p  = msg.payload ?? {};
        switch (jt) {
            case 'SPAWN_ENTITY':    spawnEntity(p);           break;
            case 'DESTROY_ENTITY':  destroyEntity(p.id);      break;
            case 'BUILD_SCENE':     clearScene();              break;
            case 'UPDATE':          updateTransform(p);        break;
            case 'UPDATE_CAMERA':   updateCamera(p);           break;
            case 'UPDATE_UI':       updateUI(p);               break;
            case 'SET_MOUSE_MODE':  setMouseMode(p.mode);      break;
        }
        return;
    }

    // Telemetry / contract events
    if (msg.event_type) {
        console.log(`[TELEMETRY] ${msg.event_type}`, msg.data);
    }
}

// ---------------------------------------------------------------------------
// Input — keyboard
// ---------------------------------------------------------------------------
const KEY_MAP = {
    'KeyW': 'w', 'KeyS': 's', 'KeyA': 'a', 'KeyD': 'd',
    'Space': 'space', 'KeyQ': 'q',
    'ArrowLeft': 'left', 'ArrowRight': 'right',
    'ArrowUp': 'up',    'ArrowDown': 'down',
};

const keyStates = {};

window.addEventListener('keydown', (e) => {
    if (e.repeat) return;
    const name = KEY_MAP[e.code];
    if (!name) return;
    if (e.code === 'Space') e.preventDefault(); // stop page scroll
    if (keyStates[name]) return;
    keyStates[name] = true;
    emitInput(name, 'press');
});

window.addEventListener('keyup', (e) => {
    const name = KEY_MAP[e.code];
    if (!name) return;
    if (!keyStates[name]) return;
    keyStates[name] = false;
    emitInput(name, 'release');
});

// Escape — release pointer lock (mirrors C++ ESC handling)
window.addEventListener('keydown', (e) => {
    if (e.code === 'Escape' && mouseCaptured) {
        document.exitPointerLock();
    }
});

// ---------------------------------------------------------------------------
// Input — mouse
// ---------------------------------------------------------------------------
document.addEventListener('mousemove', (e) => {
    if (!mouseCaptured) return;
    const dx = e.movementX ?? 0;
    const dy = e.movementY ?? 0;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;  // dead zone
    emitMouse(dx, dy);
});

// ---------------------------------------------------------------------------
// Render loop
// ---------------------------------------------------------------------------
function animate() {
    requestAnimationFrame(animate);
    renderer.render(scene, camera);
}
animate();

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
connect();
