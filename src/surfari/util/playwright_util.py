import re
import json
import asyncio
import os
import random
import time
from playwright.async_api import Locator
from typing import Dict, Tuple, Iterable

import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

COUNT_WITHOUT_REASONING = """
(() => {
  const total = document.querySelectorAll('*').length;
  const overlays = document.querySelectorAll('#__surfari_reasoning_box__').length;
  return total - overlays;
})();
"""

def remove_unescaped_control_characters(json_str):
    """
    Remove unescaped control characters (ASCII 0-31) from a JSON string.

    This function uses a regex to find control characters (characters in the range
    U+0000 to U+001F) that are not already escaped (i.e. not immediately preceded by a backslash)
    and removes them.

    Note: This may result in loss of some whitespace formatting in string values.
    """
    # The regex (?<!\\)[\x00-\x1F] matches any control character not preceded by a backslash.
    pattern = re.compile(r"(?<!\\)[\x00-\x1F]")
    return pattern.sub("", json_str)

async def wait_for_page_load_generic(page, timeout_ms=1000, post_load_timeout_ms=2000):
    """
    Waits for page to load, stabilize DOM, and final JavaScript execution.

    Args:
        page: Playwright page object.
        timeout_ms: Max time to wait in milliseconds.
        post_load_timeout_ms: Final buffer after load.
    """
    import asyncio
    import time
    start_time = time.time()
    try:
        # 1. Wait for 'load' event
        await page.wait_for_load_state("load", timeout=timeout_ms)
        logger.debug("Page load state 'load' reached.")

        # 2. Wait for DOM stabilization
        dom_stable = await wait_for_dom_stable(page, timeout=timeout_ms)
        logger.debug("Page load state DOM structure stabilized.")
        
        # 3. Wait for network idle
        network_max_inflight = config.CONFIG["app"].get("network_max_inflight", 1)
        network_idle_quiet_ms = config.CONFIG["app"].get("network_idle_quiet_ms", 200)
        network_idle_timeout_ms = config.CONFIG["app"].get("network_idle_timeout_ms", 10000)
        
        if network_idle_timeout_ms > 0:
            logger.debug("Waiting for network to become quiet...")
            await wait_for_network_quiet(page, max_inflight=network_max_inflight, quiet_ms=network_idle_quiet_ms, timeout_ms=network_idle_timeout_ms)
            logger.debug("Page load state 'networkidle' reached.")
            
    except Exception as e:
        logger.error(f"Page load state failed: {e}")

    if (post_load_timeout_ms > 0):
        await asyncio.sleep(post_load_timeout_ms / 1000)
        logger.debug(f"Page load state compensation timeout of {post_load_timeout_ms}ms completed.")
        
    total_time = time.time() - start_time
    logger.debug(f"Page load state total complete after {total_time:.2f} seconds.")

async def wait_for_dom_stable(page, timeout=3000):
    logger.debug("Polling DOM element count manually...")

    start_time = time.time()
    end_time = start_time + timeout / 1000

    prev_count = None

    while time.time() < end_time:
        try:
            count = await page.evaluate(COUNT_WITHOUT_REASONING)
            if prev_count is not None and count == prev_count:
                logger.debug(f"DOM element count stabilized at {count}")
                return True
            prev_count = count
        except Exception as e:
            logger.warning(f"Error while polling DOM elements: {e}")
        
        await asyncio.sleep(0.2)  # Poll every 200ms

    raise TimeoutError("DOM stabilization timed out.")

NOISY_URLS = re.compile(r"(google-analytics|gtm|segment|mixpanel|amplitude|hotjar|sentry|datadog|clarity)", re.I)

