import json, argparse, os, time, re, hashlib, shutil
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError

COOKIE_BLACKLIST = [
    "settings", "preferences", "customize", "options", "more info", 
    "einstellungen", "optionen", "mehr informationen", 
    "cookie settings", "privacy settings", "datenschutzeinstellungen",
    "manage", "preferences"
]

def is_cookie_settings(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return any(word in t for word in COOKIE_BLACKLIST)

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
        whitelist = ['Accept', 'Accept all', 'Allow all', 'I agree', 'Agree', 'OK', 'Got it', 'Continue', 'Принять', 'Принять все', 'Разрешить', 'Согласен', 'ОК']
        clicked = False
        cookie_found = False
        
        btns = page.locator("button:visible, [role='button']:visible, a.button:visible")
        for i in range(btns.count()):
            btn = btns.nth(i)
            txt = (btn.inner_text() or "").strip()
            if not txt: continue
            if is_cookie_settings(txt):
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

def classify_screen(page: Page, log_func):
    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    # Site-specific for Coursiv
    if "coursiv.io" in u and "selling-page" in u:
        return 'paywall'

    if any(k in t for k in ["card number", "cvv", "mm/yy", "confirm payment"]) or page.locator("input[name*='card']").count() > 0:
        return 'checkout'

    signals = []
    if any(k in t for k in ["€", "$", "£", "₽"]) or re.search(r'\d+[.,]\d+\s*[€$£₽]', t): signals.append("price")
    if any(k in t for k in ["subscribe", "trial", "billed", "week plan", "month plan"]): signals.append("billing")
    if any(k in t for k in ["get my plan", "checkout", "start trial"]): signals.append("cta")

    if len(signals) >= 2 and not re.search(r'\d+/\d+', t): return 'paywall'
    
    inputs = page.locator("input:visible")
    if ("email" in u or "email" in t) and inputs.count() > 0: return 'email'
    for i in range(inputs.count()):
        p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
        if any(k in p for k in ["email", "mail"]): return 'email'
    
    return 'question'

def find_continue_button(page: Page):
    keywords = [
        'Continue', 'Next', 'Get my plan', 'Start', 'Got it', 'Take the quiz', 
        'Get started', 'Start quiz', 'Get my offer', 'Next step', 'Proceed', 
        'Submit', 'Show my results', 'See my results'
    ]
    for text in keywords:
        btn = page.get_by_text(text, exact=False).first
        if btn.is_visible(timeout=500):
            txt = (btn.inner_text() or "").strip()
            if not is_cookie_settings(txt): return btn

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

def get_question_text(page: Page):
    try:
        els = page.locator("h1:visible, h2:visible, h3:visible, [class*='title' i]:visible, [class*='heading' i]:visible")
        for i in range(els.count()):
            txt = (els.nth(i).inner_text() or "").strip()
            if len(txt) > 2: return txt
        return ""
    except: return ""

def perform_action(page: Page, screen_type: str, log_func, results_dir: str, start_hash: str, start_url: str):
    try:
        if screen_type == 'paywall': return "stopped at paywall"
        if screen_type == 'checkout': return "checkout reached"

        if screen_type == 'email':
            checked = ensure_privacy_checkbox_checked(page, log_func)
            if checked: log_func("privacy_policy_checked=true")
            email_input = page.locator("input:visible").first
            email_input.fill(f"testuser{int(time.time())}@gmail.com")
            time.sleep(0.5)
            btn = find_continue_button(page)
            if btn:
                log_func(f"Clicking email continue: {btn.inner_text()}")
                close_popups(page, log_func)
                btn.click(force=True, timeout=1000)
            else: page.keyboard.press("Enter")
            log_func("Email submitted. Waiting for transition...")
            wait_for_transition(page, start_url, start_hash, timeout=12.0)
            return "email_submitted"

        if screen_type == 'question':
            cont_btn = find_continue_button(page)
            # Priority to "Start" buttons
            if cont_btn and any(k in (cont_btn.inner_text() or "").lower() for k in ["start", "get my", "get started", "take the", "offer"]):
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
            for s in choice_sel:
                els = page.locator(s)
                for i in range(els.count()):
                    curr = els.nth(i)
                    txt = (curr.inner_text() or "").strip()
                    if txt and not re.search(r'^\d+\s*/\s*\d+$', txt) and len(txt) < 100 and not is_cookie_settings(txt):
                        target = curr; break
                if target: break

            if not target:
                if cont_btn:
                    log_func(f"No choices with text, clicking continue: {cont_btn.inner_text()}")
                    close_popups(page, log_func)
                    cont_btn.click(force=True, timeout=1000)
                    wait_for_transition(page, start_url, start_hash)
                    return "info_continue_pressed"
                return "no_choices_found"

            start_q_text = get_question_text(page)

            # 1. Click choice
            log_func(f"Clicking choice: {target.inner_text()[:50].strip()}")
            target.scroll_into_view_if_needed()
            close_popups(page, log_func)
            target.click(force=True, timeout=2000)
            time.sleep(1.0)

            # 2. Wait for auto-transition or Next button
            start_time = time.time()
            clicked_continue = False
            while time.time() - start_time < 5.0:
                curr_q_text = get_question_text(page)
                if start_q_text and curr_q_text and curr_q_text != start_q_text:
                    # Content significantly changed, we transitioned!
                    break

                if not start_q_text and page.url != start_url:
                    break 

                curr_cont = find_continue_button(page)
                if curr_cont and curr_cont.is_enabled():
                    log_func(f"Continue button found after choice: {curr_cont.inner_text()}. Clicking...")
                    close_popups(page, log_func)
                    pre_cont_hash = get_screen_hash(page)
                    curr_cont.click(force=True, timeout=1000)
                    clicked_continue = True
                    wait_for_transition(page, page.url, pre_cont_hash, timeout=10.0)
                    return "continue_clicked"
                time.sleep(0.5)

            # 3. Multiselect
            if not clicked_continue:
                curr_cont = find_continue_button(page)
                if curr_cont and not curr_cont.is_enabled():
                    log_func("Multiselect detected. Selecting more options...")
                    for s in choice_sel:
                        els = page.locator(s)
                        for i in range(1, min(els.count(), 5)):
                            curr = els.nth(i)
                            txt = (curr.inner_text() or "").strip()
                            if txt and not re.search(r'^\d+\s*/\s*\d+$', txt) and not is_cookie_settings(txt):
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
            log(f"Navigating to {url}"); page.goto(url, wait_until='load', timeout=60000)
            
            step, history = 1, []
            while step <= 80:
                curr_u = page.url
                if any(k in curr_u for k in ["magic", "analyzing", "loading"]): 
                    time.sleep(12); curr_u = page.url
                
                close_popups(page, log)
                time.sleep(1)
                curr_h = get_screen_hash(page)
                st = classify_screen(page, log)
                
                curr_id = f"{curr_u}|{curr_h}"
                if history.count(curr_id) >= 3:
                    log(f"Stuck at {curr_u}. Stopping."); summary["error"] = "stuck_loop"; break
                history.append(curr_id)
                
                screen_name = f"{step:02d}_{st}.png"
                local_path = os.path.join(res_dir, screen_name)
                page.screenshot(path=local_path, full_page=True)
                shutil.copy2(local_path, os.path.join(classified_dir, st, f"{slug}__{screen_name}"))
                
                act = perform_action(page, st, log, res_dir, curr_h, curr_u)
                log(f"step:{step} | type:{st} | action:{act} | url:{page.url[:60]}")
                
                summary["steps_total"] = step; summary["last_url"] = page.url
                if st in ['paywall', 'checkout'] or "stopped" in act or "reached" in act:
                    if st == 'paywall': summary["paywall_reached"] = True
                    break
                
                step += 1
            browser.close()
    return summary

if __name__ == '__main__':
    with open('config.json', 'r') as f:
        config = json.load(f)
    
    all_summaries = []
    for url in config.get('funnels', []):
        print(f"\n--- Starting funnel: {url} ---")
        summary = run_funnel(url, config, config.get('headless', True))
        all_summaries.append(summary)
    
    with open(os.path.join('results', 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(all_summaries, f, indent=4)
    
    print("\nBatch run completed.")
    for s in all_summaries:
        print(f"URL: {s['url']} | Paywall reached: {s['paywall_reached']} | Steps: {s['steps_total']}")
