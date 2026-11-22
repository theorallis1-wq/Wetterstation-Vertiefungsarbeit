#!/usr/bin/env python3

"""Wetterstation-Funktionstest fuer Raspberry Pi OS / Raspberry Pi 5.

 

Standard: nur BMP280/BME280 (Temperatur/Druck) und LTR390 auf I2C1.

Wind/Regen erst nach bestaetigter potentialfreier Kontaktart mit --kontakte.

Dieser Code prueft keine Kurzschluesse, Versorgungsspannung oder Kalibrierung.

Hardwarefrei: python3 wetterstation_test.py --selbsttest

"""

import argparse

import math

import struct

import sys

import threading

import time

from datetime import datetime

 

 

class SensorFehler(RuntimeError):

    pass

 

 

def kalibrierung_lesen(daten):

    if len(daten) != 24:

        raise SensorFehler("BMP: Kalibrierblock hat nicht 24 Bytes.")

    if all(x == 0 for x in daten) or all(x == 255 for x in daten):

        raise SensorFehler("BMP: ungueltiger Kalibrierblock (nur 0/255).")

    werte = struct.unpack("<HhhHhhhhhhhh", bytes(daten))

    if werte[0] == 0 or werte[3] == 0:

        raise SensorFehler("BMP: ungueltige T1/P1-Kalibrierung.")

    return werte

 

 

def bmp_kompensieren(adc_t, adc_p, k):

    """Bosch-Gleitkomma-Kompensation: Rueckgabe Grad Celsius und Pascal."""

    t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = k

    if adc_t == 0x80000 or adc_p == 0x80000:

        raise SensorFehler("BMP: Temperatur oder Druck wurde nicht gemessen.")

    v1 = (adc_t / 16384.0 - t1 / 1024.0) * t2

    v2 = ((adc_t / 131072.0 - t1 / 8192.0) ** 2) * t3

    t_fine = v1 + v2

    temperatur = t_fine / 5120.0

    v1 = t_fine / 2.0 - 64000.0

    v2 = v1 * v1 * p6 / 32768.0

    v2 += v1 * p5 * 2.0

    v2 = v2 / 4.0 + p4 * 65536.0

    v1 = (p3 * v1 * v1 / 524288.0 + p2 * v1) / 524288.0

    v1 = (1.0 + v1 / 32768.0) * p1

    if v1 == 0:

        raise SensorFehler("BMP: Druckkompensation teilt durch null.")

    druck = (1048576.0 - adc_p - v2 / 4096.0) * 6250.0 / v1

    v1 = p9 * druck * druck / 2147483648.0

    v2 = druck * p8 / 32768.0

    druck += (v1 + v2 + p7) / 16.0

    if not (math.isfinite(temperatur) and math.isfinite(druck)):

        raise SensorFehler("BMP: Messwert ist nicht endlich.")

    return temperatur, druck

 

 

def adc20(daten):

    if len(daten) != 3:

        raise SensorFehler("BMP: ADC-Wert muss aus 3 Bytes bestehen.")

    return (daten[0] << 12) | (daten[1] << 4) | (daten[2] >> 4)

 

 

def u24_le(daten):

    if len(daten) != 3:

        raise SensorFehler("LTR: Datenwort muss aus 3 Bytes bestehen.")

    return daten[0] | (daten[1] << 8) | (daten[2] << 16)

 

 

