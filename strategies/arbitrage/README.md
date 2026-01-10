# Arbitrage Strategy

אסטרטגיית ארביטראז' בין שווקים היררכיים.

## אסטרטגיה

מזהה הבדלי מחירים בין שווקים קשורים ומבצע עסקאות דו-כיווניות.

## דוגמה

```
שוק 1: Bitcoin above $100k by Dec 31? YES @ $0.45
שוק 2: Bitcoin above $100k by Dec 15? YES @ $0.50

ארביטראז':
קנה בשוק 1 @ $0.45
מכור בשוק 2 @ $0.50
רווח: $0.05 (11.1%)
```

## הרצה

```bash
python -m strategies.arbitrage.strategy
```

## הגדרות

- `min_profit_pct`: אחוז רווח מינימלי (2%)
- `max_hours_until_close`: מקסימום שעות עד סגירה (24)
