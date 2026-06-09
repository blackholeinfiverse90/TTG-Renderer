import asyncio
import json
import math
import random
import os
import sys
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="TG Engine Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_DIR, "recordings"), exist_ok=True)
app.mount("/recordings", StaticFiles(directory=os.path.join(_DIR, "recordings")), name="recordings")

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(_DIR, "index.html"))

@app.get("/client.js")
async def serve_client():
    return FileResponse(os.path.join(_DIR, "client.js"))

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
client_websocket: WebSocket | None = None   # browser Three.js client
_all_websockets: list = []                  # all connected clients (browser + connector)
current_game                        = None
_execution_ctx: dict                = {}
_menu_queue    = asyncio.Queue()

# Event recording
_event_log: list = []
_recording: bool = False
_log_file: str   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "events.json")

# ---------------------------------------------------------------------------
# Event logging helpers
# ---------------------------------------------------------------------------
def _start_recording():
    global _recording, _event_log
    _recording = True
    _event_log = []
    print("🎬 EVENT RECORDING STARTED")

def _stop_recording():
    global _recording
    _recording = False
    print("⏹️ EVENT RECORDING STOPPED")

def _save_events(filepath: str = None):
    global _event_log
    path = filepath or _log_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(_event_log, f, indent=2)
    print(f"💾 Saved {len(_event_log)} events to {path}")

def _log_event(event_type: str, data: dict):
    global _event_log, _recording
    entry = {
        "timestamp": time.time(),
        "event_type": event_type,
        "data": data
    }
    if _recording:
        _event_log.append(entry)
    return entry

# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------
VALID_GAME_MODES = {"runner", "arena", "sidescroller"}

def validate_execution_contract(schema: dict) -> tuple[bool, str]:
    if not isinstance(schema, dict):
        return False, "schema must be a JSON object"
    if not schema.get("trace_id"):
        return False, "trace_id missing or empty"
    if not schema.get("execution_id"):
        return False, "execution_id missing or empty"
    if schema.get("mitra_decision") != "ALLOW":
        return False, f"mitra_decision is '{schema.get('mitra_decision')}' — only 'ALLOW' permitted"
    if schema.get("game_mode") not in VALID_GAME_MODES:
        return False, f"game_mode '{schema.get('game_mode')}' not in {VALID_GAME_MODES}"
    if "jobs" in schema and not isinstance(schema["jobs"], list):
        return False, "'jobs' must be an array"
    return True, "ok"

# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def _make_event(event_type: str, data: dict) -> str:
    packet = {
        "trace_id":     _execution_ctx.get("trace_id", ""),
        "execution_id": _execution_ctx.get("execution_id", ""),
        "event_type":   event_type,
        "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data":         data,
    }
    _log_event("telemetry", {"event_type": event_type, "data": data})
    print(f"[EMIT] {event_type} | {data}")
    return json.dumps(packet)

async def _emit(event_type: str, data: dict):
    evt = _make_event(event_type, data)
    for ws in list(_all_websockets):
        try:
            await ws.send_text(evt)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Scene helpers — same shape as before, sent to browser over WS
# ---------------------------------------------------------------------------
def _job(job_type: str, payload: dict) -> str:
    _log_event("job", {"jobType": job_type, "payload": payload})
    return json.dumps({"jobType": job_type, "payload": payload})

def create_prop(id, x, y, z, color, scale, collider="cube") -> str:
    payload = {
        "id": id, "type": "static",
        "transform": {"position": [x, y, z], "rotation": [0, 0, 0], "scale": scale},
        "material":  {"shader": "std", "texture": "none", "color": color},
        "components":{"mesh": collider if collider != "plane" else "plane",
                      "collider": collider, "script": ""},
    }
    _log_event("job", {"jobType": "SPAWN_ENTITY", "payload": payload})
    return _job("SPAWN_ENTITY", payload)

def set_mouse(mode: str) -> str:
    _log_event("job", {"jobType": "SET_MOUSE_MODE", "payload": {"mode": mode}})
    return _job("SET_MOUSE_MODE", {"mode": mode})

