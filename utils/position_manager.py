"""
Position Manager - Persistence Layer

מנהל פוזיציות עם שמירה קבועה לקובץ JSON.
מונע אובדן נתונים בעת הפעלה מחדש של הבוט.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class PositionManager:
    """
    מנהל פוזיציות עם persistence ל-JSON.
    
    דוגמת שימוש:
        pm = PositionManager("positions.json")
        pm.add_position(token_id, entry_price, size, metadata)
        position = pm.get_position(token_id)
        pm.remove_position(token_id)
    """
    
    def __init__(self, filepath: str = "data/positions.json"):
        """
        אתחול Position Manager.
        
        Args:
            filepath: נתיב לקובץ JSON
        """
        self.filepath = Path(filepath)
        self.positions: Dict[str, Dict[str, Any]] = {}
        
        # Create data directory if needed
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing positions
        self._load()
    
    def _load(self) -> None:
        """טוען פוזיציות מהקובץ."""
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    self.positions = json.load(f)
                logger.info(f"📂 Loaded {len(self.positions)} positions from {self.filepath}")
            except Exception as e:
                # Backup corrupted file and start fresh
                try:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup = self.filepath.with_suffix(f".corrupt_{ts}.json")
                    self.filepath.rename(backup)
                    logger.error(f"Failed to load positions: {e}. Backed up to {backup}")
                except Exception as be:
                    logger.error(f"Failed to backup corrupted positions file: {be}")
                finally:
                    self.positions = {}
        else:
            logger.info(f"No existing positions file at {self.filepath}")
            self.positions = {}
    
    def _save(self) -> None:
        """שומר את הפוזיציות לקובץ."""
        try:
            tmp_path = self.filepath.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
                f.flush()
            # Atomic replace
            tmp_path.replace(self.filepath)
        except Exception as e:
            logger.error(f"Failed to save positions atomically: {e}")
    
    def add_position(
        self,
        token_id: str,
        entry_price: float,
        size: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        מוסיף פוזיציה חדשה.
        
        Args:
            token_id: מזהה טוקן
            entry_price: מחיר כניסה
            size: גודל הפוזיציה
            metadata: מידע נוסף (question, strategy_name וכו')
        """
        self.positions[token_id] = {
            'token_id': token_id,
            'entry_price': entry_price,
            'size': size,
            'entry_time': datetime.now().isoformat(),
            'status': 'OPEN',
            **(metadata or {})
        }
        self._save()
        logger.info(f"💾 Saved position: {token_id[:12]}... @ ${entry_price:.4f} x {size}")
    
    def get_position(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        מחזיר פוזיציה לפי token_id.
        
        Args:
            token_id: מזהה טוקן
            
        Returns:
            מידע על הפוזיציה או None
        """
        return self.positions.get(token_id)
    
    def has_position(self, token_id: str) -> bool:
        """
        בודק אם קיימת פוזיציה פתוחה.
        
        Args:
            token_id: מזהה טוקן
            
        Returns:
            True אם קיימת פוזיציה
        """
        return token_id in self.positions
    
    def remove_position(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        מוחק פוזיציה ומחזיר את הנתונים שלה.
        
        Args:
            token_id: מזהה טוקן
            
        Returns:
            הפוזיציה שנמחקה או None
        """
        position = self.positions.pop(token_id, None)
        if position:
            self._save()
            logger.info(f"🗑️ Removed position: {token_id[:12]}...")
        return position
    
    def update_position(self, token_id: str, updates: Dict[str, Any]) -> bool:
        """
        מעדכן פוזיציה קיימת.
        
        Args:
            token_id: מזהה טוקן
            updates: שדות לעדכון
            
        Returns:
            True אם עודכן בהצלחה
        """
        if token_id in self.positions:
            self.positions[token_id].update(updates)
            self._save()
            return True
        return False
    
    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """מחזיר את כל הפוזיציות הפתוחות."""
        return self.positions.copy()
    
    def count(self) -> int:
        """מחזיר מספר הפוזיציות הפתוחות."""
        return len(self.positions)
    
    def clear_all(self) -> int:
        """
        מוחק את כל הפוזיציות (שימוש זהיר!).
        
        Returns:
            מספר הפוזיציות שנמחקו
        """
        count = len(self.positions)
        self.positions.clear()
        self._save()
        logger.warning(f"🗑️ Cleared all {count} positions")
        return count
    
    def get_positions_by_strategy(self, strategy_name: str) -> Dict[str, Dict[str, Any]]:
        """
        מחזיר פוזיציות של אסטרטגיה מסוימת.
        
        Args:
            strategy_name: שם האסטרטגיה
            
        Returns:
            פוזיציות שמשויכות לאסטרטגיה
        """
        return {
            token_id: pos 
            for token_id, pos in self.positions.items()
            if pos.get('strategy_name') == strategy_name
        }
