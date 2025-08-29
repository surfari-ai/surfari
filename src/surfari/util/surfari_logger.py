import logging
import sys
import os
import time
import surfari.util.config as config

# Define a TRACE level at 5 (lower than DEBUG)
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.TRACE = TRACE_LEVEL

def trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE_LEVEL):
        self._log(TRACE_LEVEL, message, args, **kwargs)
        
SENSITIVE_LEVEL = 1
logging.addLevelName(SENSITIVE_LEVEL, "SENSITIVE")
logging.SENSITIVE = SENSITIVE_LEVEL

def sensitive(self, message, *args, **kwargs):
    if self.isEnabledFor(SENSITIVE_LEVEL):
        self._log(SENSITIVE_LEVEL, message, args, **kwargs)

# Add the new level to the logger class
logging.Logger.trace = trace
logging.Logger.sensitive = sensitive

log_level = config.CONFIG["app"].get("log_level", logging.DEBUG)

log_output = config.CONFIG["app"].get("log_output", "stdout").lower()
if log_output == "file":
    log_destination = {
        "filename": os.path.join(config.logs_folder_path, "app.log")
    }
else:  # default: stdout
    log_destination = {"stream": sys.stdout}

logging.basicConfig(
    **log_destination,
    format="%(asctime)s - %(levelname)s - %(name)s  -  %(funcName)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=log_level,
)

def getLogger(name):
    logger = logging.getLogger(name)
    logger.log_text_to_file = log_text_to_file
    return logger

async def log_text_to_file(site_id, text, *args):
    logger = getLogger(__name__)
    if not logger.isEnabledFor(logging.SENSITIVE):
        return
    current_time = time.strftime("%H:%M:%S", time.localtime())
    arg0 = args[0] if len(args) > 0 else "navigation"
    arg1 = args[1] if len(args) > 1 else ""
    filename = f"{current_time}_site_id_{site_id}_{arg0}_{arg1}.txt"
    filename = filename.replace(" ", "_").replace(":", "_").replace("/", "_")
    filename = os.path.join(config.debug_files_folder_path, filename)
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logger.debug(f"An error occurred while saving text: {e}")