async def send(msg: str):
    _log_event("outgoing", {"message": msg})
    # Send to browser client only (not connector)
    if client_websocket:
        try:
            await client_websocket.send_text(msg)
        except Exception:
            pass

async def broadcast(msg: str):
    """Send to ALL connected clients including connector."""
    for ws in list(_all_websockets):
        try:
            await ws.send_text(msg)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# AABB Collision (unchanged)
# ---------------------------------------------------------------------------
class AABB:
    __slots__ = ("x", "y", "z", "hw", "hh", "hd")
    def __init__(self, x, y, z, scale):
        self.x=x; self.y=y; self.z=z
        self.hw=scale[0]*0.5; self.hh=scale[1]*0.5; self.hd=scale[2]*0.5
    def overlaps(self, o):
        return (abs(self.x-o.x)<self.hw+o.hw and
                abs(self.y-o.y)<self.hh+o.hh and
                abs(self.z-o.z)<self.hd+o.hd)
    def penetration(self, o):
        dx=self.x-o.x; px=(self.hw+o.hw)-abs(dx)
        dy=self.y-o.y; py=(self.hh+o.hh)-abs(dy)
        dz=self.z-o.z; pz=(self.hd+o.hd)-abs(dz)
        if px<py and px<pz: return "x",px,(1 if dx>0 else -1)
        elif py<pz:         return "y",py,(1 if dy>0 else -1)
        else:               return "z",pz,(1 if dz>0 else -1)

class CollisionWorld:
    def __init__(self): self._bodies={}; self._enabled=True
    def enable(self,on=True): self._enabled=on
    def add(self,id,x,y,z,scale,static=False,vel=None):
        self._bodies[id]={"box":AABB(x,y,z,scale),"static":static,"vel":vel or [0,0,0],"scale":scale}
    def remove(self,id): self._bodies.pop(id,None)
    def update_pos(self,id,x,y,z):
        if id in self._bodies:
            b=self._bodies[id]["box"]; b.x=x; b.y=y; b.z=z
    def get_pos(self,id):
        if id not in self._bodies: return None
        b=self._bodies[id]["box"]; return(b.x,b.y,b.z)
    def move(self,id,nx,ny,nz):
        if not self._enabled or id not in self._bodies: return set()
        e=self._bodies[id]
        if e["static"]: return set()
        box=e["box"]; box.x=nx; box.y=ny; box.z=nz
        hits=set()
        for _ in range(2):
            for oid,o in self._bodies.items():
                if oid==id or not o["static"]: continue
                ob=o["box"]
                if not box.overlaps(ob): continue
                hits.add(oid)
                ax,dp,sg=box.penetration(ob)
                if ax=="x": box.x+=dp*sg
                elif ax=="y": box.y+=dp*sg
                else: box.z+=dp*sg
        return hits
    def clear(self): self._bodies.clear()