class BMP:

    def __init__(self, bus, adresse=0x76):

        self.bus, self.adresse = bus, adresse

        self.chip_id = bus.read_byte_data(adresse, 0xD0)

        if self.chip_id not in (0x58, 0x60):

            raise SensorFehler(

                f"BMP: Chip-ID 0x{self.chip_id:02X} ist weder BMP280 (0x58) "

                "noch BME280 (0x60). Nicht mit geratenem Treiber weitermachen.")

        self.name = "BMP280" if self.chip_id == 0x58 else "BME280 (nur Temperatur/Druck)"

        self.bus.write_byte_data(adresse, 0xF4, 0x00)  # Sleep, kein Reset erforderlich.

        ende = time.monotonic() + 1.0

        while self.bus.read_byte_data(adresse, 0xF3) & 0x09:

            if time.monotonic() > ende:

                raise SensorFehler("BMP: Sensor ist dauerhaft beschaeftigt.")

            time.sleep(0.01)

        self.k = kalibrierung_lesen(bus.read_i2c_block_data(adresse, 0x88, 24))

        self.bus.write_byte_data(adresse, 0xF5, 0x00)  # Filter aus.

 

    def messen(self):

        # Temperatur x1, Druck x1, Forced mode. Danach automatisch Sleep.

        self.bus.write_byte_data(self.adresse, 0xF4, 0x25)

        time.sleep(0.01)

        ende = time.monotonic() + 1.0

        while self.bus.read_byte_data(self.adresse, 0xF3) & 0x08:

            if time.monotonic() > ende:

                raise SensorFehler("BMP: Messung wurde nicht fertig.")

            time.sleep(0.005)

        d = self.bus.read_i2c_block_data(self.adresse, 0xF7, 6)

        if len(d) != 6:

            raise SensorFehler("BMP: Messblock hat nicht 6 Bytes.")

        temperatur, pa = bmp_kompensieren(adc20(d[3:6]), adc20(d[0:3]), self.k)

        return temperatur, pa / 100.0

 

    def schliessen(self):

        self.bus.write_byte_data(self.adresse, 0xF4, 0x00)

 

 

class LTR:

    """LTR390: Rohwerte, bewusst keine erfundenen Lux-/UV-Index-Werte."""

    def __init__(self, bus, adresse=0x53):

        self.bus, self.adresse = bus, adresse

        self.part_id = bus.read_byte_data(adresse, 0x06)

        if (self.part_id >> 4) != 0xB:

            raise SensorFehler(f"LTR: unerwartete Part-ID 0x{self.part_id:02X}; erwartet 0xB?.")

        bus.write_byte_data(adresse, 0x00, 0x00)  # Messung zuerst ausschalten.

        # MEAS_RATE: 18 Bit / 100 ms Integration, 100 ms Wiederholrate.

        # GAIN: Code 1 = 3-fache Verstaerkung. Licht und UV vergleichbar konfigurieren.

        bus.write_byte_data(adresse, 0x04, 0x22)

        bus.write_byte_data(adresse, 0x05, 0x01)

        if bus.read_byte_data(adresse, 0x04) != 0x22:

            raise SensorFehler("LTR: Messkonfiguration liess sich nicht zuruecklesen.")

        if (bus.read_byte_data(adresse, 0x05) & 0x07) != 0x01:

            raise SensorFehler("LTR: Gain-Konfiguration liess sich nicht zuruecklesen.")

 

    def einzelmessung(self, uv):

        self.bus.write_byte_data(self.adresse, 0x00, 0x00)

        self.bus.read_byte_data(self.adresse, 0x07)  # Alten Status lesen/loeschen.

        self.bus.write_byte_data(self.adresse, 0x00, 0x0A if uv else 0x02)

        # Nicht unmittelbar nach dem Moduswechsel einen alten Messwert verwenden.

        time.sleep(0.12)

        ende = time.monotonic() + 1.0

        while not (self.bus.read_byte_data(self.adresse, 0x07) & 0x08):

            if time.monotonic() > ende:

                raise SensorFehler("LTR: kein neues Datenwort (Status-Bit 3 bleibt 0).")

            time.sleep(0.01)

        wert = u24_le(self.bus.read_i2c_block_data(

            self.adresse, 0x10 if uv else 0x0D, 3))

        if wert >= (1 << 18):

            raise SensorFehler("LTR: Datenwort ausserhalb der eingestellten 18-Bit-Aufloesung.")

        return wert

 

    def messen(self):

        # Zwei aufeinanderfolgende Messungen, nicht exakt gleichzeitig.

        return self.einzelmessung(False), self.einzelmessung(True)

 

    def schliessen(self):

        self.bus.write_byte_data(self.adresse, 0x00, 0x00)

 

 

