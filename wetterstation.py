#!/usr/bin/env python3

"""Wetterstation Raspberry Pi 5: DHT11 + Wind + Regen -> API.

Hardware (physische Pins): DHT11 S = Pin 7 (GPIO4), Wind = Pin 11 (GPIO17),
Regen = Pin 13 (GPIO27), alle Kontakte gegen GND. Bibliothek: python3-lgpio.

Start:   python3 wetterstation.py                 (misst und sendet alle 5s)
Anzeige: python3 wetterstation.py --nur-anzeige   (ohne Senden)
"""

import argparse
import http.client
import json
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

STANDARD = {
    "host": "wetter.noeg.app", "port": 80, "pfad": "/api/measurements",
    "https": False, "api_key": "wx9K-7mP4-qL2v-R8nJ-5tY3-zW6b", "intervall_s": 5.0,  # Intervall auf 5 Sekunden verkürzt
    "dht_gpio": 4, "wind_gpio": 17, "regen_gpio": 27, "dht_versuche": 5,
    "wind_kmh_pro_hz": 7.4, "regen_mm_pro_impuls": 0.3,
    "wind_sperre_ms": 2.0, "regen_sperre_ms": 50.0,
}
ZAHLEN_POSITIV = ("intervall_s", "wind_kmh_pro_hz", "regen_mm_pro_impuls")


class DhtFehler(RuntimeError):
    pass


# ---------------------------------------------------------------- Konfiguration
def konfig_laden(pfad, cli):
    cfg = dict(STANDARD)
    explizit = set()
    p = Path(pfad)
    if p.exists():
        try:
            datei = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit(f"Konfigurationsdatei {p} nicht lesbar: {e}")
        for k, v in datei.items():
            if k in cfg:
                cfg[k] = v
                explizit.add(k)
    if os.environ.get("WETTER_API_KEY"):
        cfg["api_key"] = os.environ["WETTER_API_KEY"]
    for k, v in cli.items():
        if v is not None:
            cfg[k] = v
            explizit.add(k)
    for k in ZAHLEN_POSITIV:
        try:
            ok = math.isfinite(float(cfg[k])) and float(cfg[k]) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise SystemExit(f"Einstellung {k} muss eine Zahl groesser als 0 sein.")
        cfg[k] = float(cfg[k])
    for k in ("wind_sperre_ms", "regen_sperre_ms"):
        cfg[k] = float(cfg[k])
        if not (math.isfinite(cfg[k]) and cfg[k] >= 0):
            raise SystemExit(f"Einstellung {k} muss mindestens 0 sein.")
    cfg["faktoren_bestaetigt"] = {"wind": "wind_kmh_pro_hz" in explizit,
                                  "regen": "regen_mm_pro_impuls" in explizit}
    return cfg


# ---------------------------------------------------------------- DHT11
def dht11_dekodieren(ereignisse):
    hoch, anstieg = [], None
    for pegel, t in ereignisse:
        if pegel == 1:
            anstieg = t
        elif pegel == 0 and anstieg is not None:
            hoch.append((t - anstieg) / 1000.0)
            anstieg = None
    if len(hoch) < 40:
        raise DhtFehler(f"nur {len(hoch)} von 40 Bits empfangen")
    bits = hoch[-40:]
    if any(not (10.0 <= d <= 120.0) for d in bits):
        raise DhtFehler("unplausible Pulslaengen")
    werte = [1 if d > 48.0 else 0 for d in bits]
    b = [int("".join(map(str, werte[i:i + 8])), 2) for i in range(0, 40, 8)]
    if (b[0] + b[1] + b[2] + b[3]) & 0xFF != b[4]:
        raise DhtFehler("Pruefsumme falsch")
    feuchte = b[0] + b[1] / 10.0
    temp = b[2] + (b[3] & 0x7F) / 10.0
    if b[3] & 0x80:
        temp = -temp
    if not (0.0 <= feuchte <= 100.0 and -40.0 <= temp <= 80.0):
        raise DhtFehler("Werte ausserhalb des plausiblen Bereichs")
    return temp, feuchte


