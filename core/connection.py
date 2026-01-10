"""
Polymarket Connection Module

מודול לניהול החיבור ל-Polymarket API.
משמש בסיס לכל הבוטים.
"""
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

# Load environment variables
env_path = Path(__file__).parent.parent / "config" / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

logger = logging.getLogger(__name__)


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
        chain_id: Optional[int] = None
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
        # אם אין FUNDER_ADDRESS, נתמוך בארנק EOA (signature_type=0)
        # עבור Proxy נדרשים כל המפתחות כולל FUNDER_ADDRESS
        api_key = self._get_or_env('API_KEY', 'POLYMARKET_API_KEY')
        api_secret = self._get_or_env('API_SECRET', 'POLYMARKET_API_SECRET')
        api_passphrase = self._get_or_env('API_PASSPHRASE', 'POLYMARKET_API_PASSPHRASE')
        private_key = self._get_or_env('PRIVATE_KEY', 'POLYMARKET_PRIVATE_KEY')
        funder_address = self._get_or_env('FUNDER_ADDRESS', 'POLYMARKET_FUNDER_ADDRESS')
        
        missing = []
        for name, val in [('POLYMARKET_API_KEY', api_key),
                          ('POLYMARKET_API_SECRET', api_secret),
                          ('POLYMARKET_API_PASSPHRASE', api_passphrase),
                          ('POLYMARKET_PRIVATE_KEY', private_key)]:
            if not val:
                missing.append(name)
        
        # FUNDER_ADDRESS נדרש רק במצב Proxy
        # נבדוק ונדווח אם חסר כאשר נדרש
        if missing:
            raise EnvironmentError(
                f"Missing required credentials: {', '.join(missing)}\n"
                f"Provide via constructor or config/.env"
            )
    
    def _init_client(self):
        """אתחול CLOB client"""
        try:
            # Resolve credentials (prefer injected)
            api_key = self._get_or_env('API_KEY', 'POLYMARKET_API_KEY', '')
            api_secret = self._get_or_env('API_SECRET', 'POLYMARKET_API_SECRET', '')
            api_passphrase = self._get_or_env('API_PASSPHRASE', 'POLYMARKET_API_PASSPHRASE', '')
            private_key = self._get_or_env('PRIVATE_KEY', 'POLYMARKET_PRIVATE_KEY', '')
            funder_address = self._get_or_env('FUNDER_ADDRESS', 'POLYMARKET_FUNDER_ADDRESS', None)
            clob_url = self._get_or_env('CLOB_URL', 'CLOB_URL', 'https://clob.polymarket.com')
            chain_id_val = self._get_or_env('CHAIN_ID', 'CHAIN_ID', 137)
            chain_id = int(chain_id_val) if isinstance(chain_id_val, (str, int)) else 137

            creds = ApiCreds(
                api_key=api_key.strip(),
                api_secret=api_secret.strip(),
                api_passphrase=api_passphrase.strip()
            )
            
            # Determine signature type dynamically
            sig_type = 1 if funder_address else 0
            
            # Initialize CLOB client
            self.client = ClobClient(
                host=clob_url,
                key=private_key,
                chain_id=chain_id,
                creds=creds,
                signature_type=sig_type,
                funder=funder_address if sig_type == 1 else None
            )
            
            self.client.set_api_creds(creds)
            
            # Cache for balance
            self._balance_cache: Optional[float] = None
            self._balance_is_real = False
            
            wallet_type = "Proxy (Email/Google)" if sig_type == 1 else "EOA (MetaMask)"
            logger.info(f"✅ Connected to Polymarket ({wallet_type})")
            logger.info(f"   Signer: {self.client.get_address()}")
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
        if self._balance_cache is not None and not force_refresh:
            return self._balance_cache
        
        try:
            # Try to get balance from API
            balance_info = self.client.get_balance_allowance()
            balance = float(balance_info.get('balance', 0))
            
            self._balance_cache = balance
            self._balance_is_real = True
            logger.info(f"💰 Balance: ${balance:.2f} USDC")
            
            return balance
            
        except Exception as e:
            logger.warning(f"Could not fetch balance: {e}")
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
