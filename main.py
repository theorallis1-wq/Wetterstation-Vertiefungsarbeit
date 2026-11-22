from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import pymysql
import pymysql.cursors

app = FastAPI()

API_KEY = "wx9K-7mP4-qL2v-R8nJ-5tY3-zW6b"

DB_CONFIG = {
    "host": "localhost",
    "user": "weather_api",
    "password": "WetterPasswort2026!",
    "database": "weather_station",
    "cursorclass": pymysql.cursors.DictCursor
}

LOCAL_TZ = ZoneInfo("Europe/Zurich")

class Measurement(BaseModel):
    station_id: Optional[str] = "Arduino-Station"
    temperatur: Optional[float] = None
    luftfeuchtigkeit: Optional[float] = None
    uv_index: Optional[float] = None
    wind_kmh: Optional[float] = None
    regen_mm: Optional[float] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    uv: Optional[float] = None
    wind: Optional[float] = None
    rain: Optional[float] = None

def get_db():
    return pymysql.connect(**DB_CONFIG)

def format_local_time(ts) -> str:
    if ts is None:
        return "–"
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts.astimezone(LOCAL_TZ).strftime("%H:%M:%S")

@app.post("/api/measurements")
def add_measurement(data: Measurement, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Ungültiger API-Key")
    print(f"[EINGANG] {data.model_dump()}", flush=True)

    temp = data.temperatur if data.temperatur is not None else (data.temperature or 0.0)
    hum  = data.luftfeuchtigkeit if data.luftfeuchtigkeit is not None else (data.humidity or 0.0)
    uv   = data.uv_index if data.uv_index is not None else (data.uv or 0.0)
    wind = data.wind_kmh if data.wind_kmh is not None else (data.wind or 0.0)
    rain = data.regen_mm if data.regen_mm is not None else (data.rain or 0.0)

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            sql = """
                INSERT INTO measurements (station_id, temperature, humidity, uv, wind, rain)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (data.station_id, temp, hum, uv, wind, rain))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}

@app.get("/api/measurements")
def get_measurements(range: str = "live"):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT m1.station_id,
                       CAST(m1.temperature AS DOUBLE) AS temperature,
                       CAST(m1.humidity AS DOUBLE) AS humidity,
                       CAST(m1.uv AS DOUBLE) AS uv,
                       CAST(m1.wind AS DOUBLE) AS wind,
                       CAST(m1.rain AS DOUBLE) AS rain,
                       m1.created_at
                FROM measurements m1
                INNER JOIN (
                    SELECT station_id, MAX(id) as max_id
                    FROM measurements
                    GROUP BY station_id
                ) m2 ON m1.id = m2.max_id
            """)
            latest_raw = cursor.fetchall()
            latest = []
            for row in latest_raw:
                item = dict(row)
                item["time_str"] = format_local_time(item.pop("created_at", None))
                latest.append(item)

            if range == "24h":
                cursor.execute("""
                    SELECT station_id,
                           ROUND(AVG(temperature), 2) AS temperature,
                           ROUND(AVG(humidity), 2) AS humidity,
                           ROUND(AVG(uv), 2) AS uv,
                           ROUND(AVG(wind), 2) AS wind,
                           ROUND(AVG(rain), 2) AS rain,
                           DATE_FORMAT(FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(created_at)/900)*900), '%H:%i') as time_str,
                           FLOOR(UNIX_TIMESTAMP(created_at)/900)*900 as sort_key
                    FROM measurements
                    WHERE created_at >= NOW() - INTERVAL 24 HOUR
                    GROUP BY station_id, sort_key, time_str
                    ORDER BY sort_key ASC
                """)
            elif range == "7d":
                cursor.execute("""
                    SELECT station_id,
                           ROUND(AVG(temperature), 2) AS temperature,
                           ROUND(AVG(humidity), 2) AS humidity,
                           ROUND(AVG(uv), 2) AS uv,
                           ROUND(AVG(wind), 2) AS wind,
                           ROUND(AVG(rain), 2) AS rain,
                           DATE_FORMAT(FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(created_at)/3600)*3600), '%d.%m. %H:00') as time_str,
                           FLOOR(UNIX_TIMESTAMP(created_at)/3600)*3600 as sort_key
                    FROM measurements
                    WHERE created_at >= NOW() - INTERVAL 7 DAY
                    GROUP BY station_id, sort_key, time_str
                    ORDER BY sort_key ASC
                """)
            elif range == "30d":
                cursor.execute("""
                    SELECT station_id,
                           ROUND(AVG(temperature), 2) AS temperature,
                           ROUND(AVG(humidity), 2) AS humidity,
                           ROUND(AVG(uv), 2) AS uv,
                           ROUND(AVG(wind), 2) AS wind,
                           ROUND(AVG(rain), 2) AS rain,
                           DATE_FORMAT(FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(created_at)/21600)*21600), '%d.%m. %H:00') as time_str,
                           FLOOR(UNIX_TIMESTAMP(created_at)/21600)*21600 as sort_key
                    FROM measurements
                    WHERE created_at >= NOW() - INTERVAL 30 DAY
                    GROUP BY station_id, sort_key, time_str
                    ORDER BY sort_key ASC
                """)
            else:
                cursor.execute("""
                    SELECT * FROM (
                        SELECT id, station_id,
                               CAST(temperature AS DOUBLE) AS temperature,
                               CAST(humidity AS DOUBLE) AS humidity,
                               CAST(uv AS DOUBLE) AS uv,
                               CAST(wind AS DOUBLE) AS wind,
                               CAST(rain AS DOUBLE) AS rain,
                               DATE_FORMAT(created_at, '%H:%i:%s') as time_str
                        FROM measurements
                        ORDER BY id DESC
                        LIMIT 60
                    ) sub
                    ORDER BY id ASC
                """)
            history = cursor.fetchall()
    finally:
        conn.close()
    return {"latest": latest, "history": history}

app.mount("/static", StaticFiles(directory="/opt/weather-app/static"), name="static")

@app.get("/")
def index():
    return FileResponse("/opt/weather-app/static/index.html")
