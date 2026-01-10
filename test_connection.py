"""
Quick Test Script

בדיקה מהירה של החיבור ל-Polymarket.
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core import PolymarketConnection, MarketScanner
from utils import setup_logging, format_market_info

setup_logging(log_level="INFO")


async def test_connection():
    """בודק חיבור ל-Polymarket"""
    print("\n" + "="*60)
    print("🧪 Testing Polymarket Connection")
    print("="*60 + "\n")
    
    # Test connection
    print("1️⃣ Testing API connection...")
    conn = PolymarketConnection()
    
    # Test balance
    print("\n2️⃣ Fetching balance...")
    balance = await conn.get_balance()
    print(f"   💰 Balance: ${balance:.2f} USDC")
    
    # Test scanner
    print("\n3️⃣ Testing market scanner...")
    scanner = MarketScanner()
    markets = scanner.get_active_markets(limit=10)
    print(f"   ✅ Found {len(markets)} active markets")
    
    # Show first few markets
    if markets:
        print("\n📊 Sample Markets:")
        for i, market in enumerate(markets[:3], 1):
            print(f"\n   {i}. {format_market_info(market)}")
    
    print("\n" + "="*60)
    print("✅ All tests passed!")
    print("="*60 + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(test_connection())
    except KeyboardInterrupt:
        print("\n👋 Test cancelled")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
