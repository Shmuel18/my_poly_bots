"""
Example: Simple Market Scanner

דוגמה לשימוש בסורק השווקים.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import MarketScanner
from utils import setup_logging, format_market_info, hours_until_close

setup_logging(log_level="INFO")


def main():
    """דוגמה לסריקת שווקים"""
    print("\n" + "="*60)
    print("🔍 Market Scanner Example")
    print("="*60 + "\n")
    
    scanner = MarketScanner()
    
    # Example 1: Get all active markets
    print("1️⃣ Getting active markets...")
    markets = scanner.get_all_active_markets(max_markets=100)
    print(f"   Found {len(markets)} markets\n")
    
    # Example 2: Filter crypto markets
    print("2️⃣ Filtering crypto markets...")
    crypto_markets = scanner.filter_markets(
        markets,
        keyword="bitcoin"
    )
    print(f"   Found {len(crypto_markets)} Bitcoin markets\n")
    
    # Example 3: Find markets closing soon
    print("3️⃣ Finding markets closing within 24 hours...")
    closing_soon = scanner.filter_markets(
        markets,
        max_hours_until_close=24
    )
    print(f"   Found {len(closing_soon)} markets\n")
    
    # Example 4: Find extreme prices
    print("4️⃣ Finding markets with extreme prices...")
    extreme = scanner.find_extreme_prices(
        markets,
        low_threshold=0.01,
        high_threshold=0.99
    )
    print(f"   Found {len(extreme)} markets with extreme prices\n")
    
    # Show some examples
    if extreme:
        print("💎 Sample Markets with Extreme Prices:")
        for i, market in enumerate(extreme[:5], 1):
            print(f"\n   {i}. {format_market_info(market)}")
            print(f"      Extreme price: ${market.get('extreme_price', 0):.4f} ({market.get('extreme_side', '?')})")
    
    print("\n" + "="*60)
    print("✅ Examples completed!")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
