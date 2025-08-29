from datetime import datetime, timezone
from typing import List, Dict, Any, Union
import hashlib
import json
import asyncio

import surfari.util.db_service as db_service
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger
from surfari.model.structured_llm import LLMClient
from surfari.agents.navigation_agent._record_and_replay_prompt import PARAMETERIZATION_SYSTEM_PROMPT

logger = surfari_logger.getLogger(__name__)


class RecordReplayManager:
    def __init__(self, task_description: str = None, site_id: int = None, site_name: str = None, llm_client: LLMClient = None, use_parameterization: bool = True):
        self.init_db()

        # --- Stored from DB for record & replay ---
        self.recorded_chat_history = None
        self.recorded_history_variables = None
        self.task_description = task_description
        self.parameterized_task_desc = None
        self.task_hash = None
        self.parameterized_task_hash = None
        self.site_id = site_id
        self.site_name = site_name

        # --- For the current run ---
        self.current_variables = None
        self.llm_client = llm_client or LLMClient()
        self.use_parameterization = use_parameterization

    def init_db(self) -> None:
        """Ensure the replay_tasks table exists with all needed columns."""
        with db_service.get_db_connection_sync() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS replay_tasks (
                    task_id INTEGER NOT NULL UNIQUE PRIMARY KEY AUTOINCREMENT,
                    site_id INTEGER NOT NULL,
                    site_name TEXT NOT NULL,
                    task_hash TEXT NOT NULL,
                    task_description TEXT NOT NULL,
                    parameterized_task_hash TEXT,
                    parameterized_task_desc TEXT,
                    chat_history TEXT NOT NULL,
                    history_variables TEXT,
                    created_at DATETIME NOT NULL
                );
            """)
            conn.commit()

    def save_recording(self) -> int:
        """
        Save the current instance variables as a new replay task in the database.
        If a row already exists with the same site_name, task_hash, and parameterized_task_hash,
        it is deleted before inserting the new one to avoid duplicates.
        """
        if self.recorded_chat_history is None:
            raise ValueError("recorded_chat_history (chat_history) is required and cannot be None.")

        chat_history_str = (
            json.dumps(self.recorded_chat_history, ensure_ascii=False)
            if not isinstance(self.recorded_chat_history, str)
            else self.recorded_chat_history
        )

        history_variables_str = (
            json.dumps(self.recorded_history_variables, ensure_ascii=False)
            if self.recorded_history_variables is not None and not isinstance(self.recorded_history_variables, str)
            else self.recorded_history_variables
        )

        with db_service.get_db_connection_sync() as conn:
            # Delete any existing matching row
            conn.execute("""
                DELETE FROM replay_tasks
                WHERE site_name = ?
                AND task_hash = ?
                AND parameterized_task_hash = ?
            """, (
                self.site_name,
                self.task_hash,
                self.parameterized_task_hash,
            ))

            # Insert the new recording
            cur = conn.execute("""
                INSERT INTO replay_tasks (
                    site_id, site_name, task_hash, task_description,
                    parameterized_task_hash, parameterized_task_desc,
                    chat_history, history_variables, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.site_id,
                self.site_name,
                self.task_hash,
                self.task_description,
                self.parameterized_task_hash,
                self.parameterized_task_desc,
                chat_history_str,
                history_variables_str,
                datetime.now(timezone.utc).isoformat()
            ))

            conn.commit()
            return cur.lastrowid


    def fetch_task_recording(self, match_type: str = "exact") -> None:
        """
        Fetch the most recent replay task from the DB:
        1. Try exact task_hash match first.
        2. If not found, fall back to parameterized_task_hash.
        Populates instance variables with the first match found.
        """
        if not self.site_id:
            raise ValueError("site_id must be set before fetching tasks.")

        with db_service.get_db_connection_sync() as conn:
            row = None

            if match_type == "exact":
                if not self.task_hash:
                    self.task_hash = self.generate_task_hash(self.task_description)
                cur = conn.execute(
                    """
                    SELECT * FROM replay_tasks 
                    WHERE site_id = ? AND task_hash = ? 
                    ORDER BY task_id DESC 
                    LIMIT 1
                    """,
                    (self.site_id, self.task_hash)
                )
                row = cur.fetchone()

            elif match_type == "parameterized":
                if not self.parameterized_task_hash:
                    self.parameterized_task_hash = self.generate_task_hash(self.parameterized_task_desc)
                cur = conn.execute(
                    """
                    SELECT * FROM replay_tasks 
                    WHERE site_id = ? AND parameterized_task_hash = ? 
                    ORDER BY task_id DESC 
                    LIMIT 1
                    """,
                    (self.site_id, self.parameterized_task_hash)
                )
                row = cur.fetchone()

            if not row:
                return

            row = dict(row)

            try:
                row["chat_history"] = json.loads(row["chat_history"])
            except (json.JSONDecodeError, TypeError):
                row["chat_history"] = None

            try:
                if row.get("history_variables"):
                    row["history_variables"] = json.loads(row["history_variables"])
            except (json.JSONDecodeError, TypeError):
                row["history_variables"] = None

            self.recorded_chat_history = row.get("chat_history")
            self.recorded_history_variables = row.get("history_variables")
            if match_type == "exact":
                # found exact match, didn't need to parameterize so current variables are the same
                self.current_variables = self.recorded_history_variables
            self.parameterized_task_desc = row.get("parameterized_task_desc")
            self.parameterized_task_hash = row.get("parameterized_task_hash")
  
    async def parameterize_task_description(self, task_desc: str, model: str = None) -> Dict[str, Any]:
        """Use the structured LLM service to parameterize the task description."""
        if not task_desc:
            raise ValueError("Task description cannot be empty.")

        try:
            response = await self.llm_client.process_prompt_return_json(
                system_prompt=PARAMETERIZATION_SYSTEM_PROMPT,
                user_prompt=task_desc,
                model=model or config.CONFIG["app"]["llm_model"],
                purpose=f"TaskParameterization-{self.site_name or 'UnknownSite'}",
            )
            parameterized_task_desc = response.get("parameterized_task_desc")
            variables = response.get("variables")

            if not parameterized_task_desc and not variables:
                raise ValueError("Invalid response from LLM: missing parameterized_task_desc and variables.")

            return {
                "parameterized_task_desc": parameterized_task_desc,
                "variables": variables
            }
        except Exception as e:
            logger.error(f"Error during task parameterization: {e}")
            raise

    async def attempt_load_recorded_chat_history(self, model: str = None) -> bool:
        # 1. Try loading exact task history first
        self.fetch_task_recording(match_type="exact")
        if self.recorded_chat_history:
            logger.info("Loaded exact task history for replay.")
            return True
        
        # 2. If no exact match, parameterize task and try parameterized match
        if self.use_parameterization:
            if not model:
                model = config.CONFIG["app"]["llm_model"]
                
            logger.info("No exact match found. Parameterizing task with LLM...")
            param_result = await self.parameterize_task_description(self.task_description, model)
            if not param_result or param_result.get("parameterized_task_desc") == self.task_description:
                logger.info("Parameterization did not return a valid or different task description.")
                return False
            self.parameterized_task_desc = param_result["parameterized_task_desc"]
            self.current_variables = param_result["variables"]
            self.fetch_task_recording(match_type="parameterized")

        # 3. If found, replace stored variables with current variables
        if self.recorded_chat_history and self.recorded_history_variables and self.current_variables:
            logger.info("Loaded parameterized task history for replay. Replacing variables...")
            replaced_history = []
            for msg in self.recorded_chat_history:
                new_msg = json.loads(json.dumps(msg))  # deep copy
                if isinstance(new_msg.get("content"), str):
                    for k, old_val in self.recorded_history_variables.items():
                        new_val = self.current_variables.get(k)
                        if new_val is not None:
                            new_msg["content"] = new_msg["content"].replace(old_val, new_val)
                replaced_history.append(new_msg)
            self.recorded_chat_history = replaced_history
            return True

        # 4. If still no history, proceed with LLM
        if not self.recorded_chat_history:
            logger.info("No recorded history found. Will use LLM for fresh execution.") 
            return False    
        return True   

    @staticmethod
    def generate_task_hash(text: str) -> str:
        """Generate a stable, low-collision hash for a given string."""
        if text is None:
            text = ""
        normalized = text.strip().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()[:16]


