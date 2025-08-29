# Surfari

**Surfari** is a modular, LLM-powered browser automation framework built on [Playwright](https://playwright.dev/).  
It enables secure, scriptable, and intelligent interactions with websites — perfect for data extraction, automated workflows, and AI-assisted navigation.

---

## ✨ Key Features

- **Automatic Record, Parameterize & Replay**  
  Surfari automatically records both the **exact sequence of LLM actions** *and* a **generalized, parameterized workflow** at the same time.  
  When running new tasks, Surfari plugs in the new values, replays the known workflow, and invokes the LLM only for review or recovery.  
  🔑 *Unique:* Replays are **fast and stable**, while parameterization makes them **flexible and reusable** for new but structurally similar tasks.

- **Self-Healing Replay**  
  If the recorded path fails due to layout drift, Surfari **seamlessly switches to real-time LLM reasoning** for that step, then resumes deterministic replay — combining stability with resilience.

- **Agent Delegation & Collaboration**  
  A Navigation Agent can **pause its own run and delegate subtasks** to another agent in a separate tab, then resume after the subtask completes.  
  Enables branching workflows, multi-agent collaboration, and parallel subtasks — like a team of agents cooperating inside one browser.

  ```mermaid
  sequenceDiagram
      participant A as Agent A (Main Task)
      participant B as Agent B (Delegated Subtask)
      participant H as Human-in-the-Loop

      A->>A: Start workflow
      A->>B: Delegate subtask<br/>(new tab/session)
      B->>B: Complete delegated task
      B-->>A: Return result, control resumes

      A->>A: Continue main workflow
      A-->>H: Request help if blocked<br/>(e.g. CAPTCHA, unknown field)
      H-->>A: Human resolves and resumes
      A->>A: Complete task and verify
  ```

- **Human-in-the-Loop Delegation**  
  When needed, Surfari can gracefully delegate control back to a **human operator**.  
  You complete the missing step in the live browser, then the agent continues the workflow automatically.

- **Stable, Text-Based UI Targets**  
  Instead of brittle XPaths or random IDs, Surfari uses semantic text annotations as selectors.  
  Enables highly stable record/replay with **stable, meaningful UI targets**.

- **Visual Decisioning (Action Box Overlay)**  
  Surfari can show the LLM’s reasoning and intended action in an **on-page action box overlay** next to the targeted element — making the agent’s decisions transparent, reviewable, and debuggable.

- **Configurable LLM Models (No Coding Required)**  
  Swap models like Google Gemini, OpenAI GPT, Anthropic Claude, **just by name** in config — no code changes needed.

- **Information Masking**  
  Automatically masks and unmasks account numbers, balances, and any digit-like strings, ensuring sensitive data remains protected during logs, prompts, and replays.

- **One or Multiple Actions Per Turn**  
  Choose between **step-by-step interactivity** (safer on dynamic sites) or **multi-action per turn** (faster on static or more predictable sites/workflows).

- **Custom Value Resolvers (Beyond Tool Calling)**  
  Unknown form values (inputs, select options, etc.) can be resolved automatically via direct APIs, retrieval-augmented search, or custom resolvers — **without requiring tool calls through the LLM**.

- **Tool Calling Integration**  
  - **Python Tools:** Easy integration via function calling.  
  - **MCP Tools:** Stdio or HTTP servers supported for external integrations.

- **Screenshots for Grounding**  
  Use screenshots as additional context for the LLM to ensure accurate reasoning (a tad slower)
  Supports **saving screenshots** for later review.

- **PDF Download Automation**  
  Downloads PDFs from both **direct download links** and **embedded Chrome PDF viewers**.

- **Batch Execution from CSV**  
  Run or schedule multiple tasks in one batch — each task can target a different site, goal, or credential set, with its own settings (e.g., single vs. multi-action per turn, record/replay on/off, masking enabled/disabled, screenshots enabled/disabled).

- **OTP Handling**  
  Automatically solves text-message OTPs by setting up SMS forwarding from your phone to your Gmail, then auto-filling them during login.

- **Google Tools Integration**  
  Out-of-the-box support for Gmail, Google Sheets, and Google Docs.

- **Deployment Options**  
  - **CLI Binaries:** Platform-specific executables — no Python setup required. Just download and run.  
  - **Docker Deployment:** Cloud mode with VNC-based browser streaming. Provision a VM and access the remote browser directly from your web browser.  

---

## 📦 Installation

```bash
pip install surfari
```

Or from source:

```bash
git clone https://github.com/surfari-ai/surfari.git
cd surfari
pip install .
```

---

## 🚀 Quick Start

```python
from surfari.cdp_browser import ChromiumManager
from surfari.surfari_logger import getLogger
from surfari.agents.navigation_agent import NavigationAgent
import asyncio

logger = getLogger(__name__)

async def test_navigation_agent():
    site_name, task_goal = "cricket", "Download my March-April 2025 statements."

    manager = await ChromiumManager.get_instance(use_system_chrome=False)
    page = await manager.get_new_page()

    nav_agent = NavigationAgent(site_name=site_name, enable_data_masking=False)
    answer = await nav_agent.run(page, task_goal=task_goal)

    print("Final answer:", answer)
    await ChromiumManager.stop_instance()
    
if __name__ == "__main__":
    asyncio.run(test_navigation_agent())
```

---

## 🔐 Credential Storage

- **Linux**: Key stored in `~/.surfari/key_string` with permissions `rw-------` (chmod 600).  
- **macOS**: Key stored in `~/.surfari/key_string` or system keyring (via `keyring` library).  
- **Windows**: Key stored in system keyring (via `keyring` library).  
- **Database**: Encrypted SQLite in your Surfari environment.

---

## 🛠 Development

```bash
git clone https://github.com/surfari-ai/surfari.git
cd surfari
pip install -e .[dev]
python -m playwright install chromium
```

---

## 📂 Project Structure

```
src/surfari/
  ├── __init__.py
  ├── util/config.json
  ├── security/site_credential_manager.py
  ├── agents/
  │    └── navigation_agent/
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
