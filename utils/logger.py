"""
Logging Configuration Module

מגדיר ומנהל לוגים עבור כל הבוטים.
"""
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
import colorlog


def setup_logging(
    log_level: str = "INFO",
    log_to_file: bool = True,
    log_file: str = None,
    max_file_size: int = 5 * 1024 * 1024,  # 5MB
    backup_count: int = 3,
    colored_console: bool = True
):
    """
    מגדיר מערכת לוגים.
    
    Args:
        log_level: רמת לוג (DEBUG, INFO, WARNING, ERROR)
        log_to_file: האם לשמור לוגים לקובץ
        log_file: נתיב לקובץ לוג (אם None, יצור אוטומטית)
        max_file_size: גודל מקסימלי לקובץ לוג בבייטים
        backup_count: מספר קבצי גיבוי
        colored_console: האם להשתמש בצבעים בקונסול
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler with colors
    if colored_console:
        console_formatter = colorlog.ColoredFormatter(
            '%(log_color)s%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%H:%M:%S',
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_white',
            }
        )
    else:
        console_formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%H:%M:%S'
        )
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # File handler with rotation
    if log_to_file:
        if log_file is None:
            log_dir = Path(__file__).parent.parent / "logs"
            log_dir.mkdir(exist_ok=True)
            log_file = log_dir / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        
        file_formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_file_size,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
        
        logging.info(f"📝 Logging to: {log_file}")
    
    # Reduce noise from external libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    logging.info("✅ Logging configured")


def get_logger(name: str) -> logging.Logger:
    """
    מחזיר logger עם השם הנתון.
    
    Args:
        name: שם ה-logger (בדרך כלל __name__)
        
    Returns:
        Logger object
    """
    return logging.getLogger(name)
