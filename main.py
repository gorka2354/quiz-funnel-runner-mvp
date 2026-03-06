import json, argparse, os, time, re, hashlib, shutil
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError
from concurrent.futures import ThreadPoolExecutor

COOKIE_BLACKLIST = [
    "settings", "preferences", "customize", "options", "more info", 
    "einstellungen", "optionen", "mehr informationen", 
    "cookie settings", "privacy settings", "datenschutzeinstellungen",
    "manage", "preferences"
]

SHARE_BLACKLIST = [
    "share", "sharing", "tell a friend", "invite", "recommend",
    "поделиться", "поделись", "рассказать", "пригласить",
    "view more", "show less", "read more", "back", "zurück", "назад"
]

def is_forbidden_button(el, log_func=None) -> bool:
    try:
        txt = (el.inner_text() or "").lower()
        alabel = (el.get_attribute("aria-label") or "").lower()
        cls = (el.get_attribute("class") or "").lower()
        html = (el.evaluate("el => el.innerHTML") or "").lower()

        full_text = f"{txt} {alabel} {cls} {html}"

        forbidden = False
        reason = ""
        if any(word in full_text for word in COOKIE_BLACKLIST): 
            forbidden = True; reason = "cookie"

        # Use regex with word boundaries for ALL forbidden words to avoid false positives
        forbidden_words = [
            "share", "sharing", "tell a friend", "invite", "recommend",
            "поделиться", "поделись", "рассказать", "пригласить",
            "view more", "show less", "read more", "back", "zurück", "назад"
        ]
        pattern = r'\b(' + '|'.join(re.escape(w) for w in forbidden_words) + r')\b'

        if re.search(pattern, full_text):
            forbidden = True; reason = "forbidden_ui"

        if forbidden and log_func:
            log_func(f"forbidden_skipped={reason} | text: {txt[:30]}")
        return forbidden
    except: return False

def get_choices_text(page: Page, log_func=None):
    try:
        choice_sel = [
            "[data-testid*='answer' i]:visible", 
            "button:visible", 
            "[class*='Item' i]:visible", 
            "[class*='Card' i]:visible", 
            "label:visible"
        ]
        for s in choice_sel:
            els = page.locator(s)
            texts = []
            for i in range(els.count()):
                curr = els.nth(i)
                if is_forbidden_button(curr, log_func): continue
                txt = (curr.inner_text() or "").strip()
                if txt and not re.search(r'\d+\s*/\s*\d+', txt) and len(txt) < 100:
                    texts.append(txt)
            if texts:
                return "|".join(texts[:3])
        return ""
    except: return ""

def get_ui_step(page: Page):
    try:
        # Improved regex: require total >= 10 to avoid 24/7, or ensure current <= total
        def extract_step(text):
            # Look for patterns like "3 / 23" or "3/23"
            matches = re.finditer(r'(\d+)\s*/\s*(\d+)', text)
            for m in matches:
                curr, total = int(m.group(1)), int(m.group(2))
                # Heuristic: quiz progress usually has total > 5 and curr <= total
                # and we specifically ignore 24/7
                if total > 5 and curr <= total and f"{curr}/{total}" != "24/7":
                    return f"{curr}/{total}"
            return None

        # Look in visible text first
        t = page.evaluate("() => document.body.innerText")
        res = extract_step(t)
        if res: return res
        
        # Look in all elements if not found
        res = page.evaluate(r"""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            let node;
            while(node = walker.nextNode()) {
                const m = node.textContent.match(/(\d+)\s*\/\s*(\d+)/);
                if (m) {
                    const c = parseInt(m[1]), t = parseInt(m[2]);
                    if (t > 5 && c <= t && m[0].trim() !== "24/7") return m[0].trim();
                }
            }
            return null;
        }""")
        if res: return res.replace(" ", "")
    except: pass
    return "unknown"

def get_slug(url: str):
    p = urlparse(url); d = p.netloc.replace('www.', ''); pth = p.path.strip('/').replace('/', '-')
    slug = f"{d}-{pth}" if pth else d
    if p.query:
        q_hash = hashlib.md5(p.query.encode()).hexdigest()[:6]
        slug = f"{slug}-{q_hash}"
    return slug

def get_screen_hash(page: Page):
    try:
        t = page.evaluate("() => (document.body.innerText || '').slice(0, 10000)")
        return hashlib.md5(t.encode('utf-8')).hexdigest()
    except: return ""