# ---------------------------------------------------------------------------
# GAME 1: Runner
# ---------------------------------------------------------------------------
class RunnerGame:
    def __init__(self):
        self.active=True; self.lane=0; self.z=0.0; self.y=0.0
        self.vel_y=0.0; self.score=0; self.spawn_cursor=-20
        self.game_over=False; self.obstacles=[]

    async def start(self):
        print("🏃 STARTING RUNNER...")
        _start_recording()
        await _emit("game_start", {"game_mode":"runner"})
        await send(_job("BUILD_SCENE", {"id":"run"}))
        await send(create_prop("player",0,0.9,0,[0,0.5,1],[0.8,1.8,0.8]))

    async def update(self):
        if not self.active: return
        if self.game_over:
            await send(_job("UPDATE_UI",{"title":"CRASHED! [Q] Menu"})); return

        self.z-=0.4; self.score=int(abs(self.z))
        if self.y>0 or self.vel_y>0:
            self.y+=self.vel_y; self.vel_y-=0.025
            if self.y<=0: self.y=0; self.vel_y=0

        px=self.lane*3.0
        prev_z = self.z + 0.4  # position before this tick
        for obs in self.obstacles:
            # Swept check — did player pass through obstacle this tick?
            passed_through = (prev_z >= obs['z'] - 1.5) and (self.z <= obs['z'] + 1.5)
            in_lane = abs(px - obs['x']) < 1.8
            not_jumping = self.y < 1.2
            if passed_through and in_lane and not_jumping:
                self.game_over=True
                await _emit("game_over",{"game_mode":"runner","score":self.score})

        await send(_job("UPDATE",{"id":"player","position":[px,self.y+0.9,self.z]}))
        await send(_job("UPDATE_CAMERA",{"position":[0,6,self.z+10],"lookAt":[0,0,self.z-10]}))
        await send(_job("UPDATE_UI",{"title":f"RUNNER: {self.score} | [A/D] Lane  [SPACE] Jump  [Q] Menu"}))
        # Emit telemetry to connector every tick
        await _emit("telemetry", {"fps": 60, "score": self.score, "lives": 3, "duration": abs(int(self.z))//10, "game_mode": "runner"})

        if self.z<self.spawn_cursor+40:
            self.spawn_cursor-=15
            await send(create_prop(f"f_{self.spawn_cursor}",0,-0.1,self.spawn_cursor,[0.2,0.2,0.2],[15,0.2,15],"plane"))
            if random.random()>0.4:
                lane=random.choice([-1,0,1])*3.0
                await send(create_prop(f"o_{self.spawn_cursor}",lane,1.0,self.spawn_cursor,[1,0,0],[2.5,2,1]))
                self.obstacles.append({'x':lane,'z':self.spawn_cursor})
        self.obstacles=[o for o in self.obstacles if o['z']>self.z-5]

    async def handle_input(self,key,action):
        if self.game_over:
            if key=="q" and action=="press":
                await _emit("game_exit",{"game_mode":"runner","score":self.score})
                _save_events(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "runner_events.json"))
                _stop_recording()
                self.active=False
            return
        if action=="press":
            if   key in("a","left")  and self.lane>-1: self.lane-=1
            elif key in("d","right") and self.lane<1:  self.lane+=1
            elif key=="space" and self.y==0:            self.vel_y=0.45
            elif key=="q":
                await _emit("game_exit",{"game_mode":"runner","score":self.score})
                _save_events(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "runner_events.json"))
                _stop_recording()
                self.active=False