async def wait_for_network_quiet(
    page,
    *,
    max_inflight: int = 1,
    quiet_ms: int = 500,
    timeout_ms: int = 15000,
    ignore_patterns: Iterable[str] | None = None,
):
    """
    Wait until network is 'quiet':
      - non-noise requests stay <= max_inflight for quiet_ms continuously.
    Logs the elapsed time at INFO and raises TimeoutError on failure.
    """
    ignore_patterns = tuple((p.lower() for p in (ignore_patterns or ())))

    inflight = 0
    quiet_event = asyncio.Event()
    quiet_timer = None

    def is_noise(url: str) -> bool:
        u = (url or "").lower()
        if NOISY_URLS.search(u):
            return True
        # Ignore long-lived connections explicitly
        # (resource_type is available on the request object, but we only have URL here;
        # websockets/SSE often include these substrings—extend as needed)
        if any(k in u for k in ("/ws", "eventsource", "sse")):
            return True
        if any(p in u for p in ignore_patterns):
            return True
        return False

    loop = asyncio.get_event_loop()

    def cancel_timer():
        nonlocal quiet_timer
        if quiet_timer and not quiet_timer.cancelled():
            quiet_timer.cancel()
        quiet_timer = None

    def arm_timer_if_quiet():
        nonlocal quiet_timer
        if inflight <= max_inflight:
            cancel_timer()
            quiet_timer = loop.call_later(quiet_ms / 1000, quiet_event.set)
        else:
            quiet_event.clear()
            cancel_timer()

    def on_request(req):
        nonlocal inflight
        try:
            if not is_noise(req.url):
                inflight += 1
                quiet_event.clear()
                cancel_timer()
        except Exception:
            # Never let handlers crash
            pass

    def on_done(req):
        nonlocal inflight
        try:
            if not is_noise(req.url):
                inflight = max(0, inflight - 1)
                arm_timer_if_quiet()
        except Exception:
            pass

    # Attach listeners
    page.on("request", on_request)
    page.on("requestfinished", on_done)
    page.on("requestfailed", on_done)

    # Helper to detach listeners (supports both modern & old Playwright)
    def _remove_listener(event_name, handler):
        if hasattr(page, "remove_listener"):
            page.remove_listener(event_name, handler)
        elif hasattr(page, "off"):  # unlikely in Python, but safe fallback
            page.off(event_name, handler)
        else:
            # Last-resort: do nothing; GC will clean up when page closes
            pass

    start = time.monotonic()
    try:
        # Kick off the initial quiet timer based on current inflight (0)
        arm_timer_if_quiet()
        await asyncio.wait_for(quiet_event.wait(), timeout=timeout_ms / 1000)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.debug(f"wait_for_network_quiet: network quiet after {elapsed_ms} ms (<= {max_inflight} in-flight for {quiet_ms} ms)")
    except asyncio.TimeoutError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            f"wait_for_network_quiet: timeout after {elapsed_ms} ms "
            f"(in-flight={inflight}, threshold={max_inflight}, quiet_ms={quiet_ms}, timeout_ms={timeout_ms})"
        )
        raise TimeoutError("network-idle timeout") from e
    finally:
        cancel_timer()
        _remove_listener("request", on_request)
        _remove_listener("requestfinished", on_done)
        _remove_listener("requestfailed", on_done)


