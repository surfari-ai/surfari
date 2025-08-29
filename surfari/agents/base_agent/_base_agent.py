from random import uniform
import json
from typing import Dict, Any

import surfari.util.db_service as db_service
import surfari.util.config as config
from surfari.security.data_masker import NumericMasker
from surfari.security.site_credential_manager import SiteCredentialManager
from surfari.model.structured_llm import LLMClient

import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

class BaseAgent:
    def __init__(
        self,
        model=None,
        site_id=None,
        name=None,
        enable_data_masking=True,
    ):
        self.name = name if name else "BaseAgent"
        if model is None:
            llm_model_key = f"llm_model_{self.name}"
            llm_model = config.CONFIG["app"].get(llm_model_key)
            if not llm_model:
                llm_model = config.CONFIG["app"]["llm_model"]             
            model = llm_model
        self.model = model
        # Create a single AmountReplacer instance for reuse
        if enable_data_masking:
            self.enable_data_masking = True
            self.sensitive_handler = NumericMasker()
        else:
            self.enable_data_masking = False
            self.sensitive_handler = None
        self.site_id = site_id if site_id else 0
        self.chat_history = []
        self.llm_client = LLMClient()
        self.secret_manager = SiteCredentialManager()

    def get_secrets_to_mask(self) -> Dict[str, str]:
        secrets = self.secret_manager.load_site_with_secrets(self.site_id)

        username = secrets.get("UsernameAssistant")
        password = secrets.get("PasswordAssistant")
        if username:
            masked_username = f"U{"#" * int(len(username) * uniform(0.8, 1.2))}"
        if password:
            masked_password = f"P{"#" * int(len(password) * uniform(0.8, 1.2))}"
        
        if username and password:
            return {username: masked_username, password: masked_password}
        if username:
            return {username: masked_username}
        if password:
            return {password: masked_password}
        
        return {}

    def add_donot_mask_terms_from_string(self, in_string: str):
        """
        Tokenize the in_string and add digit-containing tokens to terms that shouldn't be masked.
        """
        if not self.sensitive_handler:
            return
        
        self.sensitive_handler.add_donot_mask_terms_from_string(in_string)
    
    def mask_sensitive_info(self, text: str, donot_mask=[]):
        if not self.sensitive_handler:
            return text
        return self.sensitive_handler.mask_sensitive_info(text, donot_mask=donot_mask)

    def unmask_sensitive_info(self, modified_text: str):
        if not self.sensitive_handler:
            return modified_text
        return self.sensitive_handler.unmask_sensitive_info(modified_text)
    
    def unmask_sensitive_info_in_json(self, json_obj):
        """
        Recursively processes a JSON object to unmask and normalize numbers in:
        - String values (e.g., "twenty five" → "25")
        - Numeric values (e.g., 25.0 → "25" or 25.5 → "25.5")
        
        Args:
            json_obj: Input JSON (dict, list, or primitive)
            
        Returns:
            A new object with numbers unmasked and normalized in strings/numbers.
        """
        if isinstance(json_obj, dict):
            new_obj = {}
            for key, value in json_obj.items():
                if key == "value":
                    new_obj["orig_value"] = value  # Save the original value first
                if key == "target":
                    new_obj["orig_target"] = value  # Save the original target first
                new_obj[key] = self.unmask_sensitive_info_in_json(value)
            return new_obj            
        elif isinstance(json_obj, list):
            return [self.unmask_sensitive_info_in_json(item) for item in json_obj]
        elif isinstance(json_obj, str):
            return self.unmask_sensitive_info(json_obj)
        elif isinstance(json_obj, (int, float)):
            # Normalize numbers (e.g., 25.0 → "25", 25.5 → "25.5")
            return str(int(json_obj)) if json_obj == int(json_obj) else str(json_obj)
        else:
            return json_obj  # Leave booleans/None unchanged
    
    def get_llm_stats(self) -> Dict[str, Any]:
        return self.llm_client.token_stats.get_token_stats()
    
    async def insert_run_stats(self):
        """Insert LLM stats into the database"""
        llm_stats = self.get_llm_stats()        
            
        model = config.CONFIG["app"]["llm_model"]
        model_input_cost = config.CONFIG["app"]["model_costs"]["input"]
        model_output_cost = config.CONFIG["app"]["model_costs"]["output"]

        # for each key in llm_stats, insert the value into the database
        with db_service.get_db_connection_sync() as conn:
            c = conn.cursor()        
            for agent_name, stats in llm_stats.items():
                prompt_token_count = stats.get("prompt_token_count", 0)
                candidates_token_count = stats.get("candidates_token_count", 0)
                prompt_token_cost = prompt_token_count * model_input_cost / 1_000_000.00
                candidates_token_cost = candidates_token_count * model_output_cost / 1_000_000.00
                total_llm_cost = prompt_token_cost + candidates_token_cost
                stats["prompt_token_cost"] = float(f"{prompt_token_cost:.3f}")
                stats["candidates_token_cost"] = float(f"{candidates_token_cost:.3f}")
                stats["total_llm_cost"] = float(f"{total_llm_cost:.3f}")
                logger.debug(f"Inserting stats for : {agent_name}, Stats: {json.dumps(stats, indent=2)}")
                
                # Insert the stats into the database
                c.execute("""INSERT INTO agent_run_stats (model, agent_name, prompt_token_count, candidates_token_count, prompt_token_cost, candidates_token_cost, total_llm_cost) 
                    VALUES (:model, :agent_name, :prompt_token_count, :candidates_token_count, :prompt_token_cost, :candidates_token_cost, :total_llm_cost)""",
                    {
                    "model": model,
                    "agent_name": agent_name,
                    "prompt_token_count": prompt_token_count,
                    "candidates_token_count": candidates_token_count,
                    "prompt_token_cost": prompt_token_cost,
                    "candidates_token_cost": candidates_token_cost,
                    "total_llm_cost": total_llm_cost
                    })
                    
            conn.commit()
            conn.close()    
    
            
                
                