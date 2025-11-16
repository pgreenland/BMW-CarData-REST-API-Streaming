# BMW CarData REST API & Streaming

Python-Script zum Abrufen von BMW CarData Fahrzeugdaten per REST API und Streaming.

## Features

✅ **REST API-Integration**
- Fahrzeugliste abrufen
- Fahrzeugdaten und Status abfragen
- Ladehistorie abrufen
- Fahrzeugbilder herunterladen
- Standortbasierte Ladeeinstellungen
- Smart Maintenance Reifendiagnose
- Telematikdaten abrufen
- Automatische Access-Token-Erneuerung

✅ **MQTT Streaming (Echtzeit-Daten)**
- Live-Fahrzeugdaten über MQTT-Stream
- GPS-Position, Batterie, Reichweite
- Türen- und Fensterstatus
- Automatische ID-Token-Erneuerung
- JSON Lines Ausgabe für einfache Analyse
- Einmaliges Streaming für schnelle Statusabfragen (aggregierte Daten)

## Inhaltsverzeichnis

- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- **REST API**
  - [Verwendung](#rest-api)
  - [Endpunkte](#api-endpunkte)
- **MQTT Streaming**
  - [Verwendung](#mqtt-streaming)
  - [Datenfelder](#streaming-datenfelder)
  - [Systemd-Service](#systemd-service)

## Voraussetzungen

1. **Python Environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **BMW Cardata Portal Registrierung**
   - Einloggen in My BMW: https://www.bmw.de/de-de/mybmw/vehicle-overview
   - "BMW CarData" beim entsprechenden Fahrzeug auswählen
   - "Technischer Zugang zu BMW CarData" anlegen mit Scope CarData API und CarData Stream
   - Client-ID für Setup-Script kopieren
   
3. **Detaillierte Informationen un BMW CarData Portal** (optional)
   - https://bmw-cardata.bmwgroup.com/customer/public/home

## Installation

```bash
# 1. Dependencies installieren
cd /home/pi/bmwcardata
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Config erstellen (benötigt Client-ID aus My BMW)
./setup_config.sh

# 3. REST API: Fahrzeugliste abrufen
./bmw_cardata.sh --vehicles

# 4. MQTT Streaming: 30 Sekunden Test
./bmw_cardata.sh --stream --stream-duration 30 --stream-output test.jsonl
```

---

## REST API

BMW CarData REST API für Fahrzeugdaten-Abfragen.

### Verwendung

**Shell-Script (empfohlen):**
```bash
./bmw_cardata.sh --vehicles
```

**Direkt mit Python:**
```bash
source venv/bin/activate
./bmw_cardata.py --vehicles
```

### Beispiel-Abfragen

```bash
# Übersicht
./bmw_cardata.sh --help

# Fahrzeugliste
./bmw_cardata.sh --vehicles

# Fahrzeugdaten
./bmw_cardata.sh --vin WBY71HH090CW09664

# Fahrzeugstatus
./bmw_cardata.sh --status --vin WBY71HH090CW09664

# Ladehistorie (letzte 30 Tage)
./bmw_cardata.sh --charging-history --vin WBY71HH090CW09664

# Ladehistorie (spezifischer Zeitraum)
./bmw_cardata.sh --charging-history --vin WBY71HH090CW09664 \
  --from-date "2025-01-01T00:00:00Z" \
  --to-date "2025-12-31T23:59:59Z"

# Fahrzeugbild
./bmw_cardata.sh --image --vin WBY71HH090CW09664 --image-output auto.png

# Ladeeinstellungen
./bmw_cardata.sh --charging-settings --vin WBY71HH090CW09664

# Reifendiagnose
./bmw_cardata.sh --tyre-diagnosis --vin WBY71HH090CW09664

# Telematikdaten
./bmw_cardata.sh --telematic-data --vin WBY71HH090CW09664 --container-id CONTAINER_ID
```

### API-Endpunkte

| Endpunkt | Beschreibung |
|----------|--------------|
| `GET /customers/vehicles/mappings` | Fahrzeugliste |
| `GET /customers/vehicles/{vin}/basicData` | Basis-Fahrzeugdaten |
| `GET /customers/vehicles/{vin}/chargingHistory` | Ladehistorie |
| `GET /customers/vehicles/{vin}/image` | Fahrzeugbild (PNG, 1000x600px) |
| `GET /customers/vehicles/{vin}/locationBasedChargingSettings` | Ladeeinstellungen |
| `GET /customers/vehicles/{vin}/smartMaintenanceTyreDiagnosis` | Reifendiagnose |
| `GET /customers/vehicles/{vin}/telematicData` | Telematikdaten (benötigt Container-ID) |

**Hinweise:**
- Base-URL: `https://api-cardata.bmwgroup.com`
- Header `x-version: v1` wird automatisch gesetzt
- API-Spezifikation: https://bmw-cardata.bmwgroup.com/customer/public/assets/swagger/swagger-customer-api-v1.json

---

## MQTT Streaming

Echtzeit-Fahrzeugdaten über MQTT-Stream.

**Technische Details:** MQTT 3.1.1, TLS/SSL, Topic: `{GCID}/{VIN}`

**Hinweis:** Streaming-Zugangsdaten (GCID, ID-Token) werden automatisch aus `device_token_response` verwendet. ID-Token läuft nach 1h ab und wird automatisch erneuert.

### Verwendung

```bash
# Unbegrenzt (Strg+C zum Beenden)
./bmw_cardata.sh --stream

# Bestimmte Dauer
./bmw_cardata.sh --stream --stream-duration 60

# Mit Ausgabedatei (JSON Lines)
./bmw_cardata.sh --stream --stream-output stream_data.jsonl

# Einmaliges Streaming (aggregiert, neueste Werte pro Key nach 60s)
./bmw_cardata.sh --stream --stream-once --stream-output status.json
```

### Streaming-Datenformat

JSON Lines Format (eine JSON-Zeile pro Nachricht):

```json
{
  "timestamp": "2025-11-16T10:54:56.723083",
  "topic": "41c477fd-5714-4a2c-af0b-d1d7fbf0addf/WBY71HH090CW09664",
  "payload": {
    "vin": "WBY71HH090CW09664",
    "entityId": "41c477fd-5714-4a2c-af0b-d1d7fbf0addf",
    "timestamp": "2025-11-16T09:54:56.276Z",
    "data": {
      "vehicle.cabin.infotainment.navigation.currentLocation.altitude": {
        "timestamp": "2025-11-16T09:54:55Z",
        "value": 319,
        "unit": "m"
      }
    }
  }
}
```

### Streaming-Datenfelder

Vollständiger Katalog der verfügbaren Telematik-Felder:

**https://www.bmw.de/de-de/mybmw/public/cardata-telematic-catalogue/**

**Hinweis:** Die gewünschten Felder müssen im My BMW unter "CarData Streaming" gemapped werden.

**Beispiel-Felder:**
- GPS: `vehicle.cabin.infotainment.navigation.currentLocation.*` (latitude, longitude, altitude)
- Batterie: `vehicle.drivetrain.batteryManagement.header` (SOC in %)
- Reichweite: `vehicle.drivetrain.electricEngine.kombiRemainingElectricRange`
- Türen: `vehicle.cabin.door.*` / `vehicle.body.trunk.door.isOpen`
- Fenster: `vehicle.cabin.window.*`

### Systemd-Service

Dauerhaftes Streaming beim Systemstart:

**Service-Datei:** `/etc/systemd/system/bmw-streaming.service`

```ini
[Unit]
Description=BMW CarData Streaming
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/bmwcardata
ExecStart=/home/pi/bmwcardata/bmw_cardata.sh --stream --stream-output /home/pi/stream_data.jsonl
Restart=always

[Install]
WantedBy=multi-user.target
```

**Aktivieren:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bmw-streaming.service
```