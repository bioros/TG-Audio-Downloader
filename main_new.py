import os, asyncio, logging, tkinter as tk
from tkinter import filedialog
from telethon import TelegramClient, functions, types, errors
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("APP")
load_dotenv()

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
GROUP_ID = int(os.getenv('GROUP_ID', 0))

client = TelegramClient("bot_session", API_ID, API_HASH)
scan_semaphore = asyncio.Semaphore(1) 
dl_semaphore = asyncio.Semaphore(3)

def get_track_info(msg):
    p, t = "Неизвестен", "Без названия"
    if msg.document:
        for attr in msg.document.attributes:
            if isinstance(attr, types.DocumentAttributeAudio):
                p = (attr.performer or "Неизвестен").strip()
                t = (attr.title or "Без названия").strip()
                return p, t
    if msg.file and msg.file.name: t = msg.file.name
    return p, t

@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()
    logger.info("БЭКЕНД ГОТОВ К РАБОТЕ")
    yield
    await client.disconnect()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_index(): return FileResponse('static/index_new.html')

@app.get("/api/select_folder")
async def select_folder():
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    path = filedialog.askdirectory(); root.destroy()
    return {"path": path}

async def scan_topic_task(websocket: WebSocket, tid: int):
    async with scan_semaphore:
        try:
            msgs = []
            async for m in client.iter_messages(GROUP_ID, reply_to=tid):
                if m.audio or m.voice or m.photo or (m.document and m.file and "image" in (m.file.mime_type or "")):
                    msgs.append(m)
            
            total = len(msgs)
            if total == 0:
                await websocket.send_json({"type": "scan_done", "topic_id": tid})
                return

            for i, msg in enumerate(reversed(msgs)):
                p, t = get_track_info(msg)
                track_data = {
                    "id": msg.id, "type": "audio" if (msg.audio or msg.voice) else "image",
                    "p": p if (msg.audio or msg.voice) else "[ОБЛОЖКА]", "t": t,
                    "s": f"{msg.file.size / 1048576:.1f} МБ" if (msg.audio or msg.voice) else f"{msg.file.size / 1024:.1f} КБ"
                }
                await websocket.send_json({
                    "type": "entry", "topic_id": tid, "scan_percent": int(((i+1)/total)*100), 
                    "track": track_data, "total_found": total
                })
                if i % 5 == 0: await asyncio.sleep(0.01) # Даем WS продышаться
            await websocket.send_json({"type": "scan_done", "topic_id": tid})
        except Exception as e: logger.error(f"Ошибка скана: {e}")

async def download_task(websocket: WebSocket, data: dict):
    async with dl_semaphore:
        mid, tid = data['id'], data['topic_id']
        try:
            msg = await client.get_messages(GROUP_ID, ids=int(mid))
            p, t = get_track_info(msg)
            safe_folder = "".join([c for c in data['folder'] if c.isalnum() or c==' ']).strip()
            target_dir = os.path.join(data['path'], safe_folder)
            os.makedirs(target_dir, exist_ok=True)
            ext = msg.file.ext if msg.file.ext else (".mp3" if msg.audio else ".jpg")
            fname = f"{p} - {t}{ext}".replace("/", "_") if msg.audio else (msg.file.name or f"img_{mid}{ext}")
            
            async def cb(c, t_val):
                try: await websocket.send_json({"type": "dl_status", "id": mid, "progress": int((c/t_val)*100), "topic_id": tid})
                except: pass
            
            await client.download_media(msg, file=os.path.join(target_dir, fname), progress_callback=cb)
            await websocket.send_json({"type": "dl_done", "id": mid, "topic_id": tid})
        except Exception as e: logger.error(f"Ошибка загрузки: {e}")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        res = await client(functions.channels.GetForumTopicsRequest(channel=GROUP_ID, offset_date=0, offset_id=0, offset_topic=0, limit=100))
        ts = [{"id": t.id, "title": t.title} for t in res.topics if hasattr(t, 'title')]
        await websocket.send_json({"type": "init", "topics": ts})
        while True:
            d = await websocket.receive_json()
            if d['type'] == 'scan_topic': asyncio.create_task(scan_topic_task(websocket, int(d['topic_id'])))
            elif d['type'] == 'download': asyncio.create_task(download_task(websocket, d))
    except WebSocketDisconnect: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)