async def start_expansion_watch(control_locator):
    """
    Start a minimal DOM/ARIA watch in the locator's frame.
    - Records baseline DOM element count (top document only, minus __surfari_reasoning_box__).
    - Starts a MutationObserver just to flip popup/overlay flags when nodes appear.
    - Finds the nearest ARIA element (self -> closest ancestor -> container query)
      and records its baseline aria-expanded / aria-haspopup values.
    Returns immediately.
    """
    return await control_locator.evaluate("""
    (node) => {
      // Clean any previous run in this frame
      try { window.__surfariWatch?.cleanup?.(); } catch(_) {}

      const out = {
        domCountStart: 0,
        popup: false,
        overlay: false,
        startedAt: Date.now(),
        // ARIA baseline (filled below)
        ariaFound: false,
        ariaStrategy: null,
        ariaTag: null,
        ariaId: null,
        ariaClass: null,
        ariaExpandedBefore: null,
        ariaHaspopupBefore: null
      };

      // EXACT domElementCount the user provided
      const domElementCount = () => {
        try {
          return document.querySelectorAll('*').length
               - document.querySelectorAll('#__surfari_reasoning_box__').length;
        } catch(_) { return 0; }
      };

      // Detect popup/overlay on newly added nodes
      const mark = (n) => {
        if (!n || n.nodeType !== 1) return;
        const role = n.getAttribute?.('role') || '';
        if (role === 'dialog' || role === 'menu' || role === 'listbox') out.popup = true;
        const st = getComputedStyle(n);
        if (st.position === 'fixed' && parseFloat(st.opacity) > 0.01 &&
            (parseInt(st.width)||0) > 200 && (parseInt(st.height)||0) > 100) {
          out.overlay = true;
        }
      };

      // Minimal MutationObserver (top document only)
      let mo = null;
      try {
        mo = new MutationObserver(muts => {
          for (const m of muts) if (m.type === 'childList') {
            for (const n of m.addedNodes || []) mark(n);
          }
        });
        mo.observe(document.documentElement, { subtree: true, childList: true });
      } catch(_) {}

      // Find nearest ARIA element relative to control
      const norm = (v) => (v == null ? null : String(v).toLowerCase());
      const hasAria = (el) => el && (el.hasAttribute('aria-expanded') || el.hasAttribute('aria-haspopup'));

      let ariaTarget = null;
      let strategy = "self";
      if (hasAria(node)) {
        ariaTarget = node;
        strategy = "self";
      } else {
        ariaTarget = node.closest?.('[aria-expanded],[aria-haspopup]') || null;
        if (ariaTarget) {
          strategy = "closest-ancestor";
        } else {
          const container = node.closest?.('[id],[class]') || node.parentElement || document.body;
          ariaTarget = container.querySelector?.('[aria-expanded],[aria-haspopup]') || null;
          if (ariaTarget) strategy = "container-query";
        }
      }

      if (ariaTarget) {
        out.ariaFound = true;
        out.ariaStrategy = strategy;
        out.ariaTag = ariaTarget.tagName || null;
        out.ariaId = ariaTarget.id || null;
        out.ariaClass = ariaTarget.className || null;
        out.ariaExpandedBefore = norm(ariaTarget.getAttribute?.('aria-expanded'));
        out.ariaHaspopupBefore = norm(ariaTarget.getAttribute?.('aria-haspopup'));
      }

      out.domCountStart = domElementCount();

      window.__surfariWatch = {
        out,
        ariaTarget,
        cleanup: () => { try { mo && mo.disconnect(); } catch(_) {} }
      };

      // Return a tiny baseline (optional)
      return { started: true, baseline: out };
    }
    """)