# ---------------------------------------------------------------------------
# GAME 2: Sidescroller
# ---------------------------------------------------------------------------
class ScrollerGame:
    def __init__(self):
        self.active=True; self.x=0.0; self.y=0.0; self.vel_y=0.0
        self.score=0; self.game_over=False; self.obstacles=[]
        self.moving_left=False; self.moving_right=False
        self.spawn_cursor=10; self.chunk_size=10; self.on_ground=True

    async def start(self):
        print("👾 STARTING MARIO MODE...")
        _start_recording()
        await _emit("game_start",{"game_mode":"sidescroller"})
        await send(_job("BUILD_SCENE",{"id":"scroll"}))
        for i in range(-2,5): await self._spawn_chunk(i*self.chunk_size)
        self.spawn_cursor=40
        await send(create_prop("player",0,0.5,0,[1,0.5,0],[1,1,1]))

    async def _spawn_chunk(self,x_pos):
        await send(create_prop(f"f_{x_pos}",x_pos,-0.5,0,[0.2,0.3,0.2],[self.chunk_size,1,5],"plane"))
        if random.random()>0.3:
            h=random.choice([0.5,2.5])
            col=[0.8,0.2,0.2] if h<1 else [0.2,0.2,0.8]
            await send(create_prop(f"o_{x_pos}",x_pos,h,0,col,[1,1,1]))
            self.obstacles.append({'x':x_pos,'y':h,'top':h+0.5,'bottom':h-0.5})

    async def update(self):
        if not self.active: return
        if self.game_over:
            await send(_job("UPDATE_UI",{"title":"DEAD! [Q] Menu"})); return

        if self.moving_right: self.x+=0.2
        if self.moving_left:  self.x-=0.2
        self.score=max(self.score,int(self.x))
        if not self.on_ground: self.vel_y-=0.025
        self.y+=self.vel_y
        self.on_ground=False
        if self.y<=0: self.y=0; self.vel_y=0; self.on_ground=True

        pr={'l':self.x-0.4,'r':self.x+0.4,'b':self.y,'t':self.y+1.0}
        for obs in self.obstacles:
            if(pr['r']>obs['x']-0.5 and pr['l']<obs['x']+0.5 and
               pr['t']>obs['bottom'] and pr['b']<obs['top']):
                if self.vel_y<=0 and self.y>=obs['top']-0.3:
                    self.y=obs['top']; self.vel_y=0; self.on_ground=True
                else:
                    self.game_over=True
                    await _emit("game_over",{"game_mode":"sidescroller","score":self.score})

        if self.x+30>self.spawn_cursor:
            self.spawn_cursor+=self.chunk_size
            await self._spawn_chunk(self.spawn_cursor)

        await send(_job("UPDATE",{"id":"player","position":[self.x,self.y+0.5,0]}))
        await send(_job("UPDATE_CAMERA",{"position":[self.x,3,12],"lookAt":[self.x+2,1,0]}))
        await send(_job("UPDATE_UI",{"title":f"MARIO: {self.score} | [A/D] Move  [SPACE] Jump  [Q] Menu"}))
        # Emit telemetry to connector every tick
        await _emit("telemetry", {"fps": 60, "score": self.score, "lives": 3, "duration": int(self.x), "game_mode": "sidescroller"})

    async def handle_input(self,key,action):
        if self.game_over:
            if key=="q" and action=="press":
                await _emit("game_exit",{"game_mode":"sidescroller","score":self.score})
                _save_events(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "sidescroller_events.json"))
                _stop_recording()
                self.active=False
            return
        if   key in("a","left"):  self.moving_left=(action=="press")
        elif key in("d","right"): self.moving_right=(action=="press")
        elif key=="space" and action=="press" and self.on_ground: self.vel_y=0.6
        elif key=="q" and action=="press":
            await _emit("game_exit",{"game_mode":"sidescroller","score":self.score})
            _save_events(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "sidescroller_events.json"))
            _stop_recording()
            self.active=False

# ---------------------------------------------------------------------------
# GAME 3: Free roam
# ---------------------------------------------------------------------------
class FreeRoamGame:
    def __init__(self):
        self.active=True; self.x=0.0; self.y=0.0; self.vel_y=0.0
        self.on_ground=True; self.z=0.0; self.yaw=0.0; self.pitch=-15.0
        self.speed=0.15; self.moving={"w":False,"s":False,"a":False,"d":False}

    async def start(self):
        print("🌍 STARTING FREE ROAM...")
        _start_recording()
        await _emit("game_start",{"game_mode":"arena"})
        await send(set_mouse("capture"))
        await send(_job("BUILD_SCENE",{"id":"roam"}))
        await send(create_prop("ground",0,-0.5,0,[0.25,0.45,0.25],[80,1,80],"plane"))
        random.seed(42)
        colors=[[0.8,0.3,0.2],[0.2,0.4,0.8],[0.7,0.6,0.2],[0.5,0.2,0.7],[0.2,0.7,0.5],[0.8,0.5,0.2]]
        for i in range(30):
            bx=random.uniform(-25,25); bz=random.uniform(-25,25)
            if abs(bx)<3 and abs(bz)<3: bz+=6
            h=random.uniform(1.0,5.0); col=random.choice(colors); w=random.uniform(1.0,3.0)
            await send(create_prop(f"box_{i}",bx,h*0.5,bz,col,[w,h,w]))
        await send(create_prop("player",0,1.0,0,[0.2,0.5,1.0],[0.8,1.8,0.8]))
        await send(_job("UPDATE_UI",{"title":"FREE ROAM | WASD move | Mouse look | ESC release mouse | [Q] Menu"}))

    async def update(self):
        if not self.active: return
        rad=math.radians(self.yaw)
        fx=math.sin(rad); fz=-math.cos(rad)
        rx=math.cos(rad); rz=math.sin(rad)
        if self.moving["w"]: self.x+=fx*self.speed; self.z+=fz*self.speed
        if self.moving["s"]: self.x-=fx*self.speed; self.z-=fz*self.speed
        if self.moving["a"]: self.x-=rx*self.speed; self.z-=rz*self.speed
        if self.moving["d"]: self.x+=rx*self.speed; self.z+=rz*self.speed
        self.vel_y-=0.025; self.y+=self.vel_y; self.on_ground=False
        if self.y<=0.0: self.y=0.0; self.vel_y=0.0; self.on_ground=True
        cd=6.0; pr=math.radians(self.pitch)
        hd=cd*math.cos(pr)
        cx=self.x-fx*hd; cz=self.z-fz*hd; cy=self.y+1.0+cd*math.sin(-pr)
        await send(_job("UPDATE",{"id":"player","position":[self.x,self.y+0.9,self.z]}))
        await send(_job("UPDATE_CAMERA",{"position":[cx,cy,cz],"lookAt":[self.x,self.y+1.0,self.z]}))
        # Emit telemetry to connector every tick
        await _emit("telemetry", {"fps": 60, "score": int(abs(self.x)+abs(self.z)), "lives": 3, "duration": 0, "game_mode": "arena"})

    async def handle_input(self,key,action):
        p=(action=="press"); r=(action=="release")
        if key in self.moving:
            if p: self.moving[key]=True
            if r: self.moving[key]=False
        if key=="space" and p and self.on_ground: self.vel_y=0.45
        if key=="q" and p:
            await _emit("game_exit",{"game_mode":"arena"})
            _save_events(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "freeroam_events.json"))
            _stop_recording()
            await send(set_mouse("free"))
            self.active=False

    async def handle_mouse(self,dx,dy):
        self.yaw+=dx
        self.pitch=max(-60.0,min(30.0,self.pitch-dy))