class Kontakte:

    def __init__(self, wind_ms, regen_ms):

        # Import nur beim ausdruecklich freigegebenen GPIO-Test.

        from gpiozero import Button, Device

        from gpiozero.pins.lgpio import LGPIOFactory

        Device.pin_factory = LGPIOFactory()

        self.lock = threading.Lock()

        self.wind_n = self.regen_n = 0

        self.wind = self.regen = None

        try:

            self.wind = Button(17, pull_up=True, bounce_time=wind_ms / 1000.0 or None)

            self.regen = Button(27, pull_up=True, bounce_time=regen_ms / 1000.0 or None)

            self.wind.when_pressed = self._wind

            self.regen.when_pressed = self._regen

        except Exception:

            self.schliessen()

            raise

 

    def _wind(self):

        with self.lock:

            self.wind_n += 1

 

    def _regen(self):

        with self.lock:

            self.regen_n += 1

 

    def werte(self):

        with self.lock:

            n = self.wind_n, self.regen_n

        return (*n, self.wind.is_pressed, self.regen.is_pressed)

 

    def schliessen(self):

        for kontakt in (self.wind, self.regen):

            if kontakt is not None:

                kontakt.close()

 

 

def positiv(s):

    x = float(s)

    if not math.isfinite(x) or x <= 0:

        raise argparse.ArgumentTypeError("Wert muss endlich und groesser als 0 sein.")

    return x

 

 

def endlich(s):

    x = float(s)

    if not math.isfinite(x):

        raise argparse.ArgumentTypeError("Wert muss endlich sein.")

    return x

 

 

def nichtnegativ(s):

    x = float(s)

    if not math.isfinite(x) or x < 0:

        raise argparse.ArgumentTypeError("Wert muss endlich und mindestens 0 sein.")

    return x

 

 

def selbsttest():

    """Rechen-, Parsing- und simulierte Registertests. Kein Hardwaretest!"""

    from unittest.mock import patch

    k = (27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)

    assert kalibrierung_lesen(struct.pack("<HhhHhhhhhhhh", *k)) == k

    t, p = bmp_kompensieren(519888, 415148, k)

    assert abs(t - 25.08247793) < 1e-6, t

    assert abs(p - 100653.2667758) < 0.01, p

    assert adc20([0x65, 0x5A, 0xC0]) == 415148

    assert u24_le([0x56, 0x34, 0x02]) == 0x23456

    for schlecht in ([0] * 24, [255] * 24, [1] * 23):

        try:

            kalibrierung_lesen(schlecht)

        except SensorFehler:

            pass

        else:

            raise AssertionError("Fehlerhafte Kalibrierung nicht erkannt.")

 

    class FakeBus:

        def __init__(self):

            self.reg = {(0x76, 0xD0): 0x58, (0x53, 0x06): 0xB2}

            self.writes = []

        def read_byte_data(self, adr, reg):

            if adr == 0x76 and reg == 0xF3:

                return 0

            if adr == 0x53 and reg == 0x07:

                return 0x08 if self.reg.get((adr, 0x00), 0) & 2 else 0

            return self.reg.get((adr, reg), 0)

        def write_byte_data(self, adr, reg, wert):

            self.reg[(adr, reg)] = wert

            self.writes.append((adr, reg, wert))

        def read_i2c_block_data(self, adr, reg, n):

            if adr == 0x76 and reg == 0x88:

                return list(struct.pack("<HhhHhhhhhhhh", *k))

            if adr == 0x76 and reg == 0xF7:

                return [0x65, 0x5A, 0xC0, 0x7E, 0xED, 0x00]

            if adr == 0x53 and reg == 0x0D:

                return [0xD2, 0x04, 0x00]  # 1234 ALS counts

            if adr == 0x53 and reg == 0x10:

                return [0x2A, 0x00, 0x00]  # 42 UV counts

            raise AssertionError((adr, reg, n))

 

    bus = FakeBus()

    with patch.object(time, "sleep", lambda _: None):

        bmp = BMP(bus)

        t2, hpa = bmp.messen()

        assert abs(t2 - t) < 1e-9 and abs(hpa - p / 100) < 1e-9

        ltr = LTR(bus)

        assert ltr.messen() == (1234, 42)

        ltr.schliessen()

        bmp.schliessen()

    assert (0x53, 0x00, 0x02) in bus.writes

    assert (0x53, 0x00, 0x0A) in bus.writes

    bus.reg[(0x76, 0xD0)] = 0x61

    try:

        BMP(bus)

    except SensorFehler:

        pass

    else:

        raise AssertionError("Falsche Chip-ID nicht erkannt.")

    print("SELBSTTEST OK: Bosch-Referenz, Byte-Reihenfolge, Fehlerfaelle und simulierte I2C-Messungen.")

    print(f"Referenz: {t:.5f} °C / {p / 100:.5f} hPa.")

    print("NICHT geprueft: reale Verkabelung, Sensorhardware, GPIO-Ereignisse oder Messgenauigkeit.")

    return 0

 

 

