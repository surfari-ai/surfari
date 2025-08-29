const PARENT_LAYER_UP = 10;
const TOO_SMALL = 1;
const NON_INTERACTIVE = 0;
const CLICKABLE = 1;
const EXPANDABLE = 2;
const REMOVABLE = -1;
const INCREMENT = -2;
const DECREMENT = -3;
const PREVIOUS = -4;
const NEXT = -5;

const __ignoredTags = new Set([
    "STYLE", "SCRIPT", "NOSCRIPT",
    "OBJECT", "EMBED", "LINK", "META",
    "TEMPLATE", "HEAD"
]);

const INTERACTIVE_ROLES = new Set(["button", "link", "combobox", "searchbox", "slider", "menu", "menuitem", "menubar", "radio", "checkbox", "tab", "listbox", "option", "spinbutton", "textbox"]);

const __alreadyProcessedNodes = new Set();
const __zIndexCache = new WeakMap();
const __styleCache = new WeakMap();
const __visibilityCache = new WeakMap();

// Caches top modal across invocations
let __attemptedFindTopModal = false;
let __topModalCache = null;

const orig = (window.console && console.log) ? console.log.bind(console) : null;
console.log = (...args) => {
    // try { orig && orig(...args); } catch { }
    try { window.pyLog(...args); } catch { }
};

function debugLog(...args) {
    if (consoleDebugLogEnabled) {
        console.log(...args);
    }
}

// Reuse style cache
function getStyle(e) {
    let s = __styleCache.get(e);
    if (!s) { s = window.getComputedStyle(e); __styleCache.set(e, s); }
    return s;
}

function generateXPathJSInline(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return "";

    const rootNode = el.getRootNode();

    const xpathSegments = [];
    // Track the segment string that corresponds to the shadow host (if any)
    const shadowHost = rootNode instanceof ShadowRoot ? rootNode.host : null;
    let hostSegString = null;

    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
        const tag = current.tagName.toLowerCase();
        let index = 1;
        let sib = current.previousElementSibling;
        while (sib) {
            if (sib.tagName.toLowerCase() === tag) index++;
            sib = sib.previousElementSibling;
        }

        const seg = `${tag}[${index}]`;
        // Remember which segment is the shadow host so we can insert the marker after it
        if (shadowHost && current === shadowHost) {
            hostSegString = seg;
        }
        xpathSegments.unshift(seg);

        let parent = current.parentElement;
        if (!parent) {
            const r = current.getRootNode();
            parent = r instanceof ShadowRoot ? r.host : null;
        }
        current = parent;
    }

    // Insert "/#shadow-root/" after the host segment if we had a shadow root
    if (hostSegString) {
        const idx = xpathSegments.indexOf(hostSegString);
        if (idx !== -1) {
            xpathSegments.splice(idx + 1, 0, "#shadow-root");
        }
    }

    const xpath_whole = "/" + xpathSegments.join("/");
    debugLog("Generated XPath: " + xpath_whole);
    return xpath_whole;
}


function isNonBlocking(el) {
    if (!el || !(el instanceof Element)) return true;
    if (el.id && ["__surfari_control_bar__", "__surfari_reasoning_box__"].includes(el.id)) return true;
    if (el.getAttribute('aria-hidden')?.toLowerCase() === 'true') return true;
    const s = getStyle(el);
    // if (s.pointerEvents === 'none') return true;
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return true;
    return false;
}