# ---------------------------------------------------------------------------
# Menu loop
# ---------------------------------------------------------------------------
_GAME_CLS = {1: RunnerGame, 2: ScrollerGame, 3: FreeRoamGame}
_MODE_MAP  = {"runner":(1,RunnerGame), "sidescroller":(2,ScrollerGame), "arena":(3,FreeRoamGame)}

async def main_menu_loop():
    global current_game
    print("⏳ Waiting for client connection...")
    while client_websocket is None:
        await asyncio.sleep(0.1)

    while True:
        print("\n=== GAME CONSOLE ===")
        await send(_job("BUILD_SCENE",   {"id":"menu"}))
        await send(_job("UPDATE_CAMERA", {"position":[0,2,10],"lookAt":[0,0,0]}))
        await send(_job("UPDATE_UI",     {"title":"MENU: [W] Runner  [S] Mario  [D] Free Roam  [Q] Quit"}))

        choice = await _menu_queue.get()
        if choice == "QUIT":
            print("🛑 Shutting down..."); os._exit(0)
        if choice not in _GAME_CLS:
            continue

        current_game = _GAME_CLS[choice]()
        await current_game.start()
        while current_game.active:
            await current_game.update()
            await asyncio.sleep(0.016)
        current_game = None

# ---------------------------------------------------------------------------
# Contract execution
# ---------------------------------------------------------------------------
async def execute_contract(schema: dict):
    global _execution_ctx

    ok, reason = validate_execution_contract(schema)
    if not ok:
        evt = _make_event("contract_rejected", {
            "reason": reason,
            "trace_id":     schema.get("trace_id",""),
            "execution_id": schema.get("execution_id",""),
        })
        print(f"❌ CONTRACT REJECTED: {reason}")
        if client_websocket:
            await client_websocket.send_text(evt)
        return

    _execution_ctx = {"trace_id": schema["trace_id"], "execution_id": schema["execution_id"]}
    await _emit("contract_accepted", {
        "trace_id":       schema["trace_id"],
        "execution_id":   schema["execution_id"],
        "game_mode":      schema["game_mode"],
        "mitra_decision": schema["mitra_decision"],
    })
    print(f"✅ CONTRACT ACCEPTED  trace={schema['trace_id']}  exec={schema['execution_id']}")

    mode_info = _MODE_MAP.get(schema["game_mode"])
    if mode_info:
        await _emit("job_created",  {"job_type":"launch_game","game_mode":schema["game_mode"]})
        # Drain any stale entries from previous sessions
        while not _menu_queue.empty():
            try: _menu_queue.get_nowait()
            except: break
        # Force-stop current game if one is running
        global current_game
        if current_game and current_game.active:
            current_game.active = False
            print(f"[SERVER] Force-stopped current game for new contract: {schema['game_mode']}")
        await _menu_queue.put(mode_info[0])
        await _emit("job_executed", {"job_type":"launch_game","game_mode":schema["game_mode"],"status":"queued"})

