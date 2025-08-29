import json
import re
import copy
import time
import os
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import httpx
from playwright.async_api import Error, Page, BrowserContext, Response
from urllib.parse import urlparse, unquote
from datetime import datetime
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger
import surfari.util.playwright_util as playwright_util
import surfari.view.text_layouter as text_layouter
from surfari.util.cdp_browser import ChromiumManager
from surfari.security.gmail_otp_fetcher import GmailOTPClientAsync
from surfari.security.site_credential_manager import SiteCredentialManager
from surfari.view.full_text_extractor import WebPageTextExtractor
from surfari.agents import BaseAgent
from surfari.agents.navigation_agent._record_and_replay import RecordReplayManager
from surfari.model.mcp.tool_registry import MCPToolRegistry
from surfari.model.tool_executor import execute_tool_calls
from surfari.agents.navigation_agent._value_resolver import (
    resolve_missing_value_in_llm_response,
    extract_steps,
    create_resolver_from_config
)
from surfari.agents.navigation_agent._typing import (
    ChatMessage,
    LLMActionStep,
    LLMResponse,
    LocatorActionResult,
)   
from surfari.agents.navigation_agent._prompts import (
    NAVIGATION_AGENT_SYSTEM_PROMPT,
    URL_RESOLUTION_SYSTEM_PROMPT,
    REVIEW_SUCCESS_SYSTEM_PROMPT,
    REVIEW_USER_DELEGATION_SYSTEM_PROMPT,    
    NAVIGATION_USER_PROMPT,
    SINGLE_ACTION_EXAMPLE_PART,
    MULTI_ACTION_EXAMPLE_PART,
    AGENT_DELEGATION_PROMPT_PART,
    BASE_TOOL_CALL_PROMPT_PART,
)

logger = surfari_logger.getLogger(__name__)

async def _validate_url(url: str) -> str:
    """Return the same URL if valid and reachable, else empty string."""
    logger.info(f"Validating LLM Suggested URL: {url}")
    if not url or not urlparse(url).scheme.startswith("http"):
        return ""

    async with httpx.AsyncClient(follow_redirects=True, timeout=5) as client:
        DEFAULT_HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        try:
            resp = await client.head(url, headers=DEFAULT_HEADERS)
            if resp.status_code < 400 or resp.status_code in (405, 429):
                return url
            # Retry with GET for servers that don't support HEAD
            resp = await client.get(url, headers=DEFAULT_HEADERS)
            if resp.status_code < 400 or resp.status_code in (405, 429):
                return url
        except httpx.RequestError:
            return ""
    return ""


