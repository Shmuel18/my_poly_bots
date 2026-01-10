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

### 3. הרצת בוט

```bash
python -m strategies.example_bot.run
```

## 📚 איך לבנות בוט חדש

1. צור תיקייה חדשה ב-`strategies/`
2. צור קובץ עם הלוגיקה שלך
3. השתמש ב-Core modules לחיבור ומסחר
4. הרץ!

## 🛠️ Core Modules

### Connection
```python
from core.connection import PolymarketConnection

conn = PolymarketConnection()
markets = conn.get_markets()
```

### Scanner
```python
from core.scanner import MarketScanner

scanner = MarketScanner()
opportunities = scanner.scan_for_opportunities(filters={...})
```

### Executor
```python
from core.executor import TradeExecutor

executor = TradeExecutor()
result = executor.execute_trade(token_id, side, size, price)
```

## 🎯 אסטרטגיות מובנות

- **Arbitrage Bot** - זיהוי והפעלת הזדמנויות ארביטראז'
- **Extreme Price Bot** - קנייה במחירים קיצוניים (על בסיס הקוד שלך)
- **Template** - תבנית לבוט חדש

## ⚠️ אזהרה

מסחר אוטומטי כרוך בסיכון! התחל עם סכומים קטנים.

---

**בהצלחה! 🚀**
