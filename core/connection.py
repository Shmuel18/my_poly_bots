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
    
    דוגמת שימוש:
        conn = PolymarketConnection()
        balance = await conn.get_balance()
    """
    
    def __init__(self):
        """אתחול חיבור עם מפתחות API מקובץ .env"""
        self._validate_env_vars()
        self._init_client()
        
    def _validate_env_vars(self):
        """בדיקה שכל המפתחות הנדרשים קיימים"""
        required_vars = [
            "POLYMARKET_API_KEY",
            "POLYMARKET_API_SECRET", 
            "POLYMARKET_API_PASSPHRASE",
            "POLYMARKET_PRIVATE_KEY",
            "POLYMARKET_FUNDER_ADDRESS"
        ]
        
        missing = [var for var in required_vars if not os.getenv(var)]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Please check your config/.env file"
            )
    
    def _init_client(self):
        """אתחול CLOB client"""
        try:
            # API credentials
            creds = ApiCreds(
                api_key=os.getenv("POLYMARKET_API_KEY").strip(),
                api_secret=os.getenv("POLYMARKET_API_SECRET").strip(),
                api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE").strip()
            )
            
            # Determine signature type dynamically:
            # If FUNDER exists, it's a Proxy wallet (email/Google) = signature_type 1
            # Otherwise, it's a regular EOA wallet (MetaMask) = signature_type 0
            funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS")
            sig_type = 1 if funder_address else 0
            
            # Initialize CLOB client
            self.client = ClobClient(
                host=os.getenv("CLOB_URL", "https://clob.polymarket.com"),
                key=os.getenv("POLYMARKET_PRIVATE_KEY"),
                chain_id=int(os.getenv("CHAIN_ID", "137")),
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
