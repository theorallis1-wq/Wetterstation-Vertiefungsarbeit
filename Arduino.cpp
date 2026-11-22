#include <Arduino.h>
#include <SPI.h>
#include <WiFiNINA.h>

// -------- 1. HIER DEIN WLAN EINTRAGEN --------
const char* WIFI_SSID = "DEIN_WLAN_NAME";
const char* WIFI_PASS = "DEIN_WLAN_PASSWORT";

// -------- 2. SERVER-EINSTELLUNGEN --------
const char* SERVER_HOST = "wetter.noeg.app"; 
const uint16_t SERVER_PORT = 80; // Port 80 (HTTP)
const char* API_PFAD = "/api/measurements";
const char* API_KEY  = "wx9K-7mP4-qL2v-R8nJ-5tY3-zW6b";

WiFiClient client;

void verbindeWLAN() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.print("Verbinde mit 2.4 GHz WLAN: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint8_t versuche = 0;
  while (WiFi.status() != WL_CONNECTED && versuche < 20) {
    delay(500);
    Serial.print(".");
    versuche++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWLAN verbunden!");
    Serial.print("Nano IP-Adresse: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWLAN-Verbindung fehlgeschlagen.");
  }
}

void sendeJsonAnServer(String json) {
  verbindeWLAN();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("API-Abbruch: Kein WLAN.");
    return;
  }

  Serial.print("Sende an ");
  Serial.print(SERVER_HOST);
  Serial.print(": ");
  Serial.println(json);

  if (client.connect(SERVER_HOST, SERVER_PORT)) {
    client.print("POST ");
    client.print(API_PFAD);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.println(SERVER_HOST);
    client.println("User-Agent: ArduinoNano33IoT/1.0");
    client.println("Content-Type: application/json");
    client.print("X-API-Key: ");
    client.println(API_KEY);
    client.print("Content-Length: ");
    client.println(json.length());
    client.println("Connection: close");
    client.println();
    client.println(json);

    unsigned long start = millis();
    while (client.connected() && millis() - start < 5000) {
      if (client.available()) {
        String antwort = client.readStringUntil('\n');
        if (antwort.startsWith("HTTP/1.")) {
          Serial.print("Server-Antwort: ");
          Serial.println(antwort); // Erwartet: "HTTP/1.1 200 OK"
          break;
        }
      }
    }
    client.stop();
  } else {
    Serial.println("Verbindung zu wetter.noeg.app fehlgeschlagen!");
  }
}

void setup() {
  Serial.begin(115200);
  Serial1.begin(9600); // Empfängt an Pin RX/D0 das JSON vom Uno Pin D9
  delay(1500);

  Serial.println("Nano 33 IoT gestartet. Warte auf Daten an Pin RX/D0...");
  verbindeWLAN();
}

void loop() {
  if (Serial1.available()) {
    String datenVomUno = Serial1.readStringUntil('\n');
    datenVomUno.trim();

    // Prüft, ob eine gültige JSON-Zeile vom Uno kam:
    // z.B. {"temperatur": 26.7,"luftfeuchtigkeit": 37.0,"uv_index": 2.45,"wind_kmh": 0.00,"regen_mm": 0.00}
    if (datenVomUno.startsWith("{") && datenVomUno.endsWith("}")) {
      Serial.print("Vom Uno empfangen: ");
      Serial.println(datenVomUno);
      sendeJsonAnServer(datenVomUno);
    }
  }
}
