# Contributing Guide

## איך להוסיף אסטרטגיה חדשה

### 1. צור תיקייה חדשה

```
strategies/
└── my_new_strategy/
    ├── __init__.py
    ├── strategy.py
    └── README.md
```

### 2. רשת מ-BaseStrategy

```python
from strategies.base_strategy import BaseStrategy

class MyNewStrategy(BaseStrategy):
    def __init__(self, **kwargs):
        super().__init__(strategy_name="MyNewStrategy", **kwargs)

    async def scan(self):
        # הלוגיקה שלך לסריקה
        pass

    async def should_enter(self, opportunity):
        # הלוגיקה שלך להיכנס
        pass

    async def should_exit(self, position):
        # הלוגיקה שלך לצאת
        pass
```

### 3. הוסף תיעוד

צור `README.md` שמסביר את האסטרטגיה:

- מה היא עושה
- איך להשתמש בה
- פרמטרים להגדרה
- דוגמאות

### 4. בדוק

```bash
python -m strategies.my_new_strategy.strategy
```

## מבנה Core

אם אתה צריך להוסיף פונקציונליות ל-Core:

1. **Connection** - כל מה שקשור ל-API connection
2. **Scanner** - פונקציות סריקה ומיון
3. **Executor** - ביצוע עסקאות
4. **WebSocket** - real-time data

## Best Practices

- ✅ השתמש ב-logger במקום print
- ✅ הוסף type hints
- ✅ כתוב docstrings בעברית וברורים
- ✅ טפל ב-exceptions
- ✅ בדוק balance לפני כל עסקה
- ✅ השתמש ב-helpers מ-utils

## Testing

```python
# בדיקה מהירה
python test_connection.py

# בדיקת סורק
python examples/scanner_example.py

# הרצת אסטרטגיה
python -m strategies.extreme_price.strategy
```