def close_popups(page: Page, log_func):
    try:
        whitelist = ['Accept', 'Accept all', 'Allow all', 'I agree', 'Agree', 'Принять', 'Принять все', 'Разрешить', 'Согласен', 'Alle akzeptieren', 'Alle ablehnen']
        clicked = False
        cookie_found = False
        
        btns = page.locator("button:visible, [role='button']:visible, a.button:visible")
        for i in range(btns.count()):
            btn = btns.nth(i)
            txt = (btn.inner_text() or "").strip()
            if not txt: continue
            if is_forbidden_button(btn, log_func):
                cookie_found = True
                continue
            
            lower_txt = txt.lower()
            if any(w.lower() == lower_txt or w.lower() in lower_txt for w in whitelist):
                cookie_found = True
                try:
                    btn.click(timeout=1000)
                    log_func(f"cookie_action=clicked_normal | text: {txt}")
                except:
                    btn.click(force=True, timeout=1000)
                    log_func(f"cookie_action=clicked_force | text: {txt}")
                clicked = True; break
        
        hidden_provider = page.evaluate("""() => {
            const providers = [
                '#onetrust-banner-sdk', '#onetrust-accept-btn-handler', '.onetrust-close-btn-handler',
                '#truste-consent-track', '#consent_blackbar',
                '.qc-cmp2-container', '.qc-cmp2-summary-buttons button',
                '#cookie-law-info-bar', '#cookie_action_close_header', '.cky-btn-accept'
            ];
            let hidden = false;
            providers.forEach(s => { 
                document.querySelectorAll(s).forEach(el => {
                    if (el.style.display !== 'none') {
                        el.style.display = 'none';
                        hidden = true;
                    }
                });
            });
            return hidden;
        }""")
        
        if hidden_provider:
            cookie_found = True
            log_func(f"cookie_action=hidden_provider_css")

        hidden_fallback = page.evaluate("""() => {
            let hidden = false;
            const els = Array.from(document.querySelectorAll('*')).filter(el => {
                const style = window.getComputedStyle(el);
                return (style.position === 'fixed' || style.position === 'sticky') && style.display !== 'none';
            });
            
            els.forEach(el => {
                const t = el.innerText.toLowerCase();
                if (t.includes('cookie') || t.includes('consent') || t.includes('privacy')) {
                    const primary = ['continue', 'next', 'submit', 'get my plan', 'claim', 'spin', 'start'];
                    if (!primary.some(w => t.includes(w))) { 
                        el.style.display = 'none'; 
                        hidden = true;
                    }
                }
            });
            return hidden;
        }""")
        
        if hidden_fallback:
            cookie_found = True
            log_func(f"cookie_action=hidden_fallback_css")
        
        if cookie_found:
            log_func(f"cookie_found=true")
            
    except: pass


def ensure_privacy_checkbox_checked(page: Page, log_func) -> bool:
    try:
        keywords = ["I have read and understood", "consent to the processing", "personal data"]
        found_text = None
        for kw in keywords:
            elements = page.get_by_text(kw, exact=False)
            for i in range(elements.count()):
                el = elements.nth(i)
                if el.is_visible(timeout=500): found_text = el; break
            if found_text: break
        
        if not found_text: return False
        container = found_text.locator("xpath=./ancestor::*[self::label or self::div][1]").first
        checkbox = container.locator("input[type='checkbox']").first
        if checkbox.count() > 0:
            if not checkbox.is_checked(): checkbox.click(force=True, timeout=1000)
            return checkbox.is_checked()
        container.click(force=True, timeout=1000)
        return True
    except: return False

DEBUG_CLASSIFY = True

