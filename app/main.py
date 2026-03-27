from typing import Optional
from pathlib import Path
import time, threading
import asyncio
from collections import deque

from fastapi import FastAPI, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File, Form, HTTPException

from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build

from datetime import datetime
import os
import shutil
import uuid
import json
import traceback
import io
import zipfile

GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "1n6Q5kY7AG3d1bf4C6RIhh4JNao2JubObIeZAKaOoiJc")
GOOGLE_SHEETS_RANGE = os.getenv("GOOGLE_SHEETS_RANGE", "MMIReports!A:Q")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/etc/secrets/google-service-account.json")

class MMIReportRequest(BaseModel):
    user_uid: str | None = None
    user_lat: float
    user_lon: float

    mmi_value: int
    mmi_code: str
    feeling_text: str | None = None

    event_time_th: str | None = None
    event_time_utc: str | None = None
    event_lat: float
    event_lon: float
    event_mag: float | None = None
    event_depth_km: float | None = None
    event_changwat: str | None = None

    distance_km: float | None = None
    estimated_pga_percent_g: float | None = None
    source: str | None = "manual_user_report"

from .logic import (
    fetch_latest_event_in_thailand,
    compute_overlay_from_event,
    simulate_event,
    get_soil_info,
    predict_mmi_ai,
)
# ==== ดึงฟังก์ชันจาก logic.py ของโปรเจกคุณ ====
# - fetch_latest_event_in_thailand() : ดึงเมตาเหตุการณ์ล่าสุด
# - compute_overlay_from_event(ev)   : คำนวณ/สร้างผลลัพธ์แผนที่จาก ev
# - simulate_event(lat, lon, depth_km, mag) : จำลองเหตุการณ์


app = FastAPI(title="SHAKEMAP API", version="1.3.0")

from .logic import debug_vs30_paths

@app.get("/api/soil_debug")
def soil_debug():
    return debug_vs30_paths()


# CORS (เปิดกว้างสำหรับ dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ================== In-memory Cache (คำนวณครั้งแรก) ==================
_CACHE_LOCK = threading.Lock()
_CACHE = {
    "data": None,        # JSON ผลลัพธ์เต็ม (รวม data URL/HTML/เมตา)
    "event_key": None,   # คีย์อ้างอิงเหตุการณ์ล่าสุดที่คำนวณแล้ว
    "ts": 0.0,           # เวลาที่คำนวณ (epoch)
}

# ตั้ง TTL ถ้าอยากให้รีเฟรชอัตโนมัติเมื่อพ้นเวลา; None = ไม่หมดอายุเอง
CACHE_TTL_SEC: Optional[int] = None  # เช่น 600 = 10 นาที

def _safe_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", ".")).strip() or "upload.csv"

def _make_event_key(meta: dict) -> str:
    return f"{meta.get('time_utc') or meta.get('time_th')}|{meta.get('lat')}|{meta.get('lon')}|{meta.get('mag')}|{meta.get('depth_km')}"

def _get_cached_ok() -> bool:
    if _CACHE["data"] is None:
        return False
    if CACHE_TTL_SEC is None:
        return True
    return (time.time() - (_CACHE["ts"] or 0)) < CACHE_TTL_SEC

def _compute_and_store() -> dict:
    """คำนวณผลจากเหตุการณ์ล่าสุด แล้วเก็บลงแคช (ต้องเรียกภายใต้ LOCK)"""
    ev = fetch_latest_event_in_thailand()
    data = compute_overlay_from_event(ev)
    meta = data.get("meta", {})
    _CACHE["data"] = data
    _CACHE["event_key"] = _make_event_key(meta)
    _CACHE["ts"] = time.time()
    return data