class NavigationAgent(BaseAgent):
    def __init__(
        self,
        model: Optional[str] = None,
        site_id: int = 9999,
        site_name: str = "Unknown Site",
        url: Optional[str] = None,
        name: Optional[str] = None,
        enable_data_masking: bool = True,
        multi_action_per_turn: bool = False,
        record_and_replay: bool = False,
        rr_use_parameterization: bool = True,
        use_screenshot: bool = False,
        save_screenshot: bool = False,
        tools: List[Callable[..., Any]] = [],
        mcp_tool_registry: Optional[MCPToolRegistry] = None,
        agent_delegation_site_list: List[Dict[str, Any]] = None,
    ) -> None:
        name = name if name else f"NavigationAgent-{site_name}"
        # look up by site_name fuzzy match
        self.site_name: str = site_name
        if self.site_name != "Unknown Site":
            scm = SiteCredentialManager()
            site_info = scm.find_site_info_by_name(site_name)
            if site_info:
                url = site_info.get("url")
                site_id = site_info.get("site_id")

        self.url: Optional[str] = url
        self.site_id: int = site_id
        self.web_page_text_extractor = WebPageTextExtractor()
        self.multi_action_per_turn: bool = multi_action_per_turn
        self.record_and_replay: bool = record_and_replay
        self.rr_use_parameterization: bool = rr_use_parameterization
        self.using_recording: bool = False
        self.use_screenshot: bool = use_screenshot
        self.save_screenshot: bool = save_screenshot
        self._native_tools = list(tools or [])
        self.tools = list(self._native_tools)  # will be replaced with merged list later
        self.mcp_tool_registry: MCPToolRegistry = mcp_tool_registry
        self.agent_delegation_site_list: List[Dict[str, Any]] = agent_delegation_site_list or []
        self.tabs: List[Page] = []
        self.pdf_file_detected = False
        
        super().__init__(model=model, site_id=site_id, name=name, enable_data_masking=enable_data_masking)

    async def _setup_download_listener(self, page: Page) -> None:
        async def handle_download(download) -> None:
            logger.debug(f"Download started: {download.suggested_filename}")
            # Wait for download to complete (path() blocks until done)
            logger.debug(f"Temporary path: {await download.path()}")

            # Save to custom location
            site_folder = os.path.join(config.download_folder_path, self.site_name.replace(" ", "_"))
            os.makedirs(site_folder, exist_ok=True)
            dest_path = os.path.join(site_folder, download.suggested_filename)            
            await download.save_as(dest_path)
            logger.debug(f"Download saved to: {dest_path}")

        def _derive_filename_from_url(url: str) -> str:
            """
            Derives the filename from the URL.
            - If the URL ends with .pdf (case-insensitive), use the filename from the URL.
            """
            if url.lower().endswith(".pdf"):
                # Extract the filename from the URL
                parsed = urlparse(url)
                base_name = os.path.basename(parsed.path)
                base_name = unquote(base_name)  # Decode URL-encoded sequences
                return base_name
            else:
                # Fallback to default naming logic
                pdf_filename = f"downloaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                return pdf_filename
            
        async def pdf_response_handler(response: Response) -> None:
            ctype = (response.headers.get("content-type") or "").lower()
            dispo = (response.headers.get("content-disposition") or "").lower()
            
            if "application/pdf" in ctype and "attachment" not in dispo:
                try:
                    content = await response.body()
                    # Skip false positives: check for PDF magic header
                    if not content.startswith(b"%PDF"):
                        logger.debug(f"Skipping non-PDF masquerading as PDF: {response.url}")
                        return
                    
                    self.pdf_file_detected = True
                    filename = _derive_filename_from_url(response.url)
                    site_folder = os.path.join(config.download_folder_path, self.site_name.replace(" ", "_"))
                    os.makedirs(site_folder, exist_ok=True)
                    dest_path = os.path.join(site_folder, filename)

                    with open(dest_path, "wb") as f:
                        f.write(content)
                    logger.debug(f"PDF saved to: {dest_path}")
                except Exception as e:
                    logger.error(f"Failed to save PDF from {response.url}: {e}")


        # Attach handlers
        page.on("download", handle_download)
        page.on("response", pdf_response_handler)

        
    async def _setup_popup_listener(self, page: Page) -> None:
        async def handle_popup(new_page: Page):
            try:
                # Switch to the new popup page
                self.tabs.append(new_page)
                await self._setup_download_listener(new_page)
                await self._setup_popup_listener(new_page)
                self.current_working_tab = new_page
                logger.info(f"Popup detected, appended newly opened new tab with URL: {new_page.url} and set up listeners.")
            except Exception as e:
                logger.error(f"Error handling popup: {e}")
        
        # Attach the handler
        page.on("popup", handle_popup)
        
    async def _merge_tools(self) -> None:
        merged = list(self._native_tools)
        if self.mcp_tool_registry:
            try:
                await self.mcp_tool_registry.refresh()
                mcp_funcs = self.mcp_tool_registry.as_python_proxy_tools()  # (your renamed method)
                merged.extend(mcp_funcs)
            except Exception as e:
                logger.warning(f"Skipping MCP tools (refresh failed): {e}")

        # De-dupe by tool_name (preferred) or __name__ fallback
        seen = set()
        deduped = []
        for fn in merged:
            name = getattr(fn, "tool_name", None) or getattr(fn, "__name__", None) or repr(fn)
            if name in seen:
                continue
            seen.add(name)
            deduped.append(fn)

        self.tools = deduped
        logger.debug("Merged tools: %s", [getattr(f, "tool_name", getattr(f, "__name__", None)) for f in self.tools])
        

    async def run(self, page: Page, task_goal: str = "View statements and tax forms") -> str:
        # Set up the download listener
        self.add_donot_mask_terms_from_string(task_goal)

        await self._merge_tools()  # now self.tools is the merged list

        # record_and_replay_manager pre-processing
        if self.record_and_replay:
            self.record_and_replay_manager = RecordReplayManager(
                task_description=task_goal,
                site_id=self.site_id,
                site_name=self.site_name,
                llm_client= self.llm_client,
                use_parameterization=self.rr_use_parameterization
            )
            self.using_recording = await self.record_and_replay_manager.attempt_load_recorded_chat_history(model=self.model)

        if self.site_name != "Unknown Site" and (not self.url):
            task_goal = f"{self.site_name}: {task_goal}"
            
        await self.resolve_url_for_task(task_goal)

        # url guard
        if not self.url:
            logger.error("No URL available to navigate to.")
            return "No URL available to navigate to."

        await page.goto(self.url, timeout=60000)
        logger.info("Before turns, setting up download and popup listeners...")
        await self._setup_download_listener(page)
        await self._setup_popup_listener(page)
        self.tabs = [page]  # start tab tracking at the initial page
        self.current_working_tab = page

        console_debug_log_enabled = config.CONFIG["app"].get("console_debug_log_enabled", False)
        if console_debug_log_enabled:
            page.on("console", lambda msg: logger.debug(f"Console message: {msg.type}: {msg.text}"))
            # await current_page.expose_function("pyLog", lambda *args: logger.debug(*args))

        self.chat_history: List[ChatMessage] = [{"role": "user", "content": "Task Goal: " + task_goal}]

        max_turns: int = int(config.CONFIG["app"].get("max_number_of_turns", 35))
        wait_time_heuristic: int = int(config.CONFIG["app"].get("wait_time_heuristic", -1))
        task_successful: bool = False
        answer: str = ""
        reasoning: str = ""
        total_errors: int = 0

        if self.multi_action_per_turn:
            navigation_agent_system_prompt = NAVIGATION_AGENT_SYSTEM_PROMPT.replace("__step_execution_example_part__", MULTI_ACTION_EXAMPLE_PART)
        else:
            navigation_agent_system_prompt = NAVIGATION_AGENT_SYSTEM_PROMPT.replace("__step_execution_example_part__", SINGLE_ACTION_EXAMPLE_PART)
            
        if self.tools:
            navigation_agent_system_prompt = navigation_agent_system_prompt.replace("__tool_calling_prompt_part__", BASE_TOOL_CALL_PROMPT_PART)
        else:
            navigation_agent_system_prompt = navigation_agent_system_prompt.replace("__tool_calling_prompt_part__", "")

        if self.agent_delegation_site_list:
            navigation_agent_system_prompt = navigation_agent_system_prompt.replace("__agent_delegation_prompt_part__", AGENT_DELEGATION_PROMPT_PART)
            navigation_agent_system_prompt = navigation_agent_system_prompt.replace("__agent_delegation_site_list__", json.dumps(self.agent_delegation_site_list))
        else:
            navigation_agent_system_prompt = navigation_agent_system_prompt.replace("__agent_delegation_prompt_part__", "")

        value_resolver = None
        if "value_resolver" in config.CONFIG and config.CONFIG["value_resolver"]:
            value_resolver = create_resolver_from_config(config.CONFIG["value_resolver"])
            logger.debug(f"Using value resolver: {value_resolver}")

        resolver_context = {"site_id": self.site_id, "site_name": self.site_name, "task_goal": task_goal}

        try:
            for turns in range(1, max_turns + 1):      
                # central place to switch tabs
                # other places are responsible for setting the current working tab, e.g. a new tab being opened, a tab being closed
                if page != self.current_working_tab:
                    page = self.current_working_tab                    
                    self.chat_history.append({"role": "user", "content": f"I switched to the tab with URL: {page.url}"})
                    logger.info(f"Switched to the tab with URL: {page.url}")
                    
                await playwright_util.wait_for_page_load_generic(page, post_load_timeout_ms=wait_time_heuristic)
                current_url = page.url
                resolver_context["current_url"] = current_url
                logger.info(f"Turn {turns}/{max_turns}, current URL: {current_url}")                
                page_layout: str = await self.generate_text_representation(page)

                llm_response_json: Optional[LLMResponse] = None
                if self.using_recording:
                    logger.info(f"Using recorded history for LLM response, turns={turns}")
                    llm_response_json = self.get_llm_response_json_from_recorded_history()
                    if llm_response_json and llm_response_json.get("step_execution") == "SUCCESS":
                        logger.info("Using recorded history: LLM response indicates success, finishing task with real LLM review.")
                        self.using_recording = False
                        self.record_and_replay_manager.recorded_chat_history = None
                        continue  # to next turn

                if not llm_response_json:
                    logger.info(f"Calling model in real time for LLM response, turns={turns}")

                    llm_response_json = await self.get_llm_response_json_real_time(
                        page=page,
                        system_prompt=navigation_agent_system_prompt,
                        user_prompt=NAVIGATION_USER_PROMPT.format(page_content=page_layout)
                    )
                    # attempt to switch back to use recorded history again after asking LLM to intervene
                    if self.record_and_replay and self.record_and_replay_manager.recorded_chat_history:
                        logger.debug(f"Switching back to recorded history after using LLM for a turn, turns={turns}")
                        self.using_recording = True

                if llm_response_json is None:
                    llm_response_json = {}

                # IMPORTANT: Do this before unmasking sensitive info because this is sent back to LLM as history
                self.chat_history.append({"role": "assistant", "content": json.dumps(llm_response_json or {})})

                llm_response_json = self.unmask_sensitive_info_in_json(llm_response_json)  # type: ignore[arg-type]

                if llm_response_json and "tool_calls" in llm_response_json:
                    logger.debug(f"LLM response contains tool calls, will execute the calls")
                    tool_call_timeout = int(config.CONFIG["app"].get("tool_call_timeout", 300))
                    t0 = time.perf_counter()
                    results = await execute_tool_calls(llm_response_json, tools=self.tools, timeout=tool_call_timeout)
                    logger.debug("execute_tool_calls took %.1f ms", (time.perf_counter() - t0) * 1000)
                    
                    # Append tool responses (one message per result is fine)
                    calls = llm_response_json["tool_calls"]
                    for call, tr in zip(calls, results["tool_results"]):  # keep order!
                        payload = tr["result"] if tr["ok"] else {"error": tr["error"]}
                        if tr["id"]:
                            self.chat_history.append({"role": "tool", "name": call["name"], "call_id": tr["id"], "content": json.dumps(payload)})
                        else:
                            self.chat_history.append({"role": "tool", "name": call["name"], "content": json.dumps(payload)})
                    continue

                step_execution: str = llm_response_json.get("step_execution", "SEQUENCE")  # type: ignore[assignment]
                reasoning: str = llm_response_json.get("reasoning", "No reasoning provided.")  # type: ignore[assignment]
                answer: str = llm_response_json.get("answer", "")  # type: ignore[assignment]
                show_reasoning_box_duration = config.CONFIG["app"].get("show_reasoning_box_duration", 2000)    

                if await self._handled_page_level_actions(page, step_execution, reasoning, show_reasoning_box_duration):
                    continue  # to next turn

                if step_execution == "SUCCESS":
                    if await self._verify_task_success_response(page, page_layout):
                        task_successful = True
                        break  # out of loop for task successful
                    continue  # to next turn
                steps: List[LLMActionStep] = []
                involve_user: bool = False

                if step_execution == "DELEGATE_TO_USER":
                    if await self._overwrite_delegate_to_user_response(page, page_layout):
                        continue  # to next turn
                    involve_user = True
                else:
                    llm_response_json = resolve_missing_value_in_llm_response(llm_response_json, value_resolver, context=resolver_context)
                    step_execution = llm_response_json.get("step_execution", "SEQUENCE")  # type: ignore[assignment]
                    if step_execution == "DELEGATE_TO_USER":
                        logger.debug("Trying to resolve missing value, we need to delegate to user: task has incomplete information")
                        reasoning = llm_response_json.get("reasoning", "No reasoning provided.")  # type: ignore[assignment]
                        involve_user = True
                    else:
                        steps = extract_steps(llm_response_json)  # type: ignore[assignment]

                if not involve_user:
                    if not steps or not isinstance(steps, list):
                        total_errors += 1
                        logger.info(f"No valid steps found in LLM response, incrementing error count to {total_errors}.")
                        continue  # to next turn
                    
                    if step_execution == "DELEGATE_TO_AGENT":
                        await self._handle_delegate_to_agent(steps)
                        continue  # to next turn

                if not involve_user:
                    try:
                        result, updated_steps = await self._check_steps_for_otp_and_solve(steps)
                        if result > 0:
                            logger.debug(f"Applied OTP to fill {result} steps")
                            steps = updated_steps
                    except Exception:
                        logger.exception("Error during OTP application; delegate for manual resolution.")
                        reasoning = "Please clear the second factor authentication manually."
                        involve_user = True

                if involve_user:
                    await playwright_util.inject_control_bar(page, message=reasoning)
                    resumed = await self.wait_for_user_resume(page)
                    if not resumed:
                        return "Timeout waiting for user to take actions."
                    self.chat_history.append({"role": "user", "content": "I have completed the required actions and reviewed/updated the information on the page.  You can move on to the next step."})
                    continue

                # If we reach here, we have valid steps to perform
                if await self._scroll_page_performed(page, steps, reasoning, show_reasoning_box_duration):
                    logger.debug("Page scroll performed, skipping locator resolution.")
                    continue  # to next turn

                for step_idx, step in enumerate(steps, start=1):
                    target = step.get("target", "")

                    locator, is_expandable_element = None, False
                    try:
                        locator, is_expandable_element = await self.get_locator_from_text(page, target)
                    except Exception as e:
                        logger.error(f"Error getting locator from text: {e}")
                        
                    if locator:
                        step["locator"] = locator
                        if is_expandable_element:
                            logger.debug(f"Found a Locator that is expandable: {locator}, skipping the rest")
                            step["is_expandable_element"] = True
                            break
                        continue  # proceed to the next step

                    if step_idx == 1 and self.using_recording:
                        locator, is_expandable_element = await self._retry_replay_get_locator_from_text(page, target)
                        if locator:
                            step["locator"] = locator
                            if is_expandable_element:
                                logger.debug(f"Replaying: Found the first locator that is expandable: {locator}, skipping the rest")
                                step["is_expandable_element"] = True
                                break
                            continue  # proceed to the next step

                    if step_idx == 1:
                        # First locator failed — hard fail
                        self._notify_llm_first_target_not_found(step)
                        total_errors += 1
                        logger.info(f"First locator failed to resolve: {step}, incrementing error count to {total_errors}.")
                    else:  # not the first step
                        logger.warning(f"Subsequent locator failed to resolve: {step}, skipping the rest")
                    break

                # Perform actions on the page after collecting all steps
                # Check if the first step has a locator set
                if steps and steps[0].get("locator"):
                    locator_actions: List[LocatorActionResult] = await playwright_util.take_actions(
                        page, steps, num_steps=len(steps), reasoning=reasoning
                    )
                    total_errors = self._process_locator_action_results(locator_actions, total_errors)

            answer = f"{str(reasoning)}: {str(answer)}" if answer else str(reasoning)
        except Exception:
            logger.exception("Error during navigation")
            answer = "Error occurred. Please check the logs for details."
        finally:
            logger.debug(f"Final chat history: {json.dumps(self.chat_history, indent=2)}")
            await self.insert_run_stats()
            # save task to db to replay later
            if self.record_and_replay and not self.using_recording:
                save_successful_task_only = bool(config.CONFIG["app"].get("save_successful_task_only", True))
                if not save_successful_task_only or task_successful:
                    logger.info("Saving new task to RecordReplayManager.")
                    self.record_and_replay_manager.recorded_chat_history = self.chat_history
                    self.record_and_replay_manager.recorded_history_variables = (
                        self.record_and_replay_manager.current_variables  # Set during parameterization step
                    )
                    self.record_and_replay_manager.save_recording()
            logger.info(f"Total errors: {total_errors}")
            logger.info(f"Final answer: {answer}")
            
            if config.CONFIG["app"].get("log_output", "file") == "file":
                print(f"Final answer: {answer}")

        return answer

    async def _handle_delegate_to_agent(self, steps: Union[Dict[str, Any], List[Dict[str, Any]]]) -> None:
        """Handle delegation to another navigation agent."""
        # Normalize to a list
        steps_list: List[Dict[str, Any]] = steps if isinstance(steps, list) else [steps]

        # Precompute a lookup for sites (case-insensitive)
        site_index = {
            (s.get("site_name", "") or "").strip().lower(): s
            for s in (self.agent_delegation_site_list or [])
        }

        for step in steps_list:
            target: Optional[str] = (step or {}).get("target")
            value: Optional[str]  = (step or {}).get("value")

            if not target or not value:
                logger.warning("Invalid delegation step; missing target or value.")
                self.chat_history.append({
                    "role": "user",
                    "content": "Invalid delegation step; missing target or value."
                })
                continue

            key = target.strip().lower()
            site = site_index.get(key)
            url = site.get("url") if site else None

            if not site or not url:
                logger.warning(f"Site not found for delegation: {target}")
                allowed = ", ".join(sorted(k for k in site_index.keys() if k))
                msg = f"Site not found for delegation: {target}. It must match one of the provided sites: {allowed or 'N/A'}"
                self.chat_history.append({"role": "user", "content": msg})
                continue

            logger.info(f"Delegating to {target} with value: {value}")
            # Reuse same browser context so cookies/session carry over
            manager = await ChromiumManager.get_instance(use_system_chrome=False)
            context: BrowserContext = manager.browser_context

            page: Optional[Page] = None
            try:
                page = await context.new_page()

                delegate_agent = NavigationAgent(
                    site_name=site.get("site_name") or target,
                    url=url,
                    model=self.model,
                    enable_data_masking=self.enable_data_masking,
                    multi_action_per_turn=self.multi_action_per_turn,
                    record_and_replay=self.record_and_replay,
                    tools=self.tools,
                )

                result = await delegate_agent.run(page, value)
                self.chat_history.append({
                    "role": "user",
                    "content": f"Delegated to {target}: {result}"
                })
            except Exception as e:
                logger.exception(f"Delegation to {target} failed: {e}")
                self.chat_history.append({
                    "role": "user",
                    "content": f"Delegation to {target} failed: {e}"
                })
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("Failed to close delegated page (ignored).")

    async def _retry_replay_get_locator_from_text(
        self, page: Page, target: str, max_retries: int = 3
    ) -> Tuple[Optional[Any], bool]:
        logger.debug("Replaying: first locator not found; checking recorded user actions.")

        recorded = self.get_user_response_json_from_recorded_history()
        if isinstance(recorded, list):
            recorded = recorded[0]

        if not isinstance(recorded, dict) or recorded.get("result") != "success":
            return None, False

        logger.debug("Recording shows success; retrying locator …")
        locator: Optional[Any] = None

        for attempt in range(1, max_retries + 1):
            await asyncio.sleep(1)
            await self.generate_text_representation(page)

            try:
                locator, is_expandable_element = await self.get_locator_from_text(page, target)                
                if locator:
                    logger.debug(f"Locator resolved on retry #{attempt}.")
                    return locator, is_expandable_element                
            except Exception as e:
                logger.error(f"Error getting locator from text: {e}")

        # still not found
        logger.error("Replaying: locator still missing after %s retries; disabling replay.", max_retries)
        self.using_recording = False
        return None, False

    def get_llm_response_json_from_recorded_history(self) -> Optional[LLMResponse]:
        """Get LLM response JSON from recorded history."""
        if not self.using_recording:
            logger.error("Error: not using recording, cannot get LLM response from recorded history.")
            return None
        if not self.record_and_replay_manager.recorded_chat_history:
            logger.warning("Recorded history has become empty")
            self.using_recording = False
            return None

        while self.record_and_replay_manager.recorded_chat_history:
            message = self.record_and_replay_manager.recorded_chat_history.pop(0)
            if message["role"] == "assistant":
                logger.debug(f"Replaying with recorded LLM response: {message.get('content', '')}")
                try:
                    parsed: LLMResponse = json.loads(message.get("content", "{}"))
                except json.JSONDecodeError:
                    return None
                return parsed
        # If no assistant message was found
        logger.warning("No assistant message found in recorded history")
        return None

    def get_user_response_json_from_recorded_history(self) -> Optional[Dict[str, Any]]:
        """Get next user response JSON from recorded history."""
        if not self.using_recording:
            logger.error("Error: not using recording, cannot get LLM response from recorded history.")
            return None
        if not self.record_and_replay_manager.recorded_chat_history:
            logger.warning("Recorded history has become empty")
            return None

        message = self.record_and_replay_manager.recorded_chat_history.pop(0)
        if message["role"] == "user":
            logger.debug(f"Replaying, checking recorded user response: {message.get('content', '')}")
            try:
                parsed: Dict[str, Any] = json.loads(message.get("content", "{}"))
            except json.JSONDecodeError:
                return None
            return parsed
        return None

    async def get_llm_response_json_real_time(self, page: Page, system_prompt: str, user_prompt: str, model: str = None, purpose: str = None) -> Union[LLMResponse, Dict[str, Any]]:
        use_screenshot = self.use_screenshot or config.CONFIG["app"].get("use_screenshot", False)
        save_screenshot = self.save_screenshot or config.CONFIG["app"].get("save_screenshot", False)
        screenshot_format = config.CONFIG["app"].get("screenshot_format", "jpeg")
        logger.info("calling LLM for navigation step with screenshot=%s, save_screenshot=%s", use_screenshot, save_screenshot)
        image_data: Optional[bytes] = None
        if use_screenshot or save_screenshot:
            logger.debug("Taking screenshot.......")
            screenshot_quality = config.CONFIG["app"].get("screenshot_quality", 30)
            screenshot_full_page = config.CONFIG["app"].get("screenshot_full_page", False)
            if save_screenshot:
                current_time = time.strftime("%H:%M:%S", time.localtime())
                image_data = await page.screenshot(path=os.path.join(config.screenshot_folder_path, f"{current_time}-site_id-{self.site_id}_screenshot.{screenshot_format}"), full_page=screenshot_full_page, type=screenshot_format, quality=screenshot_quality)
            else:
                image_data = await page.screenshot(full_page=screenshot_full_page, type=screenshot_format, quality=screenshot_quality)
            
            if not use_screenshot:
                image_data = None        
        
        if image_data:
            user_prompt += "\n[Screenshot of the page is also provided for reference]"


            
        return await self.llm_client.process_prompt_return_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            chat_history=self.chat_history,
            image_data=image_data,
            image_format=screenshot_format,
            tools=self.tools,
            model=model or self.model,
            purpose=purpose or self.name,
            site_id=self.site_id,
        )

    async def resolve_url_for_task(self, task_goal: str) -> None:
        if not self.url:
            logger.info(f"Resolving URL for task_goal={task_goal}")
            input_data = {
                "task_goal": task_goal,
            }
            system_prompt = URL_RESOLUTION_SYSTEM_PROMPT
            user_prompt = json.dumps(input_data)

            llm_response_json: Dict[str, Any] = await self.llm_client.process_prompt_return_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                purpose=f"ResolveURLForTask-{self.site_name}",
                site_id=self.site_id,
            )

            self.url = await _validate_url(llm_response_json.get("url", ""))

            if not self.url:
                logger.error("Failed to resolve or validate URL, using google.")
                self.url = "https://www.google.com"
            else:
                logger.info(f"Resolved URL: {self.url}")

    async def _review_navigation_execution(self, page: Page, system_prompt: Optional[str] = None, user_prompt: Optional[str] = None) -> Tuple[str, str]:
        reviewer_llm = config.CONFIG["app"].get("reviewer_model", self.model)
        logger.debug(f"Reviewing navigation execution with model: {reviewer_llm}")

        llm_response_json: Dict[str, Any] = await self.get_llm_response_json_real_time(
            page=page,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=reviewer_llm,
            purpose=f"ReviewNavigationExecution-{self.site_name}",
        )
        review_decision: str = llm_response_json.get("review_decision", "Goal Not Met")
        review_feedback: str = llm_response_json.get("review_feedback", "No feedback provided.")
        return review_decision, review_feedback

    async def wait_for_user_resume(self, page: Page) -> bool:
        logger.info("Delegated to human: User action is required to continue.")
        polling_times: int = int(config.CONFIG["app"].get("hil_polling_times", 60))

        while polling_times > 0:
            polling_times -= 1
            try:
                mode = await page.evaluate("window.surfariMode")
                if mode is None:
                    logger.debug("Automation mode disappeared, assuming user has taken action.")
                    return True
                if mode:
                    logger.debug("Automation manually re-enabled by the user.")
                    await playwright_util.remove_control_bar(page)
                    return True
            except Error as e:
                if "Execution context was destroyed" in str(e):
                    logger.debug("Page navigated — assuming automation should continue.")
                    await page.wait_for_load_state("domcontentloaded")
                    return True
                else:
                    raise

            if polling_times % 10 == 0:
                logger.debug(f"Waiting for user to take actions... {polling_times} seconds left.")
            await asyncio.sleep(1)

        logger.error("Timeout waiting for user to take actions. Exiting.")
        return False

    async def _check_steps_for_otp_and_solve(self, steps: List[LLMActionStep]) -> Tuple[int, List[LLMActionStep] | str]:
        digit_steps: List[Tuple[int, int]] = []
        otp_fill_indices: List[int] = []

        # Step 1: Scan for both types of OTP fill targets
        for i, step in enumerate(steps):
            if step.get("action") != "fill":
                continue

            target = step.get("target", "")
            value = step.get("value")

            if value == "OTP":
                otp_fill_indices.append(i)
            else:
                match = re.fullmatch(r"\{\_(\d+)\}", target)
                if match and value == "*":
                    # This is a digit-per-box OTP field
                    digit_index = int(match.group(1))
                    digit_steps.append((digit_index, i))

        if not otp_fill_indices and not digit_steps:
            return 0, steps  # No OTP-related patterns found

        # Step 2: Fetch OTP code once
        gmail_otp_fetcher = GmailOTPClientAsync()
        otp_code = await gmail_otp_fetcher.get_otp_code()
        if not otp_code:
            logger.debug("No OTP code fetched, unable to proceed. Returning failure.")
            return 0, "failure getting otp code"

        updated_steps = copy.deepcopy(steps)
        replacements = 0

        # Step 3: Replace full OTP (value == "OTP")
        for idx in otp_fill_indices:
            updated_steps[idx]["value"] = otp_code
            replacements += 1

        # Step 4: Replace digit-per-box steps
        if digit_steps:
            digit_steps.sort()
            expected = list(range(1, len(digit_steps) + 1))
            actual = [index for index, _ in digit_steps]
            if actual != expected:
                logger.debug("Invalid OTP digit field sequence. Skipping per-digit substitution.")
            elif len(otp_code) != len(digit_steps):
                logger.debug("OTP length mismatch for digit fields. Skipping per-digit substitution.")
            else:
                for (_digit_index, step_idx), digit in zip(digit_steps, otp_code):
                    step = updated_steps[step_idx]
                    if step.get("value") == "*":
                        step["value"] = digit
                        replacements += 1

        return replacements, updated_steps

    async def get_locator_from_text(self, page: Page, text: str) -> Tuple[Optional[Any], bool]:
        """Get locator info from text."""
        locator, is_expandable_element = await self.web_page_text_extractor.get_locator_from_text(page, text)  # type: ignore[assignment]
        return locator, bool(is_expandable_element)

    async def generate_text_representation(self, page: Page) -> str:
        logger.debug(f"Extracting info with text representation, site_id={self.site_id}")
        secrets_to_mask = self.get_secrets_to_mask()
        full_page_text, legend_dict = await self.web_page_text_extractor.get_full_text(page, secrets_to_mask=secrets_to_mask)
        if not full_page_text and not self.pdf_file_detected:
            logger.debug(f"Failed to extract text from page, site_id={self.site_id}, retrying after 5 seconds")
            await page.wait_for_timeout(5000)
            full_page_text, legend_dict = await self.web_page_text_extractor.get_full_text(page, secrets_to_mask=secrets_to_mask)

        await logger.log_text_to_file(self.site_id, full_page_text, self.name, "content")

        duplicate_texts = self.web_page_text_extractor.get_duplicate_texts()
        logger.trace(f"Duplicate texts: {duplicate_texts}")

        legend_str = self.web_page_text_extractor.filter_legend(legend_dict)

        if self.pdf_file_detected:
            self.pdf_file_detected = False
            if not full_page_text:
                full_page_text = """
                === Embedded PDF Viewer Detected ===
                This page is showing a PDF document inside Chrome’s built-in viewer.
                The PDF file has been downloaded successfully.
                You can safely close this tab.
                """
            else:
                full_page_text = text_layouter.rearrange_texts(full_page_text, additional_text=legend_str)
        else:
            full_page_text = text_layouter.rearrange_texts(full_page_text, additional_text=legend_str)

        await logger.log_text_to_file(self.site_id, full_page_text, self.name, "layout")

        # # Mask amounts with random values
        if self.enable_data_masking:
            full_page_text = self.mask_sensitive_info(full_page_text, donot_mask=duplicate_texts)
            await logger.log_text_to_file(self.site_id, full_page_text, self.name, "masked_layout")

        return full_page_text

    async def _handled_page_level_actions(self, page: Page, step_execution: str, reasoning: str = "", show_reasoning_box_duration: int = 2000) -> bool:
        """Handle page-level actions based on step_execution."""
        if step_execution == "BACK":
            await playwright_util.show_reasoning_box(page, locator_or_box=None, reasoning=reasoning, show_reasoning_box_duration=show_reasoning_box_duration)
            logger.info("BACK: Going back to the previous page.")
            await asyncio.sleep(show_reasoning_box_duration / 1000)
            await page.go_back(timeout=60000)
            self.chat_history.append({"role": "user", "content": "I went back to the previous page."})
            return True

        if step_execution == "DISMISS_MODAL":
            await playwright_util.show_reasoning_box(page, locator_or_box=None, reasoning=reasoning, show_reasoning_box_duration=show_reasoning_box_duration)
            logger.info("Dismissing modal.")
            await asyncio.sleep(show_reasoning_box_duration / 1000)
            await page.mouse.click(1, 1)
            self.chat_history.append({"role": "user", "content": "I dismissed the modal."})
            return True

        if step_execution == "WAIT":
            await playwright_util.show_reasoning_box(page, locator_or_box=None, reasoning=reasoning, show_reasoning_box_duration=show_reasoning_box_duration)
            logger.info("WAIT: page might still be loading.")
            retry_wait_time_seconds = 2000 / 1000
            await asyncio.sleep(retry_wait_time_seconds)
            self.chat_history.append(
                {"role": "user", "content": f"I waited {retry_wait_time_seconds:.2f} more seconds for the page to load."}
            )
            return True

        if step_execution == "CLOSE_CURRENT_TAB":
            await playwright_util.show_reasoning_box(page, locator_or_box=None, reasoning=reasoning, show_reasoning_box_duration=show_reasoning_box_duration)
            logger.info("Closing current tab.")
            await asyncio.sleep(show_reasoning_box_duration / 1000)
            self.tabs.remove(page)
            self.current_working_tab = self.tabs[-1] if self.tabs else None
            await page.close()
            self.chat_history.append({"role": "user", "content": "I closed the tab."})
            return True

        return False  # No page-level action handled

    async def _verify_task_success_response(self, page: Page, page_layout: str) -> bool:
        """Handle task success response."""
        review_decision, review_feedback = await self._review_navigation_execution(
            page=page,
            system_prompt=REVIEW_SUCCESS_SYSTEM_PROMPT,
            user_prompt=NAVIGATION_USER_PROMPT.format(page_content=page_layout),
        )
        if review_decision == "Goal Met":
            logger.info("SUCCESS: After review, task has been completed successfully.")
            return True

        logger.info("Goal Not Met: After review, task has not been completed successfully.")
        self.chat_history.append({"role": "user", "content": "After review, the goal has not been met: " + review_feedback})
        return False

    async def _overwrite_delegate_to_user_response(self, page: Page, page_layout: str) -> bool:
        review_decision, review_feedback = await self._review_navigation_execution(
            page=page,
            system_prompt=REVIEW_USER_DELEGATION_SYSTEM_PROMPT,
            user_prompt=NAVIGATION_USER_PROMPT.format(page_content=page_layout),
        )
        if review_decision == "Suggestion":
            logger.info("DELEGATE_TO_USER: After review, a suggestion is provided instead of delegating to user.")
            self.chat_history.append(
                {"role": "user", "content": "After review, instead of delegating to user, here is a suggestion: " + review_feedback}
            )
            return True  # Do not delegate, continue with suggestion
        logger.info("DELEGATE_TO_USER: Replaying mode or review confirmed user action is indeed required to continue.")
        return False

    def _notify_llm_first_target_not_found(self, step: LLMActionStep) -> None:
        """Notify LLM that the first target was not found."""
        orig_target = step.get("orig_target", "")
        if "orig_value" in step:
            step["value"] = step["orig_value"]  # type: ignore[assignment]
            del step["orig_value"]

        if "orig_target" in step:
            step["target"] = step["orig_target"]  # type: ignore[assignment]
            del step["orig_target"]

        if not orig_target.startswith("[") and not orig_target.startswith("{") and not any(symbol in orig_target for symbol in ["☐", "✅", "🔘", "🟢"]):
            step["result"] = f"Error: I can not interact with {orig_target}. An interactable element must start with [ or {{ or is a radio button or checkbox."
        else:
            step["result"] = f"Error: I can not interact with {orig_target}. Do you see the EXACT target in the page? Please double check and make sure correct [ or {{ are used"

        self.chat_history.append({"role": "user", "content": f"{json.dumps(step)}"})

    def _process_locator_action_results(self, locator_actions: List[LocatorActionResult], total_errors: int = 0) -> int:
        for locator_action in locator_actions:
            # remove the locator from the action
            if "locator" in locator_action:
                del locator_action["locator"]
            if "orig_value" in locator_action:
                locator_action["value"] = locator_action["orig_value"]  # type: ignore[assignment]
                del locator_action["orig_value"]
            elif "value" in locator_action:  # just be careful
                logger.info("Removing value from locator_action to be sure")
                del locator_action["value"]

            if "orig_target" in locator_action:
                locator_action["target"] = locator_action["orig_target"]  # type: ignore[assignment]
                del locator_action["orig_target"]
            elif "target" in locator_action:  # just be careful
                logger.info("Removing target from locator_action to be sure")
                del locator_action["target"]

            if "result" in locator_action:
                result = locator_action["result"] or ""
                if len(result) > 200:
                    result = result[:200] + "..."
                locator_action["result"] = result
                if "Error:" in result:
                    total_errors += 1
                    logger.error(f"Locator action resulted in error: {result}, incrementing error count to {total_errors}.")
        self.chat_history.append({"role": "user", "content": f"{json.dumps(locator_actions)}"})
        return total_errors

    async def _scroll_page_performed(self, page: Page, steps: List[LLMActionStep], reasoning: str = "", show_reasoning_box_duration: int = 2000) -> bool:
        for step in steps:
            if step.get("action") == "scroll" and step.get("target") == "page":
                await playwright_util.show_reasoning_box(page, locator_or_box=None, reasoning=reasoning, show_reasoning_box_duration=show_reasoning_box_duration)
                # Special case for scrolling
                direction = step.get("value")
                if direction == "up":
                    scrolled = await playwright_util.scroll_main_scrollable(page, to_top=True)
                elif direction == "down":
                    scrolled = await playwright_util.scroll_main_scrollable(page)
                scroll_result = f"Scroll {direction} successful" if scrolled else f"Warning: no more content to scroll {direction}."
                self.chat_history.append({"role": "user", "content": scroll_result})
                return True
        return False