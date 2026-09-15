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


def get_logger(name="app_logger", log_file="app.log", level=logging.INFO):
    # Vercel and most serverless platforms have a read-only filesystem.
    # Only /tmp is writable. We use /tmp/logs for file logging when possible,
    # and fall back gracefully to console-only if even that fails.
    log_dir = os.environ.get("LOG_DIR", "/tmp/logs")

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, log_file)
        file_logging_enabled = True
    except OSError:
        # Read-only filesystem (e.g. Vercel /var/task) — console only
        file_logging_enabled = False

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Prevent duplicate handlers on repeated calls
    if not logger.handlers:
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s"
        )
        color_formatter = ColorFormatter(
            "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s"
        )

        # Console handler (always active — works on Vercel)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(color_formatter)
        logger.addHandler(console_handler)

        # Optional rotating file handler (only when filesystem is writable)
        if file_logging_enabled:
            try:
                file_handler = RotatingFileHandler(
                    log_file_path,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=3,
                    delay=True,
                )
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except OSError:
                pass  # silently skip file logging if not writable

    return logger