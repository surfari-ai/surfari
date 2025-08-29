import argparse
import asyncio
import os
import csv
import sys  # Added for system exit

import surfari.util.config as config
from surfari.security.site_credential_manager import SiteCredentialManager
from surfari.util.cdp_browser import ChromiumManager
from surfari.agents.navigation_agent import NavigationAgent

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
):
    """Executes a single navigation task."""
    page = None
    if username and password and site_name and url:
        cred_manager = SiteCredentialManager()
        cred_manager.save_credentials(site_name=site_name, url=url, username=username, password=password)
        logger.info(f"[{site_name}] Credentials saved")

    manager = await ChromiumManager.get_instance(use_system_chrome=use_system_chrome)
    await asyncio.sleep(1)
    page = await manager.get_new_page()
    logger.info("Successfully got a new page")

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

    try:
        if page and not page.is_closed():
            await page.close()
    except Exception:
        pass


async def _worker(semaphore, kwargs):
    async with semaphore:
        await run_single_task(**kwargs)


async def run_batch_csv(csv_path, model, use_system_chrome, num_of_tabs):
    """Runs tasks from a CSV batch file with limited concurrency."""
    tasks = []
    semaphore = asyncio.Semaphore(num_of_tabs)

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

            enable_data_masking = row.get("enable_data_masking", "").strip().lower() in ("1", "true", "yes")
            multi_action_per_turn = row.get("multi_action_per_turn", "").strip().lower() in ("1", "true", "yes")
            record_and_replay = row.get("record_and_replay", "").strip().lower() in ("1", "true", "yes")
            rr_use_parameterization = row.get("rr_use_parameterization", "").strip().lower() in ("1", "true", "yes")
            use_screenshot = row.get("use_screenshot", "").strip().lower() in ("1", "true", "yes")
            save_screenshot = row.get("save_screenshot", "").strip().lower() in ("1", "true", "yes")

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
        if args.batch_file:
            if not os.path.isfile(args.batch_file):
                raise FileNotFoundError(f"Batch file not found: {args.batch_file}")
            await run_batch_csv(
                csv_path=args.batch_file,
                model=args.llm_model,
                use_system_chrome=args.use_system_chrome,
                num_of_tabs=args.num_of_tabs,
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
            )
    except Exception:
        logger.critical("Browser was forcefully closed. Stopping all processes.", exc_info=True)
        sys.exit(1)
    finally:
        await ChromiumManager.stop_instance()


if __name__ == "__main__":
    asyncio.run(main())