def classify_screen(page: Page, log_func):
    def debug_return(ctype, reason):
        if DEBUG_CLASSIFY:
            log_func(f"classify_reason={reason}")
        return ctype

    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    # 1. Checkout
    checkout_kws = ["card number", "cvv", "mm/yy", "confirm payment", "paypal", "buy now"]
    if any(k in t for k in checkout_kws) or page.locator("input[name*='card']").count() > 0:
        return debug_return('checkout', "Checkout keywords or card inputs found")

    # 2. Paywall
    has_price = any(k in t for k in ["€", "$", "£", "₽"]) or re.search(r'\d+[.,]\d+\s*[€$£₽]', t)        
    has_billing = any(k in t for k in ["subscribe", "trial", "billed", "week plan", "month plan", "pricing", "plans"])
    has_cta = any(k in t for k in ["get my plan", "checkout", "start trial"])
    if ("coursiv.io" in u and "selling-page" in u) or (has_price and has_billing and has_cta):
        return debug_return('paywall', "Paywall signals detected")

    # 3. Game/Prize/Spin screens (should be info)
    game_kws = ["spin", "wheel", "prize"]
    if any(k in t for k in game_kws) or "prize-wheel" in u:
        # Check if there is an actual SPIN button
        btn_text = (page.evaluate("() => Array.from(document.querySelectorAll('button, a, [role=\"button\"]')).map(el => el.innerText).join(' ')") or "").lower()
        if any(k in btn_text for k in game_kws) or "prize-wheel" in u:
            return debug_return('info', "Game/Prize screen detected (keyword in buttons/URL)")

    # 4. Email
    has_email_input = page.locator("input[type='email'], input[autocomplete*='email' i]").count() > 0
    if not has_email_input:
        inputs = page.locator("input:not([type='hidden'])")
        for i in range(inputs.count()):
            try:
                p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
                if any(k in p for k in ["email", "e-mail", "mail@"]): has_email_input = True; break
            except: pass

    # If attributes are not enough, check if there is an input AND "email" keywords in text/URL
    if not has_email_input and page.locator("input:not([type='hidden'])").count() > 0:
        if any(k in t for k in ["email", "e-mail", "электронная почта", "адрес почты"]):
            # Only if it's not a profile data screen (age, weight, etc.)
            pd_kws = ["age", "height", "weight", "name", "рост", "вес", "возраст", "имя"]
            if not any(k in t for k in pd_kws):
                has_email_input = True

    if has_email_input: return debug_return('email', "Email field found via attributes or text")

    # 4. Input (with animation safety)
    pd_kws = ["age", "height", "weight", "name", "cm", "kg", "years", "call you"]
    if any(k in t for k in pd_kws) or any(k in u for k in ["name", "age", "weight", "height"]):
        try: page.wait_for_selector("input:not([type='hidden']):visible", timeout=2000)
        except: pass
    
    inputs = page.locator("input:not([type='hidden']):visible, textarea:visible")
    if inputs.count() >= 1: return debug_return('input', f"{inputs.count()} input(s) found")

    # 5. Question vs Info (Smart separation)
    nav_words = [
        "next", "continue", "skip", "back", "weiter", "zurück", "next step", "proceed", 
        "got it", "ok", "okay", "great", "understood", "yes", "i'm in", "let's go", 
        "see results", "start", "принять", "ок", "начать", "понятно", "хорошо",
        "do it", "ready", "begin", "let's", "go", "transformation"
    ]
    
    # Get all potential interactive elements
    # Broaden selectors to include 'a' and 'div[role="button"]' as requested
    raw_els = page.locator("[data-testid*='answer' i]:visible, [class*='Card' i]:not([class*='testimonial' i]):not([class*='review' i]):visible, label:visible, button:visible, a:visible, [role='button']:visible, div[role='button']:visible")
    
    choices = []
    nav_btns = []
    
    for i in range(raw_els.count()):
        el = raw_els.nth(i)
        if is_forbidden_button(el, log_func): continue
        txt = (el.inner_text() or "").lower().strip()
        
        # If element is visible and interactive but has no text (like cups in DanceBit), it's likely a choice
        if not txt:
            tag = el.evaluate("el => el.tagName").lower()
            if tag in ['button', 'label', 'input']:
                choices.append("empty_choice")
            continue

        # Check if the text matches any navigation keyword
        is_nav = False
        for w in nav_words:
            if txt == w or txt.startswith(w + "!") or txt.startswith(w + ".") or (len(txt) < 20 and w in txt):
                is_nav = True
                break
        
        if is_nav:
            nav_btns.append(txt)
        else:
            # Even short or numeric texts can be choices
            choices.append(txt)

    # Logic rules
    if len(choices) >= 2:
        return debug_return('question', f"Multiple choices found ({len(choices)})")
    
    # If we have 0 or 1 choice but at least one navigation/CTA button, it's an Info screen
    if len(nav_btns) >= 1:
        # Check if the single 'choice' is actually just the same as a nav button
        return debug_return('info', f"Info screen: {len(nav_btns)} CTA button(s) found, insufficient choices for a question")

    # Fallback for questions without clear buttons
    if "?" in t and len(choices) >= 2:
        return debug_return('question', "Question mark + multiple options detected")

    if len(nav_btns) >= 1:
        return debug_return('info', "Fallback info: navigation buttons found")

    return debug_return('other', "No clear type detected")