async def finish_expansion_watch(control_locator):
    """
    Stop the watch started by start_expansion_watch(), take final snapshot, compare ARIA,
    and return {"safe": bool, "reason": str, "metrics": {...}}.
    - DOM counts & popup/overlay reflect only the top document (no iframes).
    - ARIA check looks at the same element found at start; we report if aria-expanded flipped
      from "false" to "true".
    """
    obs = await control_locator.evaluate("""
    () => {
      const sess = window.__surfariWatch;
      if (!sess || !sess.out) return { error: "no-session" };
      const out = sess.out;
      const ariaTarget = sess.ariaTarget || null;

      const domElementCount = () => {
        try {
          return document.querySelectorAll('*').length
               - document.querySelectorAll('#__surfari_reasoning_box__').length;
        } catch(_) { return 0; }
      };
      const norm = (v) => (v == null ? null : String(v).toLowerCase());

      const domCountEnd = domElementCount();
      const durationMs = Date.now() - (out.startedAt || Date.now());

      // ARIA after
      const stillThere = !!(ariaTarget && ariaTarget.isConnected);
      const ariaExpandedAfter = norm(stillThere ? ariaTarget.getAttribute('aria-expanded') : null);
      const ariaHaspopupAfter = norm(stillThere ? ariaTarget.getAttribute('aria-haspopup') : null);

      // Done; clean up and clear state
      try { sess.cleanup?.(); } catch(_) {}
      try { delete window.__surfariWatch; } catch(_) {}

      return {
        // DOM metrics
        domCountStart: out.domCountStart,
        domCountEnd,
        netDomDelta: domCountEnd - out.domCountStart,
        popup: !!out.popup,
        overlay: !!out.overlay,
        durationMs,

        // ARIA comparison
        ariaFound: !!out.ariaFound,
        ariaStrategy: out.ariaStrategy,
        ariaDetachedAfter: !!out.ariaFound && !stillThere,
        ariaTag: out.ariaTag, ariaId: out.ariaId, ariaClass: out.ariaClass,
        ariaExpandedBefore: out.ariaExpandedBefore,
        ariaExpandedAfter,
        ariaHaspopupBefore: out.ariaHaspopupBefore,
        ariaHaspopupAfter,

        // Booleans
        ariaChanged: out.ariaExpandedBefore !== ariaExpandedAfter,
        ariaFlippedFalseToTrue: (out.ariaExpandedBefore === "false" && ariaExpandedAfter === "true"),
      };
    }
    """)

    if obs and obs.get("error") == "no-session":
        return {"safe": False, "reason": "observer session missing (navigation or not started)", "metrics": {}}

    # Build metrics
    metrics = {
        # DOM
        "domCountStart":        int(obs.get("domCountStart", 0)),
        "domCountEnd":          int(obs.get("domCountEnd", 0)),
        "netDomDelta":          int(obs.get("netDomDelta", 0)),
        "popup":                bool(obs.get("popup", False)),
        "overlay":              bool(obs.get("overlay", False)),
        "durationMs":           int(obs.get("durationMs", 0)),

        # ARIA
        "ariaFound":            bool(obs.get("ariaFound", False)),
        "ariaStrategy":         obs.get("ariaStrategy"),
        "ariaDetachedAfter":    bool(obs.get("ariaDetachedAfter", False)),
        "ariaTag":              obs.get("ariaTag"),
        "ariaId":               obs.get("ariaId"),
        "ariaClass":            obs.get("ariaClass"),
        "ariaExpandedBefore":   obs.get("ariaExpandedBefore"),
        "ariaExpandedAfter":    obs.get("ariaExpandedAfter"),
        "ariaHaspopupBefore":   obs.get("ariaHaspopupBefore"),
        "ariaHaspopupAfter":    obs.get("ariaHaspopupAfter"),
        "ariaChanged":          bool(obs.get("ariaChanged", False)),
        "ariaFlippedFalseToTrue": bool(obs.get("ariaFlippedFalseToTrue", False)),
    }

    # Minimal evaluation (tune thresholds as desired)
    popup_opened   = metrics["popup"] or metrics["overlay"]
    big_dom_change = abs(metrics["netDomDelta"]) > 40
    aria_opened    = metrics["ariaFlippedFalseToTrue"]

    if popup_opened or big_dom_change or aria_opened:
        reasons = []
        if popup_opened:   reasons.append("popup/overlay added")
        if big_dom_change: reasons.append("large DOM change detected")
        if aria_opened:    reasons.append("aria-expanded changed from false to true")
        return {"safe": False, "reason": " / ".join(reasons), "metrics": metrics}

    return {"safe": True, "reason": "safe", "metrics": metrics}