def _get_or_compute(force: bool = False) -> dict:
    # 1) บังคับคำนวณใหม่
    if force:
        with _CACHE_LOCK:
            return _compute_and_store()

    # 2) ถ้ายังไม่มีแคช → คำนวณใหม่
    if _CACHE["data"] is None:
        with _CACHE_LOCK:
            if _CACHE["data"] is None:
                return _compute_and_store()
            return _CACHE["data"]

    # 3) มีแคชแล้ว → เช็กว่ามี "เหตุการณ์ใหม่" ไหม (เปรียบเทียบ event_key)
    try:
        ev = fetch_latest_event_in_thailand()  # ดึง meta ล่าสุด (ไม่เรนเดอร์ภาพ)
        if ev:
            # สร้างคีย์จาก meta ล่าสุด
            new_key = _make_event_key({
                "time_utc":  ev.get("time_utc"),
                "time_th":   ev.get("time_th"),
                "lat":       ev.get("lat"),
                "lon":       ev.get("lon"),
                "mag":       ev.get("mag"),
                "depth_km":  ev.get("depth"),
            })
            # ถ้าไม่ใช่เหตุการณ์เดิม → คำนวณใหม่ด้วย ev นี้
            if new_key and new_key != _CACHE["event_key"]:
                with _CACHE_LOCK:
                    data = compute_overlay_from_event(ev)
                    _CACHE["data"] = data
                    _CACHE["event_key"] = new_key
                    _CACHE["ts"] = time.time()
                    return data
    except Exception:
        # ถ้าเช็ก meta ล้มเหลว ให้ใช้ของเดิมไปก่อน
        pass

    # 4) เหตุการณ์เดิม → ใช้แคช
    return _CACHE["data"]



# ================== Queue: จำกัดผู้ใช้พร้อมกัน 10 คน ==================
MAX_ACTIVE = 10           # จำกัดผู้ใช้งานพร้อมกัน
HEARTBEAT_TIMEOUT = 45.0     # วินาที: ไม่ส่ง heartbeat เกินนี้ = หลุด
PROMOTE_BATCH = 5            # โปรโมตทีละกี่คนจากคิว (กันกรณีพุ่งพร้อมกัน)

_q_lock = asyncio.Lock()
_active = {}         # key=(client_id, tab_id) -> last_seen_ts
_queue = deque()     # item=(client_id, tab_id, enq_ts)

def _now() -> float:
    return time.time()

def _queue_position(client_id: str, tab_id: str):
    pos = 1
    for (c, t, _) in _queue:
        if c == client_id and t == tab_id:
            return pos
        pos += 1
    return None

async def _maintain_and_promote():
    """ลบ active ที่หมดอายุ และโปรโมตจากคิวตามช่องว่าง"""
    now = _now()
    # ตัด active หมดอายุ
    expired = [k for k, ts in list(_active.items()) if (now - ts) > HEARTBEAT_TIMEOUT]
    for k in expired:
        _active.pop(k, None)

    # โปรโมตจากคิว
    slots = max(0, MAX_ACTIVE - len(_active))
    moved = 0
    while slots > 0 and _queue and moved < PROMOTE_BATCH:
        c_id, t_id, _ = _queue[0]
        key = (c_id, t_id)
        if key in _active:
            _queue.popleft()
            continue
        _active[key] = _now()
        _queue.popleft()
        slots -= 1
        moved += 1

def append_mmi_report_to_google_sheet(payload: MMIReportRequest):
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEETS_SPREADSHEET_ID")
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_FILE")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=scopes,
    )

    service = build("sheets", "v4", credentials=creds)

    values = [[
        datetime.now().isoformat(),
        payload.user_uid,
        payload.user_lat,
        payload.user_lon,
        payload.mmi_value,
        payload.mmi_code,
        payload.feeling_text,
        payload.event_time_th,
        payload.event_time_utc,
        payload.event_lat,
        payload.event_lon,
        payload.event_mag,
        payload.event_depth_km,
        payload.event_changwat,
        payload.distance_km,
        payload.estimated_pga_percent_g,
        payload.source,
    ]]

    body = {"values": values}

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEETS_SPREADSHEET_ID,
        range=GOOGLE_SHEETS_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

# ================== Routes ==================
@app.get("/")
def index():
    # เสิร์ฟหน้าเว็บ
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.post("/api/report_mmi")
def api_report_mmi(payload: MMIReportRequest):
    try:
        append_mmi_report_to_google_sheet(payload)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "detail": str(e)}
        )

