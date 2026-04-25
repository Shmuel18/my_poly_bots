"""
Polymarket Connection Module

מודול לניהול החיבור ל-Polymarket API.
משמש בסיס לכל הבוטים.
"""
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

# Load environment variables
env_path = Path(__file__).parent.parent / "config" / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

logger = logging.getLogger(__name__)


class DummyClient:
    """Read-only client for dry-run mode with real public orderbook data."""

    def __init__(self, host: str = "https://clob.polymarket.com"):
        self.host = host.rstrip("/")

    def get_address(self) -> str:
        return "0xSIMULATION_WALLET"

    def get_balance_allowance(self) -> Dict[str, Any]:
        # Provide a generous virtual balance to avoid blocking simulated trades
        return {'balance': '100000'}

    def get_order_book(self, token_id: str) -> Dict[str, Any]:
        try:
            url = f"{self.host}/book"
            resp = requests.get(url, params={"token_id": token_id}, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            return {'bids': [], 'asks': []}
        except Exception as e:
            logging.warning(f"DummyClient failed to fetch book for {token_id}: {e}")
            return {'bids': [], 'asks': []}


class PolymarketConnection:
    """
    מנהל חיבור ל-Polymarket.
    
    ניתן להזרים מפתחות ישירות ל-__init__ כדי לתמוך בריבוי חשבונות במקביל.
    אם לא מוזרם, ייעשה שימוש בערכי סביבה (fallback).
    
    דוגמת שימוש:
        conn = PolymarketConnection(api_key=..., api_secret=..., ...)
        balance = await conn.get_balance()
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        private_key: Optional[str] = None,
        funder_address: Optional[str] = None,
        clob_url: Optional[str] = None,
        chain_id: Optional[int] = None,
        dry_run: bool = False,
    ):
        """אתחול חיבור עם מפתחות מוזרמים או fallback לסביבה"""
        self._provided = {
            'API_KEY': api_key,
            'API_SECRET': api_secret,
            'API_PASSPHRASE': api_passphrase,
            'PRIVATE_KEY': private_key,
            'FUNDER_ADDRESS': funder_address,
            'CLOB_URL': clob_url,
            'CHAIN_ID': chain_id,
        }
        self.dry_run = dry_run
        self._validate_env_vars()
        self._init_client()
        
    def _get_or_env(self, key: str, env_name: str, default: Optional[str] = None):
        """מעדיף ערך מוזרם, אחרת מהסביבה, אחרת ברירת מחדל"""
        val = self._provided.get(key)
        if val is not None:
            return val
        env_val = os.getenv(env_name)
        return env_val if env_val is not None else default

    def _validate_env_vars(self):
        """בדיקה שכל המפתחות הנדרשים קיימים לשימוש בלקוח"""
        if self.dry_run:
            # Skip strict validation in dry-run
            return

        # אם אין FUNDER_ADDRESS, נתמוך בארנק EOA (signature_type=0)
        # עבור Proxy נדרשים כל המפתחות כולל FUNDER_ADDRESS
        api_key = self._get_or_env('API_KEY', 'POLYMARKET_API_KEY')
        api_secret = self._get_or_env('API_SECRET', 'POLYMARKET_API_SECRET')
        api_passphrase = self._get_or_env('API_PASSPHRASE', 'POLYMARKET_API_PASSPHRASE')
        private_key = self._get_or_env('PRIVATE_KEY', 'POLYMARKET_PRIVATE_KEY')
        funder_address = self._get_or_env('FUNDER_ADDRESS', 'POLYMARKET_FUNDER_ADDRESS')
        
        # POLYMARKET_PRIVATE_KEY is strictly required — we derive everything else
        # from it if the L2 creds (API_SECRET / API_PASSPHRASE) aren't provided.
        if not private_key:
            raise EnvironmentError(
                "Missing required credential: POLYMARKET_PRIVATE_KEY\n"
                "Provide via constructor or config/.env"
            )
    
    def _init_client(self):
        """אתחול CLOB client עם לוגים מפורטים לאבחון"""
        try:
            if self.dry_run:
                clob_url = self._get_or_env('CLOB_URL', 'CLOB_URL', 'https://clob.polymarket.com')
                self.client = DummyClient(host=clob_url)
                self._balance_cache = 0.0
                self._balance_is_real = False
                logger.info("✅ Initialized Polymarket connection in DRY-RUN mode (no credentials required)")
                return

            # Resolve credentials (prefer injected)
            api_key = self._get_or_env('API_KEY', 'POLYMARKET_API_KEY', '')
            api_secret = self._get_or_env('API_SECRET', 'POLYMARKET_API_SECRET', '')
            api_passphrase = self._get_or_env('API_PASSPHRASE', 'POLYMARKET_API_PASSPHRASE', '')
            private_key = self._get_or_env('PRIVATE_KEY', 'POLYMARKET_PRIVATE_KEY', '')
            funder_address = self._get_or_env('FUNDER_ADDRESS', 'POLYMARKET_FUNDER_ADDRESS', None)
            clob_url = self._get_or_env('CLOB_URL', 'CLOB_URL', 'https://clob.polymarket.com')
            chain_id_val = self._get_or_env('CHAIN_ID', 'CHAIN_ID', 137)
            chain_id = int(chain_id_val) if isinstance(chain_id_val, (str, int)) else 137

            logger.info("[DEBUG] Polymarket Init: api_key=%s... api_secret=%s... api_passphrase=%s...", api_key[:6], api_secret[:6], api_passphrase[:6])
            logger.info("[DEBUG] private_key=%s... funder_address=%s", str(private_key)[:8], str(funder_address))

            # Determine signature type dynamically
            sig_type = 1 if funder_address else 0
            logger.info(f"[DEBUG] signature_type={sig_type} (1=Proxy, 0=EOA)")

            # Initialize CLOB client. If api_secret/passphrase are missing or
            # empty, we'll derive them from the private key after init.
            try:
                self.client = ClobClient(
                    host=clob_url,
                    key=private_key,
                    chain_id=chain_id,
                    signature_type=sig_type,
                    funder=funder_address if sig_type == 1 else None,
                )
            except Exception as e:
                logger.error(f"[DEBUG] ClobClient init failed: {e}")
                raise

            # Attach API creds. Prefer explicit creds from .env; fall back to
            # deriving them from the private key (py-clob-client signs a
            # deterministic message to request/recover the L2 credentials).
            creds = None
            if api_key and api_secret and api_passphrase:
                creds = ApiCreds(
                    api_key=api_key.strip(),
                    api_secret=api_secret.strip(),
                    api_passphrase=api_passphrase.strip(),
                )
                try:
                    self.client.set_api_creds(creds)
                    logger.info("[DEBUG] Attached explicit CLOB creds from .env")
                except Exception as e:
                    logger.warning(f"[DEBUG] set_api_creds failed with explicit creds: {e}; will try derive")
                    creds = None

            if creds is None:
                try:
                    derived = self.client.create_or_derive_api_creds(nonce=0)
                    self.client.set_api_creds(derived)
                    logger.info(
                        f"[DEBUG] Derived CLOB creds from private key — "
                        f"api_key={derived.api_key}"
                    )
                except Exception as e:
                    logger.error(f"[DEBUG] create_or_derive_api_creds failed: {e}")
                    raise

            # Cache for balance
            self._balance_cache: Optional[float] = None
            self._balance_is_real = False

            wallet_type = "Proxy (Email/Google)" if sig_type == 1 else "EOA (MetaMask)"
            logger.info(f"✅ Connected to Polymarket ({wallet_type})")
            try:
                logger.info(f"   Signer: {self.client.get_address()}")
            except Exception as e:
                logger.error(f"[DEBUG] get_address failed: {e}")
            if sig_type == 1:
                logger.info(f"   Funder: {funder_address}")

        except Exception as e:
            logger.error(f"Failed to initialize Polymarket connection: {e}")
            raise
    
    async def get_balance(self, force_refresh: bool = False) -> float:
        """
        קבלת יתרת USDC בארנק.
        
        Args:
            force_refresh: אם True, מאלץ רענון מה-API
            
        Returns:
            יתרה ב-USDC
        """
        if self.dry_run:
            return 0.0

        if self._balance_cache is not None and not force_refresh:
            return self._balance_cache
        
        try:
            # py-clob-client 0.34+ requires a BalanceAllowanceParams object.
            # For USDC balance, asset_type=COLLATERAL; signature_type=-1 lets
            # the client use the one it was built with (1 for proxy accounts).
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance_info = self.client.get_balance_allowance(params)

            # API returns balance in USDC micro-units (6 decimals) as a string
            raw = balance_info.get('balance', 0) if isinstance(balance_info, dict) else 0
            try:
                # Support both already-scaled ($123.45) and raw-wei (123450000) forms
                raw_float = float(raw)
                balance = raw_float / 1_000_000 if raw_float > 1_000 else raw_float
            except (TypeError, ValueError):
                balance = 0.0

            self._balance_cache = balance
            self._balance_is_real = True
            logger.info(f"💰 Balance: ${balance:.2f} USDC (via CLOB)")

            return balance

        except Exception as e:
            # If the CLOB client fails, fallback to on-chain balance query.
            # This only shows funds sitting on the proxy — Polymarket-internal
            # credits won't show up here.
            logger.warning(f"Could not fetch balance via CLOB client: {e}")

            # If we have a funder/funder address, attempt to read USDC balance directly from chain
            funder = self.get_funder_address() or None
            if funder:
                try:
                    import httpx
                    usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                    # polygon-rpc.com started returning API_KEY_DISABLED for
                    # cloud IPs — use drpc as a more reliable public RPC.
                    rpc_url = "https://polygon.drpc.org"
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "eth_call",
                        "params": [{
                            "to": usdc_contract,
                            "data": f"0x70a08231000000000000000000000000{funder[2:]}",
                        }, "latest"],
                        "id": 1
                    }

                    with httpx.Client() as client:
                        resp = client.post(rpc_url, json=payload, timeout=10)
                        if resp.status_code == 200:
                            data = resp.json()
                            if 'result' in data and data['result']:
                                balance_hex = data['result']
                                balance_wei = int(balance_hex, 16)
                                # USDC has 6 decimals
                                balance = balance_wei / 1_000_000

                                self._balance_cache = balance
                                self._balance_is_real = True
                                logger.info(f"💰 On-chain Balance: ${self._balance_cache:.2f} USDC (via RPC)")
                                return self._balance_cache
                except Exception as e2:
                    logger.warning(f"RPC balance fetch failed: {e2}")

            # Return cached or default
            if self._balance_cache:
                return self._balance_cache
            return 0.0
    
    def get_client(self) -> ClobClient:
        """מחזיר את ה-CLOB client לשימוש ישיר"""
        return self.client
    
    def get_address(self) -> str:
        """מחזיר את כתובת הארנק"""
        return self.client.get_address()
    
    def get_funder_address(self) -> str:
        """מחזיר את כתובת ה-Funder (הארנק האמיתי)"""
        return os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