async def take_actions(page, locator_actions, num_steps=1, reasoning=None) -> list[dict]:
    """
    Execute a series of actions on a page using Playwright.

    Args:
        locator_actions: a JSON string or object containing a list of locator actions to perform.
            Each locator action should be a dictionary with the following keys:
            - "action": The action to perform (e.g., "click", "fill", "select").
            - "locator": The locator string or locator object for the element to interact with.
            - "value": The value to fill or select (optional).

    Returns:
        a list of locator actions with their execution results.
    """    
    logger.debug(f"Number of steps to perform: {num_steps}")
    
    if isinstance(locator_actions, str):
        locator_actions = remove_unescaped_control_characters(locator_actions)
        locator_actions = json.loads(locator_actions)

    # if somehow action is not a list but a dict, convert it to a list
    if not isinstance(locator_actions, list):     
        locator_actions = [locator_actions]
        
    # extract the list of just locators from locator_actions
    locators = [action.get("locator") for action in locator_actions if "locator" in action]
    # try:
    #     await highlight_elements(page=page, elements=locators, color="red", duration=1000)
    # except Exception as e:
    #     logger.error(f"Error highlighting elements: {e}")
    
    skip_subsequent_actions = False
    for i, locator_action in enumerate(locator_actions, start=1):
        action_name = f"locator_action {i}"
        if skip_subsequent_actions:
            logger.debug(f"{action_name}: Skipping subsequent actions due to previous action.")
            locator_action["result"] = "Wait: The last successful action caused the page to show/hide elements. You need to re-evaluate based on the current page content."
            break
        locator_action_copy = dict(locator_action) # shallow copy for logging
        locator_action_copy.pop("value", None)  # Remove value if it exists for logging  
        logger.sensitive(f"Examining and performing {action_name}: {locator_action_copy}")

        is_expandable_element = locator_action.get("is_expandable_element", False)
        if is_expandable_element:
            skip_subsequent_actions = True
        
        action = locator_action.get("action")
        locator = locator_action.get("locator")
        value = locator_action.get("value")
        target = locator_action.get("target")
        if not action:
            logger.warning(f"{action_name}: No action provided. Skipping.")
            locator_action["result"] = "Error: No action provided"
            continue
        
        if not locator:
            logger.warning(f"{action_name}: No locator provided. Skipping.")
            locator_action["result"] = "Error: No locator provided"
            continue

        if action in ("fill", "select") and not value:
            logger.warning(f"{action_name}: No value provided for {action}.")
            locator_action["result"] = "Error: No value provided"
            continue

        try:
            if isinstance(locator, str):
                element = eval(locator)
            elif isinstance(locator, Locator):
                element = locator
            else:
                logger.warning(f"{action_name}: Invalid locator type. Skipping.")
                locator_action["result"] = "Error: Invalid locator type"
                continue
            
            element_count = await element.count()            
            if element_count == 0:
                logger.warning(f"{action_name}: Element not found. Skipping.")
                locator_action["result"] = "Error: Element not found"
                continue

            # If multiple elements found, look for the first visible one
            if element_count > 1:
                visible_element = None
                for i in range(element_count):
                    current_element = element.nth(i)
                    if await current_element.is_visible():
                        visible_element = current_element
                        break
                
                if visible_element:
                    element = visible_element
                else:
                    logger.info(f"{action_name}: Multiple elements found but none visible, using first element")
                    element = element.first()

        except Exception as e:
            logger.error(f"{action_name}: Error eval-ing locator: {e}")
            locator_action["result"] = f"Error: Invalid locator: {e}"
            continue

        element_disabled = await element.is_disabled()
        if element_disabled:
            logger.warning(f"{action_name}: Element is disabled. Skipping.")
            locator_action["result"] = "Error: Element is currently disabled. You should try something else"
            continue
        
        show_reasoning_box_duration = config.CONFIG["app"].get("show_reasoning_box_duration", 2000)    
        try:
            try:
                logger.debug(f"{action_name}: Attempting to scroll element into view and move mouse")
                await element.scroll_into_view_if_needed(timeout=2000)
                await element.wait_for(timeout=2000, state="visible")
                await move_mouse_to(element)
                if config.CONFIG["app"].get("show_reasoning_box", True) and reasoning:
                    await show_reasoning_box(page, element, reasoning, show_reasoning_box_duration)
                logger.debug("Successfully scrolled element into view, element is visible, mouse moved to element, and reasoning box shown")
            except Exception as e:
                logger.error(f"{action_name}: Will force after encountering error preparing for action: {e}")
                
            if action == "click":
                if True: # for now, always do this
                    try:
                        await element.click(timeout=2000, force=True)
                        logger.debug(f"{action_name}: Clicked element using Playwright click")
                    except Exception as e:
                        logger.error(f"{action_name}: Retry with direct JS evaluate after error force clicking element: {e}")
                        y_pos = await element.evaluate("el => el.getBoundingClientRect().top + window.scrollY", timeout=2000)
                        await page.evaluate(f"() => window.scrollTo(0, {int(y_pos) - 100})")
                        await element.evaluate("""
                        el => {
                            const event = new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            });
                            el.dispatchEvent(event);
                        }
                        """, timeout=2000)
                        logger.debug(f"{action_name}: Clicked element using JS evaluate")                        

            elif action == "fill":
                await element.click(timeout=2000, force=True)
                tag_name = await element.evaluate("el => el.tagName", timeout=2000)
                if tag_name and tag_name.lower() == "td":
                    # customization for handsontable                    
                    await element.dblclick(timeout=2000, force=True)
                    logger.debug(f"{action_name}: Double clicked td element to edit")
                    input_locator = page.locator('textarea.handsontableInput[data-hot-input]')
                    await input_locator.fill(value, timeout=2000, force=True)
                else:
                    await start_expansion_watch(element)
                    type = await element.evaluate("el => el.type", timeout=2000)    
                    if type and type.lower() == "number":
                        # If the input is a number, fill it with the value
                        await element.fill(value, timeout=2000, force=True)
                    else:
                        await element.clear(timeout=2000, force=True) 
                        await page.wait_for_timeout(300)  # Wait for .3 second before typing           
                        # For other types, use press_sequentially
                        # await element.fill(value, timeout=2000, force=True)
                        await element.press_sequentially(value, delay=50)
                    res = await finish_expansion_watch(element)
                    if not res["safe"]:
                        # Log or handle warning
                        logger.info(f"{action_name}: Filling {target} might have caused page to change: {res['reason']}. Metrics: {json.dumps(res['metrics'])}")
                        locator_action["result"] = f"Success with note: filling {target} caused the page layout to change, potentially to show matches or suggestions. If they appear, click to select the match."
                        break
                    else:
                        logger.debug(f"{action_name}: after filling in data: {json.dumps(res['metrics'])}")

            elif action == "select":
                await element.select_option(value, timeout=10000, force=True)
            elif action == "check":
                try:
                    await element.check(timeout=1000, force=True)
                except Exception as e:
                    await element.evaluate("""
                    el => {
                        let match = el.closest('mat-checkbox, [role="checkbox"], label, [role="radio"], input[type="checkbox"], input[type="radio"]');
                        // console.log('[closest check]', match);
                        if (match) match.click();
                    }
                    """)

            elif action == "uncheck":
                try:
                    await element.uncheck(timeout=1000, force=True)
                except Exception as e:
                    await element.evaluate("""
                    el => {
                        let match = el.closest('mat-checkbox, [role="checkbox"], label, [role="radio"], input[type="checkbox"], input[type="radio"]');
                        // console.log('[closest check]', match);
                        if (match) match.click();
                    }
                    """)                                        
            elif action == "dbclick":
                await element.dblclick(timeout=1000, force=True)
            else:
                logger.warning(f"{action_name}: Unsupported action: {action}. Skipping.")
                locator_action["result"] = f"Error: Unsupported action: {action}"
                continue

            locator_action["result"] = "success"
            await page.wait_for_timeout(show_reasoning_box_duration + 100)  # Wait for show_reasoning_box_duration after each action
        except Exception as e:
            logger.error(f"{action_name}: Error performing action: {e}")
            locator_action["result"] = f"Error: failed to perform action: {e}"
        
        if i == num_steps:
            break
      
    return locator_actions