function isRectObscured(rect, referenceEl) {
    if (isInsideIframe) return false;
    // traverse shadow DOMs to get *actual* foremost element
    function deepestElementFromPoint(x, y) {
        let el = document.elementFromPoint(x, y);
        while (el && el.shadowRoot) {
            // some shadow roots (e.g. closed ones) might not expose elementFromPoint
            const inner = el.shadowRoot.elementFromPoint?.(x, y);
            if (!inner || inner === el) break;
            el = inner;
        }
        return el;
    }

    // cross-shadow version of .contains()
    function composedContains(a, b) {
        for (let n = b; n; n = n.parentNode || (n.host ?? null)) {
            if (n === a) return true;
        }
        return false;
    }

    // run the usual checks at one probe point
    function probe(x, y) {
        const topEl = deepestElementFromPoint(x, y);
        if (!topEl) return { covered: false, blocker: null };

        let covered =
            topEl !== referenceEl &&
            !composedContains(topEl, referenceEl) &&
            !composedContains(referenceEl, topEl) &&
            !isNonBlocking(topEl);

        // treat sibling stacking contexts specially
        if (
            covered &&
            topEl.parentNode &&
            Array.from(topEl.parentNode.children).some(
                sib => sib !== topEl && composedContains(sib, referenceEl)
            )
        ) {
            covered = false;
        }

        if (covered && topEl.tagName === 'IMG') covered = false;
        return { covered, blocker: covered ? topEl : null };
    }

    const p1 = probe(rect.left + rect.width / 2, rect.top);
    const p2 = probe(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const p3 = probe(rect.left + rect.width / 2, rect.bottom);
    const isObscured = p1.covered && p2.covered && p3.covered;
    const blocker = isObscured ? (p2.blocker || p1.blocker || p3.blocker) : null;

    if (isObscured && isVisible(blocker, false)) {
        debugLog(
            `Element obscured by ${blocker.tagName}${blocker.id ? '#' + blocker.id : ''}` +
            `; classes: ${blocker.classList}; referenceEl: ${referenceEl.tagName}`
        );
        return true;
    }
    return false;
}

function isFontIcon(node) {
    const ICON_CLASSES = new Set([
        'material-icons', 'material-symbols-outlined', 'material-symbols-rounded',
        'fa', 'fa-solid', 'fa-regular', 'fa-brands',
        'bi', 'glyphicon', 'icon', 'iconfont'
    ]);

    return (
        (
            (node.tagName === 'SPAN' || node.tagName === 'I') &&
            [...node.classList].some(cls => ICON_CLASSES.has(cls))
        ) ||
        (getStyle(node).fontFamily || '').match(/material|awesome|bootstrap|icon/i)
    );
}



function getEffectiveZIndex(el) {
    if (__zIndexCache.has(el)) return __zIndexCache.get(el);

    let current = el;
    while (current && current !== document.body) {
        const style = getStyle(current);

        const z = style.zIndex;
        const parsed = parseInt(z, 10);
        if (!isNaN(parsed)) {
            __zIndexCache.set(el, parsed);
            return parsed;
        }
        current = current.parentElement;
    }

    __zIndexCache.set(el, 0);
    return 0;
}

function isTruePopup(el) {
    const rect = el.getBoundingClientRect();
    const points = 9;
    let allClear = true;
    let blockingSomething = false;

    for (let i = 0; i < points; i++) {
        const px = rect.left + (rect.width * ((i % 3) + 0.5) / 3);
        const py = rect.top + (rect.height * (Math.floor(i / 3) + 0.5) / 3);

        // 1) must be on top at this point
        const topEl = document.elementFromPoint(px, py);
        if (!topEl || (topEl !== el && !el.contains(topEl) && !topEl.contains(el))) {
            allClear = false;
            break;
        }

        // 2) find first element beneath that's outside the popup
        const stack = document.elementsFromPoint(px, py);
        const under = stack.find(n =>
            n !== el &&
            !el.contains(n)   // not a descendant
        );

        if (under) {
            if (!under.contains(el)) {
                blockingSomething = true;
                debugLog(`Found non-parent behind popup at (${px}, ${py}): ${under.outerHTML.substring(0, 100)}`);
            } else {
                debugLog(`Found parent behind popup at (${px}, ${py}): ${under.outerHTML.substring(0, 100)}`);
            }
        }
    }

    return allClear && blockingSomething;
}

function findTopModal() {
    const modalSelector = [
        '[role="dialog"]',
        '[aria-modal="true"]',
        '.modal',
        '.dialog',
        '.popup'
    ].join(', ');

    debugLog(`findTopModal:Searching for top modal with selector: ${modalSelector}`);
    const modals = Array.from(document.querySelectorAll(modalSelector)).filter(el => {
        const ariaModal = el.getAttribute('aria-modal');
        if (ariaModal && ariaModal.toLowerCase() === 'false') {
            debugLog(`findTopModal: checked modal, with aria-modal="false": ${el.outerHTML.substring(0, 100)}`);
            return false;
        }
        const isModalVisible = isVisible(el, true, false);
        const isModalTruePopup = isTruePopup(el);

        debugLog(`findTopModal: checked modal, isVisible ${isModalVisible}, isTruePopup ${isModalTruePopup}, ${el.outerHTML.substring(0, 100)}`);
        return isModalVisible && isModalTruePopup;
    });

    if (modals.length === 0) return null;
    debugLog(`Found ${modals.length} visible modals, checking their z-index to see who is on top...`);

    const topModal = modals.reduce((top, el) => {
        const z = getEffectiveZIndex(el);
        return (!top || z > top.z) ? { el, z } : top;
    }, null)?.el || null;

    if (topModal) {
        if (isNonBlocking(topModal)) {
            debugLog(`findTopModal: found non-blocking top modal: z-index: ${getEffectiveZIndex(topModal)}, ${topModal.outerHTML.substring(0, 100)}`);
            topModal.isNonBlocking = true;
        } else {
            debugLog(`findTopModal: found blocking top modal: z-index: ${getEffectiveZIndex(topModal)}, ${topModal.outerHTML.substring(0, 100)}`);
            topModal.isNonBlocking = false;
        }
    }
    return topModal;
}

function hasDescendantWithHigherZ(node, modalZ) {
    const elements = node.querySelectorAll("*[style], *[class]");
    for (const el of elements) {
        if (getEffectiveZIndex(el) > modalZ) return true;
    }
    return false;
}

function isHiddenByAnyModal(node, rect = null) {
    if (!__attemptedFindTopModal) {
        __topModalCache = findTopModal();
        __attemptedFindTopModal = true;
    }

    const topModal = __topModalCache;
    if (!topModal) return false;
    if (topModal.isNonBlocking) return false;

    // Early returns based on DOM containment
    if (topModal.contains(node)) return false;   // node is inside the modal
    if (node.contains(topModal)) return false;   // node is an ancestor of the modal

    // Early return: bounding box doesn't overlap
    if (rect) {
        debugLog(`Checking if node overlaps with modal: ${node.tagName}, id: ${node.id}`);
        const modalRect = topModal.getBoundingClientRect();
        const overlaps = !(
            rect.bottom <= modalRect.top ||
            rect.top >= modalRect.bottom ||
            rect.right <= modalRect.left ||
            rect.left >= modalRect.right
        );

        if (!overlaps) {
            debugLog(`Node does not overlap with modal: ${node.tagName}, id: ${node.id}`);
            return false;
        }
    }

    const modalZ = getEffectiveZIndex(topModal);
    const nodeZ = getEffectiveZIndex(node);

    // Node is below modal and has no elevated children
    if (nodeZ < modalZ && !hasDescendantWithHigherZ(node, modalZ)) {
        return true;
    }

    return false;
}


function customHidden(node) {
    if (node.classList && (node.classList.contains('ng-binding') || node.classList.contains('completed-questionnaire'))) {
        return true;
    }
    return false;
}

function hasVisibleChild(node) {
    if (!(node instanceof Element)) return false;

    const children = node.querySelectorAll('*');
    for (const child of children) {
        const style = getStyle(child);
        if (style.visibility === 'visible') {
            return true;
        }
    }
    return false;
}

function hasSizedChildIncludingShadow(el, tooSmall = 2) {
    const stack = [el];

    while (stack.length) {
        const node = stack.pop();

        if (node !== el && node instanceof Element) {
            const w = node.offsetWidth;
            const h = node.offsetHeight;
            if (w + h > tooSmall * 2) return true;
        }

        // Traverse light DOM
        stack.push(...node.children);

        // Traverse shadow DOM
        if (node.shadowRoot) {
            stack.push(...node.shadowRoot.children);
        }
    }

    return false;
}


function isVisibleCheckCached(node) {
    const cachedVisible = __visibilityCache.get(node);
    if (cachedVisible !== undefined) {
        debugLog(`Cache hit for ${node.outerHTML.substring(0, 100)}: ${cachedVisible}`);
        return cachedVisible;
    }
    // do the hardwork of checking
    const checkedVisible = isVisible(node, true, true);
    __visibilityCache.set(node, checkedVisible);
    return checkedVisible;
}

function isVisible(node, checkHasSizedChild = true, checkHiddenByModal = true) {
    // If node is a text node, use node.parentElement for visibility checks
    let el = (node.nodeType === Node.TEXT_NODE) ? node.parentElement : node;
    if (!el) {
        debugLog("Assume visible: el is null");
        return true;
    }

    if (!(el instanceof Element)) {
        const typeStr = typeof el;
        const constructorStr = el && el.constructor ? el.constructor.name : 'unknown';
        const infoStr = `el type: ${typeStr}, constructor: ${constructorStr}`;
        debugLog("Assume visible: el is not an instance of Element: " + infoStr);
        return true;
    }

    if (el.id && ["__surfari_control_bar__", "__surfari_reasoning_box__"].includes(el.id)) return false;

    function logVisibleInfo(message, withSizeLog = false, visible = false, rect = null) {
        if (!consoleLogVisibilityCheck) return;
        let elLogInfo = "";
        if (el && el instanceof Element) {
            elLogInfo = ` -- tag: ${el.tagName}, id: ${el.id}, name: ${el.name}, title: ${el.title}, class: ${el.classList}`;
        }
        if (visible == false) {
            message = "isVisible=" + visible + ": " + message + elLogInfo;
        } else {
            message = message + elLogInfo;
        }
        debugLog(message);
        if (withSizeLog && rect) {
            debugLog(`rect: ${JSON.stringify(rect)}, offsetWidth: ${el.offsetWidth}, offsetHeight: ${el.offsetHeight}`);
        }
    }

    logVisibleInfo("Starting to check visibility for element", false, true);

    if (el.hidden) {
        logVisibleInfo("Element has hidden attribute");
        return false;
    }
    if (customHidden(el)) {
        logVisibleInfo("Element is custom hidden");
        return false;
    }

    const style = getStyle(el);
    if (style.display === "contents") {
        logVisibleInfo("Assume visible: Element has display: contents", false, true);
        return true;
    }

    if (style.display === "none") {
        logVisibleInfo("Element has display: none");
        return false;
    }

    if (style.visibility === "hidden" && !hasVisibleChild(el)) {
        logVisibleInfo("Element has visibility: hidden and no child overwriting it");
        return false;
    }

    if (style.clipPath !== "none" && style.clipPath !== "unset" && style.overflow === "hidden") {
        logVisibleInfo("Element has clipPath and overflow: hidden");
        return false;
    }

    const tag = el.tagName.toLowerCase();
    const isRadioOrCheckbox = tag === "input" && (el.type === "radio" || el.type === "checkbox");
    const rect = el.getBoundingClientRect();
    const isSizeNonZero = true; //el.offsetWidth > 0 || el.offsetHeight > 0 || rect.width > 0 || rect.height > 0;

    if (isRadioOrCheckbox && isSizeNonZero) {
        logVisibleInfo("Assume visible: Element is radio/checkbox with non-zero size", false, true);
        return true;
    }

    const zNum = Number(style.zIndex);
    if (style.opacity === "0" && Number.isFinite(zNum) && zNum < 0) {
        logVisibleInfo("Element has opacity: 0 and negative z-index");
        return false;
    }
    const viewportWidth = window.innerWidth;

    // Fully offscreen
    const significantlyOffscreen =
        rect.right <= -100 ||
        rect.left >= viewportWidth + 100;

    if (significantlyOffscreen) {
        logVisibleInfo("Element is significantly off-screen, left: " + rect.left + ", right: " + rect.right);
        return false;
    }

    physicallyTooSmall =
        el.offsetWidth <= TOO_SMALL &&
        el.offsetHeight <= TOO_SMALL &&
        rect.width <= TOO_SMALL &&
        rect.height <= TOO_SMALL;

    physicallyTooSmall = physicallyTooSmall ||
        el.offsetWidth === 0 ||
        el.offsetHeight === 0 ||
        rect.width === 0 ||
        rect.height === 0;

    const horizontallyOffscreen =
        rect.right <= 0 ||
        rect.left >= viewportWidth;

    if (physicallyTooSmall || horizontallyOffscreen) {
        if (style.overflow === "hidden") {
            logVisibleInfo("Element is too small or off-screen and has overflow: hidden");
            return false;
        }
        if (checkHasSizedChild) {
            const hasSizedChild = hasSizedChildIncludingShadow(el);
            if (!hasSizedChild) {
                logVisibleInfo("Element is too small or off-screen and has no sized child", true, false, rect);
                return false;
            }
            logVisibleInfo("Assume visible: Element is too small or off-screen but has sized child", true, true, rect);
        } else {
            logVisibleInfo("Element is too small or off-screen (no sized-child check)", true, false, rect);
            return false;
        }
    }

    if (checkHiddenByModal && isHiddenByAnyModal(el)) {
        logVisibleInfo("Element is hidden by modal");
        return false;
    }

    return true;
}

function getElementRole(el) {
    if (!el || !(el instanceof Element)) return null;
    let role = null;
    // explicit role attribute
    const explicit = el.getAttribute('role');
    if (explicit && explicit.trim() !== "") {
        role = explicit.trim().toLowerCase();
    }
    // el.role property
    if (!role && 'role' in el && el.role && el.role.trim() !== "") {
        role = el.role.trim().toLowerCase();
    }

    const tag = el.tagName.toLowerCase();
    if (!role) {
        if ((tag === 'a' || tag === 'area') && el.hasAttribute('href')) {
            role = 'link';
        } else if (tag === 'button') {
            role = 'button';
        } else if (tag === 'input') {
            const type = (el.getAttribute('type') || 'text').toLowerCase();
            if (['button', 'submit', 'reset', 'image'].includes(type)) {
                role = 'button';
            } else if (['checkbox', 'radio'].includes(type)) {
                role = type;
            } else if (type === 'search') {
                role = el.hasAttribute('list') ? 'combobox' : 'searchbox';
            } else if (type === 'range') {
                role = 'slider';
            } else if (['text', 'email', 'tel', 'url'].includes(type)) {
                role = 'textbox';
            } else if (type === 'number') {
                role = 'spinbutton';
            }
        } else if (tag === 'textarea') {
            role = 'textbox';
        } else if (tag === 'select' || tag === 'datalist') {
            role = 'combobox';
        } else if (tag === 'option') {
            role = 'option';
        } else if (tag === 'img') {
            role = 'img';
        } else if (tag === 'li') {
            role = 'listitem';
        } else if (tag === 'ol' || tag === 'ul') {
            role = 'list';
        } else if (tag === 'td' || tag === 'th') {
            role = 'cell';
        }
    }

    debugLog(`Computed role: ${role} for ${el.outerHTML?.substring(0, 100)}`);
    if (role && INTERACTIVE_ROLES.has(role)) {
        return role;
    }

    return null;
}


function escapeForString(text) {
    return String(text).replace(/'/g, "\\'");
}

// Determine if an id looks like a GUID/random token (skip for selectors)
function isGuidLike(id) {
    return /^[0-9a-fA-F-]{8,}$/.test(id);
}

function normalizeWhitespaces(txt) {
    return txt ? txt.trim().replace(/\s+/g, ' ') : "";
}

function computeLabel(node, qs, getById) {
    // explicit <label for="id">
    const id = node.getAttribute("id")?.trim();
    if (id) {
        const escaped = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(id) : id;
        const labelFor = qs(`label[for="${escaped}"]`);
        if (labelFor) {
            const t = normalizeWhitespaces(labelFor.textContent);
            if (t) return t;
        }
    }
    // aria-labelledby (assume single id)
    const labelledby = node.getAttribute('aria-labelledby');
    if (labelledby) {
        const ref = labelledby.trim();
        if (ref) {
            const lbl = getById(ref);
            const t = lbl ? normalizeWhitespaces(lbl.textContent) : "";
            if (t) return t;
        }
    }

    // aria-label
    const ariaLabel = normalizeWhitespaces(node.getAttribute('aria-label'));
    if (ariaLabel) return ariaLabel;

    // implicit wrapping <label>
    const labelEl = node.closest('label');
    if (labelEl) {
        const t = normalizeWhitespaces(labelEl.textContent);
        if (t) return t;
    }

    return null;
}

// Root-scoped accessible-name computation
function computeName(el) {
    let text = null;
    const alt = el.getAttribute('alt')?.trim();
    if (alt) text = alt;

    if (!text) {
        const title = el.getAttribute('title')?.trim();
        if (title) text = title;
    }

    if (!text) {
        text = el.innerText?.trim().replace(/\s+/g, ' ');
    }
    debugLog(`Computed accessible name: ${text} for ${el.outerHTML?.substring(0, 100)}`);

    return text || null;
}


function generateLocator(node, opts = {}) {
    if (generateLocatorDisabled) {
        return "";
    }
    const doc = node.ownerDocument || document;
    const root = (node && node.getRootNode) ? node.getRootNode() : doc; // Document or ShadowRoot
    const qs = (sel) => (root instanceof ShadowRoot ? root.querySelector(sel) : doc.querySelector(sel));
    const getById = (id) => {
        if (!id || !id.trim()) return null; // guard empty/whitespace ids
        const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(id) : id;
        if (root instanceof ShadowRoot) {
            // ShadowRoot doesn't have getElementById
            return root.querySelector(`#${esc}`);
        }
        return doc.getElementById(id);
    };

    function buildFramePrefix() {
        if (!myFrameId && !myFrameName) return 'page';
        if (myFrameId) return `page.frame_locator('iframe[id=${myFrameId}]')`;
        if (myFrameName) return `page.frame_locator('iframe[name=${myFrameName}]')`;
    }

    let pg = 'page';
    if (isInsideIframe) {
        debugLog(`isInsideIframe ${isInsideIframe}, building prefix based on myFrameId ${myFrameId}, myFrameName ${myFrameName}`)
        pg = buildFramePrefix()
    }

    debugLog(`Building locator for ${node.outerHTML.substring(0, 100)}`);

    const testIdAttr = opts.testIdAttribute || 'data-testid';
    // 1) test id attributes
    const testIdCandidates = [testIdAttr, 'data-testid', 'data-test-id', 'data-test', 'data-qa', 'test-id'];
    for (const attr of testIdCandidates) {
        if (attr && node.hasAttribute(attr)) {
            const val = node.getAttribute(attr);
            return `${pg}.get_by_test_id('${escapeForString(val)}')`;
        }
    }

    const labelText = computeLabel(node, qs, getById);

    // 2) unique id (scoped to root)
    const id = node.getAttribute('id')?.trim();
    if (id && !isGuidLike(id)) {
        const valid = /^[A-Za-z][\w-]*$/.test(id);
        const selector = valid ? `#${escapeForString(id)}` : `[id="${escapeForString(id)}"]`;
        return `${pg}.locator('${selector}')`;
    }

    // 3) ARIA role + accessible name. If role is null, climb.
    // const role = getElementRoleClimb(node);
    const role = getElementRole(node);

    const name = labelText || computeName(node);
    if (role && name) {
        const roleEsc = escapeForString(role);
        const nameEsc = escapeForString(name);
        return `${pg}.get_by_role('${roleEsc}', name='${nameEsc}')`;
    }

    const tag = node.tagName.toLowerCase();
    // 4) placeholder
    if ((tag === 'input' || tag === 'textarea') && node.hasAttribute('placeholder')) {
        const placeholder = node.getAttribute('placeholder')?.trim();
        if (placeholder) {
            return `${pg}.get_by_placeholder('${escapeForString(placeholder)}')`;
        }
    }

    // 5) labels
    if (labelText) {
        return `${pg}.get_by_label('${escapeForString(labelText)}')`;
    }

    // 6) name attribute
    const nameAttr = node.getAttribute('name')?.trim();
    if (
        nameAttr &&
        ['button', 'form', 'fieldset', 'frame', 'iframe', 'input', 'keygen', 'object', 'output', 'select', 'textarea', 'map', 'meta', 'param'].includes(tag)
    ) {
        return `${pg}.locator('${tag}[name="${escapeForString(nameAttr)}"]')`;
    }

    // 7) alt text
    const alt = node.getAttribute('alt')?.trim();
    if (alt) {
        return `${pg}.get_by_alt_text('${escapeForString(alt)}')`;
    }

    // 8) title
    const titleAttr = node.getAttribute('title')?.trim();
    if (titleAttr) {
        return `${pg}.get_by_title('${escapeForString(titleAttr)}')`;
    }
    // 9) text content
    if (opts.directParentOfText) {
        const normText = normalizeWhitespaces(node.innerText)
        if (normText) {
            return `${pg}.get_by_text('${escapeForString(normText)}').and_(page.locator(':not(#__surfari_reasoning_box__):not(#__surfari_control_bar__)'))`;
        }
    }
    return ""
}

function containsKeyword(keywords, text) {
    for (const keyword of keywords) {
        const escapedKeyword = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp(`\\b${escapedKeyword}\\b`, 'i');

        if (regex.test(text)) {
            return true; // Return true as soon as the first match is found
        }
    }

    // If the loop finishes without finding any matches, return false
    return false;
}

function getElementInteractiveLevel(el) {
    if (!el || !(el instanceof Element)) return 0;
    const role = getElementRole(el);
    const tag = el.tagName.toLowerCase();
    // TO DO: handle label for <input> elements
    if (INTERACTIVE_ROLES.has(role) || tag === "button" || tag === "a") {
        if (el.hasAttribute("disabled") || el.getAttribute("aria-disabled")?.toLowerCase() === "true") {
            return 0;
        }
        const ariaExpanded = el.getAttribute("aria-expanded");
        const isCollapsed = ariaExpanded !== null && ariaExpanded == "false";
        const ariaHasPopup = el.getAttribute("aria-haspopup");
        const hasPopup = ariaHasPopup !== null && ariaHasPopup !== "false";
        // debugLog(`Checking aria attributes... tag: ${el.tagName}, role: ${role}, hasPopup: ${hasPopup} isExpanded: ${isExpanded}`);
        if (hasPopup && isCollapsed) return EXPANDABLE; // this is the expected correct behavior for documentation purpose
        if (isCollapsed) return EXPANDABLE; // we only consider aria-expanded, if dev explicitly set to false but forgot to set aria-haspopup or set it incorrectly
        if (hasPopup && ariaExpanded === null) return EXPANDABLE; // dev set aria-haspopup correctly but forgot aria-expanded

        const title = el.getAttribute("title") || "";
        const ariaLabel = el.getAttribute("aria-label") || "";
        let textContent = "";
        if (role === "button") {
            textContent = el.textContent.trim();
        }
        const accessibleText = title + " " + ariaLabel + " " + textContent;

        if (containsKeyword(["expand", "open"], accessibleText)) {
            return EXPANDABLE;
        } else if (containsKeyword(["close", "remove", "delete"], accessibleText)) {
            return REMOVABLE;
        } else if (containsKeyword(["increase", "increment", "add", "inc"], accessibleText)) {
            return INCREMENT;
        } else if (containsKeyword(["decrease", "decrement", "reduce", "subtract", "dec"], accessibleText)) {
            return DECREMENT;
        } else if (containsKeyword(["previous", "back"], accessibleText)) {
            return PREVIOUS;
        } else if (containsKeyword(["next", "forward"], accessibleText)) {
            return NEXT;
        }
        return CLICKABLE;
    } else if (el.classList &&
        (["editable-field", "upload-title"].some(cls => el.classList.contains(cls)) || [...el.classList].some(cls => cls === "action-img" || cls.endsWith("-action-img")))) {
        return CLICKABLE;
    } else if (tag === "th" || role === "columnheader" || role === "rowheader") {
        if (el.hasAttribute("aria-sort") || (el.hasAttribute("aria-label") && el.getAttribute("aria-label").toLowerCase().includes("sort"))) {
            return CLICKABLE; // sortable table header
        }
    }
    style = getStyle(el);
    if (style.pointerEvents !== 'none' && style.visibility !== 'hidden' && style.display !== 'none' && parseFloat(style.opacity) > 0 && style.cursor === 'pointer') {
        return CLICKABLE;
    }
    return NON_INTERACTIVE;
}


function composedParent(node) {
    if (!node) return null;

    // If this node is assigned to a <slot>, treat the slot as its composed parent
    if (node.assignedSlot) return node.assignedSlot;

    // Normal light-DOM parent
    if (node.parentElement) return node.parentElement;

    // If we hit a ShadowRoot, jump to its host
    const root = node.getRootNode && node.getRootNode();
    if (root && root instanceof ShadowRoot) return root.host;

    // DocumentFragment without a shadow host (rare), bail
    return null;
}

function getInteractiveLevelClimb(element) {
    let current = element;
    let effectiveLevel = NON_INTERACTIVE;
    let layerToCheck = 0;

    while (current && current.nodeType === Node.ELEMENT_NODE && layerToCheck < PARENT_LAYER_UP) {
        layerToCheck++;
        const level = getElementInteractiveLevel(current);
        if (level !== NON_INTERACTIVE && level !== CLICKABLE) {
            return level;
        } else if (level === CLICKABLE) {
            effectiveLevel = CLICKABLE;
        }
        current = composedParent(current);
    }
    return effectiveLevel;
}

function getElementRoleClimb(element) {
    let current = element;
    let layerToCheck = 0;

    while (current && current.nodeType === Node.ELEMENT_NODE && layerToCheck < PARENT_LAYER_UP) {
        layerToCheck++;
        const role = getElementRole(current);
        if (role) {
            return role;
        }
        current = composedParent(current);
    }
    return null;
}

function getInteractiveContentByLevel(interaLevel) {
    let content = ""
    if (interaLevel === CLICKABLE) {
        content = "[B]";
    } else if (interaLevel === EXPANDABLE) {
        content = "[E]";
    } else if (interaLevel === REMOVABLE) {
        content = "[X]";
    } else if (interaLevel === INCREMENT) {
        content = "[↑]";
    } else if (interaLevel === DECREMENT) {
        content = "[↓]";
    } else if (interaLevel === PREVIOUS) {
        content = "[←]";
    } else if (interaLevel === NEXT) {
        content = "[→]";
    }
    return content;
}

function findLabelForId(shadowRoot, nodeId) {
    // Check current shadow DOM
    const label = shadowRoot.querySelector(`label[for="${nodeId}"]`);
    if (label) return label;

    // Recursively check nested shadow roots
    for (const element of shadowRoot.querySelectorAll('*')) {
        if (element.shadowRoot) {
            const found = findLabelForId(element.shadowRoot, nodeId);
            if (found) return found;
        }
    }
    return null;
}

function getLabelText(node, { includeSiblingLabel = false,
    includeAriaLabelledBy = false,
    includeAriaLabel = false,
    includeTitle = false,
    includeTextContent = false,
    includeName = false }) {
    debugLog(`getLabelText: node: ${node.tagName}, id: ${node.id}, includeSiblingLabel: ${includeSiblingLabel}, includeAriaLabelledBy: ${includeAriaLabelledBy}, includeAriaLabel: ${includeAriaLabel}, includeTitle: ${includeTitle}, includeTextContent: ${includeTextContent}, includeName: ${includeName}`);
    const nodeId = node.getAttribute('id');
    let labelText = "";

    // 1) Check <label for="ID">
    if (nodeId) {
        const labelEl = findLabelForId(document, nodeId);
        debugLog(`Checking label for ${node.tagName}, id: ${nodeId}, labelEl: ${labelEl}`);
        if (labelEl) {
            labelText = extractDirectText(labelEl);
            debugLog(`Label text found: ${labelText}`);
        }
    }

    // 2) Check if node is inside a <label>
    if (!labelText) {
        const parentLabel = node.closest('label');
        if (parentLabel) {
            labelText = extractDirectText(parentLabel);
        }
    }

    // 3) Check siblings for <label>
    if (!labelText && includeSiblingLabel && node.parentElement) {
        const siblings = Array.from(node.parentElement.children);
        for (const sibling of siblings) {
            if (sibling.tagName === 'LABEL' && isVisibleCheckCached(sibling)) {
                labelText = extractDirectText(sibling);
                if (labelText) break;
            }
        }
    }

    // 4) aria-labelledby — use element.innerText instead of direct text
    if (!labelText && includeAriaLabelledBy) {
        const ariaLabelledBy = node.getAttribute('aria-labelledby');
        if (ariaLabelledBy) {
            labelText = ariaLabelledBy
                .split(/\s+/)                           // multiple IDs allowed
                .map(id => document.getElementById(id))
                .filter(el => el && isVisibleCheckCached(el))
                .map(el => el.innerText.trim())
                .filter(txt => txt)                    // drop empties
                .join(' ');
        }
    }

    if (!labelText && includeAriaLabel) {
        // 5) Check for aria-label
        const ariaLabel = node.getAttribute('aria-label');
        if (ariaLabel) {
            labelText = ariaLabel.trim();
        }
    }

    if (!labelText && includeTitle) {
        // 6) Check for title attribute
        const title = node.getAttribute('title');
        if (title) {
            labelText = title.trim();
        }
    }

    if (!labelText && includeTextContent) {
        // 7) Check for textContent
        labelText = node.textContent.trim();
    }

    if (!labelText && includeName) {
        // 8) Check for name attribute
        const name = node.getAttribute('name');
        if (name) {
            labelText = name.trim();
        }
    }
    return labelText ? labelText.replace(/\s+/g, ' ').trim() : "";
}

function extractDirectText(element) {
    return Array.from(element.childNodes)
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .map(textNode => textNode.textContent.trim())
        .join(' ') // Combine adjacent text nodes
        .replace(/\s+/g, ' '); // Normalize whitespace
}

function isFillableTdCell(td) {
    if (!td || td.tagName !== 'TD') return false;

    const row = td.parentElement;
    if (!row || row.tagName !== 'TR') return false;

    const tds = Array.from(row.querySelectorAll('td'));
    const index = tds.indexOf(td);

    if (index > 0) {
        const left = tds[index - 1];
        return (
            // left.textContent.trim().length > 0 &&
            left.classList.contains('ht-gray') &&
            !td.classList.contains('ht-gray')
        );
    }

    // first td — no left neighbor
    return false;
}

function addSegment({ type, content = undefined, x, y, width, height, xpath, locatorString = undefined, enclose = undefined, id = undefined, labelText = undefined }) {
    const segment = { type, x, y, width, height, xpath };
    if (content !== undefined) segment.content = content;
    if (locatorString !== undefined) segment.locatorString = locatorString;
    if (enclose !== undefined) segment.enclose = enclose;
    if (id !== undefined) segment.id = id;
    if (labelText !== undefined) segment.labelText = labelText;
    segments.push(segment);
}

function traverse(node) {
    if (!node || __alreadyProcessedNodes.has(node) || __ignoredTags.has(node.tagName)) return;
    if (node.nodeType === Node.COMMENT_NODE) return;

    debugLog(`Currently traversing for ${node.outerHTML?.substring(0, 100)}`);

    if (!isVisibleCheckCached(node)) {
        debugLog(`Stop Traversing and Skipping invisible node: ${node.tagName}, id: ${node.id}, class: ${node.classList}`);
        return;
    }

    if (node.nodeType === Node.TEXT_NODE) {
        // Combine with consecutive text nodes, skipping blanks
        let combinedText = node.textContent.trim();
        if (!combinedText) {
            __alreadyProcessedNodes.add(node);
            return;
        }
        let next = node.nextSibling;
        while (next && next.nodeType === Node.TEXT_NODE) {
            // because we are processing text sibling nodes here, not through the traverse
            // we need to indicate we have already processed this node
            __alreadyProcessedNodes.add(next);
            if (isVisibleCheckCached(next)) combinedText += next.textContent.trim();
            next = next.nextSibling;
        }

        const parent = node.parentElement;
        if (!parent) return;
        const parentRect = parent.getBoundingClientRect();
        let rect;
        if (parent.tagName.toLowerCase() === 'td') {
            debugLog(`Using parent's bounding box for td or combined text nodes: ${combinedText}`);
            rect = parentRect;
        } else {
            const range = document.createRange();
            range.selectNodeContents(node);
            rect = range.getBoundingClientRect();
            rect.width = parentRect.width;
            rect.height = parentRect.height;
        }
        if (isRectObscured(rect, parent)) return;

        combinedText = combinedText.replace(/&nbsp;|\s+/g, ' ').trim();

        const interaLevel = getInteractiveLevelClimb(parent);
        // icon not nested under button or anchor
        if (isFontIcon(parent)) {
            if (parent.getAttribute('aria-hidden')?.toLowerCase() === 'true') {
                return;
            }
            const labelText = getLabelText(parent, {
                includeAriaLabel: true,
                includeTitle: true,
                includeTextContent: true
            });
            const iconText = getInteractiveContentByLevel(interaLevel) || labelText || "Icon";

            addSegment({
                type: 'text',
                content: iconText,
                enclose: 0,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(parent),
                locatorString: generateLocator(parent),
                labelText: labelText
            });
            return;
        }
        if (combinedText) {
            addSegment({
                type: 'text',
                content: combinedText,
                enclose: interaLevel,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(parent),
                locatorString: generateLocator(parent, { directParentOfText: true })
            });
        }
    } else if (node.nodeType === Node.ELEMENT_NODE) {
        const elementRole = getElementRole(node);
        // If it's an iframe, store a reference so we can recurse in Python
        if (node.tagName.toLowerCase() === "iframe") {
            let frameId = "FRAME_" + counter++;
            node.setAttribute("data-frame-id", frameId);
            let rect = node.getBoundingClientRect();
            if (isRectObscured(rect, node)) {
                debugLog("Obscured iframe: " + frameId);
                return;
            }
            addSegment({
                type: 'iframe',
                id: frameId,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(node),
                locatorString: generateLocator(node)
            });
            return;
        } else if (node.tagName.toLowerCase() === 'input' ||
            node.tagName.toLowerCase() === 'select' ||
            node.tagName.toLowerCase() === 'textarea') {

            let inputType;
            if (node.tagName.toLowerCase() === 'input') {
                inputType = (node.getAttribute('type') || 'text').toLowerCase();
            }

            let rect = node.getBoundingClientRect();
            if (isRectObscured(rect, node)) {
                debugLog("Obscured input/select: " + node.tagName + ", id: " + node.id);
                return;
            }
            if (rect.width === 0 || rect.height === 0) {
                rect.width = node.offsetWidth;
                rect.height = node.offsetHeight;
            }
            let content = "";
            if (inputType === "text" || inputType === "password" || inputType === "search" || inputType === "number" ||
                inputType === "email" || inputType === "tel" || inputType === "range" ||
                node.tagName.toLowerCase() === 'textarea') {
                let val = node.value || "";
                if (!val.trim()) {
                    let placeholder = node.getAttribute('placeholder') || "";
                    if (placeholder.trim()) {
                        // Use placeholder if present
                        val = placeholder;
                    } else {
                        let labelText = getLabelText(node, { includeAriaLabelledBy: true, includeAriaLabel: true, includeTitle: true, includeTextContent: true, includeName: true });
                        debugLog("Calling getLabelText for input with no value and placeholder: " + node.tagName + ", type: " + inputType + ", id: " + node.id + ", labelText: " + labelText);
                        if (labelText.trim()) {
                            val = labelText;
                        } else {
                            val = "Enter Value";
                        }
                    }
                }
                if (inputType === "range") {
                    min = node.getAttribute('min') || "0";
                    max = node.getAttribute('max') || "100";
                    step = node.getAttribute('step') || "1";
                    debugLog("Range input: " + node.tagName + ", id: " + node.id + ", value: " + val + ", min: " + min + ", max: " + max + ", step: " + step);
                    val = `${val}-${min}-${max}-${step}`;
                }
                if (node.hasAttribute('disabled') || node.getAttribute('aria-disabled')?.toLowerCase() === 'true') {
                    content = val;
                } else {
                    content = "{" + val + "}";
                }
            } else if (inputType === "checkbox") {
                debugLog("Checkbox type: " + inputType + ", id: " + node.id);
                content = "☐";
                if (node.checked) {
                    content = "✅";
                }
            } else if (inputType === "radio") {
                content = "🔘";
                if (node.checked) {
                    content = "🟢";
                }
            } else if (node.tagName.toLowerCase() === 'select') {
                // If it's a <select>, get the selected option text
                let selectedOption = node.querySelector('option:checked');
                let val = (selectedOption ? selectedOption.textContent.trim() : "");

                if (!val) {
                    let labelText = getLabelText(node, {});
                    debugLog("Calling getLabelText for select with no selected Option: " + node.tagName + ", id: " + node.id + ", labelText: " + labelText);
                    val = labelText || "No option selected";
                }
                if (node.hasAttribute('disabled') || node.getAttribute('aria-disabled')?.toLowerCase() === 'true') {
                    content = val;
                } else {
                    content = "{{" + val + "}}";
                }
                const options = node.querySelectorAll('option');
                for (let option of options) {
                    let optionText = option.textContent.trim() || node.value;
                    if (optionText === val) continue; // Skip the selected option
                    content += `|| - ${optionText}`;
                }
            } else {
                content = ""
            }
            if (content) {
                debugLog("Pushing input node: " + content);
                addSegment({
                    type: 'input',
                    content: content,
                    x: rect.left,
                    y: rect.top,
                    width: rect.width,
                    height: rect.height,
                    xpath: generateXPathJSInline(node),
                    locatorString: generateLocator(node)
                });
                // After handling an <input>, we don't descend into its children
                // (since an <input> typically doesn't have child text nodes to handle)
                return;
            }
        }
        if (elementRole === 'button' || elementRole === 'link' || elementRole === 'option' || 
            node.tagName.toLowerCase() === 'button' || node.tagName.toLowerCase() === 'a' ||
            node.tagName.toLowerCase() === 'img' || node.tagName.toLowerCase() === 'svg') {

            const elementNodeRec = node.getBoundingClientRect();
            if (isRectObscured(elementNodeRec, node)) return;
            let content;
            const interaLevel = getInteractiveLevelClimb(node);
            let hasVisibleText = false;
            // checks visible text elements inside. If there are visible texts, let traversal to children happen
            // sometimes there could be multiple fragmented text nodes for presentation purposes, they will each be annotated
            const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const textNode = walker.currentNode;
                const trimmed = textNode.textContent.trim();
                if (!trimmed) {
                    __alreadyProcessedNodes.add(textNode);
                    continue;
                }
                const parentEl = textNode.parentElement;
                if (isVisibleCheckCached(parentEl) && !isFontIcon(parentEl)) {
                    hasVisibleText = true;
                    break;
                } else {
                    __alreadyProcessedNodes.add(textNode);
                }
            }
            if (!hasVisibleText) { // icon or svg or other buttons/anchors without text
                debugLog("Empty button/anchor: " + node.tagName + ", id: " + node.id + ", aria-label: " + node.ariaLabel + ", title: " + node.title);
                if (node.hasAttribute('disabled')) return;
                const fallbackValue = node.getAttribute("value") || "";

                // Check for any image with alt (even aria-hidden ones)
                const hasAnyImage = !!node.querySelector('img');
                let inFigureWithImage = false;
                let current = node.parentElement;
                while (current) {
                    if (current.tagName.toLowerCase() === 'figure') {
                        inFigureWithImage = !!current.querySelector('img');
                        break; // Stop at the first figure
                    }
                    current = current.parentElement;
                }
                if (hasAnyImage || inFigureWithImage) {
                    content = "[IMG]";
                } else {
                    if (fallbackValue) {
                        content = `[${fallbackValue}]`;
                    } else {
                        content = getInteractiveContentByLevel(interaLevel);
                    }
                }
                debugLog(`Empty button/anchor: content to be added: "${content}"`);
                // Check for image label
                let labelText = '';
                const imageWithAlt = Array.from(node.querySelectorAll('img[alt]')).find(img => {
                    const alt = img.getAttribute('alt')?.trim();
                    const ariaHidden = img.getAttribute('aria-hidden')?.toLowerCase();
                    return alt && ariaHidden !== 'true';
                });
                const svgWithAriaLabel = Array.from(node.querySelectorAll('svg[aria-label]')).find(svg => {
                    const ariaLabel = svg.getAttribute('aria-label')?.trim();
                    const ariaHidden = svg.getAttribute('aria-hidden')?.toLowerCase();
                    return ariaLabel && ariaHidden !== 'true';
                });
                if (imageWithAlt) {
                    labelText = imageWithAlt.getAttribute('alt').trim();
                    debugLog(`Empty button/anchor: Used <img alt> for labelText: "${labelText}"`);
                } else if (svgWithAriaLabel) {
                    labelText = svgWithAriaLabel.getAttribute('aria-label').trim();
                    debugLog(`Empty button/anchor: Used <svg aria-label> for labelText: "${labelText}"`);
                } else {
                    labelText = getLabelText(node, {
                        includeAriaLabel: true,
                        includeTitle: true,
                        includeTextContent: true
                    });
                    debugLog(`Empty button/anchor: getLabelText result: "${labelText}"`);
                }
                addSegment({
                    type: 'text',
                    content: content,
                    enclose: 0,
                    x: elementNodeRec.left,
                    y: elementNodeRec.top,
                    width: elementNodeRec.width,
                    height: elementNodeRec.height,
                    xpath: generateXPathJSInline(node),
                    locatorString: generateLocator(node),
                    labelText: labelText
                });
                return;
            }
        } else if (
            (elementRole === 'textbox' || elementRole === 'combobox') &&
            node.hasAttribute('contenteditable') &&
            node.getAttribute('contenteditable')?.toLowerCase() !== 'false'
        ) {
            // Handle contenteditable elements
            const rect = node.getBoundingClientRect();
            if (isRectObscured(rect, node)) return;
            let text = node.textContent.trim();
            if (!text) {
                text = node.getAttribute('placeholder') || "Enter Value";
            }

            text = `{${text.replace(/[{}]/g, '')}}`;
            addSegment({
                type: 'text',
                content: text,
                enclose: 0,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(node),
                locatorString: generateLocator(node)
            });
            return; // Don't traverse children of contenteditable elements
        }

        // TODO: Better handling of customization
        if (node.classList && node.classList.contains('hot-container') &&
            !node.closest('.modal-hot-container') // ✅ skip if inside modal-body
        ) {
            const rect = node.getBoundingClientRect();
            if (isRectObscured(rect, node)) return;
            addSegment({
                type: 'text',
                content: "[Click To Edit]",
                enclose: 0,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(node),
                locatorString: generateLocator(node)
            });
        }

        if (node.tagName.toLowerCase() === 'td' &&
            node.parentElement?.tagName.toLowerCase() === 'tr' &&
            node.closest('hot-table') && // ✅ ensure it's inside a <hot-table>
            node.closest('.modal-hot-container')) { // ✅ ensure it's inside a modal-hot-container
            const rect = node.getBoundingClientRect();
            if (isRectObscured(rect, node)) return;
            let textContent = node.textContent.trim();
            if (isFillableTdCell(node)) {
                if (!textContent) {
                    textContent = "Fill Data"
                }
                textContent = "{" + textContent + "}";
            }
            addSegment({
                type: 'text',
                content: textContent,
                enclose: 0,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
                xpath: generateXPathJSInline(node),
                locatorString: generateLocator(node)
            });
            return;
        }

        // Recursively descend for other visible elements
        if (node.shadowRoot) {
            debugLog("Shadow DOM detected: " + node.tagName + ", id: " + node.id);
            traverse(node.shadowRoot);
        }
        for (let child of node.childNodes) {
            traverse(child);
        }
    } else if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
        debugLog("Document fragment detected" + node.tagName + ", id: " + node.id + ", class: " + node.classList);
        for (let child of node.childNodes) {
            traverse(child);
        }
    }
}

let counter = 0;
let segments = [];
traverse(document.body);

if (__topModalCache) {
    const rect = __topModalCache.getBoundingClientRect();
    // Add modal segment at the very beginning of the segments array
    segments.unshift({
        type: 'text',
        content: '‡modal‡',
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        xpath: generateXPathJSInline(__topModalCache),
        locatorString: generateLocator(__topModalCache),
    });
}