def main():

    parser = argparse.ArgumentParser(

        description="Funktionstest: BMP280/BME280 + LTR390; Kontakte nur nach Sicherheitspruefung.",

        epilog="Ohne Datenblattfaktoren bleiben Wind und Regen Impulswerte. Keine Kalibrierungspruefung.")

    parser.add_argument("--selbsttest", action="store_true", help="Nur Software testen, kein Hardwarezugriff.")

    parser.add_argument("--kontakte", action="store_true", help="GPIO17/27 aktivieren: NUR bestaetigte passive Kontakte!")

    parser.add_argument("--intervall", type=positiv, default=5.0, help="Ausgabeintervall in Sekunden, Standard 5.")

    parser.add_argument("--dauer", type=positiv, help="Nach dieser Anzahl Sekunden beenden; sonst Ctrl+C.")

    parser.add_argument("--bmp-adresse", choices=("0x76", "0x77"), default="0x76")

    parser.add_argument("--referenz-temp", type=endlich, help="Vergleichsthermometer in °C, gleiche Umgebung.")

    parser.add_argument("--referenz-druck", type=positiv, help="Vergleichs-STATIONSDRUCK in hPa, nicht Meeresspiegeldruck.")

    parser.add_argument("--regen-mm-pro-impuls", type=positiv, help="ECHTER Datenblatt-/Kalibrierfaktor in mm/Impuls.")

    parser.add_argument("--wind-ms-pro-hz", type=positiv, help="ECHTER Faktor: (m/s) pro Hz gezaehlter Schliessimpulse.")

    parser.add_argument("--wind-entprell-ms", type=nichtnegativ, default=5.0, help="Provisorische Entprellung, Standard 5 ms.")

    parser.add_argument("--regen-entprell-ms", type=nichtnegativ, default=50.0, help="Provisorische Entprellung, Standard 50 ms.")

    args = parser.parse_args()

    if args.selbsttest:

        return selbsttest()

    if not args.kontakte and (args.regen_mm_pro_impuls or args.wind_ms_pro_hz):

        parser.error("Kalibrierfaktoren erfordern --kontakte und bestaetigte passive Kontaktart.")

    print("FUNKTIONSTEST — kein Schutz vor Kurzschluss und kein Nachweis der Messgenauigkeit.")

    if not args.kontakte:

        print("Wind/Regen deaktiviert: die unbekannten Sensorleitungen ABGEKLEMMT lassen.")

    else:

        print("ACHTUNG: --kontakte bestaetigt passive, spannungsfreie Kontakte gegen GND.")

        print("Keine aktive Sensorspannung, keine 5 V! Entprellzeiten sind provisorisch.")

    bus = None

    bmp = ltr = kontakte = None

    try:

        try:

            from smbus import SMBus

            bus = SMBus(1)

        except (ImportError, OSError) as e:

            print(f"I2C nicht verfuegbar: {e}")

            print("I2C in raspi-config aktivieren; python3-smbus installieren; /dev/i2c-1 und Rechte pruefen.")

        if bus is not None:

            try:

                bmp = BMP(bus, int(args.bmp_adresse, 16))

                print(f"OK: {bmp.name}, Chip-ID 0x{bmp.chip_id:02X}, Adresse {args.bmp_adresse}.")

            except (OSError, SensorFehler) as e:

                print(f"BMP nicht bereit: {e}")

                print("Stromlos pruefen: VCC=3,3 V; GND; SDA=Pin3; SCL=Pin5; CSB=3,3 V; SDO=GND (0x76).")

            try:

                ltr = LTR(bus)

                print(f"OK: LTR390, Part-ID 0x{ltr.part_id:02X}, Adresse 0x53.")

                print("LTR-Konfiguration: Gain 3x, 18 Bit, Integration 100 ms; ALS/UV nacheinander.")

            except (OSError, SensorFehler) as e:

                print(f"LTR nicht bereit: {e}")

                print("Stromlos pruefen: VIN=3,3 V; GND; SDA=Pin3; SCL=Pin5; 3V/INT frei.")

        if args.kontakte:

            try:

                kontakte = Kontakte(args.wind_entprell_ms, args.regen_entprell_ms)

                print("OK: Wind GPIO17/Pin11, Regen GPIO27/Pin13, interne Pull-ups aktiv.")

                print("Handtest: langsam drehen/kippen und Zaehlung beobachten.")

            except Exception as e:

                print(f"GPIO nicht bereit: {e}")

                print("python3-gpiozero/python3-lgpio, GPIO-Rechte und Raspberry Pi OS pruefen.")

        if bmp is None and ltr is None and kontakte is None:

            print("Kein Testkanal verfuegbar. Ausschalten und Aufbau/Installation pruefen.")

            return 1

        start = time.monotonic()

        letzte_kontaktzeit = start

        letzte_wind = letzte_regen = 0

        naechste_ausgabe = start

        while args.dauer is None or time.monotonic() - start < args.dauer:

            print("\n" + datetime.now().astimezone().isoformat(timespec="seconds"))

            if bmp is not None:

                try:

                    t, hpa = bmp.messen()

                    print(f"Temperatur: {t:.2f} °C | Stationsdruck: {hpa:.2f} hPa")

                    if args.referenz_temp is not None:

                        print(f"  Abweichung zur Temperaturreferenz: {t - args.referenz_temp:+.2f} °C")

                    if args.referenz_druck is not None:

                        print(f"  Abweichung zur Stationsdruckreferenz: {hpa - args.referenz_druck:+.2f} hPa")

                    if not (-40 <= t <= 85 and 300 <= hpa <= 1100):

                        print("  WARNUNG: ausserhalb des spezifizierten BMP-Messbereichs; Daten/Umgebung pruefen.")

                except (OSError, SensorFehler) as e:

                    print(f"BMP-Messfehler: {e}; fuer Kabelaenderungen erst ausschalten.")

            if ltr is not None:

                try:

                    als, uv = ltr.messen()

                    print(f"Licht (ALS): {als} Counts | UV: {uv} Counts — Rohwerte, NICHT Lux/UV-Index")

                    if als >= 262143 or uv >= 262143:

                        print("  WARNUNG: Saettigung; Rohwert nicht als gueltige Lichtstaerke interpretieren.")

                except (OSError, SensorFehler) as e:

                    print(f"LTR-Messfehler: {e}; fuer Kabelaenderungen erst ausschalten.")

            if kontakte is not None:

                jetzt = time.monotonic()

                wn, rn, wc, rc = kontakte.werte()

                dt = jetzt - letzte_kontaktzeit

                dw, dr = wn - letzte_wind, rn - letzte_regen

                hz = dw / dt if dt > 0 else 0.0

                print(f"Wind: {wn} Impulse seit Start, +{dw} in {dt:.2f} s = {hz:.3f} Hz; Kontakt {'zu' if wc else 'offen'}")

                print(f"Regen: {rn} Impulse seit Start, +{dr}; Kontakt {'zu' if rc else 'offen'}")

                if args.wind_ms_pro_hz:

                    v = hz * args.wind_ms_pro_hz

                    print(f"Wind mit DEINEM Faktor: {v:.2f} m/s = {v * 3.6:.2f} km/h (Intervallmittel)")

                else:

                    print("  Windgeschwindigkeit nicht berechnet: Faktor (m/s)/Hz fehlt.")

                if args.regen_mm_pro_impuls:

                    print(f"Regen seit Programmstart mit DEINEM Faktor: {rn * args.regen_mm_pro_impuls:.3f} mm")

                else:

                    print("  Regenmenge nicht berechnet: mm/Impuls fehlt.")

                letzte_kontaktzeit, letzte_wind, letzte_regen = jetzt, wn, rn

            naechste_ausgabe += args.intervall

            warte = max(0.0, naechste_ausgabe - time.monotonic())

            if args.dauer is not None:

                warte = min(warte, max(0.0, start + args.dauer - time.monotonic()))

            time.sleep(warte)

        return 0

    except KeyboardInterrupt:

        print("\nTest beendet. Vor Aenderungen Pi herunterfahren und Netzteil abziehen.")

        return 0

    finally:

        if kontakte is not None:

            kontakte.schliessen()

        for sensor in (bmp, ltr):

            if sensor is not None:

                try:

                    sensor.schliessen()

                except OSError:

                    pass

        if bus is not None:

            bus.close()

 

 

if __name__ == "__main__":

    sys.exit(main())