def find_continue_button(page: Page, log_func=None):
    keywords = [
        'Continue', 'Next', 'Get my plan', 'Start', 'Got it', 'Take the quiz', 
        'Get started', 'Start quiz', 'Get my offer', 'Next step', 'Proceed', 
        'Submit', 'Show my results', 'See my results', "Let's", "Do it", "I'm in"
    ]
    # Restrict to actual interactive elements to avoid picking up headlines/prompts
    button_locator = page.locator("button:visible, [role='button']:visible, a.button:visible, a:visible")
    
    for text in keywords:
        # We search within buttons/links for the text
        btns = button_locator.get_by_text(text, exact=False)
        if btns.count() > 0:
            btn = btns.first
            if btn.is_visible(timeout=500):
                if not is_forbidden_button(btn, log_func): return btn

    # We purposefully do not fallback to random buttons here, as it causes 
    # the bot to mistakenly click choice variants as 'Next' buttons.
    return None

def wait_for_transition(page: Page, old_url: str, old_hash: str, timeout=10.0):
    start = time.time()
    while time.time() - start < timeout:
        if page.url != old_url or get_screen_hash(page) != old_hash:
            return True
        time.sleep(0.5)
    return False

NAV_KEYWORDS = [
    "next", "continue", "skip", "back", "weiter", "zurück", "next step", "proceed", 
    "got it", "ok", "принять", "ок", "start", "get started", "take the quiz", 
    "accept", "allow", "agree", "alle akzeptieren", "alle ablehnen"
]

def is_nav_button(text: str) -> bool:
    if not text: return False
    t = text.lower().strip()
    return any(w == t or t.startswith(w + " ") for w in NAV_KEYWORDS)