def dht_mit_wiederholung(gpio, versuche, pause_s=2.0, schlafen=time.sleep):
    fehler = "kein Versuch"
    for i in range(versuche):
        try:
            return gpio.dht11_lesen(), None
        except DhtFehler as e:
            fehler = str(e)
        except Exception as e:
            fehler = f"GPIO-Fehler: {e}"
        if i < versuche - 1:
            schlafen(pause_s)
    return None, fehler


# ---------------------------------------------------------------- Impulse
class Impulszaehler:
    def __init__(self, sperre_ms):
        self.sperre_ns = int(sperre_ms * 1_000_000)
        self.lock = threading.Lock()
        self.n = 0
        self.letzter = None

    def flanke(self, t_ns):
        with self.lock:
            if self.letzter is None or t_ns - self.letzter >= self.sperre_ns:
                self.n += 1
                self.letzter = t_ns
                return True
            return False

    def stand(self):
        with self.lock:
            return self.n


class Gpio:
    def __init__(self, cfg, diagnose=None):
        import lgpio
        self.lg = lgpio
        self.h = self._chip_oeffnen()
        self.pins = {"dht": cfg["dht_gpio"], "wind": cfg["wind_gpio"], "regen": cfg["regen_gpio"]}
        self.wind = Impulszaehler(cfg["wind_sperre_ms"])
        self.regen = Impulszaehler(cfg["regen_sperre_ms"])
        self.cbs = []
        self.belegt = []
        try:
            for name in ("wind", "regen"):
                pin = self.pins[name]
                lgpio.gpio_claim_alert(self.h, pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
                self.belegt.append(pin)
                zaehler = self.wind if name == "wind" else self.regen
                cb = lgpio.callback(self.h, pin, lgpio.FALLING_EDGE,
                                    lambda chip, gpio, level, t, z=zaehler: z.flanke(t))
                self.cbs.append(cb)
        except Exception:
            self.schliessen()
            raise

    def _chip_oeffnen(self):
        lg = self.lg
        for chip in (4, 0):
            try:
                h = lg.gpiochip_open(chip)
            except Exception:
                continue
            try:
                label = str(lg.gpio_get_chip_info(h)[-1]).lower()
                if "rp1" not in label and chip == 4:
                    lg.gpiochip_close(h)
                    continue
            except Exception:
                pass
            return h
        raise RuntimeError("GPIO-Chip nicht gefunden")

    def stand(self):
        return self.wind.stand(), self.regen.stand()

    def dht11_lesen(self):
        lg, pin = self.lg, self.pins["dht"]
        ereignisse = []
        cb = None
        try:
            lg.gpio_claim_output(self.h, pin, 0)
            time.sleep(0.02)
            lg.gpio_free(self.h, pin)
            lg.gpio_claim_alert(self.h, pin, lg.BOTH_EDGES, lg.SET_PULL_UP)
            cb = lg.callback(self.h, pin, lg.BOTH_EDGES,
                             lambda chip, gpio, level, t: ereignisse.append((level, t)))
            time.sleep(0.06)
        finally:
            if cb is not None:
                cb.cancel()
            try:
                lg.gpio_free(self.h, pin)
            except Exception:
                pass
        return dht11_dekodieren(ereignisse)

    def schliessen(self):
        for cb in self.cbs:
            try:
                cb.cancel()
            except Exception:
                pass
        for pin in self.belegt:
            try:
                self.lg.gpio_free(self.h, pin)
            except Exception:
                pass
        try:
            self.lg.gpiochip_close(self.h)
        except Exception:
            pass


# ---------------------------------------------------------------- Messzyklus & Payload
def messzyklus(zustand, gpio, jetzt, cfg, schlafen=time.sleep):
    wn, rn = gpio.stand()
    dt = max(jetzt - zustand["zeit"], 1e-6)
    dw, dr = wn - zustand["wind_n"], rn - zustand["regen_n"]
    zustand.update(zeit=jetzt, wind_n=wn, regen_n=rn)
    zustand["regen_offen"] += dr
    hz = dw / dt
    wind_kmh = hz * cfg["wind_kmh_pro_hz"]
    
    (dht, dht_fehler) = dht_mit_wiederholung(gpio, int(cfg["dht_versuche"]), schlafen=schlafen)
    
    temp = dht[0] if dht is not None else 0.0
    hum = dht[1] if dht is not None else 0.0
    uv_idx = 0.0
    wind = wind_kmh
    rain = zustand["regen_offen"] * cfg["regen_mm_pro_impuls"]

    payload = {
        "station_id": "Pi5-Station",
        "temperatur": round(temp, 2),
        "luftfeuchtigkeit": round(hum, 2),
        "uv_index": round(uv_idx, 2),
        "wind_kmh": round(wind, 2),
        "regen_mm": round(rain, 2)
    }

    return {"payload": payload, "dt": dt, "dw": dw, "dr": dr, "hz": hz,
            "wind_gesamt": wn, "regen_gesamt": rn, "dht_fehler": dht_fehler,
            "regen_impulse_gesendet": zustand["regen_offen"]}


def ausgabe(m, cfg):
    p = m["payload"]
    print("\n" + "=" * 62)
    print("Zeit: " + datetime.now().astimezone().isoformat(timespec="seconds"))
    print(f"Station-ID:         {p['station_id']}")
    print(f"Temperatur:         {p['temperatur']:.2f} °C")
    print(f"Luftfeuchtigkeit:   {p['luftfeuchtigkeit']:.2f} % rF")
    print(f"UV-Index:           {p['uv_index']:.2f}")
    print(f"Windgeschwindigkeit:{p['wind_kmh']:7.2f} km/h")
    print(f"Regen (zu senden):  {p['regen_mm']:.2f} mm")


# ---------------------------------------------------------------- Senden
def senden(cfg, payload, timeout=10):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    kopf = {"Content-Type": "application/json", "Content-Length": str(len(body)),
            "X-API-Key": cfg["api_key"], "User-Agent": "RaspberryPi5-Wetterstation/1.0"}
    klasse = http.client.HTTPSConnection if cfg["https"] else http.client.HTTPConnection
    conn = klasse(cfg["host"], int(cfg["port"]), timeout=timeout)
    try:
        conn.request("POST", cfg["pfad"], body, kopf)
        r = conn.getresponse()
        text = r.read(300).decode("utf-8", "replace").strip()
        return r.status, text
    finally:
        conn.close()


def senden_und_melden(cfg, m, zustand, sender=senden):
    try:
        status, text = sender(cfg, m["payload"])
    except Exception as e:
        print(f"Senden fehlgeschlagen: {e}")
        return False
    if 200 <= status < 300:
        zustand["regen_offen"] = max(0, zustand["regen_offen"] - m["regen_impulse_gesendet"])
        print(f"Gesendet: HTTP {status}")
        return True
    print(f"Server hat abgelehnt: HTTP {status} {text[:200]}")
    return False


# ---------------------------------------------------------------- Hauptprogramm
def main():
    ap = argparse.ArgumentParser(description="Wetterstation Pi 5 -> API")
    ap.add_argument("--config", default=str(Path(__file__).with_name("wetter_config.json")))
    ap.add_argument("--nur-anzeige", action="store_true", help="nichts senden")
    ap.add_argument("--intervall", type=float, dest="intervall_s")
    a = ap.parse_args()
    
    cli = {k: getattr(a, k) for k in ("intervall_s", "wind_kmh_pro_hz", "regen_mm_pro_impuls") if hasattr(a, k)}
    cfg = konfig_laden(a.config, cli)
    
    gpio = Gpio(cfg)
    try:
        start = time.monotonic()
        zustand = {"zeit": start, "wind_n": 0, "regen_n": 0, "regen_offen": 0}
        naechste = start + cfg["intervall_s"]
        
        while True:
            time.sleep(max(0.0, naechste - time.monotonic()))
            m = messzyklus(zustand, gpio, time.monotonic(), cfg)
            ausgabe(m, cfg)
            if not a.nur_anzeige:
                senden_und_melden(cfg, m, zustand)
            naechste += cfg["intervall_s"]
    except KeyboardInterrupt:
        print("\nBeendet.")
        return 0
    finally:
        gpio.schliessen()

if __name__ == "__main__":
    sys.exit(main())
