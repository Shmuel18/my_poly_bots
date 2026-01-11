# Polymarket Trading Bots Framework

מסגרת מסודרת ומקצועית לבניית בוטים לפולימרקט

## 🏗️ מבנה הפרויקט

```
my_poly_bots/
├── core/                    # ליבת המערכת - משותף לכל הבוטים
│   ├── connection.py       # חיבור ל-Polymarket API
│   ├── ws_manager.py       # WebSocket לנתוני מחירים בזמן אמת
│   ├── executor.py         # ביצוע עסקאות
│   ├── scanner.py          # סריקת שווקים
│   └── config.py           # הגדרות ואימות
│
├── strategies/             # הבוטים שלך - כל אחד בתיקייה נפרדת
│   ├── example_bot/       # בוט לדוגמה
│   ├── arbitrage/         # ארביטראז'
│   └── extreme_price/     # מחירים קיצוניים
│
├── utils/                  # כלים עזר
│   ├── logger.py          # מערכת לוגים
│   ├── helpers.py         # פונקציות עזר
│   └── database.py        # שמירת נתונים
│
├── config/                 # קבצי הגדרות
│   ├── .env              # מפתחות API (לא לשתף!)
│   └── settings.yaml     # הגדרות כלליות
│
└── tests/                 # בדיקות
```

## 🚀 התחלה מהירה

### 1. התקנה

```bash
pip install -r requirements.txt
```

### 2. הגדרת מפתחות API

ערוך `config/.env`:

```
POLYMARKET_API_KEY=your_key
POLYMARKET_API_SECRET=your_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_FUNDER_ADDRESS=your_wallet_address
```

### 3. הרצת בוט (CLI חדש)

הרצה עם חשבון יחיד (קובץ `.env` אחד):

```bash
python main.py --strategy extreme_price --env config/.env
```

הרצה במקביל עם מספר חשבונות (כל חשבון בקובץ `.env` נפרד):

```bash
python main.py --strategy arbitrage --env config/account1.env --env config/account2.env
```

### טעינה דינמית של אסטרטגיות

ניתן לטעון מחלקת אסטרטגיה ממסלול דוטד (Module) או מקובץ פייתון ישירות.

```bash
# טעינה דינמית ממסלול דוטד (כולל שם המחלקה)
python main.py --strategy-path strategies.arbitrage.strategy:ArbitrageStrategy --env config/.env

# טעינה דינמית מקובץ (יחסי/מוחלט) עם שם המחלקה
python main.py --strategy-path strategies/custom_strategy.py:CustomStrategy --env config/.env

# אם לא מציינים שם מחלקה, ייטען בשם ברירת המחדל "Strategy"
python main.py --strategy-path strategies/my_strategy.py --env config/.env
```

הקונסטרקטור של אסטרטגיה דינמית צפוי לפחות לקבל `connection` ו-`log_level`. אם יש פרמטרים נוספים, ניתן להעביר אותם דרך `--strategy-args` (JSON):

```bash
# דוגמה: שינוי פרמטרים לארביטראז' (Built-in)
python main.py --strategy arbitrage --env config/.env --strategy-args "{\"min_profit_pct\": 3.5, \"scan_interval\": 120}"

# דוגמה: אסטרטגיה דינמית עם kwargs מותאמים
python main.py --strategy-path strategies/custom_strategy.py:CustomStrategy --env config/.env --strategy-args "{\"threshold\": 0.5, \"max_positions\": 5}"
```

### מצב הדמיה (Dry-Run)

להריץ הכל בלי לשלוח הזמנות אמיתיות (עם נתוני שוק אמיתיים):

```bash
python main.py --strategy extreme_price --env config/.env --dry-run

# או עם אסטרטגיה דינמית ו-parms מותאמים
python main.py --strategy-path strategies/custom_strategy.py:CustomStrategy --env config/.env --strategy-args "{\"threshold\": 0.4}" --dry-run
```