async def hideOrShowWindow(context=None, page=None):
    run_in_background = (
        config.CONFIG["app"]["run_in_background"]
        or os.getenv("SURFARI_IN_BACKGROUND", "False").lower() == "true"
    )
    logger.debug(f"run_in_background: {run_in_background}")
    if not run_in_background:
        return    
     
    if context and page:
        logger.info("Minimizing window after launch with CDP")
        client = await context.new_cdp_session(page)
        window_info = await client.send("Browser.getWindowForTarget")
        window_id = window_info["windowId"]
        await client.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "windowState": "minimized",
                },
            },
        )

async def move_mouse_to(locator):
    if not await locator.is_visible():
        logger.error(f"Can't move mouse to locator as it is not visible: {locator}")
        return
    
    box = await locator.bounding_box(timeout=2000)
    if box:
        x = box["x"] + box["width"] * random.uniform(0, 2) / 2
        y = box["y"] + box["height"] * random.uniform(0, 2) / 2
        await locator.page.mouse.move(x, y, steps=int(random.uniform(0, 5)) + 1)


async def scroll_main_scrollable_down_and_up(page, no_of_scrolls=10) -> bool:
    count = 0
    scrolled = scrolled_started = await scroll_main_scrollable(page) 
    while scrolled and count < no_of_scrolls:
        scrolled = await scroll_main_scrollable(page) 
        count += 1
    if scrolled_started:
        await page.wait_for_timeout(1000)  # Wait for 1 second after scrolling down
        await scroll_main_scrollable(page, to_top=True)
                    