def perform_action(page: Page, screen_type: str, log_func, results_dir: str, start_hash: str, start_url: str):
    try:
        if screen_type == 'paywall': return "stopped at paywall"
        if screen_type == 'checkout': return "checkout reached"

        if screen_type in ['email', 'input']:
            checked = ensure_privacy_checkbox_checked(page, log_func)
            if checked: log_func("privacy_policy_checked=true")
            
            # Wait for inputs to be visible
            try: page.wait_for_selector("input:visible, textarea:visible", timeout=3000)
            except: pass
            
            inputs = page.locator("input:visible, textarea:visible")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                itype = (inp.get_attribute("type") or "text").lower()
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                
                val = "John"
                if screen_type == 'email' or any(k in placeholder for k in ["email", "e-mail"]):
                    val = f"testuser{int(time.time())}@gmail.com"
                
                context_text = (page.evaluate("() => document.body.innerText") or "").lower()
                context_url = page.url.lower()
                
                # Use word boundaries for numeric keywords to avoid false positives like 'age' in 'magic-page'
                num_pattern = r'\b(age|height|weight|возраст|рост|вес|bmi|goal|цель)\b'
                is_numeric = itype == "number" or re.search(num_pattern, placeholder) or re.search(num_pattern, context_url)
                
                if is_numeric:
                    if any(k in placeholder or k in context_url or k in context_text for k in ["height", "рост"]): val = "170"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["goal", "цель"]): val = "60"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["weight", "вес"]): val = "70"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["age", "возраст"]): val = "30"
                    else: val = "25"
                
                try:
                    log_func(f"Filling input: itype={itype}, val={val}")
                    inp.fill(val)
                    time.sleep(0.3)
                except Exception as e:
                    log_func(f"Fill error: {str(e)[:50]}")

            btn = find_continue_button(page, log_func)
            if btn:
                log_func(f"Clicking continue: {btn.inner_text()}")
                close_popups(page, log_func)
                try:
                    btn.click(force=True, timeout=2000)
                except:
                    page.keyboard.press("Enter")
            else: page.keyboard.press("Enter")
            log_func("Input submitted. Waiting for transition...")
            wait_for_transition(page, start_url, start_hash, timeout=12.0)
            return "input_submitted"

        if screen_type in ['question', 'info', 'other']:
            cont_btn = find_continue_button(page, log_func)
            # Priority to "Start" buttons
            if cont_btn and any(k in (cont_btn.inner_text() or "").lower() for k in ["start", "get my", "get started", "take the", "offer", "claim", "discount", "spin"]):
                log_func(f"Landing/Start button found: {cont_btn.inner_text()}. Clicking...")
                close_popups(page, log_func)
                cont_btn.click(force=True, timeout=1000)
                wait_for_transition(page, start_url, start_hash)
                return "start_button_pressed"

            choice_sel = [
                "[data-testid*='answer' i]:visible", 
                "button:visible", 
                "[class*='Item' i]:visible", 
                "[class*='Card' i]:visible", 
                "label:visible"
            ]
            target = None
            # Pass 1: Try all selectors to find a choice WITH text (excluding Nav)
            for s in choice_sel:
                els = page.locator(s)
                for i in range(els.count()):
                    curr = els.nth(i)
                    if is_forbidden_button(curr, log_func): continue
                    txt = (curr.inner_text() or "").strip()
                    if txt and not re.search(r'^\d+\s*/\s*\d+$', txt) and len(txt) < 100 and not is_nav_button(txt):
                        target = curr; break
                if target: break

            # Pass 2: If no text choices found, try all selectors for empty choices
            if not target:
                for s in choice_sel:
                    els = page.locator(s)
                    for i in range(els.count()):
                        curr = els.nth(i)
                        if is_forbidden_button(curr, log_func): continue
                        if not (curr.inner_text() or "").strip():
                            tag = curr.evaluate("el => el.tagName").lower()
                            if tag in ['button', 'input']:
                                target = curr; break
                    if target: break

            if not target:
                if cont_btn:
                    log_func(f"No choices, clicking continue: {cont_btn.inner_text()}")
                    close_popups(page, log_func)
                    cont_btn.click(force=True, timeout=1000)
                    wait_for_transition(page, start_url, start_hash)
                    return "info_continue_pressed"
                return "no_choices_found"
            start_choices = get_choices_text(page, log_func)
            start_ui = get_ui_step(page)
            # 1. Click choice
            log_func(f"Clicking choice: {target.inner_text()[:50].strip()}")
            try:
                target.scroll_into_view_if_needed(timeout=2000)
            except: pass
            close_popups(page, log_func)
            try:
                target.click(force=True, timeout=2000)
            except Exception as e:
                log_func(f"Click error: {str(e)[:50]}")
            
            # Short wait for auto-advance or progress change
            log_func("Waiting for auto-transition...")
            start_wait = time.time()
            transitioned_auto = False
            while time.time() - start_wait < 2.0:
                curr_ui = get_ui_step(page)
                curr_hash = get_screen_hash(page)
                if curr_ui != start_ui or curr_hash != start_hash:
                    log_func(f"Auto-transition detected (ui_step: {start_ui} -> {curr_ui})")
                    transitioned_auto = True
                    break
                time.sleep(0.3)
            
            if transitioned_auto:
                return "auto_advanced"

            # 2. Wait for Next button if no auto-advance
            start_time = time.time()
            clicked_continue = False
            while time.time() - start_time < 3.0:
                curr_choices = get_choices_text(page, log_func)
                curr_ui = get_ui_step(page)
                if (start_choices and curr_choices and curr_choices != start_choices) or (curr_ui != start_ui):
                    break

                # Safe URL check: only break if the PATH changes
                if urlparse(page.url).path != urlparse(start_url).path:
                    break 

                curr_cont = find_continue_button(page, log_func)
                if curr_cont and curr_cont.is_enabled():
                    log_func(f"Continue button found: {curr_cont.inner_text()}. Clicking...")
                    close_popups(page, log_func)
                    pre_cont_hash = get_screen_hash(page)
                    try:
                        curr_cont.click(force=True, timeout=2000)
                    except: pass
                    clicked_continue = True
                    wait_for_transition(page, page.url, pre_cont_hash, timeout=10.0)
                    return "continue_clicked"
                time.sleep(0.5)

            # 3. Multiselect
            if not clicked_continue:
                curr_cont = find_continue_button(page, log_func)
                if curr_cont and not curr_cont.is_enabled():
                    log_func("Multiselect detected. Selecting more options...")
                    for s in choice_sel:
                        els = page.locator(s)
                        for i in range(1, min(els.count(), 5)):
                            curr = els.nth(i)
                            txt = (curr.inner_text() or "").strip()
                            if txt and not re.search(r'^\d+\s*/\s*\d+$', txt) and not is_forbidden_button(curr, log_func):
                                close_popups(page, log_func)
                                curr.click(force=True, timeout=500)
                                if curr_cont.is_enabled():
                                    close_popups(page, log_func)
                                    pre_multi_hash = get_screen_hash(page)
                                    curr_cont.click(force=True, timeout=1000)
                                    wait_for_transition(page, page.url, pre_multi_hash, timeout=10.0)   
                                    return "multiselect_completed"

            wait_for_transition(page, start_url, start_hash, timeout=5.0)
            return "screen_interaction_completed"                
    except Exception as e: return f"err:{str(e)}"
    return "none"