הדגשה: ב-Dry-Run לא נשלחות הזמנות, הלוגים מסומנים כ-[DRY-RUN], והכנסות/יציאות מחושבות סימולטיבית בלבד. החיבור משתמש בלקוח דמה שקורא Orderbook אמיתי (ציבורי) ולכן אפשר להריץ ללא מפתחות (Guest Mode) ולקבל סיגנלים על הזדמנויות אמיתיות.

טיפ: בהפעלת מספר חשבונות במקביל, שם ה-logger כולל קיצור כתובת הארנק כדי להבדיל בין התהליכים (למשל `ArbitrageStrategy_0x1234`).

## 📚 איך לבנות בוט חדש

1. צור תיקייה חדשה ב-`strategies/`
2. צור קובץ עם הלוגיקה שלך
3. השתמש ב-Core modules לחיבור ומסחר
4. הרץ!

## 🛠️ Core Modules

### Connection

תומך באופן אוטומטי בשני סוגי ארנקים, ויכול לקבל מפתחות מוזרמים (לריבוי חשבונות):

- **Proxy Wallets** (Email/Google) - עם FUNDER_ADDRESS
- **EOA Wallets** (MetaMask) - ללא FUNDER_ADDRESS

```python
from core.connection import PolymarketConnection

conn = PolymarketConnection(  # הזרמת מפתחות מאפשרת ריבוי חשבונות במקביל
    api_key="...",
    api_secret="...",
    api_passphrase="...",
    private_key="...",
    funder_address="...",     # לא חובה ב-EOA
)
markets = conn.get_markets()
```

### WebSocket Manager

חיבור WebSocket לעדכוני מחירים בזמן אמת עם:
- **Auto-Reconnection** - התחברות מחדש אוטומטית בניתוק
- **Health Monitoring** - בדיקה שהחיבור פעיל
- **Batch Subscriptions** - הרשמה לאלפי שווקים בבאצ'ים

```python
from core.ws_manager import WebSocketManager

ws = WebSocketManager(auto_reconnect=True)
await ws.connect()
await ws.subscribe_batch(token_ids, batch_size=100)

# Start reconnect loop in background
asyncio.create_task(ws.start_reconnect_loop())

# Listen to price updates
async def price_handler(token_id, price):
    print(f"{token_id}: ${price}")

await ws.receive_data(callback=price_handler)
```

### Scanner

```python
from core.scanner import MarketScanner

scanner = MarketScanner()
opportunities = scanner.scan_for_opportunities(filters={...})
```

### Executor

מטפל ב-Partial Fills, Rate Limiting ומעקב אחר גודל פוזיציות אמיתי:

```python
from core.executor import TradeExecutor

executor = TradeExecutor()
result = executor.execute_trade(token_id, side, size, price)

# בודק אם היה partial fill
if result:
    filled = result.get('sizeFilled', 0)
    requested = result.get('size', 0)
    if filled < requested:
        print(f"⚠️ Partial fill: {filled}/{requested}")
```

### Rate Limiter

מונע חסימות API ושגיאות 429:

```python
from utils.rate_limiter import POLYMARKET_RATE_LIMITER

async with POLYMARKET_RATE_LIMITER:
    # API call is automatically rate-limited
    response = client.post_order(...)

# Get stats
stats = POLYMARKET_RATE_LIMITER.get_stats()
print(f"Capacity: {stats[0]['capacity_pct']:.1f}%")
```

## 🎯 אסטרטגיות מובנות

- **Arbitrage Bot** - זיהוי והפעלת הזדמנויות ארביטראז'
- **Extreme Price Bot** - קנייה במחירים קיצוניים (על בסיס הקוד שלך)
- **Template** - תבנית לבוט חדש

## ⚠️ אזהרה

מסחר אוטומטי כרוך בסיכון! התחל עם סכומים קטנים.

---

**בהצלחה! 🚀**
