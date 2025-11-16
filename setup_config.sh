#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"

echo "=== BMW Cardata Konfiguration einrichten (Device Code Flow) ==="
echo ""

EXISTING_CLIENT_ID=""
if [ -f "$CONFIG_FILE" ]; then
    echo "Warnung: Konfigurationsdatei existiert bereits: $CONFIG_FILE"
    
    if command -v python3 >/dev/null 2>&1; then
        EXISTING_CLIENT_ID=$(python3 -c "import json; f=open('$CONFIG_FILE'); c=json.load(f); f.close(); print(c.get('client_id', ''))" 2>/dev/null)
    fi
    
    read -p "Überschreiben? (j/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[JjYy]$ ]]; then
        echo "Abgebrochen."
        exit 0
    fi
fi

if [ -n "$EXISTING_CLIENT_ID" ]; then
    read -p "Client-ID [$EXISTING_CLIENT_ID]: " CLIENT_ID
    if [ -z "$CLIENT_ID" ]; then
        CLIENT_ID="$EXISTING_CLIENT_ID"
    fi
else
    read -p "Client-ID: " CLIENT_ID
fi

if [ -z "$CLIENT_ID" ]; then
    echo "Fehler: Client-ID ist erforderlich."
    exit 1
fi

cat > "$CONFIG_FILE" << EOF
{
  "client_id": "$CLIENT_ID",
  "region": "EU",
  "vin": "",
  "streaming_host": "customer.streaming-cardata.bmwgroup.com",
  "streaming_port": 9000
}
EOF

if [ ! -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    echo "Fehler: Virtuelles Environment nicht gefunden. Bitte zuerst: python3 -m venv venv"
    exit 1
fi

PYTHON_SCRIPT=$(cat << PYTHON_EOF
import sys
import json
import time
import os
from pathlib import Path

script_dir = Path("$SCRIPT_DIR")
sys.path.insert(0, str(script_dir))

import importlib.util
spec = importlib.util.spec_from_file_location("bmw_cardata", script_dir / "bmw_cardata.py")
bmw_cardata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bmw_cardata)

config_file = sys.argv[1]
client_id = sys.argv[2]

client = bmw_cardata.BMWCardataClient(client_id=client_id, config_file=config_file)

print("Starte Device Code Flow...")
device_code_data = client.start_device_code_flow()

if not device_code_data:
    print("Fehler: Device Code Flow konnte nicht gestartet werden.")
    sys.exit(1)

user_code = device_code_data.get('user_code')
verification_uri_complete = device_code_data.get('verification_uri_complete', '')
device_code = device_code_data.get('device_code')
interval = device_code_data.get('interval', 5)
expires_in = device_code_data.get('expires_in', 600)

print("")
print("=" * 60)
print("Bitte authentifiziere das Gerät in deinem BMW Konto:")
print("=" * 60)
print(f"User Code: {user_code}")
print(f"Verification URI aufrufen: {verification_uri_complete}")
print("=" * 60)
print("")
print("Warte auf Authentifizierung...")

start_time = time.time()
timeout = expires_in

while time.time() - start_time < timeout:
    time.sleep(interval)
    token_data = client.request_device_token(device_code)
    
    if token_data:
        print("")
        print("✓ Authentifizierung erfolgreich!")
        print(f"✓ Access Token erhalten")
        if 'refresh_token' in token_data:
            print(f"✓ Refresh Token erhalten")
        
        print("")
        print("Rufe Fahrzeuge ab...")
        vehicles_data = client.get_vehicles()
        
        if vehicles_data and 'mappings' in vehicles_data:
            vins = [v.get('vin') for v in vehicles_data['mappings'] if v.get('vin')]
            
            if len(vins) == 0:
                print("⚠ Keine Fahrzeuge gefunden")
            elif len(vins) == 1:
                print(f"✓ Fahrzeug gefunden: {vins[0]}")
                selected_vin = vins[0]
                
                with open(config_file, 'r') as f:
                    config = json.load(f)
                config['vin'] = selected_vin
                config['vins'] = vins
                with open(config_file, 'w') as f:
                    json.dump(config, f, indent=2)
                print(f"✓ VIN in {config_file} gespeichert")
            else:
                print(f"✓ {len(vins)} Fahrzeuge gefunden:")
                for i, vin in enumerate(vins, 1):
                    print(f"  {i}. {vin}")
                print("")
                
                while True:
                    try:
                        choice = input(f"Welches Fahrzeug als Standard verwenden? (1-{len(vins)}): ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(vins):
                            selected_vin = vins[idx]
                            break
                        else:
                            print(f"Bitte eine Zahl zwischen 1 und {len(vins)} eingeben.")
                    except (ValueError, KeyboardInterrupt):
                        print("")
                        print("Abgebrochen.")
                        sys.exit(1)
                
                with open(config_file, 'r') as f:
                    config = json.load(f)
                config['vin'] = selected_vin
                config['vins'] = vins
                with open(config_file, 'w') as f:
                    json.dump(config, f, indent=2)
                print(f"✓ Standard-VIN: {selected_vin}")
                print(f"✓ Alle VINs in {config_file} gespeichert")
        else:
            print("⚠ Fahrzeuge konnten nicht abgerufen werden")
        
        sys.exit(0)
    
    elapsed = int(time.time() - start_time)
    remaining = timeout - elapsed
    print(f"Warte... ({elapsed}s / {timeout}s)")

print("")
print("✗ Timeout: Authentifizierung nicht abgeschlossen")
sys.exit(1)
PYTHON_EOF
)

"$SCRIPT_DIR/venv/bin/python3" -c "$PYTHON_SCRIPT" "$CONFIG_FILE" "$CLIENT_ID"

if [ $? -eq 0 ]; then
    chmod 600 "$CONFIG_FILE"
    echo ""
    echo "Konfiguration mit Device Code Flow abgeschlossen: $CONFIG_FILE"
    echo ""
    echo "Nächste Schritte:"
    echo "1. Teste die Verbindung: ./bmw_cardata.sh --vehicles"
else
    echo ""
    echo "Fehler beim Device Code Flow. Konfiguration teilweise gespeichert: $CONFIG_FILE"
    exit 1
fi