# --------- Queue APIs ----------
@app.post("/api/queue/enter")
async def queue_enter(req: Request):
    body = await req.json()
    client_id = body.get("client_id")
    tab_id    = body.get("tab_id")
    if not client_id or not tab_id:
        return JSONResponse({"error": "missing client_id/tab_id"}, status_code=400)

    async with _q_lock:
        await _maintain_and_promote()
        key = (client_id, tab_id)

        # อยู่ active แล้ว
        if key in _active:
            _active[key] = _now()
            return {"state": "active", "active": len(_active), "limit": MAX_ACTIVE}

        # ยังมีช่องว่าง
        if len(_active) < MAX_ACTIVE:
            _active[key] = _now()
            return {"state": "active", "active": len(_active), "limit": MAX_ACTIVE}

        # เต็ม -> เข้าคิวถ้ายังไม่อยู่
        if _queue_position(client_id, tab_id) is None:
            _queue.append((client_id, tab_id, _now()))
        pos = _queue_position(client_id, tab_id)
        return {"state": "queued", "position": pos, "active": len(_active), "limit": MAX_ACTIVE}

@app.get("/api/queue/status")
async def queue_status(client_id: str, tab_id: str):
    async with _q_lock:
        await _maintain_and_promote()
        key = (client_id, tab_id)
        if key in _active:
            return {"state": "active", "active": len(_active), "limit": MAX_ACTIVE}
        pos = _queue_position(client_id, tab_id)
        if pos is not None:
            return {"state": "queued", "position": pos, "active": len(_active), "limit": MAX_ACTIVE}
        return {"state": "none", "active": len(_active), "limit": MAX_ACTIVE}

@app.post("/api/queue/heartbeat")
async def queue_heartbeat(req: Request):
    body = await req.json()
    client_id = body.get("client_id")
    tab_id    = body.get("tab_id")
    if not client_id or not tab_id:
        return JSONResponse({"error": "missing client_id/tab_id"}, status_code=400)

    async with _q_lock:
        await _maintain_and_promote()
        key = (client_id, tab_id)
        if key in _active:
            _active[key] = _now()
            return {"state": "active", "active": len(_active), "limit": MAX_ACTIVE}
        pos = _queue_position(client_id, tab_id)
        if pos is not None:
            return {"state": "queued", "position": pos, "active": len(_active), "limit": MAX_ACTIVE}
        return {"state": "none", "active": len(_active), "limit": MAX_ACTIVE}

@app.post("/api/queue/leave")
async def queue_leave(req: Request):
    body = await req.json()
    client_id = body.get("client_id")
    tab_id    = body.get("tab_id")
    if not client_id or not tab_id:
        return JSONResponse({"error": "missing client_id/tab_id"}, status_code=400)

    async with _q_lock:
        key = (client_id, tab_id)
        _active.pop(key, None)
        # ลบจากคิวหากมี
        for i, (c, t, ts) in enumerate(list(_queue)):
            if c == client_id and t == tab_id:
                try:
                    _queue.remove((c, t, ts))
                except Exception:
                    pass
                break
        await _maintain_and_promote()
        return {"ok": True, "active": len(_active), "limit": MAX_ACTIVE}


# --------- Data APIs (เดิม) ----------
# GET สำหรับเปิดในเบราว์เซอร์/เทส
@app.get("/api/run")
def api_run_get():
    try:
        data = _get_or_compute(force=False)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# POST ใช้งานจริงจากหน้าเว็บ
@app.post("/api/run")
def api_run(body: dict = Body(default_factory=dict)):
    """
    โหมดปกติ: POST /api/run  (body ว่างก็ได้)
    โหมดจำลอง: POST /api/run { "mode":"simulate", "lat":..., "lon":..., "depth":..., "mag":... }
    บังคับรีเฟรช: POST /api/run { "force": true }
    """
    try:
        # โหมดจำลอง
        if body.get("mode") == "simulate":
            lat   = float(body["lat"])
            lon   = float(body["lon"])
            depth = float(body["depth"])
            mag   = float(body["mag"])
            data = simulate_event(lat=lat, lon=lon, depth_km=depth, mag=mag)
            return JSONResponse(data)

        force = bool(body.get("force"))
        data = _get_or_compute(force=force)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# รีเฟรชเหตุการณ์ล่าสุดแบบบังคับ (สำหรับแอดมิน/DevTools)
