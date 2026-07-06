import logging
from logging.handlers import RotatingFileHandler
import os


class ColorFormatter(logging.Formatter):
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    def format(self, record):
        log_message = super().format(record)

        timestamp = f"[{self.formatTime(record)}]"
        filename = f"[{record.filename}:{record.lineno}]"

        colored_timestamp = f"{self.GREEN}{timestamp}{self.RESET}"
        colored_filename = f"{self.CYAN}{filename}{self.RESET}"

        log_message = log_message.replace(timestamp, colored_timestamp)
        log_message = log_message.replace(filename, colored_filename)

        return log_message


def get_logger(name="app_logger", log_file="logs/app.log", level=logging.INFO):
    os.makedirs("logs", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # ✅ Prevent duplicate handlers
    if not logger.handlers:

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s"
        )

        color_formatter = ColorFormatter(
            "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s"
        )

        # 📁 File handler (no color)
        # file_handler = RotatingFileHandler(
        #     log_file, maxBytes=5 * 1024 * 1024, backupCount=3, delay=True
        # )
        # file_handler.setFormatter(formatter)

        # 🖥 Console handler (colored)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(color_formatter)

        # logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger