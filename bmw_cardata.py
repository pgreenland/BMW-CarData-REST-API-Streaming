#!/usr/bin/env python3
"""
Simple script for retrieving vehicle data via the BMW CarData API
"""

import os
import sys
import logging
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

logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bmw_cardata")


class BMWCardataClient:
    """Client for the BMW Cardata API"""

    BASE_URL_EU = "https://api-cardata.bmwgroup.com"
    BASE_URL_NA = "https://api-cardata.bmwgroup.com"
    OAUTH_BASE_URL = "https://customer.bmwgroup.com"

    def __init__(self, api_key: Optional[str] = None, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None, access_token: Optional[str] = None,
                 refresh_token: Optional[str] = None, config_file: Optional[str] = None,
                 region: str = "EU"):
        """
        Initializes the BMW Cardata client

        Args:
            api_key: API key for authentication
            client_id: Client ID for OAuth
            client_secret: Client secret for OAuth
            access_token: OAuth access token (can be used directly)
            refresh_token: OAuth refresh token (for renewing the access token)
            config_file: Path to the configuration file (JSON)
            region: Region (EU, NA) - default: EU
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
            raise ValueError("API key, Client ID/Secret, or Access Token must be provided")

    @property
    def base_url(self) -> str:
        """Returns the base URL for the selected region"""
        if self._base_url_override:
            return self._base_url_override
        if self.region == "NA":
            return self.BASE_URL_NA
        return self.BASE_URL_EU

    def load_config(self, config_file: str):
        """Loads configuration from a JSON file"""
        config_path = Path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")

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
        """Saves the current token values to the config file"""
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
            logger.warning(f"Could not save config file: {e}")

    def refresh_access_token(self) -> bool:
        """
        Refreshes the access token using the refresh token

        Returns:
            True if the refresh succeeded
        """
        if not self.refresh_token:
            return False

        if not self.client_id:
            logger.error("client_id is required for token refresh")
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
            logger.error(f"Error refreshing token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def start_device_code_flow(self, client_id: Optional[str] = None, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Starts the Device Code Flow with PKCE

        Args:
            client_id: Client ID for OAuth (optional, uses self.client_id if not provided)
            scope: OAuth scopes (optional, default: "authenticate_user openid cardata:api:read cardata:streaming:read")

        Returns:
            Dictionary with device_code, user_code, verification_uri, etc., or None on error
        """
        client_id = client_id or self.client_id
        if not client_id:
            logger.error("client_id is required for Device Code Flow")
            return None

        # PKCE: generate code_verifier (43-128 characters, URL-safe)
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')

        # PKCE: create code_challenge (SHA256 hash of the code_verifier)
        code_challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        code_challenge = base64.urlsafe_b64encode(code_challenge_bytes).decode('utf-8').rstrip('=')

        device_code_url = f"{self.OAUTH_BASE_URL}/gcdm/oauth/device/code"

        # Scope: default is the full scope for the CarData API
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

            # Store code_verifier for the later token request
            device_code_data['_code_verifier'] = code_verifier

            if self.config_file:
                self.save_config({'device_code_response': device_code_data})

            return device_code_data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error starting Device Code Flow: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def request_device_token(self, device_code: str, client_id: Optional[str] = None, code_verifier: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Requests a token for the device (after the Device Code Flow with PKCE)

        Args:
            device_code: Device code from start_device_code_flow
            client_id: Client ID for OAuth (optional, uses self.client_id if not provided)
            code_verifier: PKCE code_verifier (optional, loaded from config if not provided)

        Returns:
            Dictionary with access_token, refresh_token, etc., or None on error
        """
        client_id = client_id or self.client_id
        if not client_id:
            logger.error("client_id is required for token request")
            return None

        # Load code_verifier from config if not passed
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
            logger.error("code_verifier is required for PKCE flow")
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
                    logger.error(f"Error requesting device token: {error}")
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
            logger.error(f"Error requesting device token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def authenticate(self) -> bool:
        """
        Authenticates with the BMW Cardata API

        Returns:
            True if authentication succeeded
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
                logger.error(f"Authentication error: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                return False

        return False

    def get_headers(self) -> Dict[str, str]:
        """Returns the HTTP headers for API requests"""
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
        Ensures that a token is available.
        Does NOT refresh the token proactively - only on actual errors.

        Returns:
            True if a token is available
        """
        if not self.access_token:
            return self.authenticate()
        return True

    def _make_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """
        Performs an HTTP request and handles token refresh automatically

        Args:
            method: HTTP method (get, post, etc.)
            url: URL for the request
            **kwargs: Additional arguments for requests

        Returns:
            Response object or None on error
        """
        if not self.ensure_valid_token():
            return None

        try:
            response = requests.request(method, url, headers=self.get_headers(), **kwargs)

            if response.status_code == 429:
                logger.warning("API rate limit reached!")
                logger.warning("The daily request limit of the BMW API has been exceeded.")
                logger.warning("Please wait until tomorrow and try again.")
                if response.text:
                    try:
                        error_data = response.json()
                        if 'exveErrorMsg' in error_data:
                            logger.error(f"Details: {error_data['exveErrorMsg']}")
                    except:
                        pass
                return None

            if response.status_code == 401 or response.status_code == 403:
                if self.refresh_token:
                    logger.error("Authentication error (401/403). Attempting token refresh...")
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
            logger.error(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
                logger.error(f"Status code: {e.response.status_code}")
            return None

    def get_vehicles(self) -> Optional[Dict[str, Any]]:
        """
        Retrieves the list of vehicles

        Returns:
            Dictionary with vehicle data or None on error
        """
        url = f"{self.base_url}/customers/vehicles/mappings"

        if hasattr(self, '_debug') and self._debug:
            logger.debug(f"Request URL: {url}")
            logger.debug(f"Headers: {self.get_headers()}")

        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching vehicles: {e}")
                return None
        return None

    def get_vehicle_data(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves data for a specific vehicle

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided

        Returns:
            Dictionary with vehicle data or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
            return None

        url = f"{self.base_url}/customers/vehicles/{vin}/basicData"

        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching vehicle data: {e}")
                return None
        return None

    def get_charging_history(self, vin: Optional[str] = None, from_date: Optional[str] = None,
                             to_date: Optional[str] = None, next_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves the charging history for a vehicle

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided
            from_date: Start date in ISO 8601 format (e.g. "2025-01-01T00:00:00Z")
            to_date: End date in ISO 8601 format (e.g. "2025-12-31T23:59:59Z")
            next_token: Token for pagination (optional)

        Returns:
            Dictionary with charging history or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
            return None

        if not from_date or not to_date:
            from datetime import datetime, timedelta
            to_date = datetime.utcnow().isoformat() + 'Z'
            from_date = (datetime.utcnow() - timedelta(days=30)).isoformat() + 'Z'
            logger.info(f"Using default time range: {from_date} to {to_date}")

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
                logger.error(f"Error fetching charging history: {e}")
                return None
        return None

    def get_vehicle_image(self, vin: Optional[str] = None) -> Optional[bytes]:
        """
        Retrieves the vehicle image

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided

        Returns:
            Image data (bytes) or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
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
                    logger.error("Authentication error (401/403). Attempting token refresh...")
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
            logger.error(f"Error fetching vehicle image: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
                logger.error(f"Status code: {e.response.status_code}")
            return None

    def get_location_based_charging_settings(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves location-based charging settings

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided

        Returns:
            Dictionary with charging settings or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
            return None

        url = f"{self.base_url}/customers/vehicles/{vin}/locationBasedChargingSettings"

        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching charging settings: {e}")
                return None
        return None

    def get_smart_maintenance_tyre_diagnosis(self, vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves the Smart Maintenance tyre diagnosis

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided

        Returns:
            Dictionary with tyre diagnosis or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
            return None

        url = f"{self.base_url}/customers/vehicles/{vin}/smartMaintenanceTyreDiagnosis"

        response = self._make_request('get', url)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching tyre diagnosis: {e}")
                return None
        return None

    def get_telematic_data(self, vin: Optional[str] = None, container_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves telematic data

        Args:
            vin: Vehicle identification number (VIN), optional - taken from config if not provided
            container_id: Container ID (required for the telematicData endpoint)

        Returns:
            Dictionary with telematic data or None on error
        """
        vin = vin or self.vin
        if not vin:
            logger.error("VIN must be provided or present in the config")
            return None

        if not container_id:
            logger.error("container_id is required for telematicData")
            logger.info("Hint: First create a container via POST /customers/containers")
            return None

        url = f"{self.base_url}/customers/vehicles/{vin}/telematicData"
        params = {'containerId': container_id}

        response = self._make_request('get', url, params=params)
        if response:
            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching telematic data: {e}")
                return None
        return None


class BMWCardataStreamingClient:
    """Client for BMW Cardata streaming via MQTT"""

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
        Initializes the BMW Cardata streaming client

        Args:
            host: MQTT broker host
            port: MQTT broker port
            username: MQTT username (GCID)
            password: MQTT password (ID token/JWT)
            topic: MQTT topic (default: username/VIN)
            vin: Vehicle identification number
            on_message_callback: Callback function for received messages
            output_file: Optional: path to the output file for stream data
            refresh_token: Refresh token for automatic ID token renewal
            client_id: Client ID for token renewal
            config_file: Path to the config file for token storage
            stream_once: If True, all messages are collected during the stream duration and aggregated at the end (latest value per key)
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
            raise ValueError("Topic or VIN must be provided")

        if not self.topic:
            raise ValueError("Topic or VIN must be provided")

    def _check_refresh_token(self) -> bool:
        """Checks whether the ID token needs to be refreshed"""
        if self._is_token_expired():
            return self._refresh_id_token()
        return True

    def _is_token_expired(self) -> bool:
        """Checks whether the ID token has expired"""
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
            return now >= (exp - 300)  # 5 minute buffer to avoid race conditions
        except Exception as e:
            logger.warning(f"Error checking token expiry: {e}")
            return True

    def _refresh_id_token(self) -> bool:
        """
        Refreshes the ID token using the refresh token

        Returns:
            True if the refresh succeeded
        """
        if not self.refresh_token or not self.client_id:
            logger.error("Refresh token or client ID missing for token refresh")
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
            logger.info("Refreshing ID token...")
            response = requests.post(token_url, data=data, headers=headers)
            response.raise_for_status()
            token_data = response.json()

            new_id_token = token_data.get('id_token')
            if not new_id_token:
                logger.error("No ID token in response")
                return False

            # Update local password with the new ID token
            self.password = new_id_token

            if 'refresh_token' in token_data:
                self.refresh_token = token_data.get('refresh_token')

            if self.config_file:
                self._save_token_to_config()

            # Check whether the new token is actually valid
            if self._is_token_expired():
                logger.warning("New token appears to have already expired")
            else:
                logger.info("ID token refreshed successfully")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Error refreshing ID token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def _save_token_to_config(self):
        """Saves the refreshed ID token to the config file"""
        if not self.config_file:
            return

        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)

            # Update id_token in device_token_response (not as a separate streaming_password)
            if 'device_token_response' not in config:
                config['device_token_response'] = {}
            config['device_token_response']['id_token'] = self.password

            if self.refresh_token:
                config['device_token_response']['refresh_token'] = self.refresh_token

            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)

            logger.info(f"Token saved to {self.config_file}")
        except Exception as e:
            logger.warning(f"Could not save token to config: {e}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Callback invoked when the connection is established"""
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc == 0:
            logger.info(f"Connected to MQTT broker {self.host}:{self.port}")
            self.connected = True
            logger.info(f"Subscribing to topic: {self.topic}")
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
            logger.error(f"Connection failed: {error_msg} (code {rc})")
            self.connected = False

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Callback invoked when the connection is lost"""
        self.connected = False
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc != 0:
            logger.error(f"Unexpected disconnect. Code: {rc}")
        if self._check_refresh_token():
            # Provide updated username and password to the MQTT client
            client.username_pw_set(self.username, self.password)

    def _on_message(self, client, userdata, msg):
        """Callback invoked when a message is received"""
        self.message_count += 1
        timestamp = datetime.now().isoformat()

        try:
            payload = msg.payload.decode('utf-8')
            data = {
                'timestamp': timestamp,
                'topic': msg.topic,
                'payload': json.loads(payload) if payload.startswith('{') else payload
            }

            logger.info(f"[{timestamp}] Received message #{self.message_count}:")
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
            logger.error(f"Error processing message: {e}")

    def _aggregate_latest_data(self, messages: list) -> dict:
        """
        Aggregates all messages and returns the latest value for each key

        Args:
            messages: List of messages

        Returns:
            Dictionary with aggregated data (latest values per key)
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
                logger.debug(f"Message #{idx+1} skipped (payload is not a dict)")
                continue

            data = payload.get('data', {})
            if not isinstance(data, dict):
                logger.debug(f"Message #{idx+1} skipped (data is not a dict)")
                continue

            msg_keys = list(data.keys())
            logger.debug(f"Message #{idx+1} contains {len(msg_keys)} keys: {sorted(msg_keys)}")

            msg_timestamp = payload.get('timestamp')
            if msg_timestamp and (not latest_timestamp or msg_timestamp > latest_timestamp):
                latest_timestamp = msg_timestamp
                latest_payload = payload

            for key, value in data.items():
                if not isinstance(value, dict):
                    logger.debug(f"Key {key} skipped (not a dict): {type(value)}")
                    continue

                all_keys_seen.add(key)
                key_timestamp = value.get('timestamp', '')
                if key not in aggregated_data:
                    aggregated_data[key] = value
                    logger.debug(f"Key {key} added (timestamp: {key_timestamp})")
                else:
                    existing_timestamp = aggregated_data[key].get('timestamp', '')
                    if key_timestamp > existing_timestamp:
                        aggregated_data[key] = value
                        logger.debug(f"Key {key} updated (old: {existing_timestamp}, new: {key_timestamp})")
                    else:
                        logger.debug(f"Key {key} kept (old: {existing_timestamp}, new: {key_timestamp})")

        logger.debug(f"Processed {len(messages)} messages in total")
        logger.debug(f"Keys found: {sorted(all_keys_seen)}")
        logger.debug(f"Aggregated keys ({len(aggregated_data)}): {sorted(aggregated_data.keys())}")

        if 'vehicle.cabin.infotainment.navigation.currentLocation.altitude' in all_keys_seen and 'vehicle.cabin.infotainment.navigation.currentLocation.altitude' not in aggregated_data:
            logger.warning("altitude was found but not aggregated!")

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
        Checks whether a file contains valid data (no error message)

        Args:
            file_path: Path to the JSON file

        Returns:
            True if the file exists and contains valid data
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
        """Callback invoked when the topic is subscribed"""
        logger.info(f"Topic subscribed successfully (reason codes: {reason_code_list})")

    def connect(self) -> bool:
        """
        Connects to the MQTT broker

        Returns:
            True if the connection succeeded
        """
        if not self._check_refresh_token():
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

            logger.info(f"Connecting to {self.host}:{self.port} (MQTT 3.1.1)...")
            logger.info(f"Topic: {self.topic}")
            logger.info(f"Username (GCID): {self.username}")
            logger.debug(f"Password (ID-Token): {self.password[:50]}...")

            self.client.username_pw_set(self.username, self.password)

            logger.info("Configuring TLS/SSL...")
            self.client.tls_set(
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS
            )

            logger.info("Connecting to broker...")
            self.client.connect(self.host, self.port, keepalive=30)

            return True

        except Exception as e:
            logger.error(f"Error connecting: {e}")
            import traceback
            traceback.print_exc()
            return False

    def start(self, duration: Optional[int] = None):
        """
        Starts the stream

        Args:
            duration: Optional: duration in seconds (None = unlimited)
        """
        if not self.client:
            if not self.connect():
                return

        logger.info(f"Starting streaming{f' for {duration} seconds' if duration else ' (Ctrl+C to stop)'}...")

        if self.output_file:
            if self.stream_once:
                logger.info(f"Aggregated data will be saved to {self.output_file} (latest value per key)")
            else:
                logger.info(f"Output will be saved to {self.output_file}")

        try:
            if duration:
                self.client.loop_start()
                time.sleep(duration)
                self.client.loop_stop()
            else:
                self.client.loop_forever()

        except KeyboardInterrupt:
            logger.info(f"Stream stopped. {self.message_count} messages received.")

        finally:
            if self.stream_once and self.output_file:
                if self.message_count > 0:
                    aggregated = self._aggregate_latest_data(self.collected_messages)
                    output_path = Path(self.output_file)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.output_file, 'a') as f:
                        f.write(json.dumps(aggregated, ensure_ascii=False) + '\n')
                    logger.info(f"{self.message_count} messages received. Aggregated data saved to {self.output_file}")
                elif self.message_count == 0:
                    if not self._has_valid_data(self.output_file):
                        error_data = {
                            'timestamp': datetime.now().isoformat(),
                            'error': 'No message received',
                            'message': 'The vehicle may not be sending data (standby mode) or is not active.'
                        }
                        output_path = Path(self.output_file)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.output_file, 'a') as f:
                            f.write(json.dumps(error_data, ensure_ascii=False) + '\n')
                        logger.warning(f"No message received. Error status saved to {self.output_file}")
                    else:
                        logger.info(f"No new message received. Existing file {self.output_file} remains unchanged.")
            self.disconnect()

    def disconnect(self):
        """Disconnects from the MQTT broker"""
        if self.client and self.connected:
            self.client.disconnect()
            self.connected = False
            logger.info("Disconnected")


def main():
    """Main function"""
    import argparse

    parser = argparse.ArgumentParser(
        description='Retrieves vehicle data via the BMW Cardata API'
    )
    parser.add_argument(
        '--config',
        type=str,
        default=None,
        help='Path to the configuration file (default: config.json in the script directory)'
    )
    parser.add_argument(
        '--api-key',
        type=str,
        help='API key'
    )
    parser.add_argument(
        '--client-id',
        type=str,
        help='Client ID for OAuth'
    )
    parser.add_argument(
        '--client-secret',
        type=str,
        help='Client secret for OAuth'
    )
    parser.add_argument(
        '--access-token',
        type=str,
        help='OAuth access token (can be used directly)'
    )
    parser.add_argument(
        '--refresh-token',
        type=str,
        help='OAuth refresh token (for renewing the access token)'
    )
    parser.add_argument(
        '--region',
        type=str,
        default='EU',
        choices=['EU', 'NA'],
        help='Region (EU for Europe, NA for North America) - default: EU'
    )
    parser.add_argument(
        '--vin',
        type=str,
        help='Vehicle identification number (VIN)'
    )
    parser.add_argument(
        '--vehicles',
        action='store_true',
        help='Retrieve the list of all vehicles'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Retrieve the status of a vehicle (requires --vin)'
    )
    parser.add_argument(
        '--charging-history',
        action='store_true',
        help='Retrieve charging history (requires --vin)'
    )
    parser.add_argument(
        '--from-date',
        type=str,
        help='Start date for charging history (ISO 8601, e.g. 2025-01-01T00:00:00Z)'
    )
    parser.add_argument(
        '--to-date',
        type=str,
        help='End date for charging history (ISO 8601, e.g. 2025-12-31T23:59:59Z)'
    )
    parser.add_argument(
        '--image',
        action='store_true',
        help='Retrieve vehicle image (requires --vin)'
    )
    parser.add_argument(
        '--image-output',
        type=str,
        help='Output file for the vehicle image'
    )
    parser.add_argument(
        '--charging-settings',
        action='store_true',
        help='Retrieve location-based charging settings (requires --vin)'
    )
    parser.add_argument(
        '--tyre-diagnosis',
        action='store_true',
        help='Retrieve Smart Maintenance tyre diagnosis (requires --vin)'
    )
    parser.add_argument(
        '--telematic-data',
        action='store_true',
        help='Retrieve telematic data (requires --vin and --container-id)'
    )
    parser.add_argument(
        '--container-id',
        type=str,
        help='Container ID for telematic data'
    )
    parser.add_argument(
        '--base-url',
        type=str,
        help='Base URL of the API (overrides default URL)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Show debug information'
    )
    parser.add_argument(
        '--stream',
        action='store_true',
        help='Start streaming mode (MQTT)'
    )
    parser.add_argument(
        '--stream-duration',
        type=int,
        help='Streaming duration in seconds (default: unlimited)'
    )
    parser.add_argument(
        '--stream-output',
        type=str,
        help='Output file for stream data'
    )
    parser.add_argument(
        '--streaming-host',
        type=str,
        help='MQTT broker host (overrides config)'
    )
    parser.add_argument(
        '--streaming-port',
        type=int,
        help='MQTT broker port (overrides config)'
    )
    parser.add_argument(
        '--streaming-username',
        type=str,
        help='MQTT username (overrides config)'
    )
    parser.add_argument(
        '--streaming-password',
        type=str,
        help='MQTT password (overrides config)'
    )
    parser.add_argument(
        '--stream-once',
        action='store_true',
        help='Collects all messages during the stream duration and saves aggregated data (latest value per key) as JSON'
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
            logger.debug(f"Using base URL: {client.base_url}")
            logger.debug(f"Region: {client.region}")
            if client.access_token:
                logger.debug(f"Access token present: {client.access_token[:20]}...")

        if args.vehicles:
            logger.info("Fetching vehicle list...")
            vehicles = client.get_vehicles()
            if vehicles:
                print(json.dumps(vehicles, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        elif args.status:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --status or VIN must be present in the config")
                sys.exit(1)
            logger.info(f"Fetching status for VIN {vin}...")
            status = client.get_vehicle_data(vin)
            if status:
                print(json.dumps(status, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        elif args.charging_history:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --charging-history or VIN must be present in the config")
                sys.exit(1)
            logger.info(f"Fetching charging history for VIN {vin}...")
            history = client.get_charging_history(vin, from_date=args.from_date, to_date=args.to_date)
            if history:
                print(json.dumps(history, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        elif args.image:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --image or VIN must be present in the config")
                sys.exit(1)
            logger.info(f"Fetching vehicle image for VIN {vin}...")
            image_data = client.get_vehicle_image(vin)
            if image_data:
                if args.image_output:
                    with open(args.image_output, 'wb') as f:
                        f.write(image_data)
                    logger.info(f"Image saved to {args.image_output}")
                else:
                    logger.info(f"Image received ({len(image_data)} bytes). Use --image-output to save it.")
            else:
                sys.exit(1)

        elif args.charging_settings:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --charging-settings or VIN must be present in the config")
                sys.exit(1)
            logger.info(f"Fetching charging settings for VIN {vin}...")
            settings = client.get_location_based_charging_settings(vin)
            if settings:
                print(json.dumps(settings, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        elif args.tyre_diagnosis:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --tyre-diagnosis or VIN must be present in the config")
                sys.exit(1)
            logger.info(f"Fetching tyre diagnosis for VIN {vin}...")
            diagnosis = client.get_smart_maintenance_tyre_diagnosis(vin)
            if diagnosis:
                print(json.dumps(diagnosis, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        elif args.telematic_data:
            vin = args.vin or client.vin
            if not vin:
                logger.error("--vin is required for --telematic-data or VIN must be present in the config")
                sys.exit(1)
            if not args.container_id:
                logger.error("--container-id is required for --telematic-data")
                sys.exit(1)
            logger.info(f"Fetching telematic data for VIN {vin}...")
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
                logger.error("Streaming requires host, port, username and password")
                logger.info("Configure these parameters in config.json or pass them as arguments")
                sys.exit(1)

            if not vin:
                logger.error("VIN is required for streaming")
                sys.exit(1)

            if args.stream_once:
                logger.info(f"Starting one-shot streaming for VIN {vin}...")
                if not args.stream_output:
                    logger.warning("--stream-once without --stream-output. Data will only be printed, not saved.")
            else:
                logger.info(f"Starting streaming for VIN {vin}...")
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
            logger.info(f"Fetching data for VIN {vin}...")
            data = client.get_vehicle_data(vin)
            if data:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                sys.exit(1)

        else:
            parser.print_help()
            sys.exit(1)

    except Exception as e:
        logger.error(f"{e}")
        sys.exit(1)


if __name__ == '__main__':
    main()