@app.post("/api/refresh")
def api_refresh():
    try:
        data = _get_or_compute(force=True)
        return JSONResponse({"ok": True, "meta": data.get("meta", {}), "event_key": _CACHE["event_key"]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ดูสถานะแคช (debug)
@app.get("/api/cache_state")
def api_cache_state():
    return {
        "has_cache": _CACHE["data"] is not None,
        "event_key": _CACHE["event_key"],
        "ts": _CACHE["ts"],
        "ttl_sec": CACHE_TTL_SEC,
    }



# ชั้นดิน/ประเภทชั้นดิน (Site Class จาก Vs30)
@app.get("/api/soil")
def api_soil(lat: float, lon: float):
    try:
        return JSONResponse(get_soil_info(lat=lat, lon=lon))
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e), "lat": lat, "lon": lon}, status_code=500)

@app.post("/api/simulate")
def api_simulate(body: dict = Body(...)):
    try:
        lat   = float(body["lat"])
        lon   = float(body["lon"])
        depth = float(body["depth"])
        mag   = float(body["mag"])
        data = simulate_event(lat=lat, lon=lon, depth_km=depth, mag=mag)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    
from pydantic import BaseModel

class MMIPredictRequest(BaseModel):
    dist: float
    pga: float
    mag: float
    lat: float | None = None
    lon: float | None = None


@app.post("/api/predict_mmi")
def api_predict_mmi(payload: MMIPredictRequest):
    try:
        result = predict_mmi_ai(
        dist=payload.dist,
        pga=payload.pga / 100.0,   # payload มาจากเว็บเป็น %g
        mag=payload.mag,
        lat=payload.lat,
        lon=payload.lon,
    )
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    
    
@app.post("/api/train_model")
async def train_model_api(
    file: UploadFile = File(...),
    model_type: str = Form(...),   # "classifier" หรือ "regression"
    n_iter: int = Form(25),
):
    try:
        upload_dir = Path("app/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename).suffix.lower()
        if ext not in [".xlsx", ".xls", ".csv"]:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "รองรับเฉพาะ .xlsx .xls .csv"}
            )

        file_id = uuid.uuid4().hex[:10]
        safe_filename = _safe_name(file.filename)
        saved_path = upload_dir / f"{file_id}_{safe_filename}"

        with saved_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        # import ฟังก์ชัน train ตามประเภท
        if model_type == "classifier":
            from train_mmi_classifier_tuned import (
                load_input_folder_or_file,
                prepare_dataset,
                choose_cv,
                build_search_spaces,
                tune_one_model,
            )
            selection_metric = "cv_accuracy_exact"
            better = "higher"

        elif model_type == "regression":
            from train_mmi_regression_tuned import (
                load_input_folder_or_file,
                prepare_dataset,
                choose_cv,
                build_search_spaces,
                tune_one_model,
            )
            selection_metric = "cv_mae"
            better = "lower"

        else:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "model_type ต้องเป็น classifier หรือ regression"}
            )

        df = load_input_folder_or_file(str(saved_path))
        clean_df, X_raw, X, y, groups = prepare_dataset(df)
        cv, cv_name = choose_cv(y, groups) if model_type == "classifier" else choose_cv(groups)

        spaces = build_search_spaces()
        all_results = {}

        best_name = None
        best_estimator = None
        best_value = None

        from sklearn.base import clone
        import joblib

        trained_models = {}
        for name, (model, param_dist) in spaces.items():
            
            est, result = tune_one_model(
                name=name,
                model=model,
                param_dist=param_dist,
                X=X,
                y=y,
                cv=cv,
                groups=groups,
                n_iter=n_iter,
            )
            result["cv_strategy"] = cv_name
            all_results[name] = result

            fitted_est = clone(est)
            fitted_est.fit(X, y)

            trained_models[name] = fitted_est

            current_value = result[selection_metric]
            if best_value is None:
                best_value = current_value
                best_name = name
                best_estimator = clone(est)
            else:
                if better == "higher" and current_value > best_value:
                    best_value = current_value
                    best_name = name
                    best_estimator = clone(est)
                elif better == "lower" and current_value < best_value:
                    best_value = current_value
                    best_name = name
                    best_estimator = clone(est)

        best_estimator.fit(X, y)

        report = {
            "ok": True,
            "model_type": model_type,
            "n_rows": int(len(clean_df)),
            "cv_strategy": cv_name,
            "feature_columns_raw": list(X_raw.columns),
            "feature_columns_engineered": list(X.columns),
            "best_model_name": best_name,
            "selection_metric": selection_metric,
            "best_value": float(best_value),
            "all_results": all_results,
            "uploaded_file": file.filename,
        }

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            best_bytes = io.BytesIO()
            joblib.dump(best_estimator, best_bytes)
            best_bytes.seek(0)
            zf.writestr(f"{file_id}_best_{model_type}.joblib", best_bytes.read())

            for name, model_obj in trained_models.items():
                model_bytes = io.BytesIO()
                joblib.dump(model_obj, model_bytes)
                model_bytes.seek(0)
                zf.writestr(f"{file_id}_{name}.joblib", model_bytes.read())

            zf.writestr(
                f"{file_id}_train_report.json",
                json.dumps(report, indent=2, ensure_ascii=False)
            )

        zip_buffer.seek(0)

        try:
            saved_path.unlink(missing_ok=True)
        except Exception:
            pass

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{file_id}_trained_models.zip"'
            }
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
        )

