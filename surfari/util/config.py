import json
import os

# Load global config at module level

PROJECT_ROOT = os.getenv('PROJECT_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config_path = os.path.join(PROJECT_ROOT, "util", "config_dev.json")
if not os.path.exists(config_path):
    config_path = os.path.join(PROJECT_ROOT, "util", "config.json")

with open(config_path) as f:
    CONFIG = json.load(f)
    CONFIG['PROJECT_ROOT'] = PROJECT_ROOT

logs_folder_path = os.path.join(PROJECT_ROOT, CONFIG["app"].get("project_log_folder", "logs"))
if not os.path.exists(logs_folder_path):
    os.makedirs(logs_folder_path)
    
download_folder_path = os.path.join(PROJECT_ROOT, CONFIG["app"].get("download_folder", "downloads"))
if not os.path.exists(download_folder_path):
    os.makedirs(download_folder_path)
        
debug_files_folder_path = os.path.join(logs_folder_path, CONFIG["app"].get("debug_files_folder", "debugfiles"))
if not os.path.exists(debug_files_folder_path):
    os.makedirs(debug_files_folder_path)
    
screenshot_folder_path = os.path.join(logs_folder_path, CONFIG["app"].get("screenshot_folder", "screenshots"))
if not os.path.exists(screenshot_folder_path):
    os.makedirs(screenshot_folder_path)
    

