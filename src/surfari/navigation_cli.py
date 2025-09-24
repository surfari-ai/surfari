import logging
logging.getLogger("asyncio").setLevel(logging.ERROR)

import argparse
import asyncio
import os
import csv
import sys
import json
import surfari.util.config as config
from surfari.security.site_credential_manager import SiteCredentialManager
from surfari.util.cdp_browser import BrowserManager
from surfari.agents.navigation_agent import NavigationAgent
from surfari.agents.navigation_agent._record_and_replay import RecordReplayManager

import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)


def parse_args():
    """Parses command line arguments for the runner."""
    parser = argparse.ArgumentParser(description="Run Surfari navigation task")
    parser.add_argument("-t", "--task_goal", help="The user task to accomplish (ignored if --batch_file is used)")
    parser.add_argument("-u", "--url", help="The URL to navigate to")
    parser.add_argument("-n", "--site_name", help="Optional site name")
    parser.add_argument("-b", "--use_system_chrome", action="store_true", help="Use system-installed Chrome")
    parser.add_argument("-l", "--llm_model", help="Override default LLM model (e.g. llama3:8b, gpt-4)")
    parser.add_argument("-s", "--enable_data_masking", action="store_true", help="Mask sensitive info")
    parser.add_argument("-m", "--multi_action_per_turn", action="store_true", help="Allow multiple actions per turn")
    parser.add_argument("-U", "--username", help="Username to save for the site (used with --password)")
    parser.add_argument("-P", "--password", help="Password to save for the site (used with --username)")
    parser.add_argument(
        "-f", "--batch_file",
        help="Path to CSV batch file with columns: "
             "task_goal,site_name,url,username,password,enable_data_masking,"
             "multi_action_per_turn,record_and_replay,rr_use_parameterization,use_screenshot,save_screenshot"
    )
    parser.add_argument("-R", "--record_and_replay", action="store_true", help="Enable record-and-replay mode")
    parser.add_argument("-p", "--rr_use_parameterization", action="store_true", help="Enable record-and-replay parameterization")
    parser.add_argument("-S", "--use_screenshot", action="store_true", help="Send screenshot to LLM too")
    parser.add_argument("-w", "--save_screenshot", action="store_true", help="Save screenshots to disk")
    parser.add_argument("-c", "--num_of_tabs", type=int, default=10, help="Number of concurrent tabs to open for batch tasks")
    parser.add_argument(
        "-a", "--cdp_endpoint", 
        help="Connect to an already running browser via CDP at this endpoint (e.g. http://127.0.0.1:9222). "
             "If omitted or set to 'auto', Surfari will launch its own browser."
    )

    # NEW: list recorded tasks without running the agent
    parser.add_argument(
        "--list_recorded_tasks",
        action="store_true",
        help="Print the list of recorded tasks as JSON and exit (does not run the agent)."
    )
    return parser.parse_args()


async def run_single_task(
    task_goal,
    site_name=None,
    url=None,
    model=None,
    enable_data_masking=False,
    multi_action_per_turn=False,
    username=None,
    password=None,
    use_system_chrome=False,
    record_and_replay=False,
    rr_use_parameterization=False,
    use_screenshot=False,
    save_screenshot=False,
    *,
    cdp_endpoint: str | None = None,
):
    """Executes a single navigation task."""
    page = None
    if username and password and site_name and url:
        cred_manager = SiteCredentialManager()
        cred_manager.save_credentials(site_name=site_name, url=url, username=username, password=password)
        logger.info(f"[{site_name}] Credentials saved")

    manager = await BrowserManager.get_instance(
        use_system_chrome=use_system_chrome,
        cdp_endpoint=cdp_endpoint,
    )
    await asyncio.sleep(1)

    attach_mode = bool((cdp_endpoint or "").strip()) and (cdp_endpoint.strip().lower() != "auto")
    if attach_mode:
        context = manager.browser_context
        logger.info("Attach mode → reusing existing BrowserContext, context has %d pages", len(context.pages))
        for returnedPage in context.pages:
            logger.debug("Page has URL: %s (closed=%s))", returnedPage.url, returnedPage.is_closed())
            if "localhost" in returnedPage.url and "5173" in returnedPage.url:
                logger.debug("Skipping localhost:5173 page, this is the main window UI")
                continue
            page = returnedPage
            if (returnedPage.url == "about:blank" or returnedPage.url == "chrome://newtab/") and not returnedPage.is_closed():
                logger.debug("Using this blank page")
                break
    else:
        page = await manager.get_new_page()
        
    if page:
        logger.info("Successfully got a new page")
    else:
        raise RuntimeError("Failed to get a new page")

    nav_agent = NavigationAgent(
        model=model,
        site_name=site_name,
        url=url,
        enable_data_masking=enable_data_masking,
        multi_action_per_turn=multi_action_per_turn,
        record_and_replay=record_and_replay,
        rr_use_parameterization=rr_use_parameterization,
        use_screenshot=use_screenshot,
        save_screenshot=save_screenshot,
    )
    result = await nav_agent.run(page, task_goal=task_goal)
    logger.info(f"[{site_name or url}] Final answer: {result}")

    if not attach_mode:
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass

