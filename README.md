# Surfari

**Surfari** is a modular, LLM‑powered browser automation framework built on [Playwright](https://playwright.dev/).  
It enables secure, scriptable, and intelligent interactions with websites — perfect for data extraction, automated workflows, and AI‑assisted navigation.

---

## ✨ Features

- **LLM‑Driven Automation**  
  Supports OpenAI, Anthropic Claude, Google Gemini, Ollama, and more.
- **Secure Credential Management**  
  - macOS / Windows: Stores encryption key in system keyring  
  - Linux: Stores encryption key in `~/.surfari/key_string` (chmod 600)  
  - Credentials stored in SQLite with Fernet encryption
- **Modular Agents**  
  Write and load custom Agents for site‑specific or generic automation tasks.
- **Cross‑Platform**  
  Works on macOS, Linux, and Windows.
- **Playwright‑Powered**  
  High‑fidelity browser automation with installed Chrome or bundled Chromium.
- **Non‑Python Assets Bundled**  
  Ships with necessary JSON, JS, and config files.

---

## 📦 Installation

```bash
pip install surfari
```

Or from source:

```bash
git clone https://github.com/yonghuigit/surfari.git
cd surfari
pip install .
```

---

## 🚀 Quick Start

Below is an example of running Surfari’s `NavigationAgent` with a Playwright‑powered Chromium browser to complete an automated browsing task.

```python
from surfari.cdp_browser import ChromiumManager
from surfari.surfari_logger import getLogger
from surfari.agents.navigation_agent import NavigationAgent
import asyncio

logger = getLogger(__name__)

async def test_navigation_agent():
    site_name, task_goal = "cricket", "Download my March-April 2025 statements."

    # Launch Chromium (bundled or system Chrome)
    manager = await ChromiumManager.get_instance(use_system_chrome=False)
    page = await manager.get_new_page()
    # Create and run the Navigation Agent
    nav_agent = NavigationAgent(site_name=site_name, enable_data_masking=False)
    answer = await nav_agent.run(page, task_goal=task_goal)

    print("Final answer:", answer)
    await ChromiumManager.stop_instance()
    
if __name__ == "__main__":
    asyncio.run(test_navigation_agent())
```

---

## 🔐 Credential Storage

- **Linux**: Key stored in `~/.surfari/key_string` with permissions set to `rw-------` (chmod 600).  
- **macOS**: Key stored in `~/.surfari/key_string` or system keyring (via `keyring` library) if configured.
- **Windows**: Key stored in system keyring (via `keyring` library).  
- **Database**: Encrypted SQLite (`credentials` table) in your Surfari environment.

---

## 🛠 Development

Clone the repo and install in editable mode:

```bash
git clone https://github.com/yourusername/surfari.git
cd surfari
pip install -e .[dev]
```

Run Playwright browser install:

```bash
python -m playwright install chromium
```

---

## 📂 Project Structure

```
surfari/
  ├── __init__.py
  ├── util/db_service.py
  ├── util/config.py
  ├── security/site_credential_manager.py
  ├── agents/
  │    └── ...
  ├── view/html_to_text.js
  └── security/credentials.db
```

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/new-thing`)
3. Commit changes (`git commit -m "Add new thing"`)
4. Push to branch (`git push origin feature/new-thing`)
5. Open a Pull Request

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.