@app.post("/api/upload_model")
async def api_upload_model(
    file: UploadFile = File(...),
    target: str = Form("default"),
):
    try:
        ext = Path(file.filename).suffix.lower()
        if ext != ".joblib":
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "รองรับเฉพาะไฟล์ .joblib"}
            )

        model_dir = Path("app/models")
        model_dir.mkdir(parents=True, exist_ok=True)

        if target == "default":
            out_name = "mmi_model.joblib"
            label = "north/default"
        elif target == "west":
            out_name = "mmi_west.joblib"
            label = "west"
        elif target == "south":
            out_name = "mmi_south.joblib"
            label = "south"
        else:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "target ไม่ถูกต้อง"}
            )

        out_path = model_dir / out_name

        with out_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        import joblib
        from . import logic

        loaded_model = joblib.load(out_path)

        if target == "default":
            logic.MMI_MODEL = loaded_model
            logic.MMI_MODELS["north"] = loaded_model
        elif target == "west":
            logic.MMI_MODELS["west"] = loaded_model
        elif target == "south":
            logic.MMI_MODELS["south"] = loaded_model

        if logic.MMI_MODEL is None:
            logic.MMI_MODEL = loaded_model

        return {
            "ok": True,
            "message": "อัปโหลดโมเดลสำเร็จ",
            "target": target,
            "saved_to": str(out_path).replace("\\", "/"),
            "filename": file.filename,
            "label": label,
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "traceback": traceback.format_exc()}
        )
    
@app.get("/api/current_models")
def api_current_models():
    model_dir = Path("app/models")
    model_dir.mkdir(parents=True, exist_ok=True)

    items = [
        {"target": "default", "label": "โมเดลหลัก / default", "filename": "mmi_model.joblib"},
        {"target": "west", "label": "ภาคตะวันตก", "filename": "mmi_west.joblib"},
        {"target": "south", "label": "ภาคใต้", "filename": "mmi_south.joblib"},
    ]

    result = []
    for item in items:
        path = model_dir / item["filename"]
        exists = path.exists()
        stat = path.stat() if exists else None

        result.append({
            "target": item["target"],
            "label": item["label"],
            "filename": item["filename"],
            "exists": exists,
            "size_bytes": stat.st_size if stat else None,
            "download_url": f"/api/download_model/{item['target']}" if exists else None,
        })

    return {"ok": True, "models": result}


@app.get("/api/download_model/{target}")
def api_download_model(target: str):
    model_dir = Path("app/models")

    mapping = {
        "default": ("mmi_model.joblib", "mmi_model.joblib"),
        "west": ("mmi_west.joblib", "mmi_west.joblib"),
        "south": ("mmi_south.joblib", "mmi_south.joblib"),
    }

    if target not in mapping:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "target ไม่ถูกต้อง"}
        )

    real_name, download_name = mapping[target]
    path = model_dir / real_name

    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "ไม่พบไฟล์โมเดล"}
        )

    return FileResponse(
        path=str(path),
        filename=download_name,
        media_type="application/octet-stream",
    )