async def scroll_main_scrollable(page, to_top: bool = False) -> bool:
    locator, scrollable_element = await get_main_scrollable_locator(page)
    if not locator:
        logger.warning("No scrollable element found.")
        return False

    logger.info(f"Scrolling {'to top' if to_top else 'to bottom'} of: {scrollable_element}")

    before = await locator.evaluate("el => el.scrollTop")

    scroll_script = """
        (el, toTop) => {
            const target = toTop ? 0 : el.scrollHeight - el.clientHeight;
            el.scrollTo({ top: target, behavior: 'smooth' });
            console.log('[scroll]', `Smooth scroll to: ${target}`);
        }
    """
    await locator.evaluate(scroll_script, to_top)

    await page.wait_for_timeout(50)

    after = await locator.evaluate("el => el.scrollTop")
    logger.info(f"[scroll] Scrolled from {before} to {after}")

    return after != before if to_top else after > before
    
    
async def list_scrollable_elements(page):
    return await page.evaluate("""
    () => {
        function isScrollable(el) {
            const style = getComputedStyle(el);
            return (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                   el.scrollHeight > el.clientHeight;
        }

        const scrollables = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        let node = walker.nextNode();
        while (node) {
            if (isScrollable(node)) {
                scrollables.push({
                    tag: node.tagName,
                    id: node.id || null,
                    class: Array.from(node.classList),
                    scrollHeight: node.scrollHeight,
                    clientHeight: node.clientHeight
                });
            }
            node = walker.nextNode();
        }

        // Also consider document-level scrolling
        const doc = document.scrollingElement || document.body;
        if (doc.scrollHeight > doc.clientHeight) {
            scrollables.push({
                tag: doc.tagName,
                id: doc.id || null,
                class: Array.from(doc.classList),
                scrollHeight: doc.scrollHeight,
                clientHeight: doc.clientHeight
            });
        }
        // Sort by scrollHeight descending
        scrollables.sort((a, b) => b.scrollHeight - a.scrollHeight);
        return scrollables;
    }
    """)
    
def css_escape(s: str) -> str:
    # Escape special characters in CSS class names
    return re.sub(r'([^\w-])', lambda m: "\\" + m.group(1), s)

async def get_main_scrollable_locator(page) -> Tuple[Locator, Dict]:
    scrollables = await list_scrollable_elements(page)
    locator = None
    for i, scrollable_element in enumerate(scrollables):
        tag = scrollable_element["tag"]
        id_ = scrollable_element["id"]
        classes = scrollable_element["class"]

        logger.info(f"Scrollable element {i}: {scrollable_element}")

        # Try to create locator
        if id_:
            locator = page.locator(f"#{id_}")
        elif classes:
            escaped_classes = [css_escape(cls) for cls in classes]
            class_selector = "." + ".".join(escaped_classes)
            locator = page.locator(f"{tag}{class_selector}")
        else:
            locator = page.locator(tag)
        # await highlight_elements(page, [locator])
        return locator, scrollable_element # Only get the main (largest) one
    return None, {}

async def highlight_elements(page, elements, color="red", duration=500):
    """Highlight elements with a colored outline"""
    for element in elements:
        await element.evaluate(f"el => el.style.outline = '3px solid {color}'")
    await page.wait_for_timeout(duration)
    for element in elements:
        await element.evaluate("el => el.style.outline = ''")