def run_funnel(url: str, config: dict, is_headless: bool):
    slug = get_slug(url); res_dir = os.path.join('results', slug); os.makedirs(res_dir, exist_ok=True)
    classified_dir = os.path.join('results', '_classified')
    for cat in ['question', 'info', 'input', 'email', 'paywall', 'other', 'checkout']:
        os.makedirs(os.path.join(classified_dir, cat), exist_ok=True)
    
    summary = {"url": url, "slug": slug, "steps_total": 0, "paywall_reached": False, "last_url": "", "path": res_dir, "error": None}
    
    with open(os.path.join(res_dir, 'log.txt'), 'w', encoding='utf-8') as f:
        def log(m):
            l = f"[{time.strftime('%H:%M:%S')}] {m}\n"; f.write(l); print(l.strip())
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=is_headless, slow_mo=100)
            page = browser.new_context(**p.devices['iPhone 13']).new_page()
            log(f"Navigating to {url} (slug: {slug})"); page.goto(url, wait_until='load', timeout=60000)
            
            step, history = 1, []
            while step <= 80:
                curr_u = page.url
                if any(k in curr_u for k in ["magic", "analyzing", "loading", "preparePlan"]): 
                    time.sleep(10); curr_u = page.url
                
                close_popups(page, log)
                time.sleep(1)
                curr_h = get_screen_hash(page)
                st = classify_screen(page, log)
                ui_before = get_ui_step(page)
                
                curr_id = f"{curr_u}|{curr_h}"
                if history.count(curr_id) >= 3:
                    log(f"Stuck at {curr_u}. Stopping."); summary["error"] = "stuck_loop"; break
                history.append(curr_id)
                
                screen_name = f"{step:02d}_{st}.png"
                local_path = os.path.join(res_dir, screen_name)
                page.screenshot(path=local_path, full_page=True)
                shutil.copy2(local_path, os.path.join(classified_dir, st, f"{slug}__{screen_name}"))
                
                log(f"step:{step} | type:{st} | ui_step:{ui_before} | url:{page.url[:60]}")
                act = perform_action(page, st, log, res_dir, curr_h, curr_u)
                
                # Check for double skip
                time.sleep(1)
                ui_after = get_ui_step(page)
                
                def parse_ui(s):
                    if not s or s == "unknown": return -1
                    m = re.match(r'(\d+)', s)
                    return int(m.group(1)) if m else -1
                
                ui_b_num = parse_ui(ui_before)
                ui_a_num = parse_ui(ui_after)
                
                if ui_a_num > ui_b_num + 1 and ui_b_num > 0:
                    log(f"skip_detected=true | UI jumped from {ui_before} to {ui_after}")
                
                log(f"action_result:{act}")
                
                summary["steps_total"] = step; summary["last_url"] = page.url
                if st in ['paywall', 'checkout'] or "stopped" in act or "reached" in act:
                    if st == 'paywall': summary["paywall_reached"] = True
                    break
                
                step += 1
            browser.close()
    return summary

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='config.json', help='Path to config file')
    parser.add_argument('--parallel', action='store_true', help='Run funnels in parallel threads')
    parser.add_argument('--headless', type=str, help='Override headless mode (true/false)')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = json.load(f)
    
    funnels = config.get('funnels', [])
    max_f = config.get('max_funnels')
    if max_f is not None:
        funnels = funnels[:max_f]
    
    headless = config.get('headless', True)
    if args.headless is not None:
        headless = args.headless.lower() == 'true'

    if args.parallel:
        print(f"\n--- Starting {len(funnels)} funnels in PARALLEL mode ---\n")
        with ThreadPoolExecutor(max_workers=len(funnels)) as executor:
            futures = [executor.submit(run_funnel, url, config, headless) for url in funnels]
            all_summaries = [f.result() for f in futures]
    else:
        print(f"\n--- Starting {len(funnels)} funnels in SEQUENTIAL mode ---\n")
        all_summaries = []
        for url in funnels:
            print(f"\n>> Processing: {url}")
            summary = run_funnel(url, config, headless)
            all_summaries.append(summary)
    
    with open(os.path.join('results', 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(all_summaries, f, indent=4)
    
    print("\nBatch run completed.")
    for s in all_summaries:
        status = "PASSED" if s['paywall_reached'] else "FAILED"
        print(f"[{status}] URL: {s['url']} | Steps: {s['steps_total']}")