# ---------------------------------------------------------------------------
# Input router
# ---------------------------------------------------------------------------
async def route_input(key, action):
    _log_event("input", {"key": key, "action": action})
    if current_game is None:
        if action == "press":
            if   key=="w": await _menu_queue.put(1)
            elif key=="s": await _menu_queue.put(2)
            elif key=="d": await _menu_queue.put(3)
            elif key=="q": await _menu_queue.put("QUIT")
        return
    await current_game.handle_input(key, action)

async def route_mouse(dx, dy):
    _log_event("mouse", {"dx": dx, "dy": dy})
    if current_game and hasattr(current_game, "handle_mouse"):
        await current_game.handle_mouse(dx, dy)

# ---------------------------------------------------------------------------
# WebSocket — browser client
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_handler(websocket: WebSocket):
    global client_websocket
    await websocket.accept()
    _all_websockets.append(websocket)

    # First client with a browser user-agent = browser, else = connector
    user_agent = websocket.headers.get('user-agent', '')
    is_browser = 'Mozilla' in user_agent
    if is_browser:
        client_websocket = websocket
        print("✅ Browser client connected!")
    else:
        print("✅ Connector client connected!")

    try:
        async for raw in websocket.iter_text():
            try:
                data = json.loads(raw)
                event = data.get("event_type") or data.get("event")
                if event == "input":
                    await route_input(data["data"]["key"].lower(), data["data"]["action"])
                elif event == "mouse":
                    await route_mouse(data["data"]["dx"], data["data"]["dy"])
                else:
                    print(f"[RECV] {event} | {data.get('data','')}")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"WS parse error: {e}")
    except WebSocketDisconnect:
        print("Client disconnected.")
    finally:
        _all_websockets.remove(websocket)
        if websocket is client_websocket:
            client_websocket = None

# ---------------------------------------------------------------------------
# HTTP — contract submission (replaces port 8081)
# ---------------------------------------------------------------------------
class ContractPayload(BaseModel):
    trace_id:       str
    execution_id:   str
    mitra_decision: str
    game_mode:      str
    parameters:     dict = {}
    jobs:           list = []

@app.post("/execute")
async def post_contract(payload: ContractPayload):
    schema = payload.dict() if hasattr(payload, 'dict') else payload.model_dump()
    ok, reason = validate_execution_contract(schema)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    asyncio.create_task(execute_contract(schema))
    return {"status": "accepted", "trace_id": payload.trace_id}

@app.get("/health")
async def health():
    return {
        "status":    "ok",
        "connected": client_websocket is not None,
        "game":      type(current_game).__name__ if current_game else None,
        "trace_id":  _execution_ctx.get("trace_id",""),
        "recording": _recording,
        "events_logged": len(_event_log),
    }

@app.get("/events")
async def get_events():
    return {"events": _event_log, "count": len(_event_log)}

@app.post("/events/save")
async def save_events(filename: str = "events.json"):
    _save_events(filename)
    return {"status": "saved", "filename": filename, "count": len(_event_log)}

@app.post("/events/clear")
async def clear_events():
    global _event_log
    _event_log = []
    return {"status": "cleared"}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    asyncio.create_task(main_menu_loop())

# ---------------------------------------------------------------------------
# Run: uvicorn server:app --host 0.0.0.0 --port 8080 --reload
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