async def show_reasoning_box(page, locator_or_box = None, reasoning: str = "", show_reasoning_box_duration: int = 2000):
    """
    Show a floating reasoning box next to an element (locator), 
    at a precomputed bounding box, or centered if None.

    Args:
        page: Playwright page object
        locator_or_box: Playwright Locator, dict with {x,y,width,height}, or None
        reasoning: Text to display
        show_reasoning_box_duration: Duration in ms before auto-remove
    """
    # Determine box
    if locator_or_box is None:
        # No locator — box will be handled inside page.evaluate (center placement)
        box = None
    elif hasattr(locator_or_box, "bounding_box"):
        box = await locator_or_box.bounding_box(timeout=2000)
    else:
        box = locator_or_box
    try:
        await page.evaluate(
            """({ box, reasoning, timeoutMs }) => {
                const MARGIN = 16;
                const PADDING = 8;
                const vw = window.innerWidth;
                const vh = window.innerHeight;

                // Remove any existing box
                document.getElementById("__surfari_reasoning_box__")?.remove();

                const div = document.createElement("div");
                div.id = "__surfari_reasoning_box__";
                div.textContent = reasoning;

                Object.assign(div.style, {
                    position: "fixed",
                    background: "#f5f5f5",
                    color: "black",
                    fontSize: "16px",
                    fontWeight: "bold",
                    fontFamily: "Arial, Helvetica, sans-serif",
                    lineHeight: "1.4",
                    WebkitFontSmoothing: "antialiased",
                    padding: "6px 8px",
                    border: "1px solid black",
                    borderRadius: "4px",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    maxWidth: "300px",
                    zIndex: 999999,
                    boxShadow: "0px 0px 5px rgba(0,0,0,0.3)",
                    pointerEvents: "none", // don’t block clicks
                    visibility: "hidden",  // measure first
                    left: "0px",
                    top: "0px",
                });

                document.body.appendChild(div);

                const bw = div.offsetWidth;
                const bh = div.offsetHeight;

                let left, top;

                if (!box) {
                    // Center in viewport
                    left = (vw - bw) / 2;
                    top = (vh - bh) / 2;
                } else {
                    // Prefer right of element
                    left = box.x + box.width + MARGIN;
                    if (left + bw > vw - PADDING) {
                        left = box.x - MARGIN - bw;
                    }
                    left = Math.max(PADDING, Math.min(left, vw - PADDING - bw));

                    // Align vertically with element
                    top = box.y;
                    top = Math.max(PADDING, Math.min(top, vh - PADDING - bh));
                }

                div.style.left = `${left}px`;
                div.style.top  = `${top}px`;
                div.style.visibility = "visible";

                setTimeout(() => div.remove(), timeoutMs);
            }""",
            {"box": box, "reasoning": reasoning, "timeoutMs": show_reasoning_box_duration}
        )
    except Exception as e:
        logger.warning(f"Error showing reasoning box: {e}")

async def inject_control_bar(page, message: str = ""):
    js_code = f"""
    (() => {{
        const controlBar = document.createElement('div');
        controlBar.id = "__surfari_control_bar__";
        controlBar.style.position = 'fixed';
        controlBar.style.bottom = '0';
        controlBar.style.left = '0';
        controlBar.style.right = '0';
        controlBar.style.zIndex = '9999';
        controlBar.style.color = 'black';
        controlBar.style.padding = '10px';
        controlBar.style.fontSize = '14px';
        controlBar.style.display = 'flex';
        controlBar.style.alignItems = 'center';
        controlBar.style.backgroundColor = 'lightgray';
        controlBar.style.fontFamily = 'Arial, sans-serif';
        controlBar.style.boxShadow = '0px -2px 5px rgba(0,0,0,0.2)';
        
        const statusContainer = document.createElement('div');
        statusContainer.style.display = 'flex';
        statusContainer.style.alignItems = 'center';

        const messageSpan = document.createElement('span');
        messageSpan.textContent = {message!r};
        messageSpan.style.fontSize = '16px';
        messageSpan.style.fontWeight = 'bold';
        messageSpan.style.color = '#333';
        messageSpan.style.marginRight = '24px';
        statusContainer.appendChild(messageSpan);

        const toggleButton = document.createElement('button');
        toggleButton.textContent = 'Toggle Mode';
        toggleButton.style.marginLeft = 'auto';
        toggleButton.style.padding = '5px 12px';
        toggleButton.style.border = 'none';
        toggleButton.style.borderRadius = '4px';
        toggleButton.style.backgroundColor = '#555';
        toggleButton.style.color = 'white';
        toggleButton.style.cursor = 'pointer';
        
        controlBar.appendChild(statusContainer);
        controlBar.appendChild(toggleButton);
        document.body.appendChild(controlBar);

        window.surfariMode = false;

        const updateUI = (enabled) => {{
            toggleButton.textContent = enabled ? 'Switch to Manual' : 'Continue to Automation';
            controlBar.style.backgroundColor = enabled ? 'lightgreen' : 'gold';
        }};

        toggleButton.onclick = () => {{
            window.surfariMode = !window.surfariMode;
            updateUI(window.surfariMode);
        }};

        document.addEventListener('submit', (e) => {{
            if (!window.surfariMode) {{
                window.surfariMode = true;
                updateUI(true);
            }}
        }}, true);

        updateUI(window.surfariMode);
    }})();
    """
    await page.evaluate(js_code)

async def remove_control_bar(page):
    await page.evaluate("""
        const bar = document.getElementById("__surfari_control_bar__");
        if (bar) bar.remove();
    """)