async def _worker(semaphore, kwargs):
    async with semaphore:
        await run_single_task(**kwargs)


async def run_batch_csv(csv_path, model, use_system_chrome, num_of_tabs, *, cdp_endpoint: str | None = None):
    """Runs tasks from a CSV batch file with limited concurrency."""
    tasks = []
    semaphore = asyncio.Semaphore(num_of_tabs)

    def truthy(v: str) -> bool:
        return (v or "").strip().lower() in ("1", "true", "yes", "y", "t")

    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        for line_num, row in enumerate(reader, 1):
            run_flag = row.get("run", "").strip().lower()
            if run_flag not in ("1", "true", "yes"):
                continue

            task_goal = row.get("task_goal", "").strip() or None
            if not task_goal:
                logger.warning(f"[Line {line_num}] Skipped: task_goal is required")
                continue

            site_name = row.get("site_name", "").strip() or "Unknown Site"
            url = row.get("url", "").strip() or None
            username = row.get("username", "").strip() or None
            password = row.get("password", "").strip() or None

            enable_data_masking = truthy(row.get("enable_data_masking", ""))
            multi_action_per_turn = truthy(row.get("multi_action_per_turn", ""))
            record_and_replay = truthy(row.get("record_and_replay", ""))
            rr_use_parameterization = truthy(row.get("rr_use_parameterization", ""))
            use_screenshot = truthy(row.get("use_screenshot", ""))
            save_screenshot = truthy(row.get("save_screenshot", ""))

            kwargs = {
                "task_goal": task_goal,
                "site_name": site_name,
                "url": url,
                "model": model,
                "enable_data_masking": enable_data_masking,
                "multi_action_per_turn": multi_action_per_turn,
                "use_system_chrome": use_system_chrome,
                "record_and_replay": record_and_replay,
                "rr_use_parameterization": rr_use_parameterization,
                "use_screenshot": use_screenshot,
                "save_screenshot": save_screenshot,
                "cdp_endpoint": cdp_endpoint,  # applies to all rows
            }
            if username:
                kwargs["username"] = username
            if password:
                kwargs["password"] = password

            logger.info(f"[Line {line_num}] Task: {task_goal} | Site: {site_name} | URL: {url}")
            tasks.append(_worker(semaphore, kwargs))

    await asyncio.gather(*tasks)


async def main():
    args = parse_args()
    if args.llm_model:
        logger.info(f"Using custom LLM model: {args.llm_model}")

    try:
        # NEW: handle listing recorded tasks only (no agent/browser)
        if args.list_recorded_tasks:
            logging.disable(logging.CRITICAL + 1)  # blocks ALL logging calls
            mgr = RecordReplayManager()
            tasks = mgr.list_recorded_tasks()
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
            return

        if args.batch_file:
            if not os.path.isfile(args.batch_file):
                raise FileNotFoundError(f"Batch file not found: {args.batch_file}")
            await run_batch_csv(
                csv_path=args.batch_file,
                model=args.llm_model,
                use_system_chrome=args.use_system_chrome,
                num_of_tabs=args.num_of_tabs,
                cdp_endpoint=args.cdp_endpoint,
            )
        else:
            await run_single_task(
                task_goal=args.task_goal,
                site_name=args.site_name,
                url=args.url,
                model=args.llm_model,
                enable_data_masking=args.enable_data_masking,
                multi_action_per_turn=args.multi_action_per_turn,
                username=args.username,
                password=args.password,
                use_system_chrome=args.use_system_chrome,
                record_and_replay=args.record_and_replay,
                rr_use_parameterization=args.rr_use_parameterization,
                use_screenshot=args.use_screenshot,
                save_screenshot=args.save_screenshot,
                cdp_endpoint=args.cdp_endpoint,
            )
    except Exception:
        logger.critical("Browser was forcefully closed. Stopping all processes.", exc_info=True)
        sys.exit(1)
    finally:
        await BrowserManager.stop_instance()


if __name__ == "__main__":
    asyncio.run(main())