# Example usage
if __name__ == "__main__":
    # Parameterize
    #task_description = "Find tickets from Boston to Seattle, leaving on August 10, 2025, 5 days later return date, direct flight or at most 1 stop, Find flights under $500."
    task_description = "no parameter necessary for 13 days"
    rr_manager = RecordReplayManager(task_description=task_description, site_id=9999, site_name="Unknown Site")    

    loaded_recording = asyncio.run(rr_manager.attempt_load_recorded_chat_history())
    
    print(f"Loaded recording: {loaded_recording}")
    # Save
    if not loaded_recording:
        rr_manager.recorded_chat_history = [
            {"role": "user", "content": "Find tickets..."}, 
            {"role": "assistant", "content": "Here are your five options..."}
        ]
        rr_manager.recorded_history_variables = rr_manager.current_variables
        new_id = rr_manager.save_recording()
        print(f"Inserted task_id={new_id}")
        asyncio.run(rr_manager.attempt_load_recorded_chat_history())
    print("Fetched history", json.dumps(rr_manager.recorded_chat_history, indent=2, ensure_ascii=False))
    print("Fetched history variables", json.dumps(rr_manager.recorded_history_variables, indent=2, ensure_ascii=False))
    print("Fetched current variables", json.dumps(rr_manager.current_variables, indent=2, ensure_ascii=False))
    print("Fetched task description:", rr_manager.task_description)
    print("Fetched parameterized task description:", rr_manager.parameterized_task_desc)
    print("Fetched task hash:", rr_manager.task_hash)
    print("Fetched parameterized task hash:", rr_manager.parameterized_task_hash)
    print("Fetched site ID:", rr_manager.site_id)
    print("Fetched site name:", rr_manager.site_name)

