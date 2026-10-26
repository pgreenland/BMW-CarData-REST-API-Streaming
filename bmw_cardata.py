#!/usr/bin/env python3
"""
Einfaches Script zum Abrufen von Fahrzeugdaten über BMW CarData API
"""

import os
import sys
import json
import requests
import time
import ssl
import paho.mqtt.client as mqtt
import secrets
import base64
import hashlib
from typing import Optional, Dict, Any, Callable
from pathlib import Path
from datetime import datetime


class BMWCardataClient:
    """Client für BMW Cardata API"""
    
    BASE_URL_EU = "https://api-cardata.bmwgroup.com"
    BASE_URL_NA = "https://api-cardata.bmwgroup.com"
    OAUTH_BASE_URL = "https://customer.bmwgroup.com"
    
    def __init__(self, api_key: Optional[str] = None, client_id: Optional[str] = None, 
                 client_secret: Optional[str] = None, access_token: Optional[str] = None,
                 refresh_token: Optional[str] = None, config_file: Optional[str] = None,
                 region: str = "EU"):
        """
        Initialisiert den BMW Cardata Client
        
        Args:
            api_key: API-Schlüssel für Authentifizierung
            client_id: Client-ID für OAuth
            client_secret: Client-Secret für OAuth
            access_token: OAuth Access Token (kann direkt verwendet werden)
            refresh_token: OAuth Refresh Token (zum Erneuern des Access Tokens)
            config_file: Pfad zur Konfigurationsdatei (JSON)
            region: Region (EU, NA) - Standard: EU für Deutschland/Bayern
        """
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.region = region.upper()
        self.vin = None
        self.config_file = config_file
        self._base_url_override = None
        
        if config_file:
            self.load_config(config_file)
        
        if not self.api_key and not self.client_id and not self.access_token:
            raise ValueError("API-Key, Client-ID/Secret oder Access Token müssen angegeben werden")
    
    @property
    def base_url(self) -> str:
        """Gibt die Base-URL für die gewählte Region zurück"""
        if self._base_url_override:
            return self._base_url_override
        if self.region == "NA":
            return self.BASE_URL_NA
        return self.BASE_URL_EU
    
    def load_config(self, config_file: str):
        """Lädt Konfiguration aus JSON-Datei"""
        config_path = Path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {config_file}")
        
        self.config_file = config_file
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        if 'api_key' in config:
            self.api_key = config['api_key']
        if 'client_id' in config:
            self.client_id = config['client_id']
        if 'client_secret' in config:
            self.client_secret = config['client_secret']
        
        if 'device_token_response' in config:
            device_token = config['device_token_response']
            if isinstance(device_token, dict):
                if 'access_token' in device_token:
                    self.access_token = device_token['access_token']
                if 'refresh_token' in device_token:
                    self.refresh_token = device_token['refresh_token']
        
        if 'vin' in config:
            self.vin = config['vin']
        if 'region' in config:
            self.region = config['region'].upper()
        if 'base_url' in config:
            self._base_url_override = config['base_url']
    
    def save_config(self, additional_data: Optional[Dict[str, Any]] = None):
        """Speichert die aktuellen Token-Werte in die Config-Datei"""
        if not self.config_file:
            return
        
        config_path = Path(self.config_file)
        if not config_path.exists():
            return
        
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError):
            config = {}
        
        if self.client_id:
            config['client_id'] = self.client_id
        if self.vin:
            config['vin'] = self.vin
        if self.region:
            config['region'] = self.region
        
        if 'device_token_response' in config:
            if self.access_token:
                config['device_token_response']['access_token'] = self.access_token
            if self.refresh_token:
                config['device_token_response']['refresh_token'] = self.refresh_token
        elif self.access_token or self.refresh_token:
            if 'device_token_response' not in config:
                config['device_token_response'] = {}
            if self.access_token:
                config['device_token_response']['access_token'] = self.access_token
            if self.refresh_token:
                config['device_token_response']['refresh_token'] = self.refresh_token
        
        if additional_data:
            config.update(additional_data)
        
        try:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except IOError as e:
            print(f"Warnung: Konnte Config-Datei nicht speichern: {e}", file=sys.stderr)
    
    def refresh_access_token(self) -> bool:
        """
        Erneuert den Access Token mit dem Refresh Token
        
        Returns:
            True bei erfolgreicher Erneuerung
        """
        if not self.refresh_token:
            return False
        
        if not self.client_id:
            print("Fehler: client_id ist erforderlich für Token-Erneuerung", file=sys.stderr)
            return False
        
        token_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/token"
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            response = requests.post(token_url, data=data, headers=headers)
            response.raise_for_status()
            token_data = response.json()
            self.access_token = token_data.get('access_token')
            if 'refresh_token' in token_data:
                self.refresh_token = token_data.get('refresh_token')
            
            if self.access_token:
                self.save_config()
                return True
            return False
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Erneuern des Tokens: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
            return False
    
    def start_device_code_flow(self, client_id: Optional[str] = None, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Startet den Device Code Flow mit PKCE
        
        Args:
            client_id: Client-ID für OAuth (optional, verwendet self.client_id falls nicht angegeben)
            scope: OAuth Scopes (optional, Standard: "authenticate_user openid cardata:api:read cardata:streaming:read")
        
        Returns:
            Dictionary mit device_code, user_code, verification_uri, etc. oder None bei Fehler
        """
        client_id = client_id or self.client_id
        if not client_id:
            print("Fehler: client_id ist erforderlich für Device Code Flow", file=sys.stderr)
            return None
        
        # PKCE: Generiere code_verifier (43-128 Zeichen, URL-safe)
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
        
        # PKCE: Erstelle code_challenge (SHA256-Hash des code_verifier)
        code_challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        code_challenge = base64.urlsafe_b64encode(code_challenge_bytes).decode('utf-8').rstrip('=')
        
        device_code_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/device/code"
        
        # Scope: Standard ist vollständiger Scope für CarData API
        if scope is None:
            scope = "authenticate_user openid cardata:api:read cardata:streaming:read"
        
        data = {
            'client_id': client_id,
            'response_type': 'device_code',
            'scope': scope,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256'
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            response = requests.post(device_code_url, data=data, headers=headers)
            response.raise_for_status()
            device_code_data = response.json()
            
            # Speichere code_verifier für späteren Token-Request
            device_code_data['_code_verifier'] = code_verifier
            
            if self.config_file:
                self.save_config({'device_code_response': device_code_data})
            
            return device_code_data
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Starten des Device Code Flows: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
            return None
    
    def request_device_token(self, device_code: str, client_id: Optional[str] = None, code_verifier: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fordert Token für das Gerät an (nach Device Code Flow mit PKCE)
        
        Args:
            device_code: Device Code aus start_device_code_flow
            client_id: Client-ID für OAuth (optional, verwendet self.client_id falls nicht angegeben)
            code_verifier: PKCE code_verifier (optional, wird aus Config geladen falls nicht angegeben)
        
        Returns:
            Dictionary mit access_token, refresh_token, etc. oder None bei Fehler
        """
        client_id = client_id or self.client_id
        if not client_id:
            print("Fehler: client_id ist erforderlich für Token-Anfrage", file=sys.stderr)
            return None
        
        # Lade code_verifier aus Config falls nicht übergeben
        if not code_verifier and self.config_file:
            try:
                config_path = Path(self.config_file)
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                    if 'device_code_response' in config:
                        device_code_response = config['device_code_response']
                        if isinstance(device_code_response, dict) and '_code_verifier' in device_code_response:
                            code_verifier = device_code_response['_code_verifier']
            except (json.JSONDecodeError, IOError):
                pass
        
        if not code_verifier:
            print("Fehler: code_verifier ist erforderlich für PKCE Flow", file=sys.stderr)
            return None
        
        token_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/token"
        data = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': device_code,
            'client_id': client_id,
            'code_verifier': code_verifier
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            response = requests.post(token_url, data=data, headers=headers)
            
            if response.status_code == 200:
                token_data = response.json()
                
                self.access_token = token_data.get('access_token')
                if 'refresh_token' in token_data:
                    self.refresh_token = token_data.get('refresh_token')
                
                if self.config_file:
                    self.save_config({'device_token_response': token_data})
                
                return token_data if self.access_token else None
            elif response.status_code == 400:
                error_data = response.json()
                error = error_data.get('error', '')
                
                if error == 'authorization_pending':
                    return None
                elif error == 'slow_down':
                    return None
                else:
                    print(f"Fehler beim Anfordern des Device Tokens: {error}", file=sys.stderr)
                    return None
            else:
                response.raise_for_status()
                return None
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 400:
                    try:
                        error_data = e.response.json()
                        error = error_data.get('error', '')
                        if error in ['authorization_pending', 'slow_down']:
                            return None
                    except:
                        pass
            print(f"Fehler beim Anfordern des Device Tokens: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
            return None
    
    def authenticate(self) -> bool:
        """
        Authentifiziert sich bei der BMW Cardata API
        
        Returns:
            True bei erfolgreicher Authentifizierung
        """
        if not self.access_token and not self.refresh_token and self.config_file:
            config_path = Path(self.config_file)
            if config_path.exists():
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                    
                    if 'device_token_response' in config:
                        device_token = config['device_token_response']
                        if isinstance(device_token, dict):
                            if 'access_token' in device_token:
                                self.access_token = device_token['access_token']
                            if 'refresh_token' in device_token:
                                self.refresh_token = device_token['refresh_token']
                except (json.JSONDecodeError, IOError):
                    pass
        
        if self.refresh_token and not self.access_token:
            return self.refresh_access_token()
        
        if self.access_token:
            return True
        
        if self.api_key:
            self.access_token = self.api_key
            return True
        
        if self.client_id and self.client_secret:
            token_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/token"
            data = {
                'grant_type': 'client_credentials',
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
            
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            
            try:
                response = requests.post(token_url, data=data, headers=headers)
                response.raise_for_status()
                token_data = response.json()
                self.access_token = token_data.get('access_token')
                if 'refresh_token' in token_data:
                    self.refresh_token = token_data.get('refresh_token')
                
                if self.access_token:
                    self.save_config()
                    return True
                return False
            except requests.exceptions.RequestException as e:
                print(f"Authentifizierungsfehler: {e}", file=sys.stderr)
                if hasattr(e, 'response') and e.response is not None:
                    print(f"Antwort: {e.response.text}", file=sys.stderr)
                return False
        
        return False
    
    def get_headers(self) -> Dict[str, str]:
        """Gibt die HTTP-Header für API-Anfragen zurück"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-version': 'v1'
        }
        
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        
        return headers
    
    def ensure_valid_token(self) -> bool:
        """
        Stellt sicher, dass ein Token vorhanden ist.
        Erneuert den Token NICHT proaktiv - nur bei tatsächlichen Fehlern.
        
        Returns:
            True wenn ein Token vorhanden ist
        """
        if not self.access_token:
            return self.authenticate()
        return True
    
    def _make_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """
        Führt eine HTTP-Anfrage durch und behandelt Token-Erneuerung automatisch
        
        Args:
            method: HTTP-Methode (get, post, etc.)
            url: URL für die Anfrage
            **kwargs: Weitere Argumente für requests
        
        Returns:
            Response-Objekt oder None bei Fehler
        """
        if not self.ensure_valid_token():
            return None
        
        try:
            response = requests.request(method, url, headers=self.get_headers(), **kwargs)
            
            if response.status_code == 429:
                print("⚠ API Rate Limit erreicht!", file=sys.stderr)
                print("Das tägliche Anfragelimit der BMW API wurde überschritten.", file=sys.stderr)
                print("Bitte warte bis morgen und versuche es erneut.", file=sys.stderr)
                if response.text:
                    try:
                        error_data = response.json()
                        if 'exveErrorMsg' in error_data:
                            print(f"Details: {error_data['exveErrorMsg']}", file=sys.stderr)
                    except:
                        pass
                return None
            
            if response.status_code == 401 or response.status_code == 403:
                if self.refresh_token:
                    print("Authentifizierungsfehler (401/403). Versuche Token-Erneuerung...", file=sys.stderr)
                    if self.refresh_access_token():
                        response = requests.request(method, url, headers=self.get_headers(), **kwargs)
                        if response.status_code == 401 or response.status_code == 403:
                            response.raise_for_status()
                    else:
                        response.raise_for_status()
                else:
                    response.raise_for_status()
            
            return response
        except requests.exceptions.RequestException as e:
            print(f"Fehler bei API-Anfrage: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
                print(f"Status Code: {e.response.status_code}", file=sys.stderr)
            return None
    
    def get_vehicles(self) -> Optional[Dict[str, Any]]:
        """
        Ruft die Liste der Fahrzeuge ab
        
        Returns:
            Dictionary mit Fahrzeugdaten oder None bei Fehler
        """
        url = f"{self.base_url}/customers/vehicles/mappings"
        
        if hasattr(self, '_debug') and self._debug:
            print(f"Debug: Request URL: {url}", file=sys.stderr)
            print(f"Debug: Headers: {self.get_headers()}", file=sys.stderr)
        
        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Fahrzeuge: {e}", file=sys.stderr)
                return None
        return None
    
    def get_vehicle_data(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Ruft Daten für ein spezifisches Fahrzeug ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
        
        Returns:
            Dictionary mit Fahrzeugdaten oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        url = f"{self.base_url}/customers/vehicles/{vin}/basicData"
        
        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Fahrzeugdaten: {e}", file=sys.stderr)
                return None
        return None
    
    def get_charging_history(self, vin: Optional[str] = None, from_date: Optional[str] = None, 
                             to_date: Optional[str] = None, next_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Ruft Ladehistorie für ein Fahrzeug ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
            from_date: Start-Datum im ISO 8601 Format (z.B. "2025-01-01T00:00:00Z")
            to_date: End-Datum im ISO 8601 Format (z.B. "2025-12-31T23:59:59Z")
            next_token: Token für Pagination (optional)
        
        Returns:
            Dictionary mit Ladehistorie oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        if not from_date or not to_date:
            from datetime import datetime, timedelta
            to_date = datetime.utcnow().isoformat() + 'Z'
            from_date = (datetime.utcnow() - timedelta(days=30)).isoformat() + 'Z'
            print(f"Verwende Standard-Zeitraum: {from_date} bis {to_date}", file=sys.stderr)
        
        url = f"{self.base_url}/customers/vehicles/{vin}/chargingHistory"
        params = {
            'from': from_date,
            'to': to_date
        }
        if next_token:
            params['nextToken'] = next_token
        
        response = self._make_request('get', url, params=params)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Ladehistorie: {e}", file=sys.stderr)
                return None
        return None
    
    def get_vehicle_image(self, vin: Optional[str] = None) -> Optional[bytes]:
        """
        Ruft Fahrzeugbild ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
        
        Returns:
            Bilddaten (bytes) oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        url = f"{self.base_url}/customers/vehicles/{vin}/image"
        
        if not self.ensure_valid_token():
            return None
        
        headers = self.get_headers()
        headers['Accept'] = 'image/*'
        
        try:
            response = requests.get(url, headers=headers)
            
            if response.status_code == 401 or response.status_code == 403:
                if self.refresh_token:
                    print("Authentifizierungsfehler (401/403). Versuche Token-Erneuerung...", file=sys.stderr)
                    if self.refresh_access_token():
                        headers = self.get_headers()
                        headers['Accept'] = 'image/*'
                        response = requests.get(url, headers=headers)
                        if response.status_code == 401 or response.status_code == 403:
                            response.raise_for_status()
                    else:
                        response.raise_for_status()
                else:
                    response.raise_for_status()
            
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Abrufen des Fahrzeugbildes: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
                print(f"Status Code: {e.response.status_code}", file=sys.stderr)
            return None
    
    def get_location_based_charging_settings(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Ruft standortbasierte Ladeeinstellungen ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
        
        Returns:
            Dictionary mit Ladeeinstellungen oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        url = f"{self.base_url}/customers/vehicles/{vin}/locationBasedChargingSettings"
        
        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Ladeeinstellungen: {e}", file=sys.stderr)
                return None
        return None
    
    def get_smart_maintenance_tyre_diagnosis(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Ruft Smart Maintenance Reifendiagnose ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
        
        Returns:
            Dictionary mit Reifendiagnose oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        url = f"{self.base_url}/customers/vehicles/{vin}/smartMaintenanceTyreDiagnosis"
        
        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Reifendiagnose: {e}", file=sys.stderr)
                return None
        return None
    
    def get_telematic_data(self, vin: Optional[str] = None, container_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Ruft Telematikdaten ab
        
        Args:
            vin: Fahrzeugidentifikationsnummer (VIN), optional - wird aus Config verwendet falls nicht angegeben
            container_id: Container-ID (erforderlich für telematicData Endpunkt)
        
        Returns:
            Dictionary mit Telematikdaten oder None bei Fehler
        """
        vin = vin or self.vin
        if not vin:
            print("Fehler: VIN muss angegeben werden oder in der Config vorhanden sein", file=sys.stderr)
            return None
        
        if not container_id:
            print("Fehler: container_id ist für telematicData erforderlich", file=sys.stderr)
            print("Hinweis: Erstelle zuerst einen Container über POST /customers/containers", file=sys.stderr)
            return None
        
        url = f"{self.base_url}/customers/vehicles/{vin}/telematicData"
        params = {'containerId': container_id}
        
        response = self._make_request('get', url, params=params)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Fehler beim Abrufen der Telematikdaten: {e}", file=sys.stderr)
                return None
        return None


class BMWCardataStreamingClient:
    """Client für BMW Cardata Streaming via MQTT"""
    
    OAUTH_BASE_URL = "https://customer.bmwgroup.com"
    
    def __init__(self, host: str, port: int, username: str, password: str, 
                 topic: Optional[str] = None, vin: Optional[str] = None,
                 on_message_callback: Optional[Callable] = None,
                 output_file: Optional[str] = None,
                 refresh_token: Optional[str] = None,
                 client_id: Optional[str] = None,
                 config_file: Optional[str] = None,
                 stream_once: bool = False):
        """
        Initialisiert den BMW Cardata Streaming Client
        
        Args:
            host: MQTT-Broker-Host
            port: MQTT-Broker-Port
            username: MQTT-Username (GCID)
            password: MQTT-Passwort (ID-Token/JWT)
            topic: MQTT-Topic (Standard: username/VIN)
            vin: Fahrzeugidentifikationsnummer
            on_message_callback: Callback-Funktion für empfangene Nachrichten
            output_file: Optional: Pfad zur Ausgabedatei für Stream-Daten
            refresh_token: Refresh-Token für automatische ID-Token-Erneuerung
            client_id: Client-ID für Token-Erneuerung
            config_file: Pfad zur Config-Datei für Token-Speicherung
            stream_once: Wenn True, werden alle Nachrichten während der Stream-Dauer gesammelt und am Ende aggregiert (neueste Werte pro Key)
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.vin = vin
        self.on_message_callback = on_message_callback
        self.output_file = output_file
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.config_file = config_file
        self.stream_once = stream_once
        self.client = None
        self.connected = False
        self.message_count = 0
        self.collected_messages = []
        
        if topic:
            self.topic = topic
        elif vin:
            self.topic = f"{username}/{vin}"
        else:
            raise ValueError("Topic oder VIN muss angegeben werden")
        
        if not self.topic:
            raise ValueError("Topic oder VIN muss angegeben werden")
    
    def _is_token_expired(self) -> bool:
        """Prüft, ob das ID-Token abgelaufen ist"""
        if not self.password:
            return True
        try:
            import base64
            parts = self.password.split('.')
            if len(parts) < 2:
                return True
            payload = parts[1]
            payload += '=' * (4 - len(payload) % 4)
            data = json.loads(base64.b64decode(payload))
            exp = data.get('exp', 0)
            if exp == 0:
                return True
            now = time.time()
            # 5 Sekunden Puffer, um Race Conditions zu vermeiden
            return now >= (exp - 5)
        except Exception as e:
            print(f"⚠ Fehler beim Prüfen des Token-Ablaufs: {e}", file=sys.stderr)
            return True
    
    def refresh_id_token(self) -> bool:
        """
        Erneuert das ID-Token mit dem Refresh-Token
        
        Returns:
            True bei erfolgreicher Erneuerung
        """
        if not self.refresh_token or not self.client_id:
            print("✗ Refresh-Token oder Client-ID fehlt für Token-Erneuerung", file=sys.stderr)
            return False
        
        token_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/token"
        
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            print("Erneuere ID-Token...", file=sys.stderr)
            response = requests.post(token_url, data=data, headers=headers)
            response.raise_for_status()
            token_data = response.json()
            
            new_id_token = token_data.get('id_token')
            if not new_id_token:
                print("✗ Kein ID-Token in der Antwort", file=sys.stderr)
                return False
            
            self.password = new_id_token
            
            if 'refresh_token' in token_data:
                self.refresh_token = token_data.get('refresh_token')
            
            if self.config_file:
                self._save_token_to_config()
            
            # Prüfe, ob das neue Token wirklich gültig ist
            if self._is_token_expired():
                print("⚠ Warnung: Neues Token scheint bereits abgelaufen zu sein", file=sys.stderr)
            else:
                print("✓ ID-Token erfolgreich erneuert", file=sys.stderr)
            return True
        
        except requests.exceptions.RequestException as e:
            print(f"✗ Fehler beim Erneuern des ID-Tokens: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"Antwort: {e.response.text}", file=sys.stderr)
            return False
    
    def _save_token_to_config(self):
        """Speichert das erneuerte ID-Token in die Config-Datei"""
        if not self.config_file:
            return
        
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            # Aktualisiere id_token in device_token_response (nicht als separates streaming_password)
            if 'device_token_response' not in config:
                config['device_token_response'] = {}
            config['device_token_response']['id_token'] = self.password
            
            if self.refresh_token:
                config['device_token_response']['refresh_token'] = self.refresh_token
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            
            print(f"✓ Token in {self.config_file} gespeichert", file=sys.stderr)
        except Exception as e:
            print(f"Warnung: Konnte Token nicht in Config speichern: {e}", file=sys.stderr)
    
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Callback wenn Verbindung hergestellt wurde"""
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc == 0:
            print(f"✓ Verbunden mit MQTT-Broker {self.host}:{self.port}", file=sys.stderr)
            self.connected = True
            print(f"✓ Abonniere Topic: {self.topic}", file=sys.stderr)
            client.subscribe(self.topic)
        else:
            error_messages = {
                1: "Incorrect protocol version",
                2: "Invalid client identifier",
                3: "Server unavailable",
                4: "Bad username or password",
                5: "Not authorized"
            }
            error_msg = error_messages.get(rc, f"Unknown error code {rc}")
            print(f"✗ Verbindung fehlgeschlagen: {error_msg} (Code {rc})", file=sys.stderr)
            self.connected = False
    
    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Callback wenn Verbindung getrennt wurde"""
        self.connected = False
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc != 0:
            print(f"✗ Unerwartete Trennung. Code: {rc}", file=sys.stderr)
    
    def _on_message(self, client, userdata, msg):
        """Callback wenn Nachricht empfangen wurde"""
        self.message_count += 1
        timestamp = datetime.now().isoformat()
        
        try:
            payload = msg.payload.decode('utf-8')
            data = {
                'timestamp': timestamp,
                'topic': msg.topic,
                'payload': json.loads(payload) if payload.startswith('{') else payload
            }
            
            print(f"\n[{timestamp}] Nachricht #{self.message_count} empfangen:", file=sys.stderr)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            
            if self.stream_once:
                self.collected_messages.append(data)
            else:
                if self.output_file:
                    output_path = Path(self.output_file)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.output_file, 'a') as f:
                        f.write(json.dumps(data, ensure_ascii=False) + '\n')
            
            if self.on_message_callback:
                self.on_message_callback(data)
        
        except Exception as e:
            print(f"✗ Fehler beim Verarbeiten der Nachricht: {e}", file=sys.stderr)
    
    def _aggregate_latest_data(self, messages: list) -> dict:
        """
        Aggregiert alle Nachrichten und gibt für jeden Key den neuesten Wert zurück
        
        Args:
            messages: Liste von Nachrichten
            
        Returns:
            Dictionary mit aggregierten Daten (neueste Werte pro Key)
        """
        if not messages:
            return {}
        
        aggregated_data = {}
        latest_timestamp = None
        latest_payload = {}
        all_keys_seen = set()
        
        for idx, msg in enumerate(messages):
            payload = msg.get('payload', {})
            if not isinstance(payload, dict):
                print(f"Debug: Nachricht #{idx+1} übersprungen (payload kein dict)", file=sys.stderr)
                continue
            
            data = payload.get('data', {})
            if not isinstance(data, dict):
                print(f"Debug: Nachricht #{idx+1} übersprungen (data kein dict)", file=sys.stderr)
                continue
            
            msg_keys = list(data.keys())
            print(f"Debug: Nachricht #{idx+1} enthält {len(msg_keys)} Keys: {sorted(msg_keys)}", file=sys.stderr)
            
            msg_timestamp = payload.get('timestamp')
            if msg_timestamp and (not latest_timestamp or msg_timestamp > latest_timestamp):
                latest_timestamp = msg_timestamp
                latest_payload = payload
            
            for key, value in data.items():
                if not isinstance(value, dict):
                    print(f"Debug: Key {key} übersprungen (kein dict): {type(value)}", file=sys.stderr)
                    continue
                
                all_keys_seen.add(key)
                key_timestamp = value.get('timestamp', '')
                if key not in aggregated_data:
                    aggregated_data[key] = value
                    print(f"Debug: Key {key} hinzugefügt (timestamp: {key_timestamp})", file=sys.stderr)
                else:
                    existing_timestamp = aggregated_data[key].get('timestamp', '')
                    if key_timestamp > existing_timestamp:
                        aggregated_data[key] = value
                        print(f"Debug: Key {key} aktualisiert (alt: {existing_timestamp}, neu: {key_timestamp})", file=sys.stderr)
                    else:
                        print(f"Debug: Key {key} behalten (alt: {existing_timestamp}, neu: {key_timestamp})", file=sys.stderr)
        
        print(f"Debug: Gesamt {len(messages)} Nachrichten verarbeitet", file=sys.stderr)
        print(f"Debug: Gefundene Keys: {sorted(all_keys_seen)}", file=sys.stderr)
        print(f"Debug: Aggregierte Keys ({len(aggregated_data)}): {sorted(aggregated_data.keys())}", file=sys.stderr)
        
        if 'vehicle.cabin.infotainment.navigation.currentLocation.altitude' in all_keys_seen and 'vehicle.cabin.infotainment.navigation.currentLocation.altitude' not in aggregated_data:
            print(f"⚠ Warnung: altitude wurde gefunden, aber nicht aggregiert!", file=sys.stderr)
        
        result = {
            'timestamp': datetime.now().isoformat(),
            'topic': messages[0].get('topic', ''),
            'payload': {
                'vin': latest_payload.get('vin', ''),
                'entityId': latest_payload.get('entityId', ''),
                'topic': latest_payload.get('topic', ''),
                'timestamp': latest_timestamp or datetime.now().isoformat(),
                'data': aggregated_data
            }
        }
        
        return result
    
    def _has_valid_data(self, file_path: str) -> bool:
        """
        Prüft, ob eine Datei gültige Daten enthält (keine Fehlermeldung)
        
        Args:
            file_path: Pfad zur JSON-Datei
            
        Returns:
            True wenn die Datei existiert und gültige Daten enthält
        """
        if not Path(file_path).exists():
            return False
        
        try:
            with open(file_path, 'r') as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
                if not lines:
                    return False
                data = json.loads(lines[-1])
                if isinstance(data, dict) and 'error' in data:
                    return False
                return True
        except (json.JSONDecodeError, IOError):
            return False
    
    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties):
        """Callback wenn Topic abonniert wurde"""
        print(f"✓ Topic erfolgreich abonniert (Reason Codes: {reason_code_list})", file=sys.stderr)
    
    def connect(self) -> bool:
        """
        Stellt Verbindung zum MQTT-Broker her
        
        Returns:
            True bei erfolgreicher Verbindung
        """
        if self._is_token_expired():
            print("⚠ ID-Token ist abgelaufen", file=sys.stderr)
            if not self.refresh_id_token():
                print("✗ Token-Erneuerung fehlgeschlagen", file=sys.stderr)
                return False
        
        try:
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                protocol=mqtt.MQTTv311
            )
            
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.on_subscribe = self._on_subscribe
            
            print(f"Verbinde mit {self.host}:{self.port} (MQTT 3.1.1)...", file=sys.stderr)
            print(f"Topic: {self.topic}", file=sys.stderr)
            print(f"Username (GCID): {self.username}", file=sys.stderr)
            print(f"Password (ID-Token): {self.password[:50]}...", file=sys.stderr)
            
            self.client.username_pw_set(self.username, self.password)
            
            print(f"Konfiguriere TLS/SSL...", file=sys.stderr)
            self.client.tls_set(
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS
            )
            
            print(f"Verbinde zum Broker...", file=sys.stderr)
            self.client.connect(self.host, self.port, keepalive=30)
            
            return True
        
        except Exception as e:
            print(f"✗ Fehler beim Verbinden: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return False
    
    def start(self, duration: Optional[int] = None):
        """
        Startet den Stream
        
        Args:
            duration: Optional: Dauer in Sekunden (None = unbegrenzt)
        """
        if not self.client:
            if not self.connect():
                return
        
        print(f"\nStarte Streaming{f' für {duration} Sekunden' if duration else ' (Strg+C zum Beenden)'}...", file=sys.stderr)
        
        if self.output_file:
            if self.stream_once:
                print(f"Aggregierte Daten werden in {self.output_file} gespeichert (neueste Werte pro Key)", file=sys.stderr)
            else:
                print(f"Ausgabe wird in {self.output_file} gespeichert", file=sys.stderr)
        
        try:
            if duration:
                self.client.loop_start()
                time.sleep(duration)
                self.client.loop_stop()
            else:
                self.client.loop_forever()
        
        except KeyboardInterrupt:
            print(f"\n\n✓ Stream gestoppt. {self.message_count} Nachrichten empfangen.", file=sys.stderr)
        
        finally:
            if self.stream_once and self.output_file:
                if self.message_count > 0:
                    aggregated = self._aggregate_latest_data(self.collected_messages)
                    output_path = Path(self.output_file)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.output_file, 'a') as f:
                        f.write(json.dumps(aggregated, ensure_ascii=False) + '\n')
                    print(f"✓ {self.message_count} Nachrichten empfangen. Aggregierte Daten gespeichert in {self.output_file}", file=sys.stderr)
                elif self.message_count == 0:
                    if not self._has_valid_data(self.output_file):
                        error_data = {
                            'timestamp': datetime.now().isoformat(),
                            'error': 'Keine Nachricht empfangen',
                            'message': 'Das Fahrzeug sendet möglicherweise keine Daten (Standby-Modus) oder ist nicht aktiv.'
                        }
                        output_path = Path(self.output_file)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.output_file, 'a') as f:
                            f.write(json.dumps(error_data, ensure_ascii=False) + '\n')
                        print(f"⚠ Keine Nachricht empfangen. Fehlerstatus gespeichert in {self.output_file}", file=sys.stderr)
                    else:
                        print(f"✓ Keine neue Nachricht empfangen. Bestehende Datei {self.output_file} bleibt unverändert.", file=sys.stderr)
            self.disconnect()
    
    def disconnect(self):
        """Trennt Verbindung zum MQTT-Broker"""
        if self.client and self.connected:
            self.client.disconnect()
            self.connected = False
            print("✓ Verbindung getrennt", file=sys.stderr)


def main():
    """Hauptfunktion"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Ruft Fahrzeugdaten über BMW Cardata API ab'
    )
    parser.add_argument(
        '--config',
        type=str,
        default=None,
        help='Pfad zur Konfigurationsdatei (Standard: config.json im Script-Verzeichnis)'
    )
    parser.add_argument(
        '--api-key',
        type=str,
        help='API-Schlüssel'
    )
    parser.add_argument(
        '--client-id',
        type=str,
        help='Client-ID für OAuth'
    )
    parser.add_argument(
        '--client-secret',
        type=str,
        help='Client-Secret für OAuth'
    )
    parser.add_argument(
        '--access-token',
        type=str,
        help='OAuth Access Token (kann direkt verwendet werden)'
    )
    parser.add_argument(
        '--refresh-token',
        type=str,
        help='OAuth Refresh Token (zum Erneuern des Access Tokens)'
    )
    parser.add_argument(
        '--region',
        type=str,
        default='EU',
        choices=['EU', 'NA'],
        help='Region (EU für Deutschland/Bayern, NA für Nordamerika) - Standard: EU'
    )
    parser.add_argument(
        '--vin',
        type=str,
        help='Fahrzeugidentifikationsnummer (VIN)'
    )
    parser.add_argument(
        '--vehicles',
        action='store_true',
        help='Liste aller Fahrzeuge abrufen'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Status eines Fahrzeugs abrufen (benötigt --vin)'
    )
    parser.add_argument(
        '--charging-history',
        action='store_true',
        help='Ladehistorie abrufen (benötigt --vin)'
    )
    parser.add_argument(
        '--from-date',
        type=str,
        help='Start-Datum für Ladehistorie (ISO 8601, z.B. 2025-01-01T00:00:00Z)'
    )
    parser.add_argument(
        '--to-date',
        type=str,
        help='End-Datum für Ladehistorie (ISO 8601, z.B. 2025-12-31T23:59:59Z)'
    )
    parser.add_argument(
        '--image',
        action='store_true',
        help='Fahrzeugbild abrufen (benötigt --vin)'
    )
    parser.add_argument(
        '--image-output',
        type=str,
        help='Ausgabedatei für Fahrzeugbild'
    )
    parser.add_argument(
        '--charging-settings',
        action='store_true',
        help='Standortbasierte Ladeeinstellungen abrufen (benötigt --vin)'
    )
    parser.add_argument(
        '--tyre-diagnosis',
        action='store_true',
        help='Smart Maintenance Reifendiagnose abrufen (benötigt --vin)'
    )
    parser.add_argument(
        '--telematic-data',
        action='store_true',
        help='Telematikdaten abrufen (benötigt --vin und --container-id)'
    )
    parser.add_argument(
        '--container-id',
        type=str,
        help='Container-ID für Telematikdaten'
    )
    parser.add_argument(
        '--base-url',
        type=str,
        help='Base-URL der API (überschreibt Standard-URL)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Debug-Informationen anzeigen'
    )
    parser.add_argument(
        '--stream',
        action='store_true',
        help='Streaming-Modus starten (MQTT)'
    )
    parser.add_argument(
        '--stream-duration',
        type=int,
        help='Streaming-Dauer in Sekunden (Standard: unbegrenzt)'
    )
    parser.add_argument(
        '--stream-output',
        type=str,
        help='Ausgabedatei für Stream-Daten'
    )
    parser.add_argument(
        '--streaming-host',
        type=str,
        help='MQTT-Broker-Host (überschreibt Config)'
    )
    parser.add_argument(
        '--streaming-port',
        type=int,
        help='MQTT-Broker-Port (überschreibt Config)'
    )
    parser.add_argument(
        '--streaming-username',
        type=str,
        help='MQTT-Username (überschreibt Config)'
    )
    parser.add_argument(
        '--streaming-password',
        type=str,
        help='MQTT-Passwort (überschreibt Config)'
    )
    parser.add_argument(
        '--stream-once',
        action='store_true',
        help='Sammelt alle Nachrichten während der Stream-Dauer und speichert aggregierte Daten (neueste Werte pro Key) als JSON'
    )
    
    args = parser.parse_args()
    
    try:
        if args.config:
            config_file = args.config if os.path.exists(args.config) else None
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            local_config = os.path.join(script_dir, 'config.json')
            
            if os.path.exists(local_config):
                config_file = local_config
            else:
                config_file = None
        client = BMWCardataClient(
            api_key=args.api_key,
            client_id=args.client_id,
            client_secret=args.client_secret,
            access_token=args.access_token,
            refresh_token=args.refresh_token,
            config_file=config_file,
            region=args.region
        )
        
        if args.base_url:
            client._base_url_override = args.base_url
        
        if args.debug:
            client._debug = True
            print(f"Debug: Verwende Base-URL: {client.base_url}", file=sys.stderr)
            print(f"Debug: Region: {client.region}", file=sys.stderr)
            if client.access_token:
                print(f"Debug: Access Token vorhanden: {client.access_token[:20]}...", file=sys.stderr)
        
        if args.vehicles:
            print("Rufe Fahrzeugliste ab...", file=sys.stderr)
            vehicles = client.get_vehicles()
            if vehicles:
                print(json.dumps(vehicles, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.status:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --status oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Status für VIN {vin} ab...", file=sys.stderr)
            status = client.get_vehicle_data(vin)
            if status:
                print(json.dumps(status, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.charging_history:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --charging-history oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Ladehistorie für VIN {vin} ab...", file=sys.stderr)
            history = client.get_charging_history(vin, from_date=args.from_date, to_date=args.to_date)
            if history:
                print(json.dumps(history, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.image:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --image oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Fahrzeugbild für VIN {vin} ab...", file=sys.stderr)
            image_data = client.get_vehicle_image(vin)
            if image_data:
                if args.image_output:
                    with open(args.image_output, 'wb') as f:
                        f.write(image_data)
                    print(f"✓ Bild gespeichert in {args.image_output}", file=sys.stderr)
                else:
                    print(f"✓ Bild empfangen ({len(image_data)} bytes). Verwende --image-output zum Speichern.", file=sys.stderr)
            else:
                sys.exit(1)
        
        elif args.charging_settings:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --charging-settings oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Ladeeinstellungen für VIN {vin} ab...", file=sys.stderr)
            settings = client.get_location_based_charging_settings(vin)
            if settings:
                print(json.dumps(settings, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.tyre_diagnosis:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --tyre-diagnosis oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Reifendiagnose für VIN {vin} ab...", file=sys.stderr)
            diagnosis = client.get_smart_maintenance_tyre_diagnosis(vin)
            if diagnosis:
                print(json.dumps(diagnosis, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.telematic_data:
            vin = args.vin or client.vin
            if not vin:
                print("Fehler: --vin ist erforderlich für --telematic-data oder VIN muss in Config vorhanden sein", file=sys.stderr)
                sys.exit(1)
            if not args.container_id:
                print("Fehler: --container-id ist erforderlich für --telematic-data", file=sys.stderr)
                sys.exit(1)
            print(f"Rufe Telematikdaten für VIN {vin} ab...", file=sys.stderr)
            telematic = client.get_telematic_data(vin, container_id=args.container_id)
            if telematic:
                print(json.dumps(telematic, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        elif args.stream:
            streaming_host = args.streaming_host or getattr(client, '_streaming_host', None)
            streaming_port = args.streaming_port or getattr(client, '_streaming_port', None)
            streaming_username = args.streaming_username or getattr(client, '_streaming_username', None)
            streaming_password = args.streaming_password or getattr(client, '_streaming_password', None)
            vin = args.vin or client.vin
            
            if config_file:
                with open(config_file, 'r') as f:
                    config = json.load(f)
                    if not streaming_host:
                        streaming_host = config.get('streaming_host')
                    if not streaming_port:
                        streaming_port = config.get('streaming_port')
                    if not streaming_username and 'device_token_response' in config:
                        device_token = config['device_token_response']
                        if isinstance(device_token, dict) and 'gcid' in device_token:
                            streaming_username = device_token['gcid']
                    if not streaming_password and 'device_token_response' in config:
                        device_token = config['device_token_response']
                        if isinstance(device_token, dict) and 'id_token' in device_token:
                            streaming_password = device_token['id_token']
            
            if not all([streaming_host, streaming_port, streaming_username, streaming_password]):
                print("Fehler: Streaming benötigt Host, Port, Username und Passwort", file=sys.stderr)
                print("Konfiguriere diese Parameter in config.json oder übergebe sie als Argumente", file=sys.stderr)
                sys.exit(1)
            
            if not vin:
                print("Fehler: VIN wird für Streaming benötigt", file=sys.stderr)
                sys.exit(1)
            
            if args.stream_once:
                print(f"Starte einmaliges Streaming für VIN {vin} ...", file=sys.stderr)
                if not args.stream_output:
                    print("⚠ Warnung: --stream-once ohne --stream-output. Daten werden nur ausgegeben, nicht gespeichert.", file=sys.stderr)
            else:
                print(f"Starte Streaming für VIN {vin}...", file=sys.stderr)
            stream_client = BMWCardataStreamingClient(
                host=streaming_host,
                port=streaming_port,
                username=streaming_username,
                password=streaming_password,
                vin=vin,
                output_file=args.stream_output,
                refresh_token=client.refresh_token,
                client_id=client.client_id,
                config_file=config_file,
                stream_once=args.stream_once
            )
            if args.stream_once:
                stream_client.start(duration=60)
            else:
                stream_client.start(duration=args.stream_duration)
        
        elif args.vin or client.vin:
            vin = args.vin or client.vin
            print(f"Rufe Daten für VIN {vin} ab...", file=sys.stderr)
            data = client.get_vehicle_data(vin)
            if data:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)
        
        else:
            parser.print_help()
            sys.exit(1)
    
